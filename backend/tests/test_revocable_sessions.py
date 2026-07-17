import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.routes.workflows import workflow_event_stream
from app.auth.dependencies import get_current_principal
from app.auth.service import AuthService
from app.auth.tokens import decode_session_token
from app.core.config import settings
from app.db.models import (
    AuthSessionRecord,
    IncidentRecord,
    OrganizationMembershipRecord,
)
from app.domain.workflows import WorkflowType
from app.main import app
from app.workflows.store import WorkflowStore
from tests.conftest import TestingSessionLocal


def _dev_session(client: TestClient) -> dict[str, object]:
    response = client.post(
        "/api/v1/auth/dev/session",
        json={"persona": "admin", "organization_slug": "pageragent-labs"},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_issued_token_is_bound_to_a_database_session_with_the_same_expiry() -> None:
    client = TestClient(app)
    payload = _dev_session(client)
    claims = decode_session_token(str(payload["access_token"]))

    with TestingSessionLocal() as session:
        record = session.get(AuthSessionRecord, claims.session_id)
        assert record is not None
        expires_at = record.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        assert expires_at == claims.expires_at
        assert record.user_id == claims.user_id
        assert record.organization_id == claims.organization_id
        assert record.auth_method == "local"
        assert record.revoked_at is None


def test_session_response_prevents_browser_and_intermediary_caching() -> None:
    client = TestClient(app)
    _dev_session(client)

    response = client.get("/api/v1/auth/session")

    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    assert response.headers["referrer-policy"] == "no-referrer"


def test_logout_revokes_bearer_session_immediately() -> None:
    client = TestClient(app)
    payload = _dev_session(client)
    encoded = str(payload["access_token"])
    claims = decode_session_token(encoded)

    response = client.delete(
        "/api/v1/auth/session",
        headers={"Authorization": f"Bearer {encoded}"},
    )
    assert response.status_code == 204, response.text

    with TestingSessionLocal() as session:
        record = session.get(AuthSessionRecord, claims.session_id)
        assert record is not None
        assert record.revoked_at is not None
        assert record.revoked_at <= datetime.now(UTC).replace(tzinfo=None)

    app.dependency_overrides.pop(get_current_principal, None)
    denied = client.get(
        "/api/v1/incidents",
        headers={"Authorization": f"Bearer {encoded}"},
    )
    assert denied.status_code == 401
    assert denied.json()["detail"] == "Invalid or expired session"


def test_logout_terminates_an_already_open_workflow_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ConnectedRequest:
        async def is_disconnected(self) -> bool:
            return False

    monkeypatch.setattr(
        "app.api.routes.workflows.SessionLocal",
        TestingSessionLocal,
    )

    client = TestClient(app)
    payload = _dev_session(client)
    encoded = str(payload["access_token"])
    claims = decode_session_token(encoded)
    with TestingSessionLocal() as session:
        principal = AuthService(session).load_principal(
            claims.user_id,
            claims.organization_id,
        )

    revoked = client.delete(
        "/api/v1/auth/session",
        headers={"Authorization": f"Bearer {encoded}"},
    )
    assert revoked.status_code == 204

    async def read_after_revocation() -> str:
        stream = workflow_event_stream(
            ConnectedRequest(),  # type: ignore[arg-type]
            principal=principal,
            session_id=claims.session_id,
            session_expires_at=claims.expires_at,
            poll_seconds=0,
        )
        return await anext(stream)

    with pytest.raises(StopAsyncIteration):
        asyncio.run(read_after_revocation())


@pytest.mark.parametrize("authority_boundary", ["logout", "membership_deactivation"])
def test_stream_rechecks_authority_between_materialized_events_without_holding_a_session(
    monkeypatch: pytest.MonkeyPatch,
    authority_boundary: str,
) -> None:
    class ConnectedRequest:
        async def is_disconnected(self) -> bool:
            return False

    client = TestClient(app)
    payload = _dev_session(client)
    claims = decode_session_token(str(payload["access_token"]))
    with TestingSessionLocal() as session:
        principal = AuthService(session).load_principal(
            claims.user_id,
            claims.organization_id,
        )
        now = datetime.now(UTC)
        incident = IncidentRecord(
            organization_id=claims.organization_id,
            fingerprint="stream-authority-regression",
            active_fingerprint="stream-authority-regression",
            status="detected",
            service="checkout-api",
            severity="critical",
            summary="Stream authority regression fixture",
            started_at=now,
            detected_at=now,
            version=1,
        )
        session.add(incident)
        session.flush()
        WorkflowStore(session, claims.organization_id).enqueue(
            incident.id,
            WorkflowType.INCIDENT_RESPONSE,
            "investigate",
            "stream-authority-regression",
        )
        session.commit()

    open_contexts = 0

    @contextmanager
    def tracking_session_factory() -> Iterator[Session]:
        nonlocal open_contexts
        open_contexts += 1
        try:
            with TestingSessionLocal() as session:
                yield session
        finally:
            open_contexts -= 1

    monkeypatch.setattr(
        "app.api.routes.workflows.SessionLocal",
        tracking_session_factory,
    )

    async def cross_authority_boundary() -> None:
        stream = workflow_event_stream(
            ConnectedRequest(),  # type: ignore[arg-type]
            principal=principal,
            session_id=claims.session_id,
            session_expires_at=claims.expires_at,
            poll_seconds=0,
        )
        first_event = await anext(stream)
        assert "event: workflow" in first_event
        assert open_contexts == 0

        with TestingSessionLocal() as session:
            if authority_boundary == "logout":
                auth_session = session.get(AuthSessionRecord, claims.session_id)
                assert auth_session is not None
                auth_session.revoked_at = datetime.now(UTC)
            else:
                membership = session.get(
                    OrganizationMembershipRecord,
                    (claims.organization_id, claims.user_id),
                )
                assert membership is not None
                membership.is_active = False
            session.commit()

        with pytest.raises(StopAsyncIteration):
            await anext(stream)
        assert open_contexts == 0

    asyncio.run(cross_authority_boundary())


def test_organization_switch_atomically_replaces_the_current_session() -> None:
    client = TestClient(app)
    payload = _dev_session(client)
    old_encoded = str(payload["access_token"])
    old_claims = decode_session_token(old_encoded)
    current_session = payload["session"]
    alternate = next(
        membership
        for membership in current_session["memberships"]
        if membership["organization"]["id"]
        != current_session["active_organization"]["id"]
    )

    switched = client.post(
        "/api/v1/auth/session/switch",
        json={"organization_id": alternate["organization"]["id"]},
        headers={"X-CSRF-Token": current_session["csrf_token"]},
    )
    assert switched.status_code == 200, switched.text
    new_encoded = client.cookies.get(settings.session_cookie_name)
    assert new_encoded is not None
    new_claims = decode_session_token(new_encoded)
    assert new_claims.session_id != old_claims.session_id
    assert new_claims.organization_id != old_claims.organization_id

    with TestingSessionLocal() as session:
        records = list(
            session.scalars(
                select(AuthSessionRecord).order_by(AuthSessionRecord.created_at)
            )
        )
        assert len(records) == 2
        old_record = next(item for item in records if item.id == old_claims.session_id)
        new_record = next(item for item in records if item.id == new_claims.session_id)
        assert old_record.revoked_at is not None
        assert new_record.revoked_at is None
        assert new_record.auth_method == old_record.auth_method

    old_response = client.get(
        "/api/v1/auth/session",
        headers={"Authorization": f"Bearer {old_encoded}"},
    )
    assert old_response.status_code == 401
