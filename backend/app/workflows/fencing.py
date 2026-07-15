from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import WorkflowJobRecord


class WorkflowLeaseLostError(RuntimeError):
    """Raised when a stale worker tries to commit a domain-side effect."""


@dataclass(frozen=True)
class WorkflowFence:
    """Attempt token that fences domain commits against lease replacement."""

    job_id: UUID
    lease_owner: str
    attempt: int

    def assert_active(
        self,
        session: Session,
        *,
        now: datetime | None = None,
    ) -> WorkflowJobRecord:
        job = session.scalar(
            select(WorkflowJobRecord)
            .where(WorkflowJobRecord.id == self.job_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        current_time = now or datetime.now(UTC)
        expires_at = job.lease_expires_at if job is not None else None
        if expires_at is not None and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if (
            job is None
            or job.status != "running"
            or job.lease_owner != self.lease_owner
            or job.attempt_count != self.attempt
            or expires_at is None
            or expires_at <= current_time
        ):
            raise WorkflowLeaseLostError(
                f"Workflow lease for job {self.job_id} attempt {self.attempt} is no longer active"
            )
        return job

    def commit(self, session: Session) -> None:
        """Commit pending domain writes only while this attempt still owns the lease."""
        self.assert_active(session)
        session.commit()


def commit_with_fence(session: Session, fence: WorkflowFence | None) -> None:
    if fence is None:
        session.commit()
        return
    fence.commit(session)
