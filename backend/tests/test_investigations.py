from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.routes.investigations import get_investigation_service
from app.evaluation.investigations import (
    InvestigationGroundTruth,
    evaluate_investigation,
)
from app.investigation.causes import CauseRanker
from app.investigation.clustering import ErrorClusterer
from app.investigation.collectors import StaticTelemetryCollector
from app.investigation.commits import CommitRanker, FixtureGitProvider
from app.investigation.runbooks import RunbookRetriever
from app.main import app
from app.services.incidents import IncidentService
from app.services.investigations import InvestigationExecutionError, InvestigationService

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def alert_payload() -> dict[str, object]:
    return {
        "fingerprint": "checkout-api:http-server-error-rate:faulty-v2",
        "source": "simulated-threshold-evaluator",
        "service": "checkout-api",
        "severity": "critical",
        "summary": "Checkout API error rate is 13.3%, above the 5.0% threshold.",
        "started_at": "2026-07-10T18:00:00Z",
        "detected_at": "2026-07-10T18:00:05Z",
        "metric": {
            "name": "http_server_error_rate",
            "value": 0.133333,
            "threshold": 0.05,
            "window_seconds": 300,
            "request_count": 60,
            "failed_request_count": 8,
        },
        "release": {
            "name": "faulty-v2",
            "commit_sha": "8fa23c1",
            "deployed_at": "2026-07-10T17:59:55Z",
        },
        "telemetry_url": "http://checkout-api:8100/telemetry",
    }


def telemetry_payload() -> dict[str, Any]:
    deployed_at = datetime(2026, 7, 10, 17, 59, 55, tzinfo=UTC)
    events: list[dict[str, Any]] = []
    for index in range(1, 61):
        faulty_request = index > 20 and index % 5 == 0
        payment_method = "digital_wallet" if index % 5 == 0 else "card"
        events.append(
            {
                "timestamp": (deployed_at + timedelta(seconds=index)).isoformat(),
                "service": "checkout-api",
                "endpoint": "/checkout",
                "request_id": f"request-{index:03d}",
                "trace_id": f"trace-{index:03d}",
                "payment_method": payment_method,
                "release": "faulty-v2" if index > 20 else "stable-v1",
                "commit_sha": "8fa23c1" if index > 20 else "2ab1e90",
                "http_status": 500 if faulty_request else 201,
                "outcome": "failure" if faulty_request else "success",
                "latency_ms": 119.0 if faulty_request else 42.0,
                "error_type": "ValidationRuleMissing" if faulty_request else None,
            }
        )
    return {
        "service": "checkout-api",
        "observed_at": (deployed_at + timedelta(seconds=65)).isoformat(),
        "window_started_at": (deployed_at - timedelta(seconds=235)).isoformat(),
        "window_seconds": 300,
        "current_release": {
            "name": "faulty-v2",
            "commit_sha": "8fa23c1",
            "deployed_at": deployed_at.isoformat(),
        },
        "request_count": 60,
        "successful_request_count": 52,
        "failed_request_count": 8,
        "error_rate": 8 / 60,
        "p95_latency_ms": 119.0,
        "first_failure_at": events[24]["timestamp"],
        "deployments": [
            {
                "previous_release": "stable-v1",
                "release": "faulty-v2",
                "commit_sha": "8fa23c1",
                "deployed_at": deployed_at.isoformat(),
            }
        ],
        "recent_events": events,
    }


def build_service(session: Session) -> InvestigationService:
    return InvestigationService(
        session=session,
        collector=StaticTelemetryCollector(telemetry_payload()),
        git_provider=FixtureGitProvider(REPOSITORY_ROOT / "scenarios/checkout-commits.json"),
        clusterer=ErrorClusterer(),
        commit_ranker=CommitRanker(),
        cause_ranker=CauseRanker(),
        runbook_retriever=RunbookRetriever(REPOSITORY_ROOT / "runbooks"),
    )


def test_investigation_clusters_errors_and_ranks_ground_truth_first(
    db_session: Session,
) -> None:
    client = TestClient(app)
    incident_id = UUID(
        client.post("/api/v1/alerts", json=alert_payload()).json()["incident"]["id"]
    )
    service = build_service(db_session)

    result = service.run(incident_id)

    assert result.status == "completed"
    assert {artifact.kind for artifact in result.evidence} == {
        "telemetry_snapshot",
        "deployment_history",
        "commit_catalog",
        "runbook_corpus",
    }
    assert all(len(artifact.content_hash) == 64 for artifact in result.evidence)
    assert len(result.error_clusters) == 1
    assert result.error_clusters[0].error_type == "ValidationRuleMissing"
    assert result.error_clusters[0].failure_count == 8
    assert result.error_clusters[0].affected_attributes["payment_methods"] == [
        "digital_wallet"
    ]
    assert result.commit_candidates[0].commit_sha == "8fa23c1"
    assert result.commit_candidates[0].rank == 1
    assert result.commit_candidates[0].total_score > result.commit_candidates[1].total_score
    assert "Matches the commit recorded on the active release." in result.commit_candidates[
        0
    ].explanation
    assert result.runbook_matches[0].runbook_id == "checkout-api-rollback"
    assert result.runbook_matches[0].rank == 1


def test_investigation_is_reproducible_and_updates_incident_timeline(
    db_session: Session,
) -> None:
    client = TestClient(app)
    incident_id = UUID(
        client.post("/api/v1/alerts", json=alert_payload()).json()["incident"]["id"]
    )
    first_service = build_service(db_session)
    first = first_service.run(incident_id)
    second_service = build_service(db_session)

    second = second_service.run(incident_id)
    latest = second_service.get_latest(incident_id)
    incident = IncidentService(second_service.session).get_detail(incident_id)

    assert second.input_hash == first.input_hash
    assert [item.commit_sha for item in second.commit_candidates] == [
        item.commit_sha for item in first.commit_candidates
    ]
    assert [item.total_score for item in second.commit_candidates] == [
        item.total_score for item in first.commit_candidates
    ]
    assert latest.id == second.id
    assert [event.event_type for event in incident.events].count("investigation.completed") == 2


def test_investigation_api_runs_and_returns_latest_result(db_session: Session) -> None:
    client = TestClient(app)
    incident_id = client.post("/api/v1/alerts", json=alert_payload()).json()["incident"]["id"]
    app.dependency_overrides[get_investigation_service] = lambda: build_service(db_session)

    created = client.post(f"/api/v1/incidents/{incident_id}/investigations")
    latest = client.get(f"/api/v1/incidents/{incident_id}/investigations/latest")

    assert created.status_code == 201
    assert created.json()["commit_candidates"][0]["commit_sha"] == "8fa23c1"
    assert latest.status_code == 200
    assert latest.json()["id"] == created.json()["id"]


def test_investigation_meets_scenario_quality_gate(db_session: Session) -> None:
    client = TestClient(app)
    incident_id = UUID(
        client.post("/api/v1/alerts", json=alert_payload()).json()["incident"]["id"]
    )

    result = build_service(db_session).run(incident_id)
    metrics = evaluate_investigation(
        result,
        InvestigationGroundTruth(
            faulty_commit="8fa23c1",
            expected_runbook="checkout-api-rollback",
            affected_payment_method="digital_wallet",
            expected_impacted_requests=8,
        ),
    )

    assert metrics.passed
    assert metrics.model_dump() == {
        "commit_top_1": 1.0,
        "commit_top_3": 1.0,
        "runbook_top_1": 1.0,
        "impact_count_accuracy": 1.0,
        "affected_attribute_accuracy": 1.0,
        "evidence_traceability": 1.0,
    }


class FailingTelemetryCollector:
    version = "failing-telemetry-v1"

    def collect(self, source_uri: str) -> dict[str, Any]:
        raise TimeoutError(f"Telemetry timed out: {source_uri}")


def test_investigation_persists_failure_for_operator_visibility(
    db_session: Session,
) -> None:
    client = TestClient(app)
    incident_id = UUID(
        client.post("/api/v1/alerts", json=alert_payload()).json()["incident"]["id"]
    )
    service = build_service(db_session)
    service.collector = FailingTelemetryCollector()

    try:
        service.run(incident_id)
    except InvestigationExecutionError as error:
        assert "Telemetry timed out" in str(error)
    else:
        raise AssertionError("Expected the failed collector to fail the investigation")

    failed = service.get_latest(incident_id)
    assert failed.status == "failed"
    assert failed.completed_at is not None
    assert failed.failure_reason is not None
    assert "Telemetry timed out" in failed.failure_reason
