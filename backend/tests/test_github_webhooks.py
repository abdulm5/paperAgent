import asyncio
import copy
import hashlib
import hmac
import json
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.api.routes.github_webhooks import (
    MAX_GITHUB_WEBHOOK_BODY_BYTES,
    ingest_github_webhook,
    read_limited_request_body,
)
from app.auth.constants import DEFAULT_ORGANIZATION_ID, DEFAULT_ORGANIZATION_SLUG
from app.auth.dependencies import get_current_principal
from app.auth.permissions import permissions_for_role
from app.connectors.runtime import (
    GithubConnectorCustodyUnavailableError,
    load_github_connector_runtime,
)
from app.connectors.vault import CredentialContext, build_credential_cipher
from app.db.models import (
    ConnectorCredentialRecord,
    ConnectorRecord,
    GithubWebhookDeliveryRecord,
    OrganizationRecord,
)
from app.domain.auth import Principal, Role
from app.domain.connectors import ConnectorProvider
from app.main import app
from app.services.github_webhooks import (
    GithubWebhookConnectorChangedError,
    GithubWebhookService,
    verify_github_signature,
)
from tests.conftest import TEST_USER_ID, TestingSessionLocal

WEBHOOK_SECRET = "github-webhook-secret-with-at-least-32-bytes"
SECRET_SENTINEL = "raw-provider-secret-that-must-never-be-stored"
REPOSITORY = "pageragent/checkout"
INSTALLATION_ID = 2002
BEFORE_SHA = "1" * 40
AFTER_SHA = "2" * 40
COMMIT_SHA = "3" * 40


def create_github_connector(
    session: Session,
    *,
    organization_id: UUID = DEFAULT_ORGANIZATION_ID,
    enabled: bool = True,
    status: str = "configured",
    configuration: dict[str, object] | None = None,
    with_credentials: bool = True,
    webhook_secret: str = WEBHOOK_SECRET,
) -> ConnectorRecord:
    connector_id = uuid4()
    connector = ConnectorRecord(
        id=connector_id,
        organization_id=organization_id,
        name=f"GitHub {connector_id}",
        provider="github",
        configuration=configuration
        or {
            "service": "checkout-api",
            "repository": REPOSITORY,
            "app_id": 1001,
            "installation_id": INSTALLATION_ID,
            "api_url": "https://api.github.com",
        },
        enabled=enabled,
        status=status,
        version=1,
    )
    if with_credentials:
        sealed = build_credential_cipher().seal(
            {
                "private_key": "-----BEGIN PRIVATE KEY-----\nkey\n-----END PRIVATE KEY-----",
                "webhook_secret": webhook_secret,
            },
            CredentialContext(
                organization_id=organization_id,
                connector_id=connector_id,
                provider=ConnectorProvider.GITHUB,
                credential_version=1,
            ),
        )
        connector.credential = ConnectorCredentialRecord(
            connector_id=connector_id,
            credential_version=1,
            ciphertext=sealed.ciphertext,
            ciphertext_nonce=sealed.ciphertext_nonce,
            wrapped_data_key=sealed.wrapped_data_key,
            wrapped_key_nonce=sealed.wrapped_key_nonce,
            key_version=sealed.key_version,
            credential_field_names=list(sealed.credential_field_names),
        )
    session.add(connector)
    session.commit()
    return connector


def push_payload() -> dict[str, object]:
    commit = {
        "id": COMMIT_SHA,
        "message": "Deploy Unicode checkout fix 🚀",
        "timestamp": "2026-07-16T12:34:56Z",
        "author": {
            "name": "PagerAgent Bot",
            "username": "pageragent-bot",
            "email": "private@example.com",
        },
        "added": ["src/new.py"],
        "removed": ["src/old.py"],
        "modified": ["src/checkout.py"],
        "url": "https://api.github.com/secret-provider-url",
        "patch": SECRET_SENTINEL,
    }
    return {
        "repository": {
            "full_name": REPOSITORY,
            "html_url": "https://github.com/pageragent/checkout",
        },
        "installation": {"id": INSTALLATION_ID},
        "ref": "refs/heads/main",
        "before": BEFORE_SHA,
        "after": AFTER_SHA,
        "created": False,
        "deleted": False,
        "forced": False,
        "commits": [commit],
        "head_commit": commit,
        "compare": "https://github.com/provider/url",
        "secret": SECRET_SENTINEL,
    }


def pull_request_payload() -> dict[str, object]:
    return {
        "action": "opened",
        "repository": {"full_name": REPOSITORY},
        "installation": {"id": INSTALLATION_ID},
        "number": 42,
        "pull_request": {
            "number": 42,
            "title": "Ship the checkout fix",
            "body": SECRET_SENTINEL,
            "state": "open",
            "draft": False,
            "merged": False,
            "head": {"sha": AFTER_SHA},
            "base": {"sha": BEFORE_SHA},
            "created_at": "2026-07-16T12:00:00Z",
            "updated_at": "2026-07-16T12:30:00Z",
            "closed_at": None,
            "merged_at": None,
            "html_url": "https://github.com/provider/url",
        },
    }


def deployment_payload(*, with_status: bool) -> dict[str, object]:
    payload: dict[str, object] = {
        "action": "created",
        "repository": {"full_name": REPOSITORY},
        "installation": {"id": INSTALLATION_ID},
        "deployment": {
            "id": 9001,
            "sha": AFTER_SHA,
            "ref": "main",
            "environment": "production",
            "created_at": "2026-07-16T12:40:00Z",
            "updated_at": "2026-07-16T12:40:00Z",
            "statuses_url": "https://api.github.com/provider/url",
        },
    }
    if with_status:
        payload["deployment_status"] = {
            "id": 9002,
            "state": "success",
            "description": SECRET_SENTINEL,
            "created_at": "2026-07-16T12:41:00Z",
            "updated_at": "2026-07-16T12:42:00Z",
            "target_url": "https://provider.example/secret",
        }
    return payload


def release_payload() -> dict[str, object]:
    return {
        "action": "published",
        "repository": {"full_name": REPOSITORY},
        "installation": {"id": INSTALLATION_ID},
        "release": {
            "id": 7001,
            "tag_name": "v1.2.3",
            "name": "Checkout v1.2.3",
            "body": SECRET_SENTINEL,
            "draft": False,
            "prerelease": False,
            "created_at": "2026-07-16T12:50:00Z",
            "published_at": "2026-07-16T12:55:00Z",
            "assets": [{"url": "https://api.github.com/provider/url"}],
            "html_url": "https://github.com/provider/url",
        },
    }


def raw_payload(payload: dict[str, object]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def signature(body: bytes, *, secret: str = WEBHOOK_SECRET) -> str:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def webhook_headers(
    body: bytes,
    *,
    event: str = "push",
    delivery_id: str | None = None,
    secret: str = WEBHOOK_SECRET,
) -> dict[str, str]:
    return {
        "X-GitHub-Delivery": delivery_id or str(uuid4()),
        "X-GitHub-Event": event,
        "X-Hub-Signature-256": signature(body, secret=secret),
        "Content-Type": "application/json",
    }


def post_webhook(
    connector_id: UUID,
    payload: dict[str, object],
    *,
    event: str = "push",
    delivery_id: str | None = None,
    secret: str = WEBHOOK_SECRET,
) -> object:
    body = raw_payload(payload)
    return TestClient(app).post(
        f"/api/v1/webhooks/github/{connector_id}",
        content=body,
        headers=webhook_headers(
            body,
            event=event,
            delivery_id=delivery_id,
            secret=secret,
        ),
    )


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


def test_signature_verifier_matches_githubs_official_sha256_vector() -> None:
    secret = "It's a Secret to Everybody"
    body = b"Hello, World!"
    expected = (
        "sha256=757107ea0eb2509fc211221cce984b8a3"
        "7570b6d7586c22c46f4379c8b043e17"
    )

    assert verify_github_signature(secret, body, expected) is True
    assert verify_github_signature(secret, body + b"!", expected) is False
    assert verify_github_signature(secret, body, expected.replace("sha256", "sha1")) is False


def test_unicode_raw_bytes_are_signed_before_json_parsing_and_only_normalized_evidence_is_stored(
    db_session: Session,
) -> None:
    connector = create_github_connector(db_session)
    payload = push_payload()
    body = raw_payload(payload)
    headers = webhook_headers(body)

    response = TestClient(app).post(
        f"/api/v1/webhooks/github/{connector.id}",
        content=body,
        headers=headers,
    )

    assert response.status_code == 202
    assert response.json()["duplicate"] is False
    delivery = db_session.scalar(select(GithubWebhookDeliveryRecord))
    assert delivery is not None
    normalized = json.dumps(delivery.normalized_payload, ensure_ascii=False)
    assert "🚀" in normalized
    assert delivery.body_sha256 == hashlib.sha256(body).hexdigest()
    assert SECRET_SENTINEL not in normalized
    assert "private@example.com" not in normalized
    assert "https://" not in normalized
    assert "patch" not in normalized
    assert delivery.normalized_payload["commits"][0]["files"] == {
        "added": ["src/new.py"],
        "removed": ["src/old.py"],
        "modified": ["src/checkout.py"],
    }
    assert delivery.normalized_payload["commits"][0]["file_stats"] == {
        "added": 1,
        "removed": 1,
        "modified": 1,
    }


def test_multiline_provider_titles_are_reduced_to_one_control_safe_line(
    db_session: Session,
) -> None:
    connector = create_github_connector(db_session)
    payload = pull_request_payload()
    pull_request = payload["pull_request"]
    assert isinstance(pull_request, dict)
    pull_request["title"] = "Ship checkout safely\nSENTINEL_UNTRUSTED_SECOND_LINE\x1b[31m"

    response = post_webhook(connector.id, payload, event="pull_request")

    assert response.status_code == 202
    delivery = db_session.scalar(select(GithubWebhookDeliveryRecord))
    assert delivery is not None
    assert delivery.normalized_payload["title"] == "Ship checkout safely"
    assert "SENTINEL" not in json.dumps(delivery.normalized_payload)


@pytest.mark.parametrize(
    "filename",
    ["../secret", "/absolute/path", r"src\\escape.py", "src/\x1b[31m.py"],
)
def test_unsafe_provider_filenames_are_rejected_before_persistence(
    db_session: Session,
    filename: str,
) -> None:
    connector = create_github_connector(db_session)
    payload = push_payload()
    commits = payload["commits"]
    assert isinstance(commits, list)
    commit = commits[0]
    assert isinstance(commit, dict)
    commit["modified"] = [filename]

    response = post_webhook(connector.id, payload)

    assert response.status_code == 422
    assert db_session.scalar(select(func.count()).select_from(GithubWebhookDeliveryRecord)) == 0


@pytest.mark.parametrize(
    ("event", "payload", "expected"),
    [
        ("pull_request", pull_request_payload(), {"number": 42, "head_sha": AFTER_SHA}),
        ("deployment", deployment_payload(with_status=False), {"deployment_id": 9001}),
        (
            "deployment_status",
            deployment_payload(with_status=True),
            {"deployment_id": 9001, "status": "success"},
        ),
        ("release", release_payload(), {"release_id": 7001, "tag_name": "v1.2.3"}),
    ],
)
def test_supported_events_are_allowlisted_and_normalized_without_raw_provider_fields(
    db_session: Session,
    event: str,
    payload: dict[str, object],
    expected: dict[str, object],
) -> None:
    connector = create_github_connector(db_session)

    response = post_webhook(connector.id, payload, event=event)

    assert response.status_code == 202
    delivery = db_session.scalar(select(GithubWebhookDeliveryRecord))
    assert delivery is not None
    assert delivery.event_type == event
    assert delivery.action == payload["action"]
    for field, value in expected.items():
        assert delivery.normalized_payload[field] == value
    serialized = json.dumps(delivery.normalized_payload)
    assert SECRET_SENTINEL not in serialized
    assert "https://" not in serialized
    assert "body" not in serialized
    assert "assets" not in serialized


@pytest.mark.parametrize(
    "header_mutation",
    [
        {"X-GitHub-Delivery": ""},
        {"X-GitHub-Delivery": "not-a-guid"},
        {"X-GitHub-Event": "ping"},
        {"X-GitHub-Event": "Push"},
        {"X-Hub-Signature-256": ""},
        {"X-Hub-Signature-256": "sha1=" + ("a" * 40)},
        {"X-Hub-Signature-256": "sha256=" + ("A" * 64)},
    ],
)
def test_missing_legacy_and_malformed_headers_are_rejected_before_persistence(
    db_session: Session,
    header_mutation: dict[str, str],
) -> None:
    connector = create_github_connector(db_session)
    body = raw_payload(push_payload())
    headers = webhook_headers(body)
    headers.update(header_mutation)

    response = TestClient(app).post(
        f"/api/v1/webhooks/github/{connector.id}",
        content=body,
        headers=headers,
    )

    assert response.status_code == 400
    assert db_session.scalar(select(func.count()).select_from(GithubWebhookDeliveryRecord)) == 0


def test_invalid_signature_and_wrong_secret_persist_nothing(db_session: Session) -> None:
    connector = create_github_connector(db_session)

    response = post_webhook(
        connector.id,
        push_payload(),
        secret="wrong-webhook-secret-with-at-least-32-bytes",
    )

    assert response.status_code == 401
    assert WEBHOOK_SECRET not in response.text
    assert db_session.scalar(select(func.count()).select_from(GithubWebhookDeliveryRecord)) == 0


def test_signature_is_checked_before_malformed_json_is_parsed(db_session: Session) -> None:
    connector = create_github_connector(db_session)
    body = b'{"not":"complete"'
    bad_headers = webhook_headers(body)
    bad_headers["X-Hub-Signature-256"] = "sha256=" + ("0" * 64)

    bad_signature = TestClient(app).post(
        f"/api/v1/webhooks/github/{connector.id}",
        content=body,
        headers=bad_headers,
    )
    valid_signature = TestClient(app).post(
        f"/api/v1/webhooks/github/{connector.id}",
        content=body,
        headers=webhook_headers(body),
    )

    assert bad_signature.status_code == 401
    assert valid_signature.status_code == 422
    assert db_session.scalar(select(func.count()).select_from(GithubWebhookDeliveryRecord)) == 0


def test_oversized_json_integer_is_sanitized_instead_of_raising(db_session: Session) -> None:
    connector = create_github_connector(db_session)
    body = b'{"integer":' + (b"1" * 5_000) + b"}"

    response = TestClient(app).post(
        f"/api/v1/webhooks/github/{connector.id}",
        content=body,
        headers=webhook_headers(body),
    )

    assert response.status_code == 422
    assert db_session.scalar(select(func.count()).select_from(GithubWebhookDeliveryRecord)) == 0


@pytest.mark.parametrize("binding", ["repository", "installation"])
def test_repository_and_installation_are_strictly_bound_to_connector_configuration(
    db_session: Session,
    binding: str,
) -> None:
    connector = create_github_connector(db_session)
    payload = push_payload()
    if binding == "repository":
        payload["repository"] = {"full_name": "pageragent/other"}
    else:
        payload["installation"] = {"id": INSTALLATION_ID + 1}

    response = post_webhook(connector.id, payload)

    assert response.status_code == 422
    assert REPOSITORY not in response.text
    assert db_session.scalar(select(func.count()).select_from(GithubWebhookDeliveryRecord)) == 0


def test_mixed_case_provider_repository_matches_canonical_connector_binding(
    db_session: Session,
) -> None:
    connector = create_github_connector(
        db_session,
        configuration={
            "service": "checkout-api",
            "repository": "PagerAgent/Checkout",
            "app_id": 1001,
            "installation_id": INSTALLATION_ID,
            "api_url": "https://api.github.com",
        },
    )
    payload = push_payload()
    payload["repository"] = {"full_name": "PAGERAGENT/CHECKOUT"}

    response = post_webhook(connector.id, payload)

    assert response.status_code == 202
    delivery = db_session.scalar(select(GithubWebhookDeliveryRecord))
    assert delivery is not None
    assert delivery.repository == REPOSITORY


def test_stale_runtime_cannot_ingest_after_connector_revocation(db_session: Session) -> None:
    connector = create_github_connector(db_session)
    runtime = load_github_connector_runtime(db_session, connector.id)
    db_session.rollback()
    with TestingSessionLocal() as racing_session:
        current = racing_session.get(ConnectorRecord, connector.id)
        assert current is not None
        current.enabled = False
        current.status = "disabled"
        current.version += 1
        racing_session.commit()
    body = raw_payload(push_payload())

    with pytest.raises(GithubWebhookConnectorChangedError):
        GithubWebhookService(db_session, runtime).ingest(
            delivery_id=str(uuid4()),
            event_type="push",
            signature=signature(body),
            raw_body=body,
        )

    assert db_session.scalar(select(func.count()).select_from(GithubWebhookDeliveryRecord)) == 0


def test_delivery_tenant_and_connector_binding_is_enforced_at_rest(
    db_session: Session,
) -> None:
    connector = create_github_connector(db_session)
    other_organization_id = uuid4()
    db_session.add(
        OrganizationRecord(
            id=other_organization_id,
            slug="mismatched-tenant",
            name="Mismatched Tenant",
        )
    )
    db_session.commit()
    db_session.add(
        GithubWebhookDeliveryRecord(
            organization_id=other_organization_id,
            connector_id=connector.id,
            delivery_id=str(uuid4()),
            event_type="push",
            action=None,
            repository=REPOSITORY,
            installation_id=INSTALLATION_ID,
            connector_version=1,
            credential_version=1,
            body_sha256="a" * 64,
            normalized_payload={"service": "checkout-api"},
        )
    )

    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_unsupported_actions_are_rejected_without_persistence(db_session: Session) -> None:
    connector = create_github_connector(db_session)
    payload = pull_request_payload()
    payload["action"] = "submitted"

    response = post_webhook(connector.id, payload, event="pull_request")

    assert response.status_code == 422
    assert db_session.scalar(select(func.count()).select_from(GithubWebhookDeliveryRecord)) == 0


def test_same_delivery_replay_is_idempotent_and_conflicting_replay_is_rejected(
    db_session: Session,
) -> None:
    connector = create_github_connector(db_session)
    delivery_id = str(uuid4())
    payload = push_payload()

    first = post_webhook(connector.id, payload, delivery_id=delivery_id)
    replay = post_webhook(connector.id, payload, delivery_id=delivery_id)
    changed = copy.deepcopy(payload)
    changed["forced"] = True
    conflict = post_webhook(connector.id, changed, delivery_id=delivery_id)
    event_conflict = post_webhook(
        connector.id,
        release_payload(),
        event="release",
        delivery_id=delivery_id,
    )

    assert first.status_code == 202
    assert first.json()["duplicate"] is False
    assert replay.status_code == 202
    assert replay.json()["duplicate"] is True
    assert conflict.status_code == 409
    assert event_conflict.status_code == 409
    assert db_session.scalar(select(func.count()).select_from(GithubWebhookDeliveryRecord)) == 1


def test_exact_retry_remains_idempotent_across_normalizer_contract_changes(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connector = create_github_connector(db_session)
    delivery_id = str(uuid4())
    payload = push_payload()
    first = post_webhook(connector.id, payload, delivery_id=delivery_id)
    normalizer_called = False

    def retired_normalizer(*_args: object, **_kwargs: object) -> dict[str, object]:
        nonlocal normalizer_called
        normalizer_called = True
        raise AssertionError("an exact durable retry must not be normalized again")

    monkeypatch.setattr(GithubWebhookService, "_normalize", retired_normalizer)

    replay = post_webhook(connector.id, payload, delivery_id=delivery_id)

    assert first.status_code == 202
    assert replay.status_code == 202
    assert replay.json()["duplicate"] is True
    assert normalizer_called is False
    assert db_session.scalar(select(func.count()).select_from(GithubWebhookDeliveryRecord)) == 1


def test_body_cap_is_enforced_from_content_length_before_connector_lookup() -> None:
    body = b"{}"
    headers = webhook_headers(body)
    headers["Content-Length"] = str(MAX_GITHUB_WEBHOOK_BODY_BYTES + 1)

    response = TestClient(app).post(
        f"/api/v1/webhooks/github/{uuid4()}",
        content=body,
        headers=headers,
    )

    assert response.status_code == 413


def test_malformed_content_length_is_rejected_before_connector_lookup() -> None:
    body = b"{}"
    headers = webhook_headers(body)
    headers["Content-Length"] = "not-a-number"

    response = TestClient(app).post(
        f"/api/v1/webhooks/github/{uuid4()}",
        content=body,
        headers=headers,
    )

    assert response.status_code == 400


def test_streaming_body_cap_is_enforced_without_content_length() -> None:
    chunks = iter(
        [
            {
                "type": "http.request",
                "body": b"a" * MAX_GITHUB_WEBHOOK_BODY_BYTES,
                "more_body": True,
            },
            {"type": "http.request", "body": b"b", "more_body": False},
        ]
    )

    async def receive() -> dict[str, object]:
        return next(chunks)

    request = Request(
        {"type": "http", "method": "POST", "path": "/", "headers": []},
        receive,
    )

    with pytest.raises(HTTPException) as raised:
        asyncio.run(read_limited_request_body(request))
    assert raised.value.status_code == 413


def test_endpoint_caps_chunked_body_before_loading_or_decrypting_connector(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loader_called = False

    def fail_if_loaded(*_args: object, **_kwargs: object) -> object:
        nonlocal loader_called
        loader_called = True
        raise AssertionError("connector runtime must not load before the body cap")

    monkeypatch.setattr(
        "app.api.routes.github_webhooks.load_github_connector_runtime",
        fail_if_loaded,
    )
    chunks = iter(
        [
            {
                "type": "http.request",
                "body": b"a" * MAX_GITHUB_WEBHOOK_BODY_BYTES,
                "more_body": True,
            },
            {"type": "http.request", "body": b"b", "more_body": False},
        ]
    )

    async def receive() -> dict[str, object]:
        return next(chunks)

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/",
            "headers": [
                (b"x-github-delivery", str(uuid4()).encode()),
                (b"x-github-event", b"push"),
                (b"x-hub-signature-256", b"sha256=" + (b"0" * 64)),
            ],
        },
        receive,
    )

    with pytest.raises(HTTPException) as raised:
        asyncio.run(
            ingest_github_webhook(
                uuid4(),
                request,
                session=db_session,
            )
        )
    assert raised.value.status_code == 413
    assert loader_called is False


def test_unknown_disabled_invalid_legacy_and_credentialless_connectors_share_a_404(
    db_session: Session,
) -> None:
    disabled = create_github_connector(db_session, enabled=False, status="disabled")
    invalid = create_github_connector(db_session, enabled=True, status="invalid")
    legacy = create_github_connector(
        db_session,
        configuration={
            "repository": REPOSITORY,
            "app_id": 1001,
            "installation_id": INSTALLATION_ID,
        },
    )
    credentialless = create_github_connector(db_session, with_credentials=False)
    body = raw_payload(push_payload())
    expected = None

    for connector_id in (
        uuid4(),
        disabled.id,
        invalid.id,
        legacy.id,
        credentialless.id,
    ):
        response = TestClient(app).post(
            f"/api/v1/webhooks/github/{connector_id}",
            content=body,
            headers=webhook_headers(body),
        )
        assert response.status_code == 404
        expected = expected or response.json()
        assert response.json() == expected


def test_transient_credential_custody_outage_returns_retryable_503(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(*_args: object, **_kwargs: object) -> object:
        raise GithubConnectorCustodyUnavailableError

    monkeypatch.setattr(
        "app.api.routes.github_webhooks.load_github_connector_runtime",
        unavailable,
    )

    response = post_webhook(uuid4(), push_payload())

    assert db_session is not None
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "5"
    assert response.json() == {
        "detail": "GitHub webhook credential custody is temporarily unavailable"
    }


def test_tampered_credential_envelope_is_indistinguishable_from_unavailable(
    db_session: Session,
) -> None:
    connector = create_github_connector(db_session)
    credential = db_session.get(ConnectorCredentialRecord, connector.id)
    assert credential is not None
    credential.ciphertext = bytes([credential.ciphertext[0] ^ 1]) + credential.ciphertext[1:]
    db_session.commit()

    response = post_webhook(connector.id, push_payload())

    assert response.status_code == 404
    assert "cipher" not in response.text.lower()


def test_delivery_listing_is_tenant_scoped_bounded_and_requires_connector_read_permission(
    db_session: Session,
) -> None:
    other_organization_id = UUID("00000000-0000-0000-0000-000000000002")
    db_session.add(
        OrganizationRecord(
            id=other_organization_id,
            slug="other-operations",
            name="Other Operations",
        )
    )
    db_session.commit()
    default_connector = create_github_connector(db_session)
    other_connector = create_github_connector(
        db_session,
        organization_id=other_organization_id,
    )
    assert post_webhook(default_connector.id, push_payload()).status_code == 202
    assert post_webhook(other_connector.id, push_payload()).status_code == 202
    client = TestClient(app)

    default_listing = client.get(
        f"/api/v1/connectors/{default_connector.id}/github-deliveries"
    )
    assert default_listing.status_code == 200
    assert len(default_listing.json()) == 1
    assert default_listing.json()[0]["connector_id"] == str(default_connector.id)
    assert SECRET_SENTINEL not in default_listing.text

    app.dependency_overrides[get_current_principal] = lambda: principal(
        Role.INCIDENT_COMMANDER,
        other_organization_id,
    )
    assert (
        client.get(
            f"/api/v1/connectors/{default_connector.id}/github-deliveries"
        ).status_code
        == 404
    )
    other_listing = client.get(
        f"/api/v1/connectors/{other_connector.id}/github-deliveries"
    )
    assert other_listing.status_code == 200
    assert len(other_listing.json()) == 1

    app.dependency_overrides[get_current_principal] = lambda: principal(
        Role.RESPONDER,
        other_organization_id,
    )
    denied = client.get(f"/api/v1/connectors/{other_connector.id}/github-deliveries")
    assert denied.status_code == 403


def test_delivery_listing_fails_closed_on_nested_inbox_corruption(
    db_session: Session,
) -> None:
    connector = create_github_connector(db_session)
    assert post_webhook(connector.id, push_payload()).status_code == 202
    delivery = db_session.scalar(select(GithubWebhookDeliveryRecord))
    assert delivery is not None
    corrupted = copy.deepcopy(delivery.normalized_payload)
    corrupted["commits"][0]["files"]["modified"] = ["../escape.py"]
    delivery.normalized_payload = corrupted
    db_session.commit()

    response = TestClient(app).get(
        f"/api/v1/connectors/{connector.id}/github-deliveries"
    )

    assert response.status_code == 500
    assert response.json()["detail"] == "GitHub delivery ledger integrity check failed"
    assert "escape.py" not in response.text
