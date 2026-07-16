from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.auth.constants import DEFAULT_ORGANIZATION_ID, DEFAULT_ORGANIZATION_SLUG
from app.auth.dependencies import get_current_principal
from app.auth.permissions import permissions_for_role
from app.connectors.vault import build_credential_cipher
from app.db.models import (
    CollaborationOutputRecord,
    ConnectorRecord,
    IncidentEventRecord,
    OutboxMessageRecord,
    WorkflowEventRecord,
    WorkflowJobRecord,
    WorkflowRunRecord,
)
from app.domain.auth import Principal, Role
from app.domain.collaboration import (
    CollaborationDecisionRequest,
    CollaborationOutputCreateInput,
)
from app.domain.connectors import ConnectorCreateInput
from app.main import app
from app.services.collaboration import (
    CollaborationConnectorUnavailableError,
    CollaborationOutputConflictError,
    CollaborationProposalConflictError,
    CollaborationService,
)
from app.services.connectors import ConnectorService
from app.workflows.engine import ExecutionDisposition, WorkflowEngine
from tests.conftest import TEST_USER_ID
from tests.test_proposals import RecordingExecutor, incident_with_investigation, proposal_service


@dataclass(frozen=True)
class FakeSlackReceipt:
    provider_message_id: str
    channel_id: str
    message_ts: str
    reconciled: bool


class RecordingSlackPublisher:
    def __init__(self, calls: list[tuple[str, UUID]], *, fail: Exception | None = None) -> None:
        self.calls = calls
        self.fail = fail

    def publish(self, *, text: str, delivery_id: UUID) -> FakeSlackReceipt:
        self.calls.append((text, delivery_id))
        if self.fail is not None:
            raise self.fail
        return FakeSlackReceipt(
            provider_message_id=f"{delivery_id}:1712345678.000100",
            channel_id="C0123456789",
            message_ts="1712345678.000100",
            reconciled=len(self.calls) > 1,
        )

    def close(self) -> None:
        return None


class RetryableProviderError(RuntimeError):
    retryable = True
    ambiguous = True
    retry_after_seconds = 17


def _session_factory(session: Session):
    return sessionmaker(bind=session.get_bind(), autoflush=False, expire_on_commit=False)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _principal(role: Role) -> Principal:
    return Principal(
        user_id=TEST_USER_ID,
        organization_id=DEFAULT_ORGANIZATION_ID,
        organization_slug=DEFAULT_ORGANIZATION_SLUG,
        role=role,
        permissions=permissions_for_role(role),
    )


def _create_enabled_slack_connector(session: Session) -> ConnectorRecord:
    summary = ConnectorService(
        session,
        DEFAULT_ORGANIZATION_ID,
        cipher=build_credential_cipher(),
    ).create_connector(
        ConnectorCreateInput(
            name="Checkout incident Slack",
            provider="slack",
            configuration={
                "service": "checkout-api",
                "channel": "C0123456789",
                "api_url": "https://slack.com",
            },
            credentials={"bot_token": "xoxb-test-token"},
        ),
        actor="user:test-admin",
    )
    record = session.get(ConnectorRecord, summary.id)
    assert record is not None
    record.enabled = True
    record.status = "configured"
    record.last_validation_ok = True
    record.last_validated_at = datetime.now(UTC)
    session.commit()
    return record


def _proposal_fixture(session: Session) -> tuple[UUID, object]:
    incident_id, _ = incident_with_investigation(session)
    proposal = proposal_service(session, RecordingExecutor()).generate(incident_id)
    return incident_id, proposal


def _prepare_slack(
    session: Session,
    incident_id: UUID,
    proposal: object,
    *,
    factory=None,
):
    service = CollaborationService(
        session,
        slack_publisher_factory=factory,
    )
    output = service.prepare(
        incident_id,
        CollaborationOutputCreateInput(
            proposal_id=proposal.id,
            expected_proposal_hash=proposal.input_hash,
            kinds=["slack_update"],
        ),
        actor="user:responder",
    )[0]
    return service, output


def test_prepare_builds_grounded_server_content_and_is_idempotent(
    db_session: Session,
) -> None:
    _create_enabled_slack_connector(db_session)
    incident_id, proposal = _proposal_fixture(db_session)
    service, first = _prepare_slack(db_session, incident_id, proposal)

    second = service.prepare(
        incident_id,
        CollaborationOutputCreateInput(
            proposal_id=proposal.id,
            expected_proposal_hash=proposal.input_hash,
            kinds=["slack_update"],
        ),
        actor="user:other-responder",
    )[0]

    assert second.id == first.id
    assert first.status == "pending_approval"
    assert first.destination == "C0123456789"
    assert first.payload == {"text": first.payload["text"]}
    assert "PagerAgent incident update" in first.payload["text"]
    assert proposal.root_cause_summary in first.payload["text"]
    assert proposal.slack_update not in first.payload["text"]
    assert len(first.content_sha256) == 64
    assert (
        db_session.scalar(
            select(func.count()).select_from(CollaborationOutputRecord)
        )
        == 1
    )


def test_prepare_rejects_stale_proposal_and_missing_service_connector(
    db_session: Session,
) -> None:
    connector = _create_enabled_slack_connector(db_session)
    incident_id, proposal = _proposal_fixture(db_session)
    service = CollaborationService(db_session)

    with pytest.raises(CollaborationProposalConflictError):
        service.prepare(
            incident_id,
            CollaborationOutputCreateInput(
                proposal_id=proposal.id,
                expected_proposal_hash="f" * 64,
                kinds=["slack_update"],
            ),
            actor="user:responder",
        )

    connector.configuration = {
        **connector.configuration,
        "service": "catalog-api",
    }
    db_session.commit()
    with pytest.raises(CollaborationConnectorUnavailableError):
        service.prepare(
            incident_id,
            CollaborationOutputCreateInput(
                proposal_id=proposal.id,
                expected_proposal_hash=proposal.input_hash,
                kinds=["slack_update"],
            ),
            actor="user:responder",
        )


def test_rejection_is_separate_from_mitigation_and_never_queues(
    db_session: Session,
) -> None:
    _create_enabled_slack_connector(db_session)
    incident_id, proposal = _proposal_fixture(db_session)
    service, output = _prepare_slack(db_session, incident_id, proposal)

    rejected = service.decide(
        output.id,
        CollaborationDecisionRequest(
            decision="reject",
            expected_version=output.version,
            expected_content_sha256=output.content_sha256,
            note="Wait for customer support review.",
            actor="user:incident-commander",
        ),
    )

    assert rejected.status == "rejected"
    assert rejected.delivery is None
    assert rejected.workflow_run_id is None
    assert rejected.decisions[0].decision == "reject"
    assert db_session.scalar(select(func.count()).select_from(WorkflowRunRecord)) == 0
    assert db_session.scalar(select(func.count()).select_from(OutboxMessageRecord)) == 0


def test_approval_atomically_freezes_revision_and_stages_outbox(
    db_session: Session,
) -> None:
    _create_enabled_slack_connector(db_session)
    incident_id, proposal = _proposal_fixture(db_session)
    service, output = _prepare_slack(db_session, incident_id, proposal)

    approved = service.decide(
        output.id,
        CollaborationDecisionRequest(
            decision="approve",
            expected_version=output.version,
            expected_content_sha256=output.content_sha256,
            actor="user:incident-commander",
        ),
    )

    assert approved.status == "queued"
    assert approved.workflow_run_id is not None
    assert approved.delivery is not None
    assert approved.delivery.idempotency_key == f"collaboration:{output.id}"
    run = db_session.get(WorkflowRunRecord, approved.workflow_run_id)
    assert run is not None
    assert run.workflow_type == "collaboration"
    job = db_session.scalar(
        select(WorkflowJobRecord).where(WorkflowJobRecord.workflow_run_id == run.id)
    )
    assert job is not None
    assert job.step_type == "deliver_collaboration_output"
    assert job.payload == {"collaboration_output_id": str(output.id)}
    assert db_session.scalar(select(func.count()).select_from(OutboxMessageRecord)) == 1

    with pytest.raises(CollaborationOutputConflictError):
        service.decide(
            output.id,
            CollaborationDecisionRequest(
                decision="approve",
                expected_version=output.version,
                expected_content_sha256=output.content_sha256,
                actor="user:incident-commander",
            ),
        )


def test_collaboration_api_enforces_separate_prepare_and_decide_permissions(
    db_session: Session,
) -> None:
    _create_enabled_slack_connector(db_session)
    incident_id, proposal = _proposal_fixture(db_session)
    client = TestClient(app)
    request = {
        "proposal_id": str(proposal.id),
        "expected_proposal_hash": proposal.input_hash,
        "kinds": ["slack_update"],
    }

    app.dependency_overrides[get_current_principal] = lambda: _principal(Role.VIEWER)
    denied_prepare = client.post(
        f"/api/v1/incidents/{incident_id}/collaboration-outputs",
        json=request,
    )
    assert denied_prepare.status_code == 403

    app.dependency_overrides[get_current_principal] = lambda: _principal(Role.RESPONDER)
    prepared_response = client.post(
        f"/api/v1/incidents/{incident_id}/collaboration-outputs",
        json=request,
    )
    assert prepared_response.status_code == 201
    prepared = prepared_response.json()[0]
    decision = {
        "decision": "approve",
        "expected_version": prepared["version"],
        "expected_content_sha256": prepared["content_sha256"],
    }
    denied_decision = client.post(
        f"/api/v1/collaboration-outputs/{prepared['id']}/decisions",
        json=decision,
    )
    assert denied_decision.status_code == 403

    app.dependency_overrides[get_current_principal] = lambda: _principal(
        Role.INCIDENT_COMMANDER
    )
    untrusted_actor = client.post(
        f"/api/v1/collaboration-outputs/{prepared['id']}/decisions",
        json={**decision, "actor": "attacker-controlled"},
    )
    assert untrusted_actor.status_code == 422
    approved = client.post(
        f"/api/v1/collaboration-outputs/{prepared['id']}/decisions",
        json=decision,
    )
    assert approved.status_code == 200
    assert approved.json()["decisions"][0]["actor"] == f"user:{TEST_USER_ID}"


def test_delivery_persists_normalized_receipt_and_replay_does_not_publish_twice(
    db_session: Session,
) -> None:
    _create_enabled_slack_connector(db_session)
    incident_id, proposal = _proposal_fixture(db_session)
    calls: list[tuple[str, UUID]] = []
    def factory(_configuration, _credentials):
        return RecordingSlackPublisher(calls)
    service, output = _prepare_slack(
        db_session,
        incident_id,
        proposal,
        factory=factory,
    )
    approved = service.decide(
        output.id,
        CollaborationDecisionRequest(
            decision="approve",
            expected_version=output.version,
            expected_content_sha256=output.content_sha256,
            actor="user:incident-commander",
        ),
    )

    delivered = service.deliver(approved.id)
    replayed = service.deliver(approved.id)

    assert replayed.status == delivered.status == "delivered"
    assert len(calls) == 1
    assert delivered.delivery is not None
    assert delivered.delivery.attempt_count == 1
    assert delivered.delivery.provider_receipt == {
        "provider_message_id": f"{output.id}:1712345678.000100",
        "channel_id": "C0123456789",
        "message_ts": "1712345678.000100",
        "reconciled": False,
    }
    events = db_session.scalars(
        select(IncidentEventRecord).where(
            IncidentEventRecord.event_type == "collaboration.output_delivered"
        )
    ).all()
    assert len(events) == 1
    assert "text" not in events[0].payload


def test_retryable_ambiguous_provider_failure_uses_workflow_retry_and_receipt(
    db_session: Session,
) -> None:
    _create_enabled_slack_connector(db_session)
    incident_id, proposal = _proposal_fixture(db_session)
    calls: list[tuple[str, UUID]] = []
    def factory(_configuration, _credentials):
        return RecordingSlackPublisher(
            calls,
            fail=RetryableProviderError("must not be copied into the receipt"),
        )
    service, output = _prepare_slack(
        db_session,
        incident_id,
        proposal,
        factory=factory,
    )
    approved = service.decide(
        output.id,
        CollaborationDecisionRequest(
            decision="approve",
            expected_version=output.version,
            expected_content_sha256=output.content_sha256,
            actor="user:incident-commander",
        ),
    )
    factory_session = _session_factory(db_session)
    job_id = db_session.scalar(
        select(WorkflowJobRecord.id).where(
            WorkflowJobRecord.workflow_run_id == approved.workflow_run_id
        )
    )
    assert job_id is not None

    def handler(session: Session, job: WorkflowJobRecord, fence):
        output_id = UUID(str(job.payload["collaboration_output_id"]))
        return CollaborationService(
            session,
            slack_publisher_factory=factory,
        ).deliver(output_id, fence=fence).model_dump(mode="json")

    now = datetime.now(UTC)
    result = WorkflowEngine(
        factory_session,
        worker_id="collaboration-worker",
        handlers={"deliver_collaboration_output": handler},
        clock=lambda: now,
        retry_base_seconds=2,
    ).execute(job_id)

    assert result.disposition is ExecutionDisposition.RETRY_SCHEDULED
    with factory_session() as session:
        stored = CollaborationService(session).get(output.id)
        job = session.get(WorkflowJobRecord, job_id)
        assert job is not None
        assert stored.status == "retry_scheduled"
        assert stored.delivery is not None
        assert stored.delivery.attempt_count == 1
        assert stored.delivery.last_error_code == "provider_delivery_ambiguous"
        assert _as_utc(job.available_at) >= now + timedelta(seconds=17)
        assert "must not be copied" not in stored.failure_reason
        retry_event = session.scalar(
            select(WorkflowEventRecord).where(
                WorkflowEventRecord.workflow_run_id == approved.workflow_run_id,
                WorkflowEventRecord.event_type == "workflow.retry_scheduled",
            )
        )
        assert retry_event is not None
        assert retry_event.payload["changed_resources"] == [
            "incident",
            "collaboration",
        ]
