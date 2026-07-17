import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth.constants import DEFAULT_ORGANIZATION_SLUG
from app.db.models import (
    IdentityAuditEventRecord,
    OrganizationMembershipRecord,
    UserRecord,
)
from app.memberships.bootstrap import BootstrapAdminError, bootstrap_first_admin

OIDC_ISSUER = "https://identity.example.test"


def bootstrap(session: Session):
    return bootstrap_first_admin(
        session,
        organization_slug=DEFAULT_ORGANIZATION_SLUG,
        issuer=OIDC_ISSUER,
        subject="00u-hosted-admin",
        email="admin@example.test",
        display_name="Hosted Admin",
        configured_issuer=OIDC_ISSUER,
    )


def test_offline_bootstrap_creates_first_admin_and_audit_receipt(
    db_session: Session,
) -> None:
    result = bootstrap(db_session)

    membership = db_session.get(
        OrganizationMembershipRecord,
        (result.organization_id, result.user_id),
    )
    user = db_session.get(UserRecord, result.user_id)
    event = db_session.scalar(select(IdentityAuditEventRecord))
    assert membership is not None
    assert membership.role == "admin"
    assert membership.is_active is True
    assert user is not None
    assert (user.issuer, user.subject) == (OIDC_ISSUER, "00u-hosted-admin")
    assert event is not None
    assert event.actor == "bootstrap:offline"
    assert event.event_type == "membership.provisioned"
    assert event.payload == {
        "role": "admin",
        "is_active": True,
        "bootstrap": "offline",
    }


def test_offline_bootstrap_refuses_to_replace_an_existing_admin(
    db_session: Session,
) -> None:
    bootstrap(db_session)

    with pytest.raises(BootstrapAdminError, match="already has an active admin"):
        bootstrap_first_admin(
            db_session,
            organization_slug=DEFAULT_ORGANIZATION_SLUG,
            issuer=OIDC_ISSUER,
            subject="different-admin",
            email="other@example.test",
            display_name="Other Admin",
            configured_issuer=OIDC_ISSUER,
        )

    assert db_session.scalar(select(func.count()).select_from(UserRecord)) == 1
    assert (
        db_session.scalar(select(func.count()).select_from(IdentityAuditEventRecord))
        == 1
    )


def test_offline_bootstrap_requires_exact_configured_issuer(
    db_session: Session,
) -> None:
    with pytest.raises(BootstrapAdminError, match="exactly match"):
        bootstrap_first_admin(
            db_session,
            organization_slug=DEFAULT_ORGANIZATION_SLUG,
            issuer=OIDC_ISSUER,
            subject="00u-hosted-admin",
            email="admin@example.test",
            display_name="Hosted Admin",
            configured_issuer="https://other-idp.example.test",
        )

    assert db_session.scalar(select(func.count()).select_from(UserRecord)) == 0


def test_offline_bootstrap_can_create_named_hosted_organization(
    db_session: Session,
) -> None:
    result = bootstrap_first_admin(
        db_session,
        organization_slug="pageragent-production",
        organization_name="PagerAgent Production",
        issuer=OIDC_ISSUER,
        subject="00u-production-admin",
        email="production-admin@example.test",
        display_name="Production Admin",
        configured_issuer=OIDC_ISSUER,
    )

    membership = db_session.get(
        OrganizationMembershipRecord,
        (result.organization_id, result.user_id),
    )
    assert membership is not None
    assert membership.role == "admin"
