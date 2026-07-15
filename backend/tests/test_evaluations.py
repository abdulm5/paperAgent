from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.copilot.actions import derive_action
from app.copilot.citations import CitationValidator
from app.copilot.synthesis import DeterministicBriefSynthesizer
from app.domain.evaluations import CausalKind, MitigationActionType
from app.evaluation.benchmark import BenchmarkRunner
from app.evaluation.fixtures import build_telemetry_fixture
from app.evaluation.scenario_loader import ScenarioRegistry
from app.investigation.causes import CauseRanker
from app.investigation.clustering import ErrorClusterer
from app.investigation.collectors import StaticTelemetryCollector
from app.investigation.commits import CommitRanker, FixtureGitProvider
from app.investigation.runbooks import RunbookRetriever
from app.main import app
from app.services.investigations import InvestigationService
from app.services.proposals import ProposalService

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCENARIO_DIRECTORY = REPOSITORY_ROOT / "scenarios"


def runner() -> BenchmarkRunner:
    return BenchmarkRunner(
        scenario_directory=SCENARIO_DIRECTORY,
        commit_fixture_path=SCENARIO_DIRECTORY / "checkout-commits.json",
        runbook_directory=REPOSITORY_ROOT / "runbooks",
    )


def run_scenario_investigation(
    db_session: Session, scenario_id: str
) -> tuple[UUID, object]:
    scenario = ScenarioRegistry(SCENARIO_DIRECTORY).get(scenario_id)
    telemetry = build_telemetry_fixture(scenario)
    metric_name = {
        "release_regression": "http_server_error_rate",
        "upstream_dependency": "upstream_timeout_rate",
        "feature_flag_regression": "checkout_feature_error_rate",
    }[scenario.simulation.mode]
    alert = {
        "fingerprint": f"checkout-api:{metric_name}:{scenario.id}",
        "source": "benchmark-integration-test",
        "service": scenario.service,
        "severity": scenario.severity,
        "summary": scenario.title,
        "started_at": telemetry["first_failure_at"],
        "detected_at": telemetry["observed_at"],
        "metric": {
            "name": metric_name,
            "value": telemetry["error_rate"],
            "threshold": 0.05,
            "window_seconds": telemetry["window_seconds"],
            "request_count": telemetry["request_count"],
            "failed_request_count": telemetry["failed_request_count"],
        },
        "release": telemetry["current_release"],
        "telemetry_url": "http://checkout.test/telemetry",
    }
    incident_id = UUID(
        TestClient(app).post("/api/v1/alerts", json=alert).json()["incident"]["id"]
    )
    service = InvestigationService(
        session=db_session,
        collector=StaticTelemetryCollector(telemetry),
        git_provider=FixtureGitProvider(SCENARIO_DIRECTORY / "checkout-commits.json"),
        clusterer=ErrorClusterer(),
        commit_ranker=CommitRanker(),
        cause_ranker=CauseRanker(),
        runbook_retriever=RunbookRetriever(REPOSITORY_ROOT / "runbooks"),
    )
    return incident_id, service.run(incident_id)


class WriteMustNotRunExecutor:
    version = "write-must-not-run-v1"

    def execute(self, action: object, idempotency_key: str) -> object:
        raise AssertionError(f"Unexpected write: {action} / {idempotency_key}")


def test_versioned_scenario_registry_loads_the_required_failure_classes() -> None:
    scenarios = ScenarioRegistry(SCENARIO_DIRECTORY).load_all()

    assert {scenario.simulation.mode for scenario in scenarios} == {
        "release_regression",
        "upstream_dependency",
        "feature_flag_regression",
    }
    assert all(scenario.schema_version == "1.0" for scenario in scenarios)
    assert all(scenario.adversarial_cases for scenario in scenarios)


def test_benchmark_passes_all_quality_and_safety_gates() -> None:
    scorecard = runner().run()
    results = {result.scenario_id: result for result in scorecard.scenarios}

    assert scorecard.passed
    assert scorecard.scenario_count == 3
    assert set(scorecard.aggregate_metrics.values()) == {1.0}
    assert all(gate.passed for gate in scorecard.gates)
    assert all(
        probe.passed
        for result in scorecard.scenarios
        for probe in result.adversarial_probes
    )

    upstream = results["payment-provider-timeout"]
    assert upstream.predicted_cause.kind is CausalKind.UPSTREAM_DEPENDENCY
    assert upstream.predicted_cause.reference == "payment-gateway"
    assert upstream.predicted_cause.reference != "9c4e2d1"
    assert upstream.predicted_action["action_type"] == "escalate_only"
    assert upstream.predicted_action["automation_allowed"] is False

    feature_flag = results["checkout-feature-flag-regression"]
    assert feature_flag.predicted_cause.kind is CausalKind.CONFIGURATION_CHANGE
    assert feature_flag.predicted_action["action_type"] == "disable_feature_flag"
    assert feature_flag.predicted_action["feature_flag"] == "wallet_validation_v2"
    assert feature_flag.predicted_action["automation_allowed"] is True


def test_missing_or_low_confidence_evidence_cannot_unlock_a_write() -> None:
    unknown = CauseRanker().rank({}, [])[0]
    low_confidence = unknown.model_copy(
        update={"kind": CausalKind.CODE_CHANGE, "reference": "8fa23c1", "score": 0.4}
    )

    for cause in [unknown, low_confidence]:
        action = derive_action(cause)
        assert action.action_type == MitigationActionType.ESCALATE_ONLY
        assert action.automation_allowed is False


def test_evaluation_api_exposes_contracts_and_reproducible_scorecard() -> None:
    client = TestClient(app)

    scenarios = client.get("/api/v1/evaluations/scenarios")
    scorecard = client.get("/api/v1/evaluations/scorecard")

    assert scenarios.status_code == 200
    assert len(scenarios.json()) == 3
    assert scorecard.status_code == 200
    assert scorecard.json()["passed"] is True
    assert scorecard.json()["scenario_count"] == 3


@pytest.mark.parametrize(
    ("scenario_id", "cause_kind", "cause_reference", "runbook_id", "action_type", "status"),
    [
        (
            "payment-provider-timeout",
            CausalKind.UPSTREAM_DEPENDENCY,
            "payment-gateway",
            "payment-provider-degradation",
            "escalate_only",
            "advisory",
        ),
        (
            "checkout-feature-flag-regression",
            CausalKind.CONFIGURATION_CHANGE,
            "wallet_validation_v2",
            "checkout-feature-flag-rollback",
            "disable_feature_flag",
            "pending_approval",
        ),
    ],
)
def test_real_service_path_preserves_causal_class_and_write_boundary(
    db_session: Session,
    scenario_id: str,
    cause_kind: CausalKind,
    cause_reference: str,
    runbook_id: str,
    action_type: str,
    status: str,
) -> None:
    incident_id, investigation = run_scenario_investigation(db_session, scenario_id)
    proposal = ProposalService(
        session=db_session,
        synthesizer=DeterministicBriefSynthesizer(),
        citation_validator=CitationValidator(),
        executor=WriteMustNotRunExecutor(),
    ).generate(incident_id)

    assert investigation.cause_candidates[0].kind is cause_kind
    assert investigation.cause_candidates[0].reference == cause_reference
    assert investigation.runbook_matches[0].runbook_id == runbook_id
    assert proposal.action.action_type == action_type
    assert proposal.status == status
    assert proposal.action.automation_allowed is (status == "pending_approval")
