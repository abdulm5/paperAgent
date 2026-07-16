from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from threading import Event, Thread
from typing import Any
from uuid import UUID

from opentelemetry.trace import Status, StatusCode
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.core.config import settings
from app.core.telemetry import workflow_span
from app.db.models import (
    CollaborationOutputRecord,
    IncidentEventRecord,
    IncidentRecord,
    InvestigationRunRecord,
    MitigationProposalRecord,
    OutboxMessageRecord,
    WorkflowEventRecord,
    WorkflowJobRecord,
    WorkflowRunRecord,
)
from app.services.collaboration import build_collaboration_service
from app.services.investigations import build_investigation_service
from app.services.postmortems import build_postmortem_service
from app.services.proposals import build_proposal_service
from app.workflows.errors import PermanentWorkflowError, WorkflowStepError
from app.workflows.events import acquire_workflow_event_publication_lock
from app.workflows.fencing import WorkflowFence, WorkflowLeaseLostError

logger = logging.getLogger(__name__)

WorkflowHandler = Callable[[Session, WorkflowJobRecord, WorkflowFence], dict[str, Any]]


class ExecutionDisposition(StrEnum):
    COMPLETED = "completed"
    RETRY_SCHEDULED = "retry_scheduled"
    DEAD_LETTERED = "dead_lettered"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class WorkflowExecutionResult:
    disposition: ExecutionDisposition
    job_id: UUID
    error: str | None = None


@dataclass(frozen=True)
class ClaimedJob:
    job_id: UUID
    incident_id: UUID
    organization_id: UUID
    step_type: str
    trace_id: str
    attempt: int


class WorkflowEngine:
    """Database-leased, replay-safe workflow step executor."""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        *,
        worker_id: str,
        handlers: dict[str, WorkflowHandler] | None = None,
        clock: Callable[[], datetime] | None = None,
        lease_seconds: int | None = None,
        retry_base_seconds: int | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.worker_id = worker_id
        self.handlers = handlers or {}
        self.clock = clock or (lambda: datetime.now(UTC))
        self.lease_seconds = lease_seconds or settings.workflow_lease_seconds
        self.retry_base_seconds = retry_base_seconds or settings.workflow_retry_base_seconds

    def execute(self, job_id: UUID) -> WorkflowExecutionResult:
        claimed, terminal = self._claim(job_id)
        if claimed is None:
            return terminal

        try:
            with workflow_span(
                f"workflow.{claimed.step_type}",
                trace_id=claimed.trace_id,
                attributes={
                    "pageragent.workflow.job_id": str(claimed.job_id),
                    "pageragent.workflow.step": claimed.step_type,
                    "pageragent.workflow.attempt": claimed.attempt,
                    "pageragent.workflow.worker": self.worker_id,
                },
            ) as span:
                heartbeat_stop, heartbeat = self._start_lease_heartbeat(claimed)
                try:
                    result = self._invoke_handler(claimed)
                finally:
                    heartbeat_stop.set()
                    heartbeat.join(timeout=1)
                span.set_status(Status(StatusCode.OK))
            return self._complete(claimed, result)
        except WorkflowLeaseLostError as error:
            logger.info(
                "workflow attempt fenced before domain commit job_id=%s step=%s attempt=%s",
                claimed.job_id,
                claimed.step_type,
                claimed.attempt,
            )
            return WorkflowExecutionResult(
                ExecutionDisposition.SKIPPED,
                claimed.job_id,
                str(error),
            )
        except Exception as error:
            logger.exception(
                "workflow step failed job_id=%s step=%s attempt=%s",
                claimed.job_id,
                claimed.step_type,
                claimed.attempt,
            )
            return self._fail(claimed, error)

    def _claim(self, job_id: UUID) -> tuple[ClaimedJob | None, WorkflowExecutionResult]:
        now = self.clock()
        with self.session_factory() as session:
            job = session.scalar(
                select(WorkflowJobRecord)
                .where(WorkflowJobRecord.id == job_id)
                .options(selectinload(WorkflowJobRecord.workflow_run))
                .with_for_update()
            )
            if job is None:
                return None, WorkflowExecutionResult(
                    ExecutionDisposition.SKIPPED, job_id, "job no longer exists"
                )
            if job.status == "completed":
                return None, WorkflowExecutionResult(ExecutionDisposition.COMPLETED, job_id)
            if job.status == "dead_lettered":
                return None, WorkflowExecutionResult(
                    ExecutionDisposition.DEAD_LETTERED, job_id, job.last_error
                )
            if self._future(job.available_at, now):
                return None, WorkflowExecutionResult(
                    ExecutionDisposition.SKIPPED, job_id, "job is not due"
                )
            if (
                job.status == "running"
                and job.lease_expires_at is not None
                and self._future(job.lease_expires_at, now)
            ):
                return None, WorkflowExecutionResult(
                    ExecutionDisposition.SKIPPED, job_id, "job has an active lease"
                )
            if job.attempt_count >= job.max_attempts:
                message = (
                    f"Lease expired after attempt {job.attempt_count}; "
                    f"maximum of {job.max_attempts} attempts exhausted"
                )
                job.status = "dead_lettered"
                job.last_error = message
                job.lease_owner = None
                job.lease_expires_at = None
                job.completed_at = now
                job.updated_at = now
                run = self._lock_run(session, job.workflow_run_id)
                run.status = "dead_lettered"
                run.failure_reason = message
                run.completed_at = now
                run.updated_at = now
                run.version += 1
                self._sync_collaboration_failure(
                    session,
                    job,
                    run,
                    status="dead_lettered",
                    error_code="workflow_attempts_exhausted",
                    message=message,
                    now=now,
                )
                self._append_event(
                    session,
                    run,
                    "workflow.dead_lettered",
                    job=job,
                    payload={
                        "attempt": job.attempt_count,
                        "error": message,
                        "changed_resources": self._changed_resources(job.step_type),
                    },
                )
                session.commit()
                return None, WorkflowExecutionResult(
                    ExecutionDisposition.DEAD_LETTERED,
                    job_id,
                    message,
                )

            recovered = job.status == "running" and job.lease_expires_at is not None
            job.status = "running"
            job.attempt_count += 1
            job.lease_owner = self.worker_id
            job.lease_expires_at = now + timedelta(seconds=self.lease_seconds)
            job.started_at = job.started_at or now
            job.updated_at = now
            run = self._lock_run(session, job.workflow_run_id)
            run.status = "running"
            run.current_step = job.step_type
            run.version += 1
            run.updated_at = now
            run.failure_reason = None
            if recovered:
                self._append_event(
                    session,
                    run,
                    "workflow.lease_recovered",
                    job=job,
                    payload={
                        "attempt": job.attempt_count,
                        "worker_id": self.worker_id,
                        "changed_resources": [],
                    },
                )
            self._append_event(
                session,
                run,
                "workflow.step_started",
                job=job,
                payload={
                    "attempt": job.attempt_count,
                    "worker_id": self.worker_id,
                    "lease_expires_at": job.lease_expires_at.isoformat(),
                    "changed_resources": [],
                },
            )
            trace_id = run.trace_id or run.id.hex
            organization_id = session.scalar(
                select(IncidentRecord.organization_id).where(
                    IncidentRecord.id == run.incident_id
                )
            )
            if organization_id is None:
                raise RuntimeError("Workflow incident disappeared while its job was claimed")
            session.commit()
            claimed = ClaimedJob(
                job_id=job.id,
                incident_id=run.incident_id,
                organization_id=organization_id,
                step_type=job.step_type,
                trace_id=trace_id,
                attempt=job.attempt_count,
            )
            return claimed, WorkflowExecutionResult(ExecutionDisposition.SKIPPED, job_id)

    def _invoke_handler(self, claimed: ClaimedJob) -> dict[str, Any]:
        with self.session_factory() as session:
            job = session.scalar(
                select(WorkflowJobRecord)
                .where(WorkflowJobRecord.id == claimed.job_id)
                .options(selectinload(WorkflowJobRecord.workflow_run))
            )
            if job is None:
                raise RuntimeError("Claimed workflow job disappeared")
            run = job.workflow_run
            organization_id = session.scalar(
                select(IncidentRecord.organization_id).where(
                    IncidentRecord.id == run.incident_id
                )
            )
            if (
                run.incident_id != claimed.incident_id
                or organization_id != claimed.organization_id
            ):
                raise RuntimeError("Claimed workflow ownership changed before execution")
            fence = WorkflowFence(
                job_id=claimed.job_id,
                lease_owner=self.worker_id,
                attempt=claimed.attempt,
            )
            custom = self.handlers.get(job.step_type)
            if custom is not None:
                return custom(session, job, fence)
            if job.step_type == "investigate":
                investigation = build_investigation_service(
                    session, claimed.organization_id
                ).run(
                    claimed.incident_id,
                    workflow_job_id=job.id,
                    fence=fence,
                )
                return {"investigation_id": str(investigation.id)}
            if job.step_type == "generate_proposal":
                investigation_id = UUID(str(job.payload["investigation_id"]))
                investigation_incident_id = session.scalar(
                    select(InvestigationRunRecord.incident_id).where(
                        InvestigationRunRecord.id == investigation_id
                    )
                )
                if investigation_incident_id != claimed.incident_id:
                    raise RuntimeError(
                        "Workflow investigation payload does not belong to the claimed incident"
                    )
                proposal = build_proposal_service(
                    session, claimed.organization_id
                ).generate(
                    claimed.incident_id,
                    investigation_id,
                    workflow_job_id=job.id,
                    fence=fence,
                )
                return {"proposal_id": str(proposal.id)}
            if job.step_type == "generate_postmortem":
                postmortem = build_postmortem_service(
                    session, claimed.organization_id
                ).generate(
                    claimed.incident_id,
                    fence=fence,
                )
                return {"postmortem_id": str(postmortem.id)}
            if job.step_type == "execute_mitigation":
                proposal_id = UUID(str(job.payload["proposal_id"]))
                proposal_incident_id = session.scalar(
                    select(MitigationProposalRecord.incident_id).where(
                        MitigationProposalRecord.id == proposal_id
                    )
                )
                if proposal_incident_id != claimed.incident_id:
                    raise RuntimeError(
                        "Workflow proposal payload does not belong to the claimed incident"
                    )
                proposal = build_proposal_service(
                    session, claimed.organization_id
                ).execute_approved(
                    proposal_id,
                    fence=fence,
                )
                return {
                    "proposal_id": str(proposal.id),
                    "status": proposal.status.value,
                }
            if job.step_type == "deliver_collaboration_output":
                output_id = UUID(str(job.payload["collaboration_output_id"]))
                output_incident_id = session.scalar(
                    select(CollaborationOutputRecord.incident_id).where(
                        CollaborationOutputRecord.id == output_id,
                        CollaborationOutputRecord.organization_id
                        == claimed.organization_id,
                    )
                )
                if output_incident_id != claimed.incident_id:
                    raise PermanentWorkflowError(
                        "Workflow collaboration payload does not belong to the claimed incident",
                        code="collaboration_payload_scope_mismatch",
                    )
                output = build_collaboration_service(
                    session, claimed.organization_id
                ).deliver(
                    output_id,
                    fence=fence,
                )
                return {
                    "collaboration_output_id": str(output.id),
                    "provider": output.provider.value,
                    "status": output.status.value,
                }
            raise RuntimeError(f"No handler registered for workflow step {job.step_type}")

    def _complete(self, claimed: ClaimedJob, result: dict[str, Any]) -> WorkflowExecutionResult:
        now = self.clock()
        with self.session_factory() as session:
            job = session.scalar(
                select(WorkflowJobRecord)
                .where(WorkflowJobRecord.id == claimed.job_id)
                .options(selectinload(WorkflowJobRecord.workflow_run))
                .with_for_update()
            )
            if job is None:
                return WorkflowExecutionResult(
                    ExecutionDisposition.SKIPPED,
                    claimed.job_id,
                    "job no longer exists",
                )
            if job.status == "completed":
                return WorkflowExecutionResult(ExecutionDisposition.COMPLETED, claimed.job_id)
            if not self._owns_lease(job, claimed):
                return WorkflowExecutionResult(
                    ExecutionDisposition.SKIPPED,
                    claimed.job_id,
                    "worker lease was superseded",
                )

            job.status = "completed"
            job.result = result
            job.last_error = None
            job.lease_owner = None
            job.lease_expires_at = None
            job.completed_at = now
            job.updated_at = now
            run = self._lock_run(session, job.workflow_run_id)
            run.version += 1
            run.updated_at = now
            changed_resources = self._changed_resources(job.step_type)
            self._append_event(
                session,
                run,
                "workflow.step_completed",
                job=job,
                payload={
                    "attempt": job.attempt_count,
                    "result": result,
                    "changed_resources": changed_resources,
                },
            )

            if job.step_type == "investigate" and settings.auto_generate_proposals:
                self._create_job(
                    session,
                    run,
                    "generate_proposal",
                    payload={"investigation_id": result["investigation_id"]},
                    max_attempts=job.max_attempts,
                    now=now,
                )
                run.status = "queued"
                run.current_step = "generate_proposal"
            else:
                run.status = "completed"
                run.current_step = job.step_type
                run.completed_at = now
                self._append_event(
                    session,
                    run,
                    "workflow.completed",
                    job=job,
                    payload={"changed_resources": changed_resources},
                )
            session.commit()
        return WorkflowExecutionResult(ExecutionDisposition.COMPLETED, claimed.job_id)

    def _fail(self, claimed: ClaimedJob, error: Exception) -> WorkflowExecutionResult:
        now = self.clock()
        if isinstance(error, WorkflowStepError):
            error_code = error.code
            message = f"{error.code}: {error}"[:2_000]
        else:
            error_code = "workflow_step_failed"
            message = f"{type(error).__name__}: {error}"[:2_000]
        with self.session_factory() as session:
            job = session.scalar(
                select(WorkflowJobRecord)
                .where(WorkflowJobRecord.id == claimed.job_id)
                .options(selectinload(WorkflowJobRecord.workflow_run))
                .with_for_update()
            )
            if job is None:
                return WorkflowExecutionResult(
                    ExecutionDisposition.SKIPPED, claimed.job_id, message
                )
            if job.status == "completed":
                return WorkflowExecutionResult(ExecutionDisposition.COMPLETED, claimed.job_id)
            if not self._owns_lease(job, claimed):
                return WorkflowExecutionResult(
                    ExecutionDisposition.SKIPPED,
                    claimed.job_id,
                    "worker lease was superseded",
                )
            run = self._lock_run(session, job.workflow_run_id)
            job.last_error = message
            job.lease_owner = None
            job.lease_expires_at = None
            job.updated_at = now
            run.failure_reason = message
            run.version += 1
            run.updated_at = now

            if isinstance(error, PermanentWorkflowError) or job.attempt_count >= job.max_attempts:
                job.status = "dead_lettered"
                job.completed_at = now
                run.status = "dead_lettered"
                run.completed_at = now
                self._sync_collaboration_failure(
                    session,
                    job,
                    run,
                    status="dead_lettered",
                    error_code=error_code,
                    message=message,
                    now=now,
                )
                self._append_event(
                    session,
                    run,
                    "workflow.dead_lettered",
                    job=job,
                    payload={
                        "attempt": job.attempt_count,
                        "error": message,
                        "changed_resources": self._changed_resources(job.step_type),
                    },
                )
                disposition = ExecutionDisposition.DEAD_LETTERED
            else:
                delay = self.retry_base_seconds * (2 ** (job.attempt_count - 1))
                retry_after = (
                    error.retry_after_seconds
                    if isinstance(error, WorkflowStepError)
                    else None
                )
                if retry_after is not None:
                    delay = max(delay, max(1, min(retry_after, 900)))
                retry_at = now + timedelta(seconds=delay)
                job.status = "retry_scheduled"
                job.available_at = retry_at
                run.status = "retry_scheduled"
                self._sync_collaboration_failure(
                    session,
                    job,
                    run,
                    status="retry_scheduled",
                    error_code=error_code,
                    message=message,
                    now=now,
                )
                self._queue_outbox(
                    session,
                    job,
                    dispatch_attempt=job.attempt_count + 1,
                    available_at=retry_at,
                )
                self._append_event(
                    session,
                    run,
                    "workflow.retry_scheduled",
                    job=job,
                    payload={
                        "attempt": job.attempt_count,
                        "retry_at": retry_at.isoformat(),
                        "error": message,
                        "changed_resources": self._changed_resources(job.step_type),
                    },
                )
                disposition = ExecutionDisposition.RETRY_SCHEDULED
            session.commit()
        return WorkflowExecutionResult(disposition, claimed.job_id, message)

    def _start_lease_heartbeat(self, claimed: ClaimedJob) -> tuple[Event, Thread]:
        stopping = Event()
        interval = max(1.0, self.lease_seconds / 3)

        def renew() -> None:
            while not stopping.wait(interval):
                try:
                    now = self.clock()
                    with self.session_factory() as session:
                        job = session.scalar(
                            select(WorkflowJobRecord)
                            .where(WorkflowJobRecord.id == claimed.job_id)
                            .with_for_update()
                        )
                        if job is None or not self._owns_lease(job, claimed):
                            return
                        job.lease_expires_at = now + timedelta(seconds=self.lease_seconds)
                        job.updated_at = now
                        session.commit()
                except Exception:
                    logger.exception(
                        "workflow lease heartbeat failed job_id=%s worker=%s",
                        claimed.job_id,
                        self.worker_id,
                    )

        heartbeat = Thread(
            target=renew,
            name=f"workflow-heartbeat-{claimed.job_id}",
            daemon=True,
        )
        heartbeat.start()
        return stopping, heartbeat

    def _owns_lease(self, job: WorkflowJobRecord, claimed: ClaimedJob) -> bool:
        return (
            job.status == "running"
            and job.lease_owner == self.worker_id
            and job.attempt_count == claimed.attempt
        )

    @staticmethod
    def _lock_run(session: Session, run_id: UUID) -> WorkflowRunRecord:
        run = session.scalar(
            select(WorkflowRunRecord)
            .where(WorkflowRunRecord.id == run_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if run is None:
            raise RuntimeError("Workflow run disappeared while its job was executing")
        return run

    def _create_job(
        self,
        session: Session,
        run: WorkflowRunRecord,
        step_type: str,
        *,
        payload: dict[str, Any],
        max_attempts: int,
        now: datetime,
    ) -> WorkflowJobRecord:
        job = WorkflowJobRecord(
            workflow_run_id=run.id,
            step_type=step_type,
            status="queued",
            payload=payload,
            result={},
            idempotency_key=f"{run.dedupe_key}:{step_type}",
            max_attempts=max_attempts,
            available_at=now,
        )
        session.add(job)
        session.flush()
        self._queue_outbox(session, job, dispatch_attempt=1, available_at=now)
        self._append_event(
            session,
            run,
            "workflow.step_queued",
            job=job,
            payload={"changed_resources": []},
        )
        return job

    @staticmethod
    def _queue_outbox(
        session: Session,
        job: WorkflowJobRecord,
        *,
        dispatch_attempt: int,
        available_at: datetime,
    ) -> None:
        session.add(
            OutboxMessageRecord(
                workflow_job_id=job.id,
                topic="workflow.job.ready",
                payload={
                    "workflow_run_id": str(job.workflow_run_id),
                    "workflow_job_id": str(job.id),
                    "step_type": job.step_type,
                },
                dispatch_attempt=dispatch_attempt,
                available_at=available_at,
            )
        )

    @staticmethod
    def _append_event(
        session: Session,
        run: WorkflowRunRecord,
        event_type: str,
        *,
        job: WorkflowJobRecord | None = None,
        payload: dict[str, Any] | None = None,
    ) -> WorkflowEventRecord:
        acquire_workflow_event_publication_lock(session)
        last_sequence = session.scalar(
            select(func.max(WorkflowEventRecord.sequence)).where(
                WorkflowEventRecord.workflow_run_id == run.id
            )
        )
        event = WorkflowEventRecord(
            workflow_run_id=run.id,
            workflow_job_id=job.id if job is not None else None,
            sequence=(last_sequence or 0) + 1,
            event_type=event_type,
            payload=payload or {},
        )
        session.add(event)
        session.flush()
        return event

    @staticmethod
    def _changed_resources(step_type: str) -> list[str]:
        return {
            "investigate": ["incident", "investigation"],
            "generate_proposal": ["incident", "proposal"],
            "execute_mitigation": ["incident", "proposal"],
            "generate_postmortem": ["incident", "postmortem"],
            "deliver_collaboration_output": ["incident", "collaboration"],
        }.get(step_type, [])

    @staticmethod
    def _sync_collaboration_failure(
        session: Session,
        job: WorkflowJobRecord,
        run: WorkflowRunRecord,
        *,
        status: str,
        error_code: str,
        message: str,
        now: datetime,
    ) -> None:
        if job.step_type != "deliver_collaboration_output":
            return
        raw_output_id = job.payload.get("collaboration_output_id")
        try:
            output_id = UUID(str(raw_output_id))
        except (TypeError, ValueError):
            return
        organization_id = session.scalar(
            select(IncidentRecord.organization_id).where(
                IncidentRecord.id == run.incident_id
            )
        )
        if organization_id is None:
            return
        output = session.scalar(
            select(CollaborationOutputRecord)
            .where(
                CollaborationOutputRecord.id == output_id,
                CollaborationOutputRecord.incident_id == run.incident_id,
                CollaborationOutputRecord.organization_id == organization_id,
            )
            .options(selectinload(CollaborationOutputRecord.delivery))
            .with_for_update()
        )
        if output is None or output.status == "delivered":
            return
        output.status = status
        output.failure_reason = message[:500]
        output.version += 1
        if output.delivery is not None:
            output.delivery.status = status
            output.delivery.last_error_code = error_code[:64]
            output.delivery.updated_at = now
        session.add(
            IncidentEventRecord(
                incident_id=run.incident_id,
                event_type=f"collaboration.delivery_{status}",
                actor="pageragent-workflow-engine",
                from_status=None,
                to_status=run.incident.status,
                note=(
                    "Collaboration delivery exhausted its durable retry policy."
                    if status == "dead_lettered"
                    else "Collaboration delivery will retry through the durable outbox."
                ),
                payload={
                    "output_id": str(output.id),
                    "provider": output.provider,
                    "error_code": error_code[:64],
                    "attempt": job.attempt_count,
                },
            )
        )

    @staticmethod
    def _future(value: datetime, now: datetime) -> bool:
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        return value > now
