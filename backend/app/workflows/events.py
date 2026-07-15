from sqlalchemy import text
from sqlalchemy.orm import Session

# ASCII "PAGEVENT" encoded as a positive signed 64-bit integer. Every workflow
# event publisher uses this one PostgreSQL advisory-lock namespace.
WORKFLOW_EVENT_PUBLICATION_LOCK_ID = 0x5041474556454E54


def acquire_workflow_event_publication_lock(session: Session) -> None:
    """Serialize PostgreSQL event-ID allocation until the transaction resolves.

    PostgreSQL sequence values are allocated before commit, so concurrent event
    transactions can otherwise become visible out of ID order and cause an SSE
    cursor to skip a late commit. A transaction-scoped advisory lock makes the
    allocation order match commit-or-rollback order. SQLite's integer IDs are
    used only by the local/test runtime and need no cross-transaction gate.
    """
    if session.get_bind().dialect.name != "postgresql":
        return
    session.execute(
        text("SELECT pg_advisory_xact_lock(:lock_id)"),
        {"lock_id": WORKFLOW_EVENT_PUBLICATION_LOCK_ID},
    )
