from datetime import UTC, datetime, timedelta
from uuid import UUID

import jwt
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select

from app.auth.constants import (
    DEFAULT_ORGANIZATION_ID,
    INTERNAL_TOKEN_AUDIENCE,
    INTERNAL_TOKEN_ISSUER,
    INTERNAL_TOKEN_TYPE,
)
from app.auth.dependencies import get_current_principal
from app.auth.tokens import InvalidSessionTokenError, decode_session_token
from app.core.config import Settings, settings
from app.db.models import OrganizationMembershipRecord
from app.main import app
from tests.conftest import TestingSessionLocal


def _dev_session(client: TestClient, persona: str = "admin") -> dict[str, object]:
    response = client.post(
        "/api/v1/auth/dev/session",
        json={"persona": persona, "organization_slug": "pageragent-labs"},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _use_real_authentication() -> None:
    app.dependency_overrides.pop(get_current_principal, None)


def test_development_session_uses_http_only_cookie_and_database_permissions() -> None:
    client = TestClient(app)

    payload = _dev_session(client, "incident-commander")

    assert payload["session"]["active_organization"]["role"] == "incident_commander"
    assert "mitigations.decide" in payload["session"]["permissions"]
    assert payload["access_token"]
    claims = decode_session_token(str(payload["access_token"]))
    assert claims.expires_at > datetime.now(UTC)
    cookie = client.cookies.get(settings.session_cookie_name)
    assert cookie
    set_cookie = client.post(
        "/api/v1/auth/dev/session",
        json={"persona": "viewer", "organization_slug": "pageragent-labs"},
    ).headers["set-cookie"]
    assert "HttpOnly" in set_cookie
    assert "SameSite=strict" in set_cookie


def test_cookie_mutations_require_csrf_and_switch_to_an_active_membership() -> None:
    client = TestClient(app)
    payload = _dev_session(client)
    session = payload["session"]
    alternate = next(
        membership
        for membership in session["memberships"]
        if membership["organization"]["id"] != session["active_organization"]["id"]
    )

    denied = client.post(
        "/api/v1/auth/session/switch",
        json={"organization_id": alternate["organization"]["id"]},
    )
    assert denied.status_code == 403
    assert denied.json()["detail"] == "Missing or invalid CSRF token"

    switched = client.post(
        "/api/v1/auth/session/switch",
        json={"organization_id": alternate["organization"]["id"]},
        headers={"X-CSRF-Token": session["csrf_token"]},
    )
    assert switched.status_code == 200, switched.text
    switched_session = switched.json()
    assert switched_session["active_organization"]["id"] == alternate["organization"]["id"]


def test_protected_api_rejects_missing_and_tampered_bearer_tokens() -> None:
    client = TestClient(app)
    token = str(_dev_session(client)["access_token"])
    _use_real_authentication()

    client.cookies.clear()
    missing = client.get("/api/v1/incidents")
    assert missing.status_code == 401
    assert missing.headers["www-authenticate"] == "Bearer"

    header, payload_segment, signature = token.split(".")
    tampered_signature = f"{'A' if signature[0] != 'A' else 'B'}{signature[1:]}"
    tampered = ".".join((header, payload_segment, tampered_signature))
    invalid = client.get(
        "/api/v1/incidents",
        headers={"Authorization": f"Bearer {tampered}"},
    )
    assert invalid.status_code == 401
    assert "token" not in invalid.text.lower()


def test_database_role_change_immediately_reduces_existing_session_authority() -> None:
    client = TestClient(app)
    payload = _dev_session(client)
    token = str(payload["access_token"])
    user_id = payload["session"]["user"]["id"]
    with TestingSessionLocal() as session:
        membership = session.scalar(
            select(OrganizationMembershipRecord).where(
                OrganizationMembershipRecord.organization_id == DEFAULT_ORGANIZATION_ID,
                OrganizationMembershipRecord.user_id == UUID(str(user_id)),
            )
        )
        assert membership is not None
        membership.role = "viewer"
        session.commit()
    _use_real_authentication()

    denied = client.delete(
        "/api/v1/dev/incidents",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert denied.status_code == 403
    assert denied.json()["detail"] == "Missing permission: organization.reset"


def test_inactive_membership_returns_a_machine_readable_scope_revocation() -> None:
    client = TestClient(app)
    payload = _dev_session(client)
    token = str(payload["access_token"])
    user_id = UUID(str(payload["session"]["user"]["id"]))
    with TestingSessionLocal() as session:
        membership = session.scalar(
            select(OrganizationMembershipRecord).where(
                OrganizationMembershipRecord.organization_id == DEFAULT_ORGANIZATION_ID,
                OrganizationMembershipRecord.user_id == user_id,
            )
        )
        assert membership is not None
        membership.is_active = False
        session.commit()
    _use_real_authentication()

    response = client.get(
        "/api/v1/incidents",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == {
        "code": "membership_inactive",
        "message": "Session membership is inactive or unavailable",
    }


@pytest.mark.parametrize(
    ("overrides", "expected_error"),
    [
        ({"iss": "https://attacker.invalid"}, InvalidSessionTokenError),
        ({"aud": "another-api"}, InvalidSessionTokenError),
        ({"exp": datetime.now(UTC) - timedelta(seconds=1)}, InvalidSessionTokenError),
        ({"typ": "oidc-id-token"}, InvalidSessionTokenError),
        ({"jti": "not-a-uuid"}, InvalidSessionTokenError),
        ({"csrf": "too-short"}, InvalidSessionTokenError),
    ],
)
def test_session_decoder_rejects_wrong_security_claims(
    overrides: dict[str, object],
    expected_error: type[Exception],
) -> None:
    now = datetime.now(UTC)
    claims: dict[str, object] = {
        "iss": INTERNAL_TOKEN_ISSUER,
        "aud": INTERNAL_TOKEN_AUDIENCE,
        "sub": "00000000-0000-0000-0000-000000000101",
        "org": str(DEFAULT_ORGANIZATION_ID),
        "csrf": "c" * 32,
        "typ": INTERNAL_TOKEN_TYPE,
        "jti": "00000000-0000-0000-0000-000000000999",
        "iat": now,
        "nbf": now,
        "exp": now + timedelta(minutes=5),
    }
    claims.update(overrides)
    encoded = jwt.encode(
        claims,
        settings.session_secret.get_secret_value(),
        algorithm="HS256",
    )

    with pytest.raises(expected_error):
        decode_session_token(encoded)


def test_production_configuration_rejects_development_identity_defaults() -> None:
    with pytest.raises(ValidationError, match="Unsafe production configuration") as error:
        Settings(_env_file=None, PAGERAGENT_ENV="production")

    message = str(error.value)
    assert "PAGERAGENT_AUTH_MODE" in message
    assert "PAGERAGENT_SESSION_SECRET" in message
    assert "PAGERAGENT_INGEST_API_KEY" in message
    assert "PAGERAGENT_OIDC_ISSUER" in message
    assert "PAGERAGENT_CONNECTOR_MASTER_KEY" in message
    assert "PAGERAGENT_CONNECTOR_KEY_VERSION" in message
    assert "GITHUB_EVIDENCE_MODE" in message


def test_github_request_budget_must_cover_bounded_pages_and_one_token_refresh() -> None:
    with pytest.raises(ValidationError, match="GITHUB_MAX_API_REQUESTS is too small"):
        Settings(
            _env_file=None,
            GITHUB_MAX_API_REQUESTS=22,
            GITHUB_MAX_COMMITS=8,
            GITHUB_MAX_RELATED_ITEMS=10,
        )

    config = Settings(
        _env_file=None,
        GITHUB_MAX_API_REQUESTS=23,
        GITHUB_MAX_COMMITS=8,
        GITHUB_MAX_RELATED_ITEMS=10,
    )
    assert config.github_max_api_requests == 23


def test_production_configuration_accepts_complete_fail_closed_identity_settings() -> None:
    config = Settings(
        _env_file=None,
        PAGERAGENT_ENV="production",
        PAGERAGENT_AUTH_MODE="oidc",
        PAGERAGENT_SESSION_SECRET="session-secret-with-at-least-thirty-two-characters",
        PAGERAGENT_SESSION_COOKIE_SECURE=True,
        PAGERAGENT_OIDC_ISSUER="https://identity.example.com",
        PAGERAGENT_OIDC_AUDIENCE="pageragent-api",
        PAGERAGENT_OIDC_JWKS_URL="https://identity.example.com/.well-known/jwks.json",
        PAGERAGENT_INGEST_API_KEY="ingest-key-with-at-least-thirty-two-characters",
        PAGERAGENT_CONNECTOR_MASTER_KEY="eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHg=",
        PAGERAGENT_CONNECTOR_KEY_VERSION="production-v1",
        PAGERAGENT_CONNECTOR_ALLOWED_ORIGINS="https://api.github.com",
        GITHUB_EVIDENCE_MODE="connector",
        PAGERAGENT_TELEMETRY_ALLOWED_ORIGINS="https://telemetry.example.com",
        backend_cors_origins="https://pageragent.example.com",
    )

    assert config.auth_mode == "oidc"
