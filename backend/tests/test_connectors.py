from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth.constants import DEFAULT_ORGANIZATION_ID, DEFAULT_ORGANIZATION_SLUG
from app.auth.dependencies import get_current_principal
from app.auth.permissions import permissions_for_role
from app.connectors.vault import CredentialContext, SealedCredentials, build_credential_cipher
from app.db.models import (
    ConnectorAuditEventRecord,
    ConnectorCredentialRecord,
    ConnectorRecord,
    OrganizationRecord,
)
from app.domain.auth import Principal, Role
from app.domain.connectors import ConnectorProvider
from app.main import app
from tests.conftest import TEST_USER_ID, TestingSessionLocal

SECRET_SENTINEL = "connector-private-secret-sentinel"
WEBHOOK_SECRET = "github-webhook-secret-with-at-least-32-bytes"


class StubGitHubProvider:
    def validate(self) -> None:
        return None


@pytest.fixture(autouse=True)
def stub_github_provider_handshake(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.services.connectors.build_github_provider",
        lambda _configuration, _credentials: StubGitHubProvider(),
    )


def github_connector(
    *,
    name: str = "Checkout GitHub",
    secret: str = SECRET_SENTINEL,
    service: str = "checkout-api",
    repository: str = "pageragent/checkout",
) -> dict[str, object]:
    return {
        "name": name,
        "provider": "github",
        "configuration": {
            "service": service,
            "repository": repository,
            "app_id": 1001,
            "installation_id": 2002,
            "api_url": "https://api.github.com",
        },
        "credentials": {
            "private_key": secret,
            "webhook_secret": WEBHOOK_SECRET,
        },
    }


def principal(role: Role, organization_id: UUID = DEFAULT_ORGANIZATION_ID) -> Principal:
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


def test_connector_lifecycle_is_versioned_disabled_by_default_and_never_returns_secrets(
    db_session: Session,
) -> None:
    client = TestClient(app)

    created_response = client.post("/api/v1/connectors", json=github_connector())
    assert created_response.status_code == 201
    created = created_response.json()
    serialized = created_response.text
    assert SECRET_SENTINEL not in serialized
    assert "ciphertext" not in serialized
    assert created["enabled"] is False
    assert created["status"] == "disabled"
    assert created["version"] == 1
    assert created["credential_version"] == 1
    assert created["credential_fields"] == ["private_key", "webhook_secret"]
    assert SECRET_SENTINEL not in client.get("/api/v1/connectors").text
    assert SECRET_SENTINEL not in client.get(
        f"/api/v1/connectors/{created['id']}"
    ).text

    premature_enable = client.patch(
        f"/api/v1/connectors/{created['id']}",
        json={"expected_version": 1, "enabled": True},
    )
    assert premature_enable.status_code == 409
    assert premature_enable.json()["detail"] == (
        "A connector must pass its current validation before it can be enabled"
    )

    credential = db_session.scalar(select(ConnectorCredentialRecord))
    assert credential is not None
    assert SECRET_SENTINEL.encode() not in credential.ciphertext
    assert credential.key_version == "local-v1"
    assert len(credential.ciphertext_nonce) == 12
    assert len(credential.wrapped_key_nonce) == 12
    assert credential.ciphertext_nonce != credential.wrapped_key_nonce

    validated_response = client.post(
        f"/api/v1/connectors/{created['id']}/validate",
        json={"expected_version": 1},
    )
    assert validated_response.status_code == 200
    validated = validated_response.json()
    assert validated["version"] == 2
    assert validated["last_validation_ok"] is True
    assert "GitHub App installation" in validated["last_validation_message"]
    assert validated["status"] == "disabled"

    enabled_response = client.patch(
        f"/api/v1/connectors/{created['id']}",
        json={"expected_version": 2, "enabled": True},
    )
    assert enabled_response.status_code == 200
    enabled = enabled_response.json()
    assert enabled["enabled"] is True
    assert enabled["status"] == "configured"
    assert enabled["version"] == 3

    rotated_response = client.put(
        f"/api/v1/connectors/{created['id']}/credentials",
        json={
            "expected_version": 3,
            "credentials": {
                "private_key": "rotated-secret-sentinel",
                "webhook_secret": "rotated-webhook-secret-with-at-least-32-bytes",
            },
        },
    )
    assert rotated_response.status_code == 200
    rotated = rotated_response.json()
    assert rotated["version"] == 4
    assert rotated["credential_version"] == 2
    assert rotated["enabled"] is False
    assert rotated["last_validation_ok"] is None
    assert "rotated-secret-sentinel" not in rotated_response.text

    stale = client.patch(
        f"/api/v1/connectors/{created['id']}",
        json={"expected_version": 3, "name": "Stale Rename"},
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["current_version"] == 4

    events_response = client.get(f"/api/v1/connectors/{created['id']}/events")
    assert events_response.status_code == 200
    events = events_response.json()
    assert [event["connector_version"] for event in events] == [1, 2, 3, 4]
    assert all(event["actor"] == f"user:{TEST_USER_ID}" for event in events)
    assert SECRET_SENTINEL not in events_response.text
    assert "rotated-secret-sentinel" not in events_response.text
    assert db_session.scalar(select(ConnectorAuditEventRecord)) is not None
    assert client.delete(f"/api/v1/connectors/{created['id']}").status_code == 405


def test_multiline_github_pem_is_preserved_in_the_sealed_envelope_and_never_returned(
    db_session: Session,
) -> None:
    multiline_pem = (
        "-----BEGIN RSA PRIVATE KEY-----\r\n"
        "line-one\n"
        "line-two\r\n"
        "-----END RSA PRIVATE KEY-----\n"
    )
    payload = github_connector(secret=multiline_pem)

    response = TestClient(app).post("/api/v1/connectors", json=payload)

    assert response.status_code == 201
    assert multiline_pem not in response.text
    connector = db_session.scalar(select(ConnectorRecord))
    credential = db_session.scalar(select(ConnectorCredentialRecord))
    assert connector is not None
    assert credential is not None
    opened = build_credential_cipher().open(
        SealedCredentials(
            ciphertext=credential.ciphertext,
            ciphertext_nonce=credential.ciphertext_nonce,
            wrapped_data_key=credential.wrapped_data_key,
            wrapped_key_nonce=credential.wrapped_key_nonce,
            key_version=credential.key_version,
            credential_field_names=tuple(credential.credential_field_names),
        ),
        CredentialContext(
            organization_id=connector.organization_id,
            connector_id=connector.id,
            provider=ConnectorProvider.GITHUB,
            credential_version=credential.credential_version,
        ),
    )
    assert opened["private_key"] == multiline_pem
    assert opened["webhook_secret"] == WEBHOOK_SECRET


def test_github_repository_identity_is_canonicalized_and_supports_dot_names(
    db_session: Session,
) -> None:
    response = TestClient(app).post(
        "/api/v1/connectors",
        json=github_connector(repository="PagerAgent/.GitHub"),
    )

    assert response.status_code == 201
    assert response.json()["configuration"]["repository"] == "pageragent/.github"
    connector = db_session.scalar(select(ConnectorRecord))
    assert connector is not None
    assert connector.configuration["repository"] == "pageragent/.github"


def test_failed_github_handshake_is_sanitized_and_cannot_enable_connector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingGitHubProvider:
        def validate(self) -> None:
            raise RuntimeError(f"provider body contained {SECRET_SENTINEL}")

    monkeypatch.setattr(
        "app.services.connectors.build_github_provider",
        lambda _configuration, _credentials: FailingGitHubProvider(),
    )
    client = TestClient(app)
    created = client.post("/api/v1/connectors", json=github_connector()).json()

    response = client.post(
        f"/api/v1/connectors/{created['id']}/validate",
        json={"expected_version": created["version"]},
    )

    assert response.status_code == 200
    assert response.json()["last_validation_ok"] is False
    assert response.json()["status"] == "invalid"
    assert response.json()["enabled"] is False
    assert SECRET_SENTINEL not in response.text
    assert "provider body" not in response.text
    assert (
        client.patch(
            f"/api/v1/connectors/{created['id']}",
            json={"expected_version": response.json()["version"], "enabled": True},
        ).status_code
        == 409
    )


def test_only_one_enabled_github_connector_can_own_a_service_binding() -> None:
    client = TestClient(app)
    first = client.post("/api/v1/connectors", json=github_connector()).json()
    first = client.post(
        f"/api/v1/connectors/{first['id']}/validate",
        json={"expected_version": first["version"]},
    ).json()
    first = client.patch(
        f"/api/v1/connectors/{first['id']}",
        json={"expected_version": first["version"], "enabled": True},
    ).json()
    assert first["enabled"] is True

    second = client.post(
        "/api/v1/connectors",
        json=github_connector(
            name="Second checkout repository",
            repository="pageragent/checkout-worker",
        ),
    ).json()
    second = client.post(
        f"/api/v1/connectors/{second['id']}/validate",
        json={"expected_version": second["version"]},
    ).json()

    conflict = client.patch(
        f"/api/v1/connectors/{second['id']}",
        json={"expected_version": second["version"], "enabled": True},
    )

    assert conflict.status_code == 409
    assert conflict.json()["detail"] == (
        "Another enabled GitHub connector already owns this service binding"
    )
    assert client.get(f"/api/v1/connectors/{second['id']}").json()["enabled"] is False


def test_provider_validation_discards_a_result_when_connector_changes_during_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RacingGitHubProvider:
        def validate(self) -> None:
            with TestingSessionLocal() as racing_session:
                connector = racing_session.scalar(select(ConnectorRecord))
                assert connector is not None
                connector.name = "Changed during handshake"
                connector.version += 1
                racing_session.commit()

    monkeypatch.setattr(
        "app.services.connectors.build_github_provider",
        lambda _configuration, _credentials: RacingGitHubProvider(),
    )
    client = TestClient(app)
    created = client.post("/api/v1/connectors", json=github_connector()).json()

    response = client.post(
        f"/api/v1/connectors/{created['id']}/validate",
        json={"expected_version": created["version"]},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["current_version"] == 2
    with TestingSessionLocal() as verification_session:
        connector = verification_session.scalar(select(ConnectorRecord))
        assert connector is not None
        assert connector.last_validation_ok is None
        validation_events = verification_session.scalars(
            select(ConnectorAuditEventRecord).where(
                ConnectorAuditEventRecord.event_type == "connector.validation_completed"
            )
        ).all()
    assert validation_events == []


@pytest.mark.parametrize(
    ("payload", "credential_field"),
    [
        (
            {
                "name": "Metrics",
                "provider": "prometheus",
                "configuration": {"base_url": "http://prometheus:9090"},
                "credentials": {"bearer_token": "prom-token"},
            },
            "bearer_token",
        ),
        (
            {
                "name": "Incident Slack",
                "provider": "slack",
                "configuration": {
                    "channel": "C012345",
                    "api_url": "https://slack.com",
                },
                "credentials": {"bot_token": "xoxb-test"},
            },
            "bot_token",
        ),
    ],
)
def test_provider_contracts_accept_prometheus_and_slack(
    payload: dict[str, object],
    credential_field: str,
) -> None:
    response = TestClient(app).post("/api/v1/connectors", json=payload)

    assert response.status_code == 201
    assert response.json()["credential_fields"] == [credential_field]
    assert str(payload["credentials"]) not in response.text


@pytest.mark.parametrize(
    "url",
    [
        "https://api.github.com/admin",
        "https://api.github.com?token=secret",
        "https://api.github.com/#fragment",
        "https://user:password@api.github.com",
        "https://api.github.com:8443",
        "https://api.github.com.evil.example",
        "http://127.0.0.1",
    ],
)
def test_connector_urls_must_be_exact_allowlisted_origins(url: str) -> None:
    payload = github_connector()
    payload["configuration"] = {
        "service": "checkout-api",
        "repository": "pageragent/checkout",
        "app_id": 1,
        "installation_id": 2,
        "api_url": url,
    }

    response = TestClient(app).post("/api/v1/connectors", json=payload)

    assert response.status_code == 422
    assert SECRET_SENTINEL not in response.text


@pytest.mark.parametrize(
    "repository",
    [
        "owner/../repository",
        "owner/repository?token=secret",
        "owner/repository#fragment",
        "owner/%2e%2e",
        "./repository",
        "owner/..",
        "owner/repository/extra",
    ],
)
def test_github_repository_segments_are_safe_for_api_path_construction(
    repository: str,
) -> None:
    response = TestClient(app).post(
        "/api/v1/connectors",
        json=github_connector(repository=repository),
    )

    assert response.status_code == 422
    assert SECRET_SENTINEL not in response.text


def test_github_connector_requires_an_explicit_service_binding() -> None:
    payload = github_connector()
    assert isinstance(payload["configuration"], dict)
    payload["configuration"].pop("service")

    response = TestClient(app).post("/api/v1/connectors", json=payload)

    assert response.status_code == 422


@pytest.mark.parametrize(
    "credentials",
    [
        {"private_key": "", "actor": SECRET_SENTINEL},
        {"bot_token": SECRET_SENTINEL},
        SECRET_SENTINEL,
        {"private_key": SECRET_SENTINEL, "extra": "not-allowed"},
    ],
)
def test_invalid_credential_requests_never_reflect_secret_input(credentials: object) -> None:
    payload = github_connector()
    payload["credentials"] = credentials

    response = TestClient(app).post("/api/v1/connectors", json=payload)

    assert response.status_code == 422
    assert SECRET_SENTINEL not in response.text


def test_invalid_atomic_create_persists_no_connector_credential_or_audit_rows(
    db_session: Session,
) -> None:
    payload = github_connector()
    payload["credentials"] = {"private_key": SECRET_SENTINEL, "unexpected": "value"}

    response = TestClient(app).post("/api/v1/connectors", json=payload)

    assert response.status_code == 422
    assert SECRET_SENTINEL not in response.text
    assert db_session.scalar(select(func.count()).select_from(ConnectorRecord)) == 0
    assert db_session.scalar(select(func.count()).select_from(ConnectorCredentialRecord)) == 0
    assert db_session.scalar(select(func.count()).select_from(ConnectorAuditEventRecord)) == 0


def test_public_mutations_reject_actor_and_organization_fields() -> None:
    payload = github_connector()
    payload["actor"] = SECRET_SENTINEL
    payload["organization_id"] = str(DEFAULT_ORGANIZATION_ID)

    response = TestClient(app).post("/api/v1/connectors", json=payload)

    assert response.status_code == 422
    assert SECRET_SENTINEL not in response.text


def test_create_rejects_client_owned_enabled_state_without_reflecting_input() -> None:
    payload = github_connector()
    payload["enabled"] = False
    payload[SECRET_SENTINEL] = "also-secret"

    response = TestClient(app).post("/api/v1/connectors", json=payload)

    assert response.status_code == 422
    assert SECRET_SENTINEL not in response.text


def test_whitespace_name_and_multibyte_secret_bounds_fail_without_reflection() -> None:
    whitespace_name = github_connector(name="   ")
    name_response = TestClient(app).post("/api/v1/connectors", json=whitespace_name)
    assert name_response.status_code == 422
    assert SECRET_SENTINEL not in name_response.text

    oversized = github_connector(secret=SECRET_SENTINEL + ("é" * 40_000))
    secret_response = TestClient(app).post("/api/v1/connectors", json=oversized)
    assert secret_response.status_code == 422
    assert SECRET_SENTINEL not in secret_response.text


def test_duplicate_name_is_tenant_scoped_and_returns_sanitized_conflict(
    db_session: Session,
) -> None:
    client = TestClient(app)
    first = client.post("/api/v1/connectors", json=github_connector())
    duplicate = client.post("/api/v1/connectors", json=github_connector())
    assert first.status_code == 201
    assert duplicate.status_code == 409
    assert SECRET_SENTINEL not in duplicate.text

    other_id = UUID("00000000-0000-0000-0000-000000000002")
    db_session.add(
        OrganizationRecord(id=other_id, slug="other-operations", name="Other Operations")
    )
    db_session.commit()
    app.dependency_overrides[get_current_principal] = lambda: principal(Role.ADMIN, other_id)
    other = client.post("/api/v1/connectors", json=github_connector())
    assert other.status_code == 201


def test_cross_tenant_connector_ids_are_indistinguishable_from_missing(
    db_session: Session,
) -> None:
    client = TestClient(app)
    created = client.post("/api/v1/connectors", json=github_connector()).json()
    other_id = UUID("00000000-0000-0000-0000-000000000002")
    db_session.add(
        OrganizationRecord(id=other_id, slug="other-operations", name="Other Operations")
    )
    db_session.commit()
    app.dependency_overrides[get_current_principal] = lambda: principal(Role.ADMIN, other_id)

    assert client.get("/api/v1/connectors").json() == []
    assert client.get(f"/api/v1/connectors/{created['id']}").status_code == 404
    assert client.get(f"/api/v1/connectors/{created['id']}/events").status_code == 404
    assert (
        client.patch(
            f"/api/v1/connectors/{created['id']}",
            json={"expected_version": 1, "name": "Nope"},
        ).status_code
        == 404
    )
    assert (
        client.put(
            f"/api/v1/connectors/{created['id']}/credentials",
            json={
                "expected_version": 1,
                "credentials": {
                    "private_key": "other-secret",
                    "webhook_secret": "other-webhook-secret-with-at-least-32-bytes",
                },
            },
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"/api/v1/connectors/{created['id']}/validate",
            json={"expected_version": 1},
        ).status_code
        == 404
    )


def test_incident_commander_can_read_but_only_admin_can_mutate_or_validate() -> None:
    client = TestClient(app)
    created = client.post("/api/v1/connectors", json=github_connector()).json()
    app.dependency_overrides[get_current_principal] = lambda: principal(Role.INCIDENT_COMMANDER)

    assert client.get("/api/v1/connectors").status_code == 200
    assert client.get(f"/api/v1/connectors/{created['id']}").status_code == 200
    assert client.post("/api/v1/connectors", json=github_connector(name="Other")).status_code == 403
    assert (
        client.patch(
            f"/api/v1/connectors/{created['id']}",
            json={"expected_version": 1, "name": "Denied"},
        ).status_code
        == 403
    )
    assert (
        client.put(
            f"/api/v1/connectors/{created['id']}/credentials",
            json={
                "expected_version": 1,
                "credentials": {
                    "private_key": "denied-secret",
                    "webhook_secret": "denied-webhook-secret-with-at-least-32-bytes",
                },
            },
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/api/v1/connectors/{created['id']}/validate",
            json={"expected_version": 1},
        ).status_code
        == 403
    )

    for denied_role in (Role.RESPONDER, Role.VIEWER):
        app.dependency_overrides[get_current_principal] = lambda role=denied_role: principal(role)
        assert client.get("/api/v1/connectors").status_code == 403


def test_vault_tampering_marks_connector_invalid_without_exposing_details(
    db_session: Session,
) -> None:
    client = TestClient(app)
    created = client.post("/api/v1/connectors", json=github_connector()).json()
    validated = client.post(
        f"/api/v1/connectors/{created['id']}/validate",
        json={"expected_version": 1},
    ).json()
    enabled = client.patch(
        f"/api/v1/connectors/{created['id']}",
        json={"expected_version": validated["version"], "enabled": True},
    ).json()
    assert enabled["enabled"] is True
    credential = db_session.scalar(select(ConnectorCredentialRecord))
    assert credential is not None
    credential.ciphertext = bytes([credential.ciphertext[0] ^ 1]) + credential.ciphertext[1:]
    db_session.commit()

    response = client.post(
        f"/api/v1/connectors/{created['id']}/validate",
        json={"expected_version": enabled["version"]},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "invalid"
    assert response.json()["enabled"] is False
    assert response.json()["last_validation_ok"] is False
    assert "credential validation failed" in response.json()["last_validation_message"]
    assert "integrity" not in response.text.lower()


def test_tampered_audit_payload_fails_closed_without_reflecting_stored_secret(
    db_session: Session,
) -> None:
    client = TestClient(app)
    created = client.post("/api/v1/connectors", json=github_connector()).json()
    event = db_session.scalar(select(ConnectorAuditEventRecord))
    assert event is not None
    event.payload = {**event.payload, "unexpected": SECRET_SENTINEL}
    db_session.commit()

    response = client.get(f"/api/v1/connectors/{created['id']}/events")

    assert response.status_code == 500
    assert response.json()["detail"] == "Connector audit ledger integrity check failed"
    assert SECRET_SENTINEL not in response.text
