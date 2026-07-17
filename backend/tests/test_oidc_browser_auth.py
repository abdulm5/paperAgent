from base64 import urlsafe_b64encode
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from urllib.parse import parse_qs, parse_qsl, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api.routes import auth as auth_routes
from app.auth.oidc import (
    InvalidOidcTokenError,
    OidcIdentity,
    OidcTokenClient,
    OidcUpstreamError,
    OidcVerifier,
    digest_oidc_secret,
)
from app.core.config import Settings, settings
from app.db.models import (
    AuthSessionRecord,
    OidcLoginTransactionRecord,
    OrganizationMembershipRecord,
    OrganizationRecord,
    UserRecord,
)
from app.main import app
from tests.conftest import TestingSessionLocal


def _base64url_integer(value: int) -> str:
    length = (value.bit_length() + 7) // 8
    return urlsafe_b64encode(value.to_bytes(length, "big")).rstrip(b"=").decode("ascii")


def _oidc_config(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "PAGERAGENT_AUTH_MODE": "oidc",
        "PAGERAGENT_OIDC_ISSUER": "https://identity.example.com",
        "PAGERAGENT_OIDC_AUDIENCE": "pageragent-web",
        "PAGERAGENT_OIDC_JWKS_URL": "https://identity.example.com/jwks",
        "PAGERAGENT_OIDC_CLIENT_ID": "pageragent-web",
        "PAGERAGENT_OIDC_CLIENT_SECRET": "test-client-secret",
        "PAGERAGENT_OIDC_AUTHORIZATION_URL": "https://identity.example.com/authorize",
        "PAGERAGENT_OIDC_TOKEN_URL": "https://identity.example.com/token",
        "PAGERAGENT_OIDC_REDIRECT_URI": (
            "https://pageragent.test/api/v1/auth/oidc/callback"
        ),
        "PAGERAGENT_OIDC_FRONTEND_URL": "https://pageragent.test/",
        "PAGERAGENT_OIDC_DEFAULT_ORGANIZATION_SLUG": "pageragent-labs",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _configure_global_oidc(monkeypatch: pytest.MonkeyPatch) -> None:
    config = _oidc_config()
    for name in (
        "auth_mode",
        "oidc_issuer",
        "oidc_audience",
        "oidc_jwks_url",
        "oidc_client_id",
        "oidc_client_secret",
        "oidc_authorization_url",
        "oidc_token_url",
        "oidc_redirect_uri",
        "oidc_frontend_url",
        "oidc_default_organization_slug",
        "oidc_transaction_key",
        "oidc_login_cookie_name",
        "oidc_login_ttl_seconds",
        "oidc_http_timeout_seconds",
        "oidc_max_response_bytes",
    ):
        monkeypatch.setattr(settings, name, getattr(config, name))


def _provision_oidc_identity() -> OidcIdentity:
    identity = OidcIdentity(
        issuer="https://identity.example.com",
        subject="stable-subject-123",
        email="engineer@example.com",
        display_name="Elite Engineer",
    )
    with TestingSessionLocal() as session:
        organization_id = session.scalar(
            select(OrganizationRecord.id).where(
                OrganizationRecord.slug == "pageragent-labs"
            )
        )
        assert organization_id is not None
        user = UserRecord(
            issuer=identity.issuer,
            subject=identity.subject,
            email=identity.email,
            display_name=identity.display_name,
            is_active=True,
        )
        session.add(user)
        session.flush()
        session.add(
            OrganizationMembershipRecord(
                organization_id=organization_id,
                user_id=user.id,
                role="admin",
                is_active=True,
            )
        )
        session.commit()
    return identity


def test_browser_authorization_code_flow_uses_pkce_and_consumes_state_before_exchange(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_global_oidc(monkeypatch)
    identity = _provision_oidc_identity()
    client = TestClient(app, base_url="https://pageragent.test")

    login = client.get("/api/v1/auth/oidc/login", follow_redirects=False)
    assert login.status_code == 303, login.text
    location = urlsplit(login.headers["location"])
    assert f"{location.scheme}://{location.netloc}{location.path}" == (
        "https://identity.example.com/authorize"
    )
    query_pairs = parse_qsl(location.query, keep_blank_values=True)
    assert len(query_pairs) == len(dict(query_pairs))
    query = parse_qs(location.query)
    assert query["response_type"] == ["code"]
    assert query["scope"] == ["openid email profile"]
    assert query["client_id"] == ["pageragent-web"]
    assert query["redirect_uri"] == [
        "https://pageragent.test/api/v1/auth/oidc/callback"
    ]
    assert query["code_challenge_method"] == ["S256"]
    assert 43 <= len(query["code_challenge"][0]) <= 128
    assert "code_verifier" not in query
    set_cookie = login.headers["set-cookie"]
    assert "HttpOnly" in set_cookie
    assert "Secure" in set_cookie
    assert "SameSite=lax" in set_cookie
    assert "Path=/" in set_cookie

    with TestingSessionLocal() as session:
        transaction = session.scalar(select(OidcLoginTransactionRecord))
        assert transaction is not None
        assert transaction.state_hash == digest_oidc_secret(query["state"][0])
        assert transaction.nonce_hash == digest_oidc_secret(query["nonce"][0])
        assert query["state"][0].encode() not in transaction.verifier_ciphertext
        assert query["nonce"][0].encode() not in transaction.verifier_ciphertext
        transaction_id = transaction.id

    exchanges: list[tuple[str, str]] = []

    class FakeTokenClient:
        def exchange(self, code: str, code_verifier: str) -> str:
            with TestingSessionLocal() as verification_session:
                consumed = verification_session.get(
                    OidcLoginTransactionRecord,
                    transaction_id,
                )
                assert consumed is not None
                assert consumed.consumed_at is not None
            exchanges.append((code, code_verifier))
            return "opaque-id-token"

    class FakeVerifier:
        def verify(self, encoded: str, *, expected_nonce_hash: str) -> OidcIdentity:
            assert encoded == "opaque-id-token"
            assert expected_nonce_hash == digest_oidc_secret(query["nonce"][0])
            return identity

    monkeypatch.setattr(auth_routes, "_build_oidc_token_client", FakeTokenClient)
    monkeypatch.setattr(auth_routes, "_build_oidc_verifier", FakeVerifier)

    callback = client.get(
        "/api/v1/auth/oidc/callback",
        params={"code": "one-time-code", "state": query["state"][0]},
        follow_redirects=False,
    )
    assert callback.status_code == 303, callback.text
    assert callback.headers["location"] == "https://pageragent.test/"
    assert callback.headers["cache-control"] == "no-store"
    assert callback.headers["referrer-policy"] == "no-referrer"
    assert "opaque-id-token" not in callback.headers["location"]
    assert len(exchanges) == 1
    assert exchanges[0][0] == "one-time-code"
    assert 43 <= len(exchanges[0][1]) <= 128
    expected_challenge = (
        urlsafe_b64encode(sha256(exchanges[0][1].encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    assert query["code_challenge"] == [expected_challenge]

    with TestingSessionLocal() as session:
        transaction = session.get(OidcLoginTransactionRecord, transaction_id)
        assert transaction is not None and transaction.consumed_at is not None
        auth_session = session.scalar(select(AuthSessionRecord))
        assert auth_session is not None
        assert auth_session.auth_method == "oidc"

    replay = client.get(
        "/api/v1/auth/oidc/callback",
        params={"code": "another-code", "state": query["state"][0]},
        follow_redirects=False,
    )
    assert replay.status_code == 400
    assert replay.json() == {"detail": "OIDC sign-in failed"}
    assert len(exchanges) == 1


def test_callback_rejects_duplicate_parameters_without_contacting_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_global_oidc(monkeypatch)
    client = TestClient(app, base_url="https://pageragent.test")
    login = client.get("/api/v1/auth/oidc/login", follow_redirects=False)
    state = parse_qs(urlsplit(login.headers["location"]).query)["state"][0]

    class UnexpectedTokenClient:
        def exchange(self, _code: str, _code_verifier: str) -> str:
            raise AssertionError("duplicate callback must not reach the provider")

    monkeypatch.setattr(auth_routes, "_build_oidc_token_client", UnexpectedTokenClient)
    response = client.get(
        f"/api/v1/auth/oidc/callback?code=one&code=two&state={state}",
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert response.json() == {"detail": "OIDC sign-in failed"}
    assert settings.oidc_login_cookie_name in response.headers["set-cookie"]


def test_callback_boundedly_ignores_extension_parameters_but_rejects_oversized_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_global_oidc(monkeypatch)
    client = TestClient(app, base_url="https://pageragent.test")
    login = client.get("/api/v1/auth/oidc/login", follow_redirects=False)
    state = parse_qs(urlsplit(login.headers["location"]).query)["state"][0]

    class UnexpectedTokenClient:
        def exchange(self, _code: str, _code_verifier: str) -> str:
            raise AssertionError("an invalid extension must not reach the provider")

    monkeypatch.setattr(auth_routes, "_build_oidc_token_client", UnexpectedTokenClient)
    marker = "provider-extension-must-not-be-reflected"
    response = client.get(
        "/api/v1/auth/oidc/callback",
        params={
            "code": "one-time-code",
            "state": state,
            "provider_extension": marker + "x" * 1_024,
        },
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert response.json() == {"detail": "OIDC sign-in failed"}
    assert marker not in response.text
    with TestingSessionLocal() as session:
        transaction = session.scalar(select(OidcLoginTransactionRecord))
        assert transaction is not None
        assert transaction.consumed_at is None


def test_provider_error_consumes_the_bound_transaction_before_returning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_global_oidc(monkeypatch)
    client = TestClient(app, base_url="https://pageragent.test")
    login = client.get("/api/v1/auth/oidc/login", follow_redirects=False)
    state = parse_qs(urlsplit(login.headers["location"]).query)["state"][0]

    class UnexpectedTokenClient:
        def exchange(self, _code: str, _code_verifier: str) -> str:
            raise AssertionError("an authorization error must not reach the token endpoint")

    monkeypatch.setattr(auth_routes, "_build_oidc_token_client", UnexpectedTokenClient)
    response = client.get(
        "/api/v1/auth/oidc/callback",
        params={
            "error": "access_denied",
            "state": state,
            "session_state": "opaque-provider-session",
            "error_uri": "https://identity.example.com/errors/access-denied",
            "provider_extension": "ignored",
        },
        follow_redirects=False,
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "OIDC sign-in failed"}
    assert "opaque-provider-session" not in response.text
    assert "identity.example.com" not in response.text
    with TestingSessionLocal() as session:
        transaction = session.scalar(select(OidcLoginTransactionRecord))
        assert transaction is not None
        assert transaction.consumed_at is not None


def test_login_caps_pending_transactions_and_prunes_consumed_and_expired_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_global_oidc(monkeypatch)
    monkeypatch.setattr(
        auth_routes,
        "OIDC_MAX_PENDING_LOGIN_TRANSACTIONS_PER_ORGANIZATION",
        2,
    )
    client = TestClient(app, base_url="https://pageragent.test")

    first = client.get("/api/v1/auth/oidc/login", follow_redirects=False)
    second = client.get("/api/v1/auth/oidc/login", follow_redirects=False)
    capped = client.get("/api/v1/auth/oidc/login", follow_redirects=False)

    assert first.status_code == 303
    assert second.status_code == 303
    assert capped.status_code == 429
    assert capped.json() == {"detail": "OIDC sign-in temporarily unavailable"}
    assert capped.headers["cache-control"] == "no-store"
    assert capped.headers["pragma"] == "no-cache"
    assert capped.headers["retry-after"] == str(settings.oidc_login_ttl_seconds)

    now = datetime.now(UTC).replace(microsecond=0)
    with TestingSessionLocal() as session:
        transactions = list(session.scalars(select(OidcLoginTransactionRecord)))
        assert len(transactions) == 2
        transactions[0].consumed_at = now
        transactions[1].expires_at = now - timedelta(seconds=1)
        session.commit()

    resumed = client.get("/api/v1/auth/oidc/login", follow_redirects=False)
    assert resumed.status_code == 303
    with TestingSessionLocal() as session:
        remaining = list(session.scalars(select(OidcLoginTransactionRecord)))
        assert len(remaining) == 1
        assert remaining[0].consumed_at is None
        assert remaining[0].expires_at > now.replace(tzinfo=None)


def test_token_endpoint_client_uses_fixed_endpoint_basic_auth_and_pkce() -> None:
    config = _oidc_config()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert str(request.url) == "https://identity.example.com/token"
        assert request.headers["authorization"].startswith("Basic ")
        form = parse_qs(request.content.decode("ascii"))
        assert form == {
            "grant_type": ["authorization_code"],
            "code": ["authorization-code"],
            "redirect_uri": ["https://pageragent.test/api/v1/auth/oidc/callback"],
            "code_verifier": ["v" * 64],
        }
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            json={"id_token": "signed.id.token"},
        )

    client = OidcTokenClient(config, transport=httpx.MockTransport(handler))
    assert client.exchange("authorization-code", "v" * 64) == "signed.id.token"


def test_token_endpoint_client_rejects_redirects_and_oversized_responses() -> None:
    config = _oidc_config(PAGERAGENT_OIDC_MAX_RESPONSE_BYTES=1_024)

    redirecting = OidcTokenClient(
        config,
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                302,
                headers={"Location": "https://attacker.invalid/token"},
            )
        ),
    )
    with pytest.raises(OidcUpstreamError):
        redirecting.exchange("code", "v" * 64)

    oversized = OidcTokenClient(
        config,
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                headers={"Content-Type": "application/json"},
                content=b"{" + b"x" * 1_024 + b"}",
            )
        ),
    )
    with pytest.raises(OidcUpstreamError):
        oversized.exchange("code", "v" * 64)


def test_id_token_verifier_enforces_signature_nonce_audience_and_verified_email() -> None:
    config = _oidc_config()
    private_key = rsa.generate_private_key(public_exponent=65_537, key_size=2_048)
    numbers = private_key.public_key().public_numbers()
    jwks = {
        "keys": [
            {
                "kty": "RSA",
                "kid": "signing-key-1",
                "use": "sig",
                "alg": "RS256",
                "n": _base64url_integer(numbers.n),
                "e": _base64url_integer(numbers.e),
            }
        ]
    }
    verifier = OidcVerifier(config, jwks_document=jwks)
    now = datetime.now(UTC)
    nonce = "provider-nonce-value"
    claims = {
        "iss": "https://identity.example.com",
        "aud": "pageragent-web",
        "sub": "stable-subject-123",
        "iat": now,
        "exp": now + timedelta(minutes=5),
        "nonce": nonce,
        "email": "engineer@example.com",
        "email_verified": True,
        "name": "Elite Engineer",
    }

    def encode(overrides: dict[str, object] | None = None) -> str:
        token_claims = {**claims, **(overrides or {})}
        return jwt.encode(
            token_claims,
            private_key,
            algorithm="RS256",
            headers={"kid": "signing-key-1"},
        )

    identity = verifier.verify(
        encode(),
        expected_nonce_hash=digest_oidc_secret(nonce),
    )
    assert identity.subject == "stable-subject-123"

    with pytest.raises(InvalidOidcTokenError):
        verifier.verify(encode(), expected_nonce_hash=digest_oidc_secret("wrong"))
    with pytest.raises(InvalidOidcTokenError):
        verifier.verify(
            encode({"email_verified": False}),
            expected_nonce_hash=digest_oidc_secret(nonce),
        )
    with pytest.raises(InvalidOidcTokenError):
        verifier.verify(
            encode({"aud": ["pageragent-web", "another-api"], "azp": "another-api"}),
            expected_nonce_hash=digest_oidc_secret(nonce),
        )
    for invalid_claim in (
        {"sub": "stable\x00subject"},
        {"email": "engineer\n@example.com"},
        {"name": "Elite\rEngineer"},
    ):
        with pytest.raises(InvalidOidcTokenError):
            verifier.verify(
                encode(invalid_claim),
                expected_nonce_hash=digest_oidc_secret(nonce),
            )

    weak_private_key = rsa.generate_private_key(public_exponent=65_537, key_size=1_024)
    weak_numbers = weak_private_key.public_key().public_numbers()
    weak_verifier = OidcVerifier(
        config,
        jwks_document={
            "keys": [
                {
                    "kty": "RSA",
                    "kid": "weak-signing-key",
                    "use": "sig",
                    "alg": "RS256",
                    "n": _base64url_integer(weak_numbers.n),
                    "e": _base64url_integer(weak_numbers.e),
                }
            ]
        },
    )
    with pytest.warns(jwt.InsecureKeyLengthWarning):
        weak_token = jwt.encode(
            claims,
            weak_private_key,
            algorithm="RS256",
            headers={"kid": "weak-signing-key"},
        )
    with pytest.raises(InvalidOidcTokenError):
        weak_verifier.verify(
            weak_token,
            expected_nonce_hash=digest_oidc_secret(nonce),
        )


def test_legacy_oidc_bearer_exchange_is_hidden_outside_local_and_test(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "auth_mode", "oidc")
    client = TestClient(app)

    response = client.post(
        "/api/v1/auth/oidc/exchange",
        json={"organization_id": "00000000-0000-0000-0000-000000000001"},
        headers={"Authorization": "Bearer should-not-be-accepted"},
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "Not found"}
