from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.constants import DEFAULT_ORGANIZATION_ID
from app.auth.maintenance import prune_identity_state
from app.db.models import (
    AuthSessionRecord,
    OidcLoginTransactionRecord,
    OrganizationMembershipRecord,
    OrganizationRecord,
    UserRecord,
)


def test_identity_retention_prunes_all_tenants_outside_login_request_path(
    db_session: Session,
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    other_organization = OrganizationRecord(
        slug="retention-tenant",
        name="Retention Tenant",
    )
    user = UserRecord(
        issuer="urn:pageragent:test",
        subject="retention-user",
        email="retention@example.test",
        display_name="Retention User",
        is_active=True,
    )
    db_session.add_all([other_organization, user])
    db_session.flush()
    for organization_id in (DEFAULT_ORGANIZATION_ID, other_organization.id):
        db_session.add(
            OrganizationMembershipRecord(
                organization_id=organization_id,
                user_id=user.id,
                role="viewer",
                is_active=True,
            )
        )
    db_session.flush()
    stale_sessions = [
        AuthSessionRecord(
            user_id=user.id,
            organization_id=organization_id,
            auth_method="oidc",
            expires_at=now - timedelta(hours=25),
        )
        for organization_id in (DEFAULT_ORGANIZATION_ID, other_organization.id)
    ]
    recent_session = AuthSessionRecord(
        user_id=user.id,
        organization_id=DEFAULT_ORGANIZATION_ID,
        auth_method="oidc",
        expires_at=now + timedelta(hours=1),
        revoked_at=now - timedelta(hours=1),
    )
    stale_transaction = OidcLoginTransactionRecord(
        state_hash="a" * 64,
        browser_binding_hash="b" * 64,
        nonce_hash="c" * 64,
        verifier_ciphertext=b"v" * 59,
        verifier_nonce=b"n" * 12,
        organization_slug="retention-tenant",
        created_at=now - timedelta(hours=26),
        expires_at=now - timedelta(hours=25),
    )
    live_transaction = OidcLoginTransactionRecord(
        state_hash="d" * 64,
        browser_binding_hash="e" * 64,
        nonce_hash="f" * 64,
        verifier_ciphertext=b"w" * 59,
        verifier_nonce=b"m" * 12,
        organization_slug="retention-tenant",
        created_at=now,
        expires_at=now + timedelta(minutes=5),
    )
    db_session.add_all(
        [*stale_sessions, recent_session, stale_transaction, live_transaction]
    )
    db_session.commit()

    result = prune_identity_state(db_session, now=now)

    assert result.auth_sessions == 2
    assert result.oidc_transactions == 1
    assert set(db_session.scalars(select(AuthSessionRecord.id))) == {recent_session.id}
    assert set(db_session.scalars(select(OidcLoginTransactionRecord.id))) == {
        live_transaction.id
    }
