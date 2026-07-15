from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.auth.constants import DEFAULT_ORGANIZATION_ID
from app.db.models import (
    IncidentRecord,
    OutboxMessageRecord,
    WorkflowEventRecord,
    WorkflowJobRecord,
    WorkflowRunRecord,
)
from app.domain.workflows import (
    OutboxMessage,
    WorkflowEvent,
    WorkflowJob,
    WorkflowRun,
    WorkflowStatus,
    WorkflowType,
)
from app.workflows.events import acquire_workflow_event_publication_lock

WORKFLOW_TOPIC = "pageragent.workflow.jobs"


class WorkflowNotFoundError(Exception):
    pass


class WorkflowStore:
    """Persist workflow state without committing the caller's transaction."""

    def __init__(
        self,
        session: Session,
        organization_id: UUID = DEFAULT_ORGANIZATION_ID,
    ) -> None:
        self.session = session
        self.organization_id = organization_id

    def enqueue(
        self,
        incident_id: UUID,
        workflow_type: WorkflowType | str,
        first_step: str,
        dedupe_key: str,
        max_attempts: int = 5,
        payload: dict[str, Any] | None = None,
        *,
        trace_id: str | None = None,
    ) -> WorkflowRunRecord:
        """Create a run, first job, timeline events, and outbox row in one transaction."""
        incident_exists = self.session.scalar(
            select(IncidentRecord.id).where(
                IncidentRecord.id == incident_id,
                IncidentRecord.organization_id == self.organization_id,
            )
        )
        if incident_exists is None:
            raise WorkflowNotFoundError
        normalized_type = WorkflowType(workflow_type).value
        existing = self._run_by_dedupe_key(dedupe_key)
        if existing is not None:
            return existing
        self._validate_step(first_step)
        self._validate_max_attempts(max_attempts)

        run = WorkflowRunRecord(
            incident_id=incident_id,
            workflow_type=normalized_type,
            status=WorkflowStatus.QUEUED.value,
            current_step=first_step,
            dedupe_key=dedupe_key,
            trace_id=trace_id,
            version=1,
        )
        self.session.add(run)
        self.session.flush()
        self.append_event(
            run,
            "workflow.queued",
            payload={"workflow_type": normalized_type, "first_step": first_step},
        )
        self.create_job(
            run,
            first_step,
            payload=payload,
            max_attempts=max_attempts,
        )
        return run

    def append_event(
        self,
        run: WorkflowRunRecord,
        event_type: str,
        job: WorkflowJobRecord | None = None,
        payload: dict[str, Any] | None = None,
    ) -> WorkflowEventRecord:
        """Append a globally ordered event with a gap-free per-workflow sequence."""
        if not event_type.strip():
            raise ValueError("Workflow event type cannot be empty")
        self.session.flush()
        self.session.scalar(
            select(WorkflowRunRecord).where(WorkflowRunRecord.id == run.id).with_for_update()
        )
        # Every publisher locks job/outbox -> run -> global event gate. Keeping
        # that order avoids an inversion with worker and relay transactions.
        acquire_workflow_event_publication_lock(self.session)
        last_sequence = self.session.scalar(
            select(func.max(WorkflowEventRecord.sequence)).where(
                WorkflowEventRecord.workflow_run_id == run.id
            )
        )
        event = WorkflowEventRecord(
            workflow_run=run,
            job=job,
            sequence=(last_sequence or 0) + 1,
            event_type=event_type,
            payload=dict(payload or {}),
        )
        self.session.add(event)
        self.session.flush()
        return event

    def create_job(
        self,
        run: WorkflowRunRecord,
        step_type: str,
        payload: dict[str, Any] | None = None,
        max_attempts: int = 5,
    ) -> WorkflowJobRecord:
        """Create one idempotent job for a workflow step and stage its outbox message."""
        self._validate_step(step_type)
        self._validate_max_attempts(max_attempts)
        self.session.flush()
        existing = self.session.scalar(
            select(WorkflowJobRecord).where(
                WorkflowJobRecord.workflow_run_id == run.id,
                WorkflowJobRecord.step_type == step_type,
            )
        )
        if existing is not None:
            return existing

        now = datetime.now(UTC)
        previous_step = run.current_step
        previous_status = run.status
        run.current_step = step_type
        run.status = WorkflowStatus.QUEUED.value
        run.failure_reason = None
        run.completed_at = None
        run.updated_at = now
        if previous_step != step_type or previous_status != WorkflowStatus.QUEUED.value:
            run.version += 1

        job = WorkflowJobRecord(
            workflow_run=run,
            step_type=step_type,
            status=WorkflowStatus.QUEUED.value,
            payload=dict(payload or {}),
            result={},
            idempotency_key=f"workflow:{run.id}:{step_type}",
            attempt_count=0,
            max_attempts=max_attempts,
            available_at=now,
        )
        self.session.add(job)
        self.session.flush()
        self._create_outbox(job, dispatch_attempt=1, available_at=now)
        self.append_event(
            run,
            "job.queued",
            job=job,
            payload={"step_type": step_type, "max_attempts": max_attempts},
        )
        return job

    def list_for_incident(self, incident_id: UUID) -> list[WorkflowRun]:
        records = self.session.scalars(
            select(WorkflowRunRecord)
            .join(IncidentRecord)
            .where(
                WorkflowRunRecord.incident_id == incident_id,
                IncidentRecord.organization_id == self.organization_id,
            )
            .options(
                selectinload(WorkflowRunRecord.jobs).selectinload(
                    WorkflowJobRecord.outbox_messages
                ),
                selectinload(WorkflowRunRecord.events),
            )
            .order_by(WorkflowRunRecord.created_at)
        ).all()
        return [self._to_detail(record) for record in records]

    def get_detail(self, workflow_id: UUID) -> WorkflowRun:
        record = self.session.scalar(
            select(WorkflowRunRecord)
            .join(IncidentRecord)
            .where(
                WorkflowRunRecord.id == workflow_id,
                IncidentRecord.organization_id == self.organization_id,
            )
            .options(
                selectinload(WorkflowRunRecord.jobs).selectinload(
                    WorkflowJobRecord.outbox_messages
                ),
                selectinload(WorkflowRunRecord.events),
            )
            .execution_options(populate_existing=True)
        )
        if record is None:
            raise WorkflowNotFoundError
        return self._to_detail(record)

    def events_after(self, last_id: int = 0, limit: int = 100) -> list[WorkflowEventRecord]:
        bounded_limit = max(1, min(limit, 1_000))
        return list(
            self.session.scalars(
                select(WorkflowEventRecord)
                .join(WorkflowRunRecord)
                .join(IncidentRecord)
                .where(
                    WorkflowEventRecord.id > last_id,
                    IncidentRecord.organization_id == self.organization_id,
                )
                .order_by(WorkflowEventRecord.id)
                .limit(bounded_limit)
            ).all()
        )

    def _run_by_dedupe_key(self, dedupe_key: str) -> WorkflowRunRecord | None:
        return self.session.scalar(
            select(WorkflowRunRecord)
            .join(IncidentRecord)
            .where(
                WorkflowRunRecord.dedupe_key == dedupe_key,
                IncidentRecord.organization_id == self.organization_id,
            )
        )

    def _create_outbox(
        self,
        job: WorkflowJobRecord,
        dispatch_attempt: int,
        available_at: datetime,
    ) -> OutboxMessageRecord:
        message = OutboxMessageRecord(
            job=job,
            topic=WORKFLOW_TOPIC,
            payload={
                "workflow_id": str(job.workflow_run_id),
                "job_id": str(job.id),
                "incident_id": str(job.workflow_run.incident_id),
                "workflow_type": job.workflow_run.workflow_type,
                "step_type": job.step_type,
                "idempotency_key": job.idempotency_key,
                "trace_id": job.workflow_run.trace_id,
            },
            dispatch_attempt=dispatch_attempt,
            available_at=available_at,
            publish_attempts=0,
        )
        self.session.add(message)
        self.session.flush()
        return message

    @staticmethod
    def _to_detail(record: WorkflowRunRecord) -> WorkflowRun:
        return WorkflowRun(
            id=record.id,
            incident_id=record.incident_id,
            workflow_type=WorkflowType(record.workflow_type),
            status=WorkflowStatus(record.status),
            current_step=record.current_step,
            dedupe_key=record.dedupe_key,
            trace_id=record.trace_id,
            version=record.version,
            failure_reason=record.failure_reason,
            created_at=record.created_at,
            updated_at=record.updated_at,
            completed_at=record.completed_at,
            jobs=[
                WorkflowJob(
                    id=job.id,
                    workflow_run_id=job.workflow_run_id,
                    step_type=job.step_type,
                    status=WorkflowStatus(job.status),
                    payload=job.payload,
                    result=job.result,
                    idempotency_key=job.idempotency_key,
                    attempt_count=job.attempt_count,
                    max_attempts=job.max_attempts,
                    available_at=job.available_at,
                    lease_owner=job.lease_owner,
                    lease_expires_at=job.lease_expires_at,
                    last_error=job.last_error,
                    created_at=job.created_at,
                    updated_at=job.updated_at,
                    started_at=job.started_at,
                    completed_at=job.completed_at,
                    deliveries=[
                        OutboxMessage(
                            id=delivery.id,
                            workflow_job_id=delivery.workflow_job_id,
                            topic=delivery.topic,
                            payload=delivery.payload,
                            dispatch_attempt=delivery.dispatch_attempt,
                            available_at=delivery.available_at,
                            published_at=delivery.published_at,
                            publish_attempts=delivery.publish_attempts,
                            stream_message_id=delivery.stream_message_id,
                            last_error=delivery.last_error,
                            created_at=delivery.created_at,
                            updated_at=delivery.updated_at,
                        )
                        for delivery in job.outbox_messages
                    ],
                )
                for job in record.jobs
            ],
            events=[
                WorkflowEvent(
                    id=event.id,
                    workflow_run_id=event.workflow_run_id,
                    workflow_job_id=event.workflow_job_id,
                    sequence=event.sequence,
                    event_type=event.event_type,
                    payload=event.payload,
                    created_at=event.created_at,
                )
                for event in record.events
            ],
        )

    @staticmethod
    def _validate_step(step_type: str) -> None:
        if not step_type.strip() or len(step_type) > 64:
            raise ValueError("Workflow step type must contain 1 to 64 characters")

    @staticmethod
    def _validate_max_attempts(max_attempts: int) -> None:
        if max_attempts < 1:
            raise ValueError("Workflow max_attempts must be positive")
