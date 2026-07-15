from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.core.config import settings
from app.db.models import OutboxMessageRecord, WorkflowJobRecord, WorkflowRunRecord
from app.workflows.broker import WorkflowBroker
from app.workflows.store import WorkflowStore

logger = logging.getLogger(__name__)


class OutboxDispatcher:
    """Relays committed PostgreSQL outbox rows into Redis Streams."""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        broker: WorkflowBroker,
        *,
        repair_after_seconds: int | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.broker = broker
        self.repair_after_seconds = (
            repair_after_seconds
            if repair_after_seconds is not None
            else settings.workflow_delivery_repair_seconds
        )

    def dispatch_once(self, *, now: datetime | None = None, limit: int = 50) -> int:
        clock = now or datetime.now(UTC)
        repair_interval = timedelta(seconds=max(1, self.repair_after_seconds))
        repair_before = clock - repair_interval
        published = 0
        bounded_limit = max(0, min(limit, 1_000))
        latest_dispatch = (
            select(func.max(OutboxMessageRecord.dispatch_attempt))
            .where(OutboxMessageRecord.workflow_job_id == WorkflowJobRecord.id)
            .correlate(WorkflowJobRecord)
            .scalar_subquery()
        )
        for _ in range(bounded_limit):
            with self.session_factory() as session:
                message = session.scalar(
                    select(OutboxMessageRecord)
                    .join(
                        WorkflowJobRecord,
                        WorkflowJobRecord.id == OutboxMessageRecord.workflow_job_id,
                    )
                    .where(
                        OutboxMessageRecord.available_at <= clock,
                        OutboxMessageRecord.dispatch_attempt == latest_dispatch,
                        or_(
                            OutboxMessageRecord.published_at.is_(None),
                            OutboxMessageRecord.published_at <= repair_before,
                        ),
                        or_(
                            and_(
                                WorkflowJobRecord.status.in_(["queued", "retry_scheduled"]),
                                WorkflowJobRecord.available_at <= clock,
                            ),
                            and_(
                                WorkflowJobRecord.status == "running",
                                WorkflowJobRecord.lease_expires_at.is_not(None),
                                WorkflowJobRecord.lease_expires_at <= clock,
                            ),
                        ),
                    )
                    .options(
                        selectinload(OutboxMessageRecord.job).selectinload(
                            WorkflowJobRecord.workflow_run
                        )
                    )
                    .order_by(OutboxMessageRecord.created_at)
                    .limit(1)
                    .with_for_update(skip_locked=True, of=OutboxMessageRecord)
                )
                if message is None:
                    break
                if self._dispatch_message(
                    session,
                    message,
                    clock=clock,
                    repair_interval=repair_interval,
                ):
                    published += 1
                session.commit()
        return published

    def _dispatch_message(
        self,
        session: Session,
        message: OutboxMessageRecord,
        *,
        clock: datetime,
        repair_interval: timedelta,
    ) -> bool:
        """Publish or verify one receipt while holding only its transaction locks."""
        repairing = message.published_at is not None
        if repairing and message.stream_message_id is not None:
            try:
                receipt_is_healthy = self.broker.message_exists(message.stream_message_id)
            except Exception as error:
                message.available_at = clock + repair_interval
                logger.warning(
                    "outbox receipt check failed outbox_id=%s stream_message_id=%s error=%s",
                    message.id,
                    message.stream_message_id,
                    error,
                )
                return False
            if receipt_is_healthy:
                message.available_at = clock + repair_interval
                return False

        message.publish_attempts += 1
        try:
            stream_message_id = self.broker.publish(
                {
                    "outbox_id": str(message.id),
                    "workflow_job_id": str(message.workflow_job_id),
                    "topic": message.topic,
                    "dispatch_attempt": str(message.dispatch_attempt),
                    "payload": json.dumps(message.payload, sort_keys=True),
                }
            )
        except Exception as error:
            message.last_error = str(error)[:2_000]
            delay = min(60, 2 ** min(message.publish_attempts, 6))
            message.available_at = clock + timedelta(seconds=delay)
            self._record_delivery_event(
                session,
                message,
                "workflow.delivery_failed",
                clock=clock,
                payload={
                    "dispatch_attempt": message.dispatch_attempt,
                    "publish_attempt": message.publish_attempts,
                    "retry_at": message.available_at.isoformat(),
                    "error": message.last_error,
                    "changed_resources": [],
                },
            )
            logger.warning(
                "outbox publish failed outbox_id=%s attempt=%s error=%s",
                message.id,
                message.publish_attempts,
                error,
            )
            return False

        message.published_at = clock
        message.stream_message_id = stream_message_id
        message.last_error = None
        self._record_delivery_event(
            session,
            message,
            ("workflow.delivery_repaired" if repairing else "workflow.delivery_published"),
            clock=clock,
            payload={
                "dispatch_attempt": message.dispatch_attempt,
                "publish_attempt": message.publish_attempts,
                "stream_message_id": stream_message_id,
                "changed_resources": [],
            },
        )
        return True

    @staticmethod
    def _record_delivery_event(
        session: Session,
        message: OutboxMessageRecord,
        event_type: str,
        *,
        clock: datetime,
        payload: dict[str, object],
    ) -> None:
        run = session.scalar(
            select(WorkflowRunRecord)
            .where(WorkflowRunRecord.id == message.job.workflow_run_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if run is None:
            raise RuntimeError("Workflow run disappeared during publication")
        run.version += 1
        run.updated_at = clock
        WorkflowStore(session).append_event(
            run,
            event_type,
            job=message.job,
            payload=payload,
        )
