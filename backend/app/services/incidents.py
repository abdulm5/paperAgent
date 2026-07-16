from datetime import UTC, datetime
from secrets import token_hex
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.auth.constants import DEFAULT_ORGANIZATION_ID
from app.core.config import settings
from app.core.telemetry import current_trace_id
from app.db.models import (
    AlertRecord,
    CauseCandidateRecord,
    CollaborationDecisionRecord,
    CollaborationDeliveryRecord,
    CollaborationOutputRecord,
    CommitCandidateRecord,
    ErrorClusterRecord,
    EvidenceArtifactRecord,
    IncidentEventRecord,
    IncidentRecord,
    InvestigationRunRecord,
    MitigationExecutionRecord,
    MitigationProposalRecord,
    OutboxMessageRecord,
    PostmortemRecord,
    PostmortemRevisionRecord,
    ProposalDecisionRecord,
    RunbookMatchRecord,
    WorkflowEventRecord,
    WorkflowJobRecord,
    WorkflowRunRecord,
)
from app.domain.incidents import (
    AlertPayload,
    AlertSeverity,
    IncidentDetail,
    IncidentEvent,
    IncidentStatus,
    IncidentSummary,
    IncidentTransitionRequest,
)
from app.domain.workflows import WorkflowType
from app.workflows.store import WorkflowStore

ALLOWED_TRANSITIONS = {
    IncidentStatus.DETECTED: {IncidentStatus.INVESTIGATING},
    IncidentStatus.INVESTIGATING: {IncidentStatus.MITIGATED},
    IncidentStatus.MITIGATED: {IncidentStatus.RESOLVED},
    IncidentStatus.RESOLVED: set(),
}


class IncidentNotFoundError(Exception):
    pass


class InvalidTransitionError(Exception):
    def __init__(self, current: IncidentStatus, requested: IncidentStatus) -> None:
        super().__init__(f"Cannot transition incident from {current} to {requested}")


class IncidentVersionConflictError(Exception):
    def __init__(self, current_version: int) -> None:
        self.current_version = current_version
        super().__init__(f"Incident changed; current version is {current_version}")


class IncidentService:
    def __init__(
        self,
        session: Session,
        organization_id: UUID = DEFAULT_ORGANIZATION_ID,
    ) -> None:
        self.session = session
        # Service methods never interpret a missing tenant as "all tenants". The
        # default only preserves direct service usage in local/demo tests.
        self.organization_id = organization_id

    def ingest_alert(
        self,
        alert: AlertPayload,
        *,
        enqueue_workflow: bool = False,
        actor: str | None = None,
    ) -> tuple[IncidentDetail, bool]:
        incident = self._find_active_by_fingerprint(alert.fingerprint)
        deduplicated = incident is not None

        if incident is None:
            incident = IncidentRecord(
                organization_id=self.organization_id,
                fingerprint=alert.fingerprint,
                active_fingerprint=alert.fingerprint,
                status=IncidentStatus.DETECTED.value,
                service=alert.service,
                severity=alert.severity.value,
                summary=alert.summary,
                started_at=alert.started_at,
                detected_at=alert.detected_at,
                version=1,
            )
            self.session.add(incident)
            try:
                self.session.flush()
            except IntegrityError:
                self.session.rollback()
                incident = self._find_active_by_fingerprint(alert.fingerprint)
                if incident is None:
                    raise
                deduplicated = True

        self.session.add(
            AlertRecord(
                incident_id=incident.id,
                source=alert.source,
                fingerprint=alert.fingerprint,
                deduplicated=deduplicated,
                payload=alert.model_dump(mode="json"),
            )
        )
        self.session.add(
            IncidentEventRecord(
                incident_id=incident.id,
                event_type="alert.deduplicated" if deduplicated else "incident.detected",
                actor=actor or alert.source,
                from_status=None,
                to_status=IncidentStatus.DETECTED.value,
                note=(
                    "Repeated alert attached to the active incident."
                    if deduplicated
                    else "Monitoring threshold created the incident."
                ),
                payload={
                    "metric": alert.metric.model_dump(mode="json"),
                    "release": alert.release.model_dump(mode="json"),
                },
            )
        )
        if enqueue_workflow and not deduplicated:
            WorkflowStore(self.session, self.organization_id).enqueue(
                incident.id,
                WorkflowType.INCIDENT_RESPONSE,
                "investigate",
                f"incident:{incident.id}:response",
                max_attempts=settings.workflow_max_attempts,
                trace_id=current_trace_id() or token_hex(16),
            )
        self.session.commit()
        return self.get_detail(incident.id), deduplicated

    def list_incidents(self) -> list[IncidentSummary]:
        records = self.session.scalars(
            select(IncidentRecord).order_by(IncidentRecord.received_at.desc())
            .where(IncidentRecord.organization_id == self.organization_id)
        ).all()
        return [self._to_summary(record) for record in records]

    def get_detail(self, incident_id: UUID) -> IncidentDetail:
        record = self.session.scalar(
            select(IncidentRecord)
            .where(
                IncidentRecord.id == incident_id,
                IncidentRecord.organization_id == self.organization_id,
            )
            .options(
                selectinload(IncidentRecord.alerts),
                selectinload(IncidentRecord.events),
            )
            .execution_options(populate_existing=True)
        )
        if record is None:
            raise IncidentNotFoundError
        return self._to_detail(record)

    def transition(
        self,
        incident_id: UUID,
        request: IncidentTransitionRequest,
        *,
        enqueue_postmortem: bool = False,
    ) -> IncidentDetail:
        record = self.session.scalar(
            select(IncidentRecord)
            .where(
                IncidentRecord.id == incident_id,
                IncidentRecord.organization_id == self.organization_id,
            )
            .with_for_update()
        )
        if record is None:
            raise IncidentNotFoundError

        current_status = IncidentStatus(record.status)
        if record.version != request.expected_version:
            raise IncidentVersionConflictError(record.version)
        if request.to_status not in ALLOWED_TRANSITIONS[current_status]:
            raise InvalidTransitionError(current_status, request.to_status)

        now = datetime.now(UTC)
        record.status = request.to_status.value
        record.version += 1
        record.updated_at = now
        if request.to_status is IncidentStatus.RESOLVED:
            record.resolved_at = now
            record.active_fingerprint = None

        self.session.add(
            IncidentEventRecord(
                incident_id=record.id,
                event_type="incident.status_changed",
                actor=request.actor,
                from_status=current_status.value,
                to_status=request.to_status.value,
                note=request.note,
                payload={"version": record.version},
            )
        )
        if request.to_status is IncidentStatus.RESOLVED and enqueue_postmortem:
            WorkflowStore(self.session, self.organization_id).enqueue(
                record.id,
                WorkflowType.POSTMORTEM,
                "generate_postmortem",
                f"incident:{record.id}:postmortem",
                max_attempts=settings.workflow_max_attempts,
                trace_id=current_trace_id() or token_hex(16),
            )
        self.session.commit()
        return self.get_detail(record.id)

    def clear(self) -> int:
        incident_ids = select(IncidentRecord.id).where(
            IncidentRecord.organization_id == self.organization_id
        )
        investigation_ids = select(InvestigationRunRecord.id).where(
            InvestigationRunRecord.incident_id.in_(incident_ids)
        )
        proposal_ids = select(MitigationProposalRecord.id).where(
            MitigationProposalRecord.incident_id.in_(incident_ids)
        )
        collaboration_output_ids = select(CollaborationOutputRecord.id).where(
            CollaborationOutputRecord.incident_id.in_(incident_ids)
        )
        postmortem_ids = select(PostmortemRecord.id).where(
            PostmortemRecord.incident_id.in_(incident_ids)
        )
        workflow_run_ids = select(WorkflowRunRecord.id).where(
            WorkflowRunRecord.incident_id.in_(incident_ids)
        )
        workflow_job_ids = select(WorkflowJobRecord.id).where(
            WorkflowJobRecord.workflow_run_id.in_(workflow_run_ids)
        )
        count = int(
            self.session.scalar(
                select(func.count()).select_from(IncidentRecord).where(
                    IncidentRecord.organization_id == self.organization_id
                )
            )
            or 0
        )

        # Keep the reset endpoint deterministic even when SQLite foreign-key
        # cascades are disabled in unit tests. Every delete is tenant-bounded.
        self.session.execute(
            delete(PostmortemRevisionRecord).where(
                PostmortemRevisionRecord.postmortem_id.in_(postmortem_ids)
            )
        )
        self.session.execute(
            delete(PostmortemRecord).where(PostmortemRecord.incident_id.in_(incident_ids))
        )
        self.session.execute(
            delete(CollaborationDecisionRecord).where(
                CollaborationDecisionRecord.output_id.in_(collaboration_output_ids)
            )
        )
        self.session.execute(
            delete(CollaborationDeliveryRecord).where(
                CollaborationDeliveryRecord.output_id.in_(collaboration_output_ids)
            )
        )
        self.session.execute(
            delete(CollaborationOutputRecord).where(
                CollaborationOutputRecord.incident_id.in_(incident_ids)
            )
        )
        self.session.execute(
            delete(MitigationExecutionRecord).where(
                MitigationExecutionRecord.proposal_id.in_(proposal_ids)
            )
        )
        self.session.execute(
            delete(ProposalDecisionRecord).where(
                ProposalDecisionRecord.incident_id.in_(incident_ids)
            )
        )
        self.session.execute(
            delete(MitigationProposalRecord).where(
                MitigationProposalRecord.incident_id.in_(incident_ids)
            )
        )
        for model in (
            RunbookMatchRecord,
            CommitCandidateRecord,
            CauseCandidateRecord,
            ErrorClusterRecord,
            EvidenceArtifactRecord,
        ):
            self.session.execute(
                delete(model).where(model.investigation_id.in_(investigation_ids))
            )
        self.session.execute(
            delete(InvestigationRunRecord).where(
                InvestigationRunRecord.incident_id.in_(incident_ids)
            )
        )
        self.session.execute(
            delete(OutboxMessageRecord).where(
                OutboxMessageRecord.workflow_job_id.in_(workflow_job_ids)
            )
        )
        self.session.execute(
            delete(WorkflowEventRecord).where(
                WorkflowEventRecord.workflow_run_id.in_(workflow_run_ids)
            )
        )
        self.session.execute(
            delete(WorkflowJobRecord).where(
                WorkflowJobRecord.workflow_run_id.in_(workflow_run_ids)
            )
        )
        self.session.execute(
            delete(WorkflowRunRecord).where(
                WorkflowRunRecord.incident_id.in_(incident_ids)
            )
        )
        self.session.execute(
            delete(IncidentEventRecord).where(
                IncidentEventRecord.incident_id.in_(incident_ids)
            )
        )
        self.session.execute(
            delete(AlertRecord).where(AlertRecord.incident_id.in_(incident_ids))
        )
        self.session.execute(
            delete(IncidentRecord).where(
                IncidentRecord.organization_id == self.organization_id
            )
        )
        self.session.commit()
        return count

    def _find_active_by_fingerprint(self, fingerprint: str) -> IncidentRecord | None:
        return self.session.scalar(
            select(IncidentRecord).where(
                IncidentRecord.organization_id == self.organization_id,
                IncidentRecord.active_fingerprint == fingerprint,
            )
        )

    @staticmethod
    def _to_summary(record: IncidentRecord) -> IncidentSummary:
        return IncidentSummary(
            id=record.id,
            status=IncidentStatus(record.status),
            service=record.service,
            severity=AlertSeverity(record.severity),
            summary=record.summary,
            started_at=record.started_at,
            detected_at=record.detected_at,
            received_at=record.received_at,
            updated_at=record.updated_at,
            resolved_at=record.resolved_at,
            version=record.version,
        )

    @classmethod
    def _to_detail(cls, record: IncidentRecord) -> IncidentDetail:
        if not record.alerts:
            raise RuntimeError("Persisted incident is missing its source alert")
        summary = cls._to_summary(record)
        return IncidentDetail(
            **summary.model_dump(),
            alert=AlertPayload.model_validate(record.alerts[0].payload),
            alert_count=len(record.alerts),
            events=[
                IncidentEvent(
                    id=event.id,
                    event_type=event.event_type,
                    actor=event.actor,
                    from_status=(
                        IncidentStatus(event.from_status) if event.from_status is not None else None
                    ),
                    to_status=(
                        IncidentStatus(event.to_status) if event.to_status is not None else None
                    ),
                    note=event.note,
                    payload=event.payload,
                    created_at=event.created_at,
                )
                for event in record.events
            ],
        )
