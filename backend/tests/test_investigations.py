from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.routes.investigations import get_investigation_service
from app.domain.github import GitEvidenceBundle, GitWebhookReceipt
from app.domain.prometheus import (
    PrometheusEvidenceBundle,
    PrometheusSample,
    PrometheusSeries,
)
from app.evaluation.investigations import (
    InvestigationGroundTruth,
    evaluate_investigation,
)
from app.investigation.causes import CauseRanker
from app.investigation.clustering import ErrorClusterer
from app.investigation.collectors import StaticTelemetryCollector
from app.investigation.commits import CommitRanker, FixtureGitProvider, GitProvider
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


def build_service(
    session: Session,
    *,
    git_provider: GitProvider | None = None,
    prometheus_provider: object | None = None,
) -> InvestigationService:
    return InvestigationService(
        session=session,
        collector=StaticTelemetryCollector(telemetry_payload()),
        git_provider=git_provider
        or FixtureGitProvider(REPOSITORY_ROOT / "scenarios/checkout-commits.json"),
        clusterer=ErrorClusterer(),
        commit_ranker=CommitRanker(),
        cause_ranker=CauseRanker(),
        runbook_retriever=RunbookRetriever(REPOSITORY_ROOT / "runbooks"),
        prometheus_provider=prometheus_provider,
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


class GitHubArtifactProvider:
    version = "github-artifact-test-v1"

    def collect_evidence(
        self,
        deployed_at: datetime,
        service: str,
        active_commit_sha: str,
    ) -> GitEvidenceBundle:
        fixture = FixtureGitProvider(
            REPOSITORY_ROOT / "scenarios/checkout-commits.json"
        ).collect_evidence(deployed_at, service, active_commit_sha)
        return GitEvidenceBundle.model_validate(
            {
                **fixture.model_dump(),
                "source_uri": "github://octo-org/pageragent",
                "provider": "github_app",
                "repository": "octo-org/pageragent",
                "provider_version": self.version,
                "connector_id": "22222222-2222-4222-8222-222222222222",
                "connector_version": 4,
                "credential_version": 2,
                "webhook_receipts": [
                    GitWebhookReceipt(
                        delivery_id="11111111-1111-4111-8111-111111111111",
                        event_type="deployment_status",
                        action="created",
                        repository="octo-org/pageragent",
                        installation_id=67890,
                        connector_version=4,
                        credential_version=2,
                        body_sha256="d" * 64,
                        received_at=deployed_at,
                    ).model_dump()
                ],
            }
        )


class PrometheusArtifactProvider:
    version = "prometheus-artifact-test-v1"

    def __init__(self, calls: list[str] | None = None) -> None:
        self.calls = calls if calls is not None else []

    def collect_evidence(
        self,
        *,
        metric_name: str,
        service: str,
        observed_at: datetime,
        window_seconds: int,
    ) -> PrometheusEvidenceBundle:
        self.calls.append("prometheus.collect")
        assert metric_name == "http_server_error_rate"
        assert service == "checkout-api"
        assert window_seconds == 300
        connector_id = UUID("33333333-3333-4333-8333-333333333333")
        return PrometheusEvidenceBundle(
            provider_version="prometheus-http-api-v1",
            catalog_version="prometheus-query-catalog-v1",
            query_id="alert.http-server-error-rate.v1",
            metric_name=metric_name,
            service=service,
            window_started_at=observed_at - timedelta(seconds=window_seconds),
            window_ended_at=observed_at,
            step_seconds=15,
            series_count=1,
            sample_count=2,
            series=[
                PrometheusSeries(
                    labels={"service": service},
                    samples=[
                        PrometheusSample(
                            observed_at=observed_at - timedelta(seconds=15),
                            value=0.11,
                        ),
                        PrometheusSample(observed_at=observed_at, value=0.13),
                    ],
                )
            ],
            source_uri=f"prometheus://connector/{connector_id}/{service}",
            connector_id=connector_id,
            connector_version=5,
            credential_version=3,
        )

    def lock_current_revision(self, evidence: PrometheusEvidenceBundle | None) -> None:
        assert evidence is not None
        self.calls.append("prometheus.lock")


class OrderedFixtureGitProvider:
    version = "ordered-fixture-git-v1"

    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def collect_evidence(
        self,
        deployed_at: datetime,
        service: str,
        active_commit_sha: str,
    ) -> GitEvidenceBundle:
        self.calls.append("git.collect")
        return FixtureGitProvider(
            REPOSITORY_ROOT / "scenarios/checkout-commits.json"
        ).collect_evidence(deployed_at, service, active_commit_sha)


def test_investigation_persists_normalized_github_artifacts_with_traceable_provenance(
    db_session: Session,
) -> None:
    incident_id = UUID(
        TestClient(app).post("/api/v1/alerts", json=alert_payload()).json()["incident"]["id"]
    )

    result = build_service(
        db_session,
        git_provider=GitHubArtifactProvider(),
    ).run(incident_id)
    evidence_by_kind = {artifact.kind: artifact for artifact in result.evidence}

    assert set(evidence_by_kind) == {
        "telemetry_snapshot",
        "deployment_history",
        "commit_catalog",
        "github_pull_request_history",
        "github_deployment_history",
        "github_release_history",
        "github_webhook_history",
        "runbook_corpus",
    }
    for kind in {
        "commit_catalog",
        "github_pull_request_history",
        "github_deployment_history",
        "github_release_history",
        "github_webhook_history",
    }:
        artifact = evidence_by_kind[kind]
        assert artifact.source_uri == "github://octo-org/pageragent"
        assert artifact.payload["connector_id"] == (
            "22222222-2222-4222-8222-222222222222"
        )
        assert artifact.payload["connector_version"] == 4
        assert artifact.payload["credential_version"] == 2
    webhook_payload = evidence_by_kind["github_webhook_history"].payload
    assert webhook_payload["deliveries"][0]["body_sha256"] == "d" * 64
    assert "normalized_payload" not in str(webhook_payload)
    provider_evidence_ids = {
        str(evidence_by_kind[kind].id)
        for kind in {
            "github_pull_request_history",
            "github_deployment_history",
            "github_release_history",
            "github_webhook_history",
        }
    }
    assert provider_evidence_ids.issubset(result.commit_candidates[0].evidence_ids)


def test_prometheus_snapshot_is_fenced_hashed_cited_and_used_as_corroboration(
    db_session: Session,
) -> None:
    incident_id = UUID(
        TestClient(app).post("/api/v1/alerts", json=alert_payload()).json()["incident"]["id"]
    )
    calls: list[str] = []
    prometheus = PrometheusArtifactProvider(calls)

    result = build_service(
        db_session,
        git_provider=OrderedFixtureGitProvider(calls),
        prometheus_provider=prometheus,
    ).run(incident_id)

    assert calls == ["prometheus.collect", "git.collect", "prometheus.lock"]
    artifact = next(
        item for item in result.evidence if item.kind == "prometheus_metric_snapshot"
    )
    assert artifact.source_uri == (
        "prometheus://connector/33333333-3333-4333-8333-333333333333/checkout-api"
    )
    assert artifact.payload["query_id"] == "alert.http-server-error-rate.v1"
    assert artifact.payload["catalog_version"] == "prometheus-query-catalog-v1"
    assert artifact.payload["connector_version"] == 5
    assert artifact.payload["credential_version"] == 3
    assert artifact.payload["series_count"] == 1
    assert artifact.payload["sample_count"] == 2
    assert "query" not in artifact.payload
    assert "source_uri" not in artifact.payload
    assert len(artifact.content_hash) == 64
    assert str(artifact.id) in result.cause_candidates[0].evidence_ids
    assert str(artifact.id) not in result.commit_candidates[0].evidence_ids
    assert str(artifact.id) not in result.runbook_matches[0].evidence_ids
    assert (
        "The bounded Prometheus error-rate window independently corroborates the "
        "structured failure signal."
    ) in result.cause_candidates[0].explanation


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
