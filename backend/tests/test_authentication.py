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
    assert "PAGERAGENT_INGEST_ORGANIZATION_SLUG" in message
    assert "DATABASE_URL" in message
    assert "REDIS_URL" in message
    assert "PAGERAGENT_OIDC_ISSUER" in message
    assert "PAGERAGENT_CONNECTOR_CIPHER_PROVIDER" in message
    assert "PAGERAGENT_OIDC_CLIENT_ID" in message
    assert "PAGERAGENT_OIDC_TRANSACTION_KEY" in message
    assert "PAGERAGENT_TRUSTED_HOSTS" in message
    assert "GITHUB_EVIDENCE_MODE" in message
    assert "PROMETHEUS_EVIDENCE_MODE" in message


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


@pytest.mark.parametrize(
    "overrides",
    [
        {
            "PROMETHEUS_MAX_WINDOW_SECONDS": 60,
            "PROMETHEUS_QUERY_STEP_SECONDS": 300,
        },
        {
            "PROMETHEUS_MAX_WINDOW_SECONDS": 1_800,
            "PROMETHEUS_QUERY_STEP_SECONDS": 15,
            "PROMETHEUS_MAX_SAMPLES": 100,
        },
    ],
)
def test_prometheus_query_limits_must_form_a_complete_bounded_window(
    overrides: dict[str, int],
) -> None:
    with pytest.raises(ValidationError, match="PROMETHEUS_"):
        Settings(_env_file=None, **overrides)


def test_production_configuration_accepts_complete_fail_closed_identity_settings() -> None:
    config = Settings(
        _env_file=None,
        PAGERAGENT_ENV="production",
        PAGERAGENT_AUTH_MODE="oidc",
        PAGERAGENT_SESSION_SECRET="session-secret-with-at-least-thirty-two-characters",
        PAGERAGENT_SESSION_COOKIE_SECURE=True,
        PAGERAGENT_SESSION_COOKIE_NAME="__Host-pageragent_session",
        PAGERAGENT_OIDC_ISSUER="https://identity.acme.dev",
        PAGERAGENT_OIDC_AUDIENCE="pageragent-api",
        PAGERAGENT_OIDC_JWKS_URL="https://identity.acme.dev/.well-known/jwks.json",
        PAGERAGENT_OIDC_CLIENT_ID="pageragent-api",
        PAGERAGENT_OIDC_CLIENT_SECRET=(
            "oidc-client-secret-with-at-least-thirty-two-characters"
        ),
        PAGERAGENT_OIDC_AUTHORIZATION_URL="https://identity.acme.dev/oauth2/authorize",
        PAGERAGENT_OIDC_TOKEN_URL="https://identity.acme.dev/oauth2/token",
        PAGERAGENT_OIDC_REDIRECT_URI=(
            "https://pageragent.acme.dev/api/v1/auth/oidc/callback"
        ),
        PAGERAGENT_OIDC_FRONTEND_URL="https://pageragent.acme.dev/",
        PAGERAGENT_OIDC_DEFAULT_ORGANIZATION_SLUG="pageragent-labs",
        PAGERAGENT_OIDC_LOGIN_COOKIE_NAME="__Host-pageragent_oidc_login",
        PAGERAGENT_OIDC_TRANSACTION_KEY=(
            "ZmVkY2JhOTg3NjU0MzIxMGZlZGNiYTk4NzY1NDMyMTA="
        ),
        PAGERAGENT_INGEST_API_KEY="ingest-key-with-at-least-thirty-two-characters",
        PAGERAGENT_INGEST_ORGANIZATION_SLUG="pageragent-production",
        PAGERAGENT_CONNECTOR_CIPHER_PROVIDER="aws_kms",
        PAGERAGENT_CONNECTOR_KMS_KEY_ARN=(
            "arn:aws:kms:us-east-1:123456789012:"
            "key/12345678-1234-1234-1234-123456789012"
        ),
        PAGERAGENT_CONNECTOR_KMS_REGION="us-east-1",
        PAGERAGENT_CONNECTOR_ALLOWED_ORIGINS="https://api.github.com",
        GITHUB_EVIDENCE_MODE="connector",
        PROMETHEUS_EVIDENCE_MODE="connector",
        PAGERAGENT_TELEMETRY_ALLOWED_ORIGINS="https://telemetry.acme.dev",
        PAGERAGENT_TRUSTED_HOSTS="PAGERAGENT.ACME.DEV",
        backend_cors_origins="https://pageragent.acme.dev",
        DATABASE_URL=(
            "postgresql+psycopg://pageragent:secret@db.acme.dev:5432/pageragent"
            "?sslmode=verify-full"
        ),
        REDIS_URL="rediss://cache.acme.dev:6380/0?ssl_check_hostname=true",
    )

    assert config.auth_mode == "oidc"
    assert config.backend_trusted_hosts == "pageragent.acme.dev"


@pytest.mark.parametrize(
    "service_name",
    ["pageragent-migration", "pageragent-outbox-relay"],
)
def test_production_storage_roles_start_without_browser_identity_secrets(
    service_name: str,
) -> None:
    values: dict[str, object] = {
        "service_name": service_name,
        "PAGERAGENT_ENV": "production",
        "DATABASE_URL": (
            "postgresql+psycopg://pageragent:secret@db.acme.dev/pageragent"
            "?sslmode=verify-full"
        ),
    }
    if service_name == "pageragent-outbox-relay":
        values["REDIS_URL"] = (
            "rediss://cache.acme.dev:6380/0?ssl_check_hostname=true"
        )

    config = Settings(_env_file=None, **values)

    assert config.service_name == service_name
    assert config.auth_mode == "local"


def test_production_worker_requires_only_storage_and_connector_capabilities() -> None:
    config = Settings(
        _env_file=None,
        service_name="pageragent-workflow-worker",
        PAGERAGENT_ENV="production",
        DATABASE_URL=(
            "postgresql+psycopg://pageragent:secret@db.acme.dev/pageragent"
            "?sslmode=verify-full"
        ),
        REDIS_URL="rediss://cache.acme.dev:6380/0?ssl_check_hostname=true",
        PAGERAGENT_CONNECTOR_CIPHER_PROVIDER="aws_kms",
        PAGERAGENT_CONNECTOR_KMS_KEY_ARN=(
            "arn:aws:kms:us-east-1:123456789012:"
            "key/12345678-1234-1234-1234-123456789012"
        ),
        PAGERAGENT_CONNECTOR_KMS_REGION="us-east-1",
        PAGERAGENT_CONNECTOR_ALLOWED_ORIGINS="https://api.github.com",
        GITHUB_EVIDENCE_MODE="connector",
        PROMETHEUS_EVIDENCE_MODE="connector",
        PAGERAGENT_TELEMETRY_ALLOWED_ORIGINS="https://telemetry.acme.dev",
    )

    assert config.service_name == "pageragent-workflow-worker"
    assert config.auth_mode == "local"


def test_production_workload_role_cannot_bypass_the_storage_boundary() -> None:
    with pytest.raises(ValidationError, match="DATABASE_URL"):
        Settings(
            _env_file=None,
            service_name="pageragent-migration",
            PAGERAGENT_ENV="production",
            DATABASE_URL="postgresql+psycopg://user:secret@localhost/pageragent",
        )


def test_production_relay_cannot_inherit_connector_custody() -> None:
    with pytest.raises(ValidationError, match="disable KMS connector custody"):
        Settings(
            _env_file=None,
            service_name="pageragent-outbox-relay",
            PAGERAGENT_ENV="production",
            DATABASE_URL=(
                "postgresql+psycopg://pageragent:secret@db.acme.dev/pageragent"
                "?sslmode=verify-full"
            ),
            REDIS_URL="rediss://cache.acme.dev:6380/0?ssl_check_hostname=true",
            PAGERAGENT_CONNECTOR_CIPHER_PROVIDER="aws_kms",
            PAGERAGENT_CONNECTOR_KMS_KEY_ARN=(
                "arn:aws:kms:us-east-1:123456789012:"
                "key/12345678-1234-1234-1234-123456789012"
            ),
            PAGERAGENT_CONNECTOR_KMS_REGION="us-east-1",
        )


@pytest.mark.parametrize(
    ("override", "message"),
    [
        (
            {"PAGERAGENT_SESSION_COOKIE_NAME": "pageragent_session"},
            "PAGERAGENT_SESSION_COOKIE_NAME",
        ),
        (
            {"PAGERAGENT_OIDC_LOGIN_COOKIE_NAME": "pageragent_oidc_login"},
            "PAGERAGENT_OIDC_LOGIN_COOKIE_NAME",
        ),
        (
            {
                "PAGERAGENT_OIDC_REDIRECT_URI": (
                    "https://pageragent.acme.dev/unregistered/callback"
                )
            },
            "PAGERAGENT_OIDC_REDIRECT_URI",
        ),
        (
            {
                "PAGERAGENT_OIDC_FRONTEND_URL": (
                    "https://console.pageragent.acme.dev/"
                )
            },
            "share one browser origin",
        ),
        (
            {"PAGERAGENT_TRUSTED_HOSTS": "*"},
            "PAGERAGENT_TRUSTED_HOSTS",
        ),
        (
            {"PAGERAGENT_TRUSTED_HOSTS": "unrelated.acme.dev"},
            "exact OIDC frontend hostname",
        ),
        (
            {
                "PAGERAGENT_TRUSTED_HOSTS": (
                    "pageragent.acme.dev,api.acme.dev"
                )
            },
            "only the exact OIDC frontend hostname",
        ),
        (
            {"DATABASE_URL": "postgresql+psycopg://pageragent:secret@localhost/db"},
            "DATABASE_URL",
        ),
        (
            {
                "DATABASE_URL": (
                    "postgresql+psycopg://pageragent:secret@db.acme.dev/db"
                    "?sslmode=require&sslmode=disable"
                )
            },
            "exactly one secure sslmode",
        ),
        (
            {
                "DATABASE_URL": (
                    "postgresql+psycopg://pageragent:secret@db.acme.dev/db"
                    "?sslmode=verify-full&host=localhost"
                )
            },
            "override its authority",
        ),
        (
            {
                "DATABASE_URL": (
                    "postgresql+psycopg://pageragent:secret@db.acme.dev/db"
                    "?sslmode=verify-full&hostaddr=127.0.0.1"
                )
            },
            "override its authority",
        ),
        (
            {"REDIS_URL": "redis://cache.acme.dev:6379/0"},
            "REDIS_URL",
        ),
        (
            {
                "REDIS_URL": (
                    "rediss://cache.acme.dev:6380/0"
                    "?ssl_check_hostname=true&ssl_cert_reqs=none"
                )
            },
            "certificate verification",
        ),
        (
            {
                "REDIS_URL": (
                    "rediss://cache.acme.dev:6380/0?ssl_check_hostname=false"
                )
            },
            "ssl_check_hostname=true",
        ),
        (
            {
                "DATABASE_URL": (
                    "postgresql+psycopg://pageragent:secret@%6cocalhost/db"
                    "?sslmode=verify-full"
                )
            },
            "canonical DNS name or IP address",
        ),
        (
            {
                "REDIS_URL": (
                    "rediss://%2Ftmp%2Fredis.sock:6380/0"
                    "?ssl_check_hostname=true"
                )
            },
            "canonical DNS name or IP address",
        ),
        (
            {"PAGERAGENT_INGEST_ORGANIZATION_SLUG": ""},
            "PAGERAGENT_INGEST_ORGANIZATION_SLUG",
        ),
        (
            {
                "PAGERAGENT_SESSION_SECRET": (
                    "ingest-key-with-at-least-thirty-two-characters"
                )
            },
            "must be distinct",
        ),
        (
            {"DURABLE_MITIGATION_ENABLED": True},
            "DURABLE_MITIGATION_ENABLED",
        ),
        (
            {
                "DATABASE_URL": (
                    "postgresql+psycopg://pageragent:secret@db.example.net/db"
                    "?sslmode=verify-full"
                )
            },
            "DATABASE_URL must use a non-reserved public endpoint",
        ),
        (
            {
                "REDIS_URL": (
                    "rediss://cache.example.org:6380/0?ssl_check_hostname=true"
                )
            },
            "REDIS_URL must use a non-reserved public endpoint",
        ),
        (
            {"PAGERAGENT_OIDC_ISSUER": "https://identity.example.com"},
            "PAGERAGENT_OIDC_ISSUER must use a non-reserved public host",
        ),
        (
            {
                "PAGERAGENT_CONNECTOR_ALLOWED_ORIGINS": (
                    "https://api.github.com,https://prometheus.example.com"
                )
            },
            "PAGERAGENT_CONNECTOR_ALLOWED_ORIGINS must use non-reserved public hosts",
        ),
        (
            {
                "PAGERAGENT_TELEMETRY_ALLOWED_ORIGINS": (
                    "https://telemetry.example.com"
                )
            },
            "PAGERAGENT_TELEMETRY_ALLOWED_ORIGINS must use non-reserved public hosts",
        ),
        (
            {"PAGERAGENT_TRUSTED_HOSTS": "pageragent.example.com"},
            "PAGERAGENT_TRUSTED_HOSTS must use non-reserved public hosts",
        ),
        (
            {"backend_cors_origins": "https://pageragent.example.com"},
            "BACKEND_CORS_ORIGINS must use non-reserved public hosts",
        ),
    ],
)
def test_production_configuration_rejects_cookie_tossing_and_callback_drift(
    override: dict[str, object],
    message: str,
) -> None:
    production = {
        "PAGERAGENT_ENV": "production",
        "PAGERAGENT_AUTH_MODE": "oidc",
        "PAGERAGENT_SESSION_SECRET": (
            "session-secret-with-at-least-thirty-two-characters"
        ),
        "PAGERAGENT_SESSION_COOKIE_SECURE": True,
        "PAGERAGENT_SESSION_COOKIE_NAME": "__Host-pageragent_session",
        "PAGERAGENT_OIDC_ISSUER": "https://identity.acme.dev",
        "PAGERAGENT_OIDC_AUDIENCE": "pageragent-api",
        "PAGERAGENT_OIDC_JWKS_URL": "https://identity.acme.dev/jwks",
        "PAGERAGENT_OIDC_CLIENT_ID": "pageragent-api",
        "PAGERAGENT_OIDC_CLIENT_SECRET": "client-secret-with-32-characters-minimum",
        "PAGERAGENT_OIDC_AUTHORIZATION_URL": "https://identity.acme.dev/authorize",
        "PAGERAGENT_OIDC_TOKEN_URL": "https://identity.acme.dev/token",
        "PAGERAGENT_OIDC_REDIRECT_URI": (
            "https://pageragent.acme.dev/api/v1/auth/oidc/callback"
        ),
        "PAGERAGENT_OIDC_FRONTEND_URL": "https://pageragent.acme.dev/",
        "PAGERAGENT_OIDC_DEFAULT_ORGANIZATION_SLUG": "pageragent-labs",
        "PAGERAGENT_OIDC_TRANSACTION_KEY": (
            "ZmVkY2JhOTg3NjU0MzIxMGZlZGNiYTk4NzY1NDMyMTA="
        ),
        "PAGERAGENT_OIDC_LOGIN_COOKIE_NAME": "__Host-pageragent_oidc_login",
        "PAGERAGENT_INGEST_API_KEY": "ingest-key-with-at-least-thirty-two-characters",
        "PAGERAGENT_INGEST_ORGANIZATION_SLUG": "pageragent-production",
        "PAGERAGENT_CONNECTOR_CIPHER_PROVIDER": "aws_kms",
        "PAGERAGENT_CONNECTOR_KMS_KEY_ARN": (
            "arn:aws:kms:us-east-1:123456789012:"
            "key/12345678-1234-1234-1234-123456789012"
        ),
        "PAGERAGENT_CONNECTOR_KMS_REGION": "us-east-1",
        "PAGERAGENT_CONNECTOR_ALLOWED_ORIGINS": "https://api.github.com",
        "GITHUB_EVIDENCE_MODE": "connector",
        "PROMETHEUS_EVIDENCE_MODE": "connector",
        "PAGERAGENT_TELEMETRY_ALLOWED_ORIGINS": "https://telemetry.acme.dev",
        "PAGERAGENT_TRUSTED_HOSTS": "pageragent.acme.dev",
        "backend_cors_origins": "https://pageragent.acme.dev",
        "DATABASE_URL": (
            "postgresql+psycopg://pageragent:secret@db.acme.dev:5432/pageragent"
            "?sslmode=verify-full"
        ),
        "REDIS_URL": "rediss://cache.acme.dev:6380/0?ssl_check_hostname=true",
    }
    production.update(override)

    with pytest.raises(ValidationError, match=message):
        Settings(_env_file=None, **production)
