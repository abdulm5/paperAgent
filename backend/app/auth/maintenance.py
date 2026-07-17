"""Bounded offline retention for hosted identity security state."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from app.db.models import AuthSessionRecord, OidcLoginTransactionRecord
from app.db.session import SessionLocal

IDENTITY_RETENTION_HOURS = 24
IDENTITY_RETENTION_BATCH_SIZE = 1_000


@dataclass(frozen=True)
class IdentityRetentionResult:
    auth_sessions: int
    oidc_transactions: int


def prune_identity_state(
    session: Session,
    *,
    now: datetime | None = None,
    retention_hours: int = IDENTITY_RETENTION_HOURS,
    batch_size: int = IDENTITY_RETENTION_BATCH_SIZE,
) -> IdentityRetentionResult:
    """Delete one bounded global batch outside unauthenticated request paths."""

    if not 1 <= retention_hours <= 24 * 365 or not 1 <= batch_size <= 10_000:
        raise ValueError("Identity retention bounds are invalid")
    current = now or datetime.now(UTC)
    cutoff = current - timedelta(hours=retention_hours)

    stale_session_ids = (
        select(AuthSessionRecord.id)
        .where(
            or_(
                AuthSessionRecord.expires_at <= cutoff,
                AuthSessionRecord.revoked_at <= cutoff,
            )
        )
        .order_by(AuthSessionRecord.id)
        .limit(batch_size)
    )
    deleted_sessions = session.execute(
        delete(AuthSessionRecord)
        .where(AuthSessionRecord.id.in_(stale_session_ids))
        .execution_options(synchronize_session=False)
    ).rowcount

    stale_transaction_ids = (
        select(OidcLoginTransactionRecord.id)
        .where(
            or_(
                OidcLoginTransactionRecord.expires_at <= cutoff,
                OidcLoginTransactionRecord.consumed_at <= cutoff,
            )
        )
        .order_by(OidcLoginTransactionRecord.id)
        .limit(batch_size)
    )
    deleted_transactions = session.execute(
        delete(OidcLoginTransactionRecord)
        .where(OidcLoginTransactionRecord.id.in_(stale_transaction_ids))
        .execution_options(synchronize_session=False)
    ).rowcount
    session.commit()
    return IdentityRetentionResult(
        auth_sessions=max(deleted_sessions or 0, 0),
        oidc_transactions=max(deleted_transactions or 0, 0),
    )


def main() -> int:
    with SessionLocal() as session:
        result = prune_identity_state(session)
    print(
        "Pruned hosted identity state "
        f"auth_sessions={result.auth_sessions} "
        f"oidc_transactions={result.oidc_transactions}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
