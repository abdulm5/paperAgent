import json
from typing import Any
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.routes.proposals import get_proposal_service
from app.copilot.citations import CitationValidator
from app.copilot.execution import ExecutionResult, SimulatorMitigationExecutor
from app.copilot.synthesis import DeterministicBriefSynthesizer, OpenAIBriefSynthesizer
from app.domain.incidents import IncidentStatus, IncidentTransitionRequest
from app.domain.proposals import ActionEnvelope, ProposalDecisionRequest
from app.evaluation.proposals import evaluate_proposal
from app.main import app
from app.services.incidents import IncidentService
from app.services.proposals import ProposalGenerationError, ProposalService
from tests.test_investigations import alert_payload, build_service


class RecordingExecutor:
    version = "recording-executor-v1"

    def __init__(self, recovery_verified: bool = True) -> None:
        self.recovery_verified = recovery_verified
        self.calls: list[tuple[object, str]] = []

    def execute(self, action: object, idempotency_key: str) -> ExecutionResult:
        self.calls.append((action, idempotency_key))
        return ExecutionResult(
            response_payload={
                "deployment": {"release": "stable-v1"},
                "canary_request_count": 15,
                "recovery_failure_count": 0,
            },
            before_telemetry={"current_release": {"name": "faulty-v2"}},
            after_telemetry={
                "current_release": {"name": "stable-v1"},
                "request_count": 75,
                "failed_request_count": 8,
            },
            recovery_verified=self.recovery_verified,
        )


def incident_with_investigation(db_session: Session) -> tuple[UUID, object]:
    client = TestClient(app)
    incident_id = UUID(
        client.post("/api/v1/alerts", json=alert_payload()).json()["incident"]["id"]
    )
    investigation = build_service(db_session).run(incident_id)
    return incident_id, investigation


def proposal_service(
    db_session: Session,
    executor: RecordingExecutor,
    synthesizer: object | None = None,
) -> ProposalService:
    return ProposalService(
        session=db_session,
        synthesizer=synthesizer or DeterministicBriefSynthesizer(),
        citation_validator=CitationValidator(),
        executor=executor,
    )


def test_proposal_is_grounded_and_cannot_execute_without_approval(
    db_session: Session,
) -> None:
    incident_id, investigation = incident_with_investigation(db_session)
    executor = RecordingExecutor()

    proposal = proposal_service(db_session, executor).generate(incident_id)

    assert proposal.status == "pending_approval"
    assert proposal.action.expected_faulty_commit == "8fa23c1"
    assert proposal.action.target_release == "stable-v1"
    assert proposal.confidence > 0.9
    assert {claim.kind for claim in proposal.claims} == {
        "root_cause",
        "impact",
        "recommendation",
        "risk",
    }
    allowed_ids = {
        str(record.id)
        for record in [
            *investigation.evidence,
            *investigation.error_clusters,
            *investigation.commit_candidates,
            *investigation.runbook_matches,
        ]
    }
    assert all(set(claim.evidence_ids) <= allowed_ids for claim in proposal.claims)
    assert executor.calls == []


def test_rejection_is_append_only_and_does_not_execute(db_session: Session) -> None:
    incident_id, _ = incident_with_investigation(db_session)
    executor = RecordingExecutor()
    service = proposal_service(db_session, executor)
    proposal = service.generate(incident_id)

    rejected = service.decide(
        proposal.id,
        ProposalDecisionRequest(
            decision="reject", actor="oncall@example.com", note="Need more evidence."
        ),
    )
    incident = IncidentService(db_session).get_detail(incident_id)

    assert rejected.status == "rejected"
    assert rejected.decisions[0].decision == "reject"
    assert rejected.decisions[0].note == "Need more evidence."
    assert rejected.execution is None
    assert executor.calls == []
    assert "proposal.rejected" in [event.event_type for event in incident.events]


def test_approved_rollback_verifies_recovery_and_mitigates_incident(
    db_session: Session,
) -> None:
    incident_id, investigation = incident_with_investigation(db_session)
    executor = RecordingExecutor()
    service = proposal_service(db_session, executor)
    proposal = service.generate(incident_id)
    IncidentService(db_session).transition(
        incident_id,
        IncidentTransitionRequest(
            to_status=IncidentStatus.INVESTIGATING,
            actor="oncall@example.com",
            note="Ranked evidence reviewed.",
            expected_version=1,
        ),
    )

    approved = service.decide(
        proposal.id,
        ProposalDecisionRequest(
            decision="approve",
            actor="oncall@example.com",
            note="Approve rollback to the known-good release.",
        ),
    )
    incident = IncidentService(db_session).get_detail(incident_id)
    allowed_ids = {
        str(record.id)
        for record in [
            *investigation.evidence,
            *investigation.error_clusters,
            *investigation.commit_candidates,
            *investigation.runbook_matches,
        ]
    }

    assert approved.status == "verification_passed"
    assert approved.execution is not None
    assert approved.execution.recovery_verified
    assert len(executor.calls) == 1
    assert incident.status == "mitigated"
    assert incident.version == 3
    assert evaluate_proposal(approved, allowed_ids).passed
    assert "proposal.approved" in [event.event_type for event in incident.events]
    assert "mitigation.recovery_verified" in [
        event.event_type for event in incident.events
    ]


def test_approval_requires_incident_investigating(db_session: Session) -> None:
    incident_id, _ = incident_with_investigation(db_session)
    executor = RecordingExecutor()
    service = proposal_service(db_session, executor)
    proposal = service.generate(incident_id)

    with pytest.raises(Exception, match="must be investigating"):
        service.decide(
            proposal.id,
            ProposalDecisionRequest(decision="approve", actor="oncall@example.com"),
        )

    assert executor.calls == []


def test_failed_recovery_does_not_mark_incident_mitigated(db_session: Session) -> None:
    incident_id, _ = incident_with_investigation(db_session)
    executor = RecordingExecutor(recovery_verified=False)
    service = proposal_service(db_session, executor)
    proposal = service.generate(incident_id)
    IncidentService(db_session).transition(
        incident_id,
        IncidentTransitionRequest(
            to_status=IncidentStatus.INVESTIGATING,
            actor="oncall@example.com",
            expected_version=1,
        ),
    )

    failed = service.decide(
        proposal.id,
        ProposalDecisionRequest(decision="approve", actor="oncall@example.com"),
    )
    incident = IncidentService(db_session).get_detail(incident_id)

    assert failed.status == "execution_failed"
    assert failed.execution is not None
    assert not failed.execution.recovery_verified
    assert incident.status == "investigating"
    assert incident.version == 2


class InvalidCitationSynthesizer(DeterministicBriefSynthesizer):
    version = "invalid-citation-synthesizer"

    def generate(self, context: dict[str, Any]):  # type: ignore[no-untyped-def]
        brief = super().generate(context)
        brief.claims[0].evidence_ids = ["not-real-evidence"]
        return brief


def test_uncited_model_output_is_rejected(db_session: Session) -> None:
    incident_id, _ = incident_with_investigation(db_session)
    service = proposal_service(
        db_session, RecordingExecutor(), InvalidCitationSynthesizer()
    )

    with pytest.raises(ProposalGenerationError, match="unknown evidence"):
        service.generate(incident_id)


def test_openai_adapter_uses_responses_structured_output() -> None:
    requests: list[dict[str, Any]] = []
    draft = DeterministicBriefSynthesizer().generate(
        {
            "alert": {"metric": {"request_count": 60, "value": 8 / 60}},
            "error_clusters": [
                {
                    "error_type": "ValidationRuleMissing",
                    "endpoint": "/checkout",
                    "failure_count": 8,
                    "affected_attributes": {"payment_methods": ["digital_wallet"]},
                    "evidence_ids": ["evidence-1"],
                }
            ],
            "commit_candidates": [
                {
                    "commit_sha": "8fa23c1",
                    "title": "Refactor digital wallet validation rules",
                    "total_score": 0.96,
                    "evidence_ids": ["evidence-1"],
                }
            ],
            "runbook_matches": [{"evidence_ids": ["evidence-1"]}],
        }
    )

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {"type": "output_text", "text": draft.model_dump_json()}
                        ],
                    }
                ]
            },
        )

    client = httpx.Client(
        base_url="https://api.openai.test/v1", transport=httpx.MockTransport(handler)
    )
    synthesizer = OpenAIBriefSynthesizer(
        api_key="test-key",
        model_name="gpt-5.6-luna",
        base_url="https://api.openai.test/v1",
        timeout_seconds=5,
        client=client,
    )

    result = synthesizer.generate({"evidence": "bounded fixture"})

    assert result.root_cause_summary == draft.root_cause_summary
    assert requests[0]["model"] == "gpt-5.6-luna"
    assert requests[0]["store"] is False
    assert requests[0]["text"]["format"]["type"] == "json_schema"
    assert requests[0]["text"]["format"]["strict"] is True


def test_simulator_executor_rolls_back_and_verifies_canary_cohort() -> None:
    deployed = False
    events: list[dict[str, Any]] = []
    canary_payloads: list[dict[str, Any]] = []
    deployed_at = "2026-07-11T18:00:00Z"

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal deployed
        if request.method == "GET" and request.url.path == "/telemetry":
            return httpx.Response(
                200,
                json={
                    "current_release": {
                        "name": "stable-v1" if deployed else "faulty-v2"
                    },
                    "recent_events": events,
                },
            )
        if request.url.path == "/admin/releases/stable-v1/activate":
            deployed = True
            return httpx.Response(
                200,
                json={
                    "previous_release": "faulty-v2",
                    "release": "stable-v1",
                    "commit_sha": "2ab1e90",
                    "deployed_at": deployed_at,
                },
            )
        if request.url.path == "/checkout":
            payload = json.loads(request.content)
            canary_payloads.append(payload)
            events.append(
                {
                    "timestamp": "2026-07-11T18:00:01Z",
                    "outcome": "success",
                    "payment_method": payload["payment_method"],
                }
            )
            return httpx.Response(201, json={"status": "accepted"})
        return httpx.Response(404)

    client = httpx.Client(
        base_url="http://checkout.test", transport=httpx.MockTransport(handler)
    )
    executor = SimulatorMitigationExecutor(
        base_url="http://checkout.test", canary_requests=15, client=client
    )

    result = executor.execute(
        ActionEnvelope(expected_faulty_commit="8fa23c1"), "proposal-test-id"
    )

    assert result.recovery_verified
    assert result.response_payload["canary_request_count"] == 15
    assert result.response_payload["recovery_failure_count"] == 0
    assert sum(
        payload["payment_method"] == "digital_wallet" for payload in canary_payloads
    ) == 5


def test_proposal_api_generates_and_returns_latest(db_session: Session) -> None:
    incident_id, _ = incident_with_investigation(db_session)
    service = proposal_service(db_session, RecordingExecutor())
    app.dependency_overrides[get_proposal_service] = lambda: service
    client = TestClient(app)

    created = client.post(f"/api/v1/incidents/{incident_id}/proposals")
    latest = client.get(f"/api/v1/incidents/{incident_id}/proposals/latest")

    assert created.status_code == 201
    assert created.json()["status"] == "pending_approval"
    assert latest.status_code == 200
    assert latest.json()["id"] == created.json()["id"]
