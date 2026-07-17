from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from app.auth.constants import (
    DEFAULT_ORGANIZATION_ID,
    DEFAULT_ORGANIZATION_SLUG,
    LOCAL_MEMBERSHIP_ISSUER,
)
from app.auth.dependencies import get_current_principal
from app.auth.permissions import permissions_for_role
from app.core.config import settings
from app.db.models import (
    AuthSessionRecord,
    IdentityAuditEventRecord,
    OrganizationMembershipRecord,
    OrganizationRecord,
    UserRecord,
)
from app.domain.auth import Permission, Principal, Role
from app.domain.memberships import MembershipUpdateInput
from app.main import app
from app.memberships.service import MembershipService
from tests.conftest import TEST_USER_ID

OIDC_ISSUER = "https://identity.example.test"
OTHER_ORGANIZATION_ID = UUID("00000000-0000-0000-0000-000000000202")


def _principal(role: Role, organization_id: UUID = DEFAULT_ORGANIZATION_ID) -> Principal:
    return Principal(
        user_id=TEST_USER_ID,
        organization_id=organization_id,
        organization_slug=(
            DEFAULT_ORGANIZATION_SLUG
            if organization_id == DEFAULT_ORGANIZATION_ID
            else "other-operations"
        ),
        role=role,
        permissions=permissions_for_role(role),
    )


def _user(
    *,
    user_id: UUID | None = None,
    subject: str,
    email: str,
    display_name: str,
    issuer: str = OIDC_ISSUER,
) -> UserRecord:
    return UserRecord(
        id=user_id or uuid4(),
        issuer=issuer,
        subject=subject,
        email=email,
        display_name=display_name,
        is_active=True,
    )


def _membership(
    user: UserRecord,
    *,
    organization_id: UUID = DEFAULT_ORGANIZATION_ID,
    role: Role = Role.VIEWER,
    is_active: bool = True,
    version: int = 1,
) -> OrganizationMembershipRecord:
    return OrganizationMembershipRecord(
        organization_id=organization_id,
        user_id=user.id,
        role=role.value,
        is_active=is_active,
        version=version,
    )


def _seed_admin(session: Session) -> UserRecord:
    admin = _user(
        user_id=TEST_USER_ID,
        subject="admin-subject",
        email="admin@example.test",
        display_name="Avery Admin",
    )
    session.add(admin)
    session.flush()
    session.add(_membership(admin, role=Role.ADMIN))
    session.commit()
    return admin


def _provision_payload(subject: str = "responder-subject") -> dict[str, object]:
    return {
        "issuer": OIDC_ISSUER,
        "subject": subject,
        "email": "riley@example.test",
        "display_name": "Riley Responder",
        "role": "responder",
    }


def test_membership_permissions_are_admin_only() -> None:
    assert Permission.MEMBERSHIPS_READ in permissions_for_role(Role.ADMIN)
    assert Permission.MEMBERSHIPS_MANAGE in permissions_for_role(Role.ADMIN)
    for role in (Role.VIEWER, Role.RESPONDER, Role.INCIDENT_COMMANDER):
        assert Permission.MEMBERSHIPS_READ not in permissions_for_role(role)
        assert Permission.MEMBERSHIPS_MANAGE not in permissions_for_role(role)


def test_list_is_tenant_scoped_and_hidden_below_admin_permission(db_session: Session) -> None:
    _seed_admin(db_session)
    other_organization = OrganizationRecord(
        id=OTHER_ORGANIZATION_ID,
        slug="other-operations",
        name="Other Operations",
    )
    other_user = _user(
        subject="other-subject",
        email="other@example.test",
        display_name="Other Operator",
    )
    db_session.add_all([other_organization, other_user])
    db_session.flush()
    db_session.add(_membership(other_user, organization_id=other_organization.id))
    db_session.commit()

    response = TestClient(app).get("/api/v1/memberships")

    assert response.status_code == 200
    assert [item["user"]["id"] for item in response.json()] == [str(TEST_USER_ID)]

    app.dependency_overrides[get_current_principal] = lambda: _principal(
        Role.INCIDENT_COMMANDER
    )
    denied = TestClient(app).get("/api/v1/memberships")
    assert denied.status_code == 403
    assert denied.json()["detail"] == "Missing permission: memberships.read"


def test_provisions_by_stable_subject_without_linking_on_email(
    db_session: Session,
    monkeypatch,
) -> None:
    _seed_admin(db_session)
    monkeypatch.setattr(settings, "oidc_issuer", OIDC_ISSUER)
    same_email = _user(
        subject="different-subject",
        email="riley@example.test",
        display_name="Existing Riley",
    )
    db_session.add(same_email)
    db_session.commit()

    response = TestClient(app).post(
        "/api/v1/memberships",
        json=_provision_payload(),
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["user"]["subject"] == "responder-subject"
    assert body["user"]["id"] != str(same_email.id)
    assert body["version"] == 1
    assert body["is_active"] is True
    users_with_email = db_session.scalars(
        select(UserRecord).where(UserRecord.email == "riley@example.test")
    ).all()
    assert len(users_with_email) == 2

    audit = db_session.scalar(
        select(IdentityAuditEventRecord).where(
            IdentityAuditEventRecord.target_user_id == UUID(body["user"]["id"])
        )
    )
    assert audit is not None
    assert audit.event_type == "membership.provisioned"
    assert audit.actor == f"user:{TEST_USER_ID}"
    assert audit.membership_version == 1
    assert audit.payload == {"role": "responder", "is_active": True}
    assert "email" not in audit.payload
    assert "subject" not in audit.payload


def test_stock_local_configuration_can_provision_demo_identity(
    db_session: Session,
    monkeypatch,
) -> None:
    _seed_admin(db_session)
    monkeypatch.setattr(settings, "environment", "local")
    monkeypatch.setattr(settings, "oidc_issuer", "")

    response = TestClient(app).post(
        "/api/v1/memberships",
        json={
            **_provision_payload("stock-local-subject"),
            "issuer": LOCAL_MEMBERSHIP_ISSUER,
        },
    )

    assert response.status_code == 201, response.text
    assert response.json()["user"]["issuer"] == LOCAL_MEMBERSHIP_ISSUER


def test_existing_stable_identity_can_join_another_tenant_but_claim_conflicts_fail(
    db_session: Session,
    monkeypatch,
) -> None:
    _seed_admin(db_session)
    monkeypatch.setattr(settings, "oidc_issuer", OIDC_ISSUER)
    other_organization = OrganizationRecord(
        id=OTHER_ORGANIZATION_ID,
        slug="other-operations",
        name="Other Operations",
    )
    identity = _user(
        subject="responder-subject",
        email="riley@example.test",
        display_name="Riley Responder",
    )
    db_session.add_all([other_organization, identity])
    db_session.flush()
    db_session.add(_membership(identity, organization_id=other_organization.id))
    db_session.commit()

    joined = TestClient(app).post("/api/v1/memberships", json=_provision_payload())
    assert joined.status_code == 201, joined.text
    assert joined.json()["user"]["id"] == str(identity.id)

    conflicting_identity = _user(
        subject="conflicting-subject",
        email="original@example.test",
        display_name="Original Profile",
    )
    db_session.add(conflicting_identity)
    db_session.commit()
    conflict = TestClient(app).post(
        "/api/v1/memberships",
        json={
            **_provision_payload("conflicting-subject"),
            "email": "changed@example.test",
        },
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "identity_conflict"


def test_provisioning_rejects_unconfigured_issuer_and_client_owned_fields(
    db_session: Session,
    monkeypatch,
) -> None:
    _seed_admin(db_session)
    monkeypatch.setattr(settings, "oidc_issuer", OIDC_ISSUER)

    wrong_issuer = TestClient(app).post(
        "/api/v1/memberships",
        json={**_provision_payload(), "issuer": "https://attacker.example.test"},
    )
    assert wrong_issuer.status_code == 409
    assert wrong_issuer.json()["detail"]["code"] == "identity_conflict"

    injected_tenant = TestClient(app).post(
        "/api/v1/memberships",
        json={**_provision_payload(), "organization_id": str(OTHER_ORGANIZATION_ID)},
    )
    assert injected_tenant.status_code == 422


def test_update_uses_optimistic_version_and_appends_sanitized_receipt(
    db_session: Session,
) -> None:
    _seed_admin(db_session)
    target = _user(
        subject="target-subject",
        email="target@example.test",
        display_name="Target Operator",
    )
    db_session.add(target)
    db_session.flush()
    db_session.add(_membership(target, role=Role.RESPONDER, version=3))
    db_session.commit()

    stale = TestClient(app).patch(
        f"/api/v1/memberships/{target.id}",
        json={"expected_version": 2, "role": "incident_commander"},
    )
    assert stale.status_code == 409
    assert stale.json()["detail"] == {
        "code": "membership_version_conflict",
        "message": "Membership changed; current version is 3",
        "current_version": 3,
    }

    updated = TestClient(app).patch(
        f"/api/v1/memberships/{target.id}",
        json={"expected_version": 3, "role": "incident_commander", "is_active": False},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["version"] == 4
    assert updated.json()["role"] == "incident_commander"
    assert updated.json()["is_active"] is False

    events = TestClient(app).get("/api/v1/memberships/audit").json()
    assert len(events) == 1
    assert events[0]["target_user_id"] == str(target.id)
    assert events[0]["membership_version"] == 4
    assert events[0]["payload"] == {
        "changed_fields": ["role", "is_active"],
        "role": "incident_commander",
        "is_active": False,
    }


def test_membership_mutations_do_not_lock_joined_global_users(
    db_session: Session,
) -> None:
    """Keep cross-tenant actor/target lock ordering out of the global user table."""

    _seed_admin(db_session)
    target = _user(
        subject="lock-scope-target",
        email="lock-scope@example.test",
        display_name="Lock Scope Target",
    )
    db_session.add(target)
    db_session.flush()
    db_session.add(_membership(target))
    db_session.commit()

    locked_statements: list[object] = []

    def capture_lock(execute_state) -> None:
        statement = execute_state.statement
        if getattr(statement, "_for_update_arg", None) is not None:
            locked_statements.append(statement)

    event.listen(db_session, "do_orm_execute", capture_lock)
    try:
        MembershipService(
            db_session,
            DEFAULT_ORGANIZATION_ID,
            configured_issuer=OIDC_ISSUER,
        ).update(
            target.id,
            MembershipUpdateInput(expected_version=1, role=Role.RESPONDER),
            actor_user_id=TEST_USER_ID,
        )
    finally:
        event.remove(db_session, "do_orm_execute", capture_lock)

    postgres_lock_sql = [
        str(statement.compile(dialect=postgresql.dialect()))
        for statement in locked_statements
        if "JOIN users" in str(statement.compile(dialect=postgresql.dialect()))
    ]
    assert len(postgres_lock_sql) == 2
    assert all(
        "FOR UPDATE OF organization_memberships" in statement
        for statement in postgres_lock_sql
    )
    assert all("FOR UPDATE OF users" not in statement for statement in postgres_lock_sql)


def test_deactivation_revokes_sessions_and_reactivation_does_not_restore_them(
    db_session: Session,
) -> None:
    _seed_admin(db_session)
    target = _user(
        subject="session-target",
        email="session-target@example.test",
        display_name="Session Target",
    )
    db_session.add(target)
    db_session.flush()
    db_session.add(_membership(target, role=Role.RESPONDER))
    db_session.flush()
    active_session = AuthSessionRecord(
        user_id=target.id,
        organization_id=DEFAULT_ORGANIZATION_ID,
        auth_method="oidc",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    db_session.add(active_session)
    db_session.commit()

    deactivated = TestClient(app).patch(
        f"/api/v1/memberships/{target.id}",
        json={"expected_version": 1, "is_active": False},
    )
    assert deactivated.status_code == 200, deactivated.text
    db_session.refresh(active_session)
    assert active_session.revoked_at is not None

    reactivated = TestClient(app).patch(
        f"/api/v1/memberships/{target.id}",
        json={"expected_version": 2, "is_active": True},
    )
    assert reactivated.status_code == 200, reactivated.text
    db_session.refresh(active_session)
    assert active_session.revoked_at is not None


def test_self_and_last_admin_invariants_are_preserved(db_session: Session) -> None:
    _seed_admin(db_session)

    last_admin = TestClient(app).patch(
        f"/api/v1/memberships/{TEST_USER_ID}",
        json={"expected_version": 1, "is_active": False},
    )
    assert last_admin.status_code == 409
    assert "at least one active administrator" in last_admin.json()["detail"]["message"]

    second_admin = _user(
        subject="second-admin",
        email="second-admin@example.test",
        display_name="Second Admin",
    )
    db_session.add(second_admin)
    db_session.flush()
    db_session.add(_membership(second_admin, role=Role.ADMIN))
    db_session.commit()
    self_demotion = TestClient(app).patch(
        f"/api/v1/memberships/{TEST_USER_ID}",
        json={"expected_version": 1, "role": "responder"},
    )
    assert self_demotion.status_code == 409
    assert "cannot demote" in self_demotion.json()["detail"]["message"]

    actor_membership = db_session.get(
        OrganizationMembershipRecord,
        (DEFAULT_ORGANIZATION_ID, TEST_USER_ID),
    )
    assert actor_membership is not None
    assert actor_membership.role == Role.ADMIN.value
    assert actor_membership.is_active is True
    assert actor_membership.version == 1


def test_inactive_user_membership_does_not_satisfy_last_admin_invariant(
    db_session: Session,
) -> None:
    _seed_admin(db_session)
    inactive_admin = _user(
        subject="inactive-admin",
        email="inactive-admin@example.test",
        display_name="Inactive Admin",
    )
    inactive_admin.is_active = False
    db_session.add(inactive_admin)
    db_session.flush()
    db_session.add(_membership(inactive_admin, role=Role.ADMIN))
    db_session.commit()

    response = TestClient(app).patch(
        f"/api/v1/memberships/{TEST_USER_ID}",
        json={"expected_version": 1, "is_active": False},
    )

    assert response.status_code == 409
    assert "at least one active administrator" in response.json()["detail"]["message"]


def test_mutation_rechecks_actor_and_does_not_disclose_cross_tenant_membership(
    db_session: Session,
) -> None:
    admin = _seed_admin(db_session)
    admin_membership = db_session.get(
        OrganizationMembershipRecord,
        (DEFAULT_ORGANIZATION_ID, admin.id),
    )
    assert admin_membership is not None
    admin_membership.role = Role.INCIDENT_COMMANDER.value
    other_organization = OrganizationRecord(
        id=OTHER_ORGANIZATION_ID,
        slug="other-operations",
        name="Other Operations",
    )
    other_user = _user(
        subject="other-target",
        email="other-target@example.test",
        display_name="Other Target",
    )
    db_session.add_all([other_organization, other_user])
    db_session.flush()
    db_session.add(_membership(other_user, organization_id=other_organization.id))
    db_session.commit()

    changed_authority = TestClient(app).patch(
        f"/api/v1/memberships/{other_user.id}",
        json={"expected_version": 1, "role": "responder"},
    )
    assert changed_authority.status_code == 403
    assert changed_authority.json()["detail"]["code"] == "membership_inactive"

    admin_membership.role = Role.ADMIN.value
    db_session.commit()
    cross_tenant = TestClient(app).patch(
        f"/api/v1/memberships/{other_user.id}",
        json={"expected_version": 1, "role": "responder"},
    )
    assert cross_tenant.status_code == 404
