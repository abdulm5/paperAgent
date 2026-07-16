import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from pydantic import SecretStr

import app.connectors.github as github_module
from app.connectors.github import (
    DEFAULT_GITHUB_API_VERSION,
    GitHubAppEvidenceProvider,
    GitHubAuthenticationError,
    GitHubClientLimits,
    GitHubConfigurationError,
    GitHubProviderError,
    GitHubProviderResponseError,
    GitHubRateLimitError,
    GitHubRedirectError,
    GitHubRequestBudgetExceededError,
    GitHubResponseTooLargeError,
    GitHubRetryableError,
)
from app.domain.connectors import GithubConfiguration, GithubCredentials

NOW = datetime(2026, 7, 16, 16, 30, tzinfo=UTC)
PRIVATE_KEY = rsa.generate_private_key(public_exponent=65_537, key_size=2_048)
PRIVATE_KEY_PEM = PRIVATE_KEY.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption(),
).decode()


def configuration(**overrides: object) -> GithubConfiguration:
    values: dict[str, object] = {
        "service": "checkout-api",
        "repository": "octo-org/pageragent",
        "app_id": 12_345,
        "installation_id": 67_890,
        "api_url": "https://api.github.com",
    }
    values.update(overrides)
    return GithubConfiguration.model_validate(values)


def credentials(private_key: str = PRIVATE_KEY_PEM) -> GithubCredentials:
    return GithubCredentials(
        private_key=SecretStr(private_key),
        webhook_secret=SecretStr("w" * 32),
    )


def token_response(
    request: httpx.Request,
    token: str = "t",
    *,
    expires_at: datetime | None = None,
) -> httpx.Response:
    expiry = expires_at or NOW + timedelta(hours=1)
    return httpx.Response(
        201,
        json={"token": token, "expires_at": expiry.isoformat().replace("+00:00", "Z")},
        request=request,
    )


def make_provider(
    handler: httpx.MockTransport | object,
    *,
    limits: GitHubClientLimits = GitHubClientLimits(),
    now=lambda: NOW,
    api_version: str = DEFAULT_GITHUB_API_VERSION,
    auto_installation_binding: bool = True,
) -> tuple[GitHubAppEvidenceProvider, httpx.Client]:
    downstream = (
        handler if isinstance(handler, httpx.MockTransport) else httpx.MockTransport(handler)
    )

    def wrapped_handler(request: httpx.Request) -> httpx.Response:
        if (
            auto_installation_binding
            and request.url.path == "/repos/octo-org/pageragent/installation"
        ):
            return httpx.Response(200, json={"id": 67_890}, request=request)
        return downstream.handle_request(request)

    transport = httpx.MockTransport(wrapped_handler)
    client = httpx.Client(transport=transport)
    provider = GitHubAppEvidenceProvider(
        configuration(),
        credentials(),
        client=client,
        limits=limits,
        now=now,
        api_version=api_version,
    )
    return provider, client


def test_app_jwt_uses_rs256_and_nine_minute_claim_window() -> None:
    client = httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(500)))
    provider = GitHubAppEvidenceProvider(
        configuration(),
        credentials(),
        client=client,
        now=lambda: NOW,
    )

    encoded = provider.build_app_jwt()
    claims = jwt.decode(
        encoded,
        PRIVATE_KEY.public_key(),
        algorithms=["RS256"],
        options={"verify_exp": False, "verify_iat": False},
    )

    assert jwt.get_unverified_header(encoded)["alg"] == "RS256"
    assert claims == {
        "iat": int(NOW.timestamp()) - 60,
        "exp": int(NOW.timestamp()) + 540,
        "iss": "12345",
    }
    client.close()


def test_owned_client_disables_environment_and_bounds_connections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeClient:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

        def close(self) -> None:
            captured["closed"] = True

    monkeypatch.setattr(github_module.httpx, "Client", FakeClient)
    provider = GitHubAppEvidenceProvider(
        configuration(),
        credentials(),
        limits=GitHubClientLimits(timeout_seconds=4.0),
    )
    provider.close()

    assert captured["base_url"] == "https://api.github.com"
    assert captured["trust_env"] is False
    assert captured["follow_redirects"] is False
    timeout = captured["timeout"]
    connection_limits = captured["limits"]
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.connect == 3.0
    assert timeout.read == 4.0
    assert isinstance(connection_limits, httpx.Limits)
    assert connection_limits.max_connections == 1
    assert connection_limits.max_keepalive_connections == 1
    assert captured["closed"] is True


@pytest.mark.parametrize("field", ["max_pull_requests", "max_deployments", "max_releases"])
def test_related_evidence_limits_match_bundle_capacity(field: str) -> None:
    with pytest.raises(ValueError, match="cannot exceed 50"):
        GitHubClientLimits(**{field: 51})


@pytest.mark.parametrize(
    "private_key",
    [
        "not a pem SENTINEL_PRIVATE_KEY",
        rsa.generate_private_key(public_exponent=65_537, key_size=1_024)
        .private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        .decode(),
        ec.generate_private_key(ec.SECP256R1())
        .private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        .decode(),
    ],
)
def test_private_key_parsing_is_generic_fail_closed_and_sanitized(private_key: str) -> None:
    with pytest.raises(GitHubConfigurationError) as raised:
        GitHubAppEvidenceProvider(configuration(), credentials(private_key))

    assert "SENTINEL_PRIVATE_KEY" not in str(raised.value)
    assert "PRIVATE KEY" not in str(raised.value)


def test_encrypted_rsa_private_key_is_rejected_without_echoing_it() -> None:
    encrypted = PRIVATE_KEY.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.BestAvailableEncryption(b"SENTINEL_PASSWORD"),
    ).decode()

    with pytest.raises(GitHubConfigurationError) as raised:
        GitHubAppEvidenceProvider(configuration(), credentials(encrypted))

    assert "SENTINEL" not in str(raised.value)


def test_token_exchange_pins_headers_repository_and_read_only_permissions() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/repos/octo-org/pageragent/installation":
            return httpx.Response(200, json={"id": 67_890}, request=request)
        if request.url.path == "/app/installations/67890/access_tokens":
            return token_response(request, token="x")
        return httpx.Response(200, json={"full_name": "octo-org/pageragent"}, request=request)

    provider, client = make_provider(
        handler,
        api_version="2026-04-01",
        auto_installation_binding=False,
    )
    provider.validate()

    installation_request, token_request, repository_request = requests
    assert installation_request.method == "GET"
    assert installation_request.url.path == "/repos/octo-org/pageragent/installation"
    assert installation_request.headers["authorization"].startswith("Bearer ")
    assert installation_request.headers["x-github-api-version"] == "2026-04-01"
    assert token_request.method == "POST"
    assert token_request.headers["accept"] == "application/vnd.github+json"
    assert token_request.headers["x-github-api-version"] == "2026-04-01"
    assert token_request.headers["authorization"].startswith("Bearer ")
    assert json.loads(token_request.content) == {
        "repositories": ["pageragent"],
        "permissions": {
            "contents": "read",
            "pull_requests": "read",
            "deployments": "read",
        },
    }
    assert repository_request.headers["authorization"] == "Bearer x"
    assert repository_request.headers["accept"] == "application/vnd.github+json"
    assert repository_request.headers["x-github-api-version"] == "2026-04-01"
    client.close()


def test_repository_installation_mismatch_fails_before_token_exchange() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/repos/octo-org/pageragent/installation":
            return httpx.Response(200, json={"id": 99_999}, request=request)
        raise AssertionError("installation token must not be requested after a binding mismatch")

    provider, client = make_provider(handler, auto_installation_binding=False)

    with pytest.raises(GitHubAuthenticationError) as raised:
        provider.validate()

    assert [request.url.path for request in requests] == [
        "/repos/octo-org/pageragent/installation"
    ]
    assert "99999" not in str(raised.value)
    assert "67890" not in str(raised.value)
    client.close()


def test_dot_prefixed_repository_name_is_supported_and_canonicalized() -> None:
    normalized = configuration(repository="Octo-Org/.GitHub")
    client = httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(500)))
    provider = GitHubAppEvidenceProvider(normalized, credentials(), client=client)

    assert normalized.repository == "octo-org/.github"
    assert provider.repository == "octo-org/.github"
    client.close()


def test_repository_owner_must_begin_with_an_alphanumeric_character() -> None:
    with pytest.raises(ValueError):
        configuration(repository=".octo-org/pageragent")


def test_installation_token_is_cached_without_assuming_token_length() -> None:
    token_requests = 0
    repository_requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_requests, repository_requests
        if request.url.path.endswith("/access_tokens"):
            token_requests += 1
            return token_response(request, token="z")
        repository_requests += 1
        return httpx.Response(200, json={}, request=request)

    provider, client = make_provider(handler)
    provider.validate()
    provider.validate()

    assert token_requests == 1
    assert repository_requests == 2
    client.close()


def test_token_cache_refreshes_before_expiry() -> None:
    current = NOW
    issued_tokens: list[str] = []

    def clock() -> datetime:
        return current

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/access_tokens"):
            token = f"token-{len(issued_tokens) + 1}"
            issued_tokens.append(token)
            return token_response(request, token, expires_at=NOW + timedelta(hours=1))
        return httpx.Response(200, json={}, request=request)

    provider, client = make_provider(handler, now=clock)
    provider.validate()
    current = NOW + timedelta(minutes=59, seconds=1)
    provider.validate()

    assert issued_tokens == ["token-1", "token-2"]
    client.close()


def test_unauthorized_request_refreshes_once_and_replays_once() -> None:
    issued_tokens: list[str] = []
    repository_tokens: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/access_tokens"):
            token = f"token-{len(issued_tokens) + 1}"
            issued_tokens.append(token)
            return token_response(request, token)
        repository_tokens.append(request.headers["authorization"])
        if request.headers["authorization"] == "Bearer token-1":
            return httpx.Response(
                401,
                json={"message": "SENTINEL_PROVIDER_BODY"},
                request=request,
            )
        return httpx.Response(200, json={}, request=request)

    provider, client = make_provider(handler)
    provider.validate()

    assert issued_tokens == ["token-1", "token-2"]
    assert repository_tokens == ["Bearer token-1", "Bearer token-2"]
    client.close()


def test_second_unauthorized_response_is_sanitized_and_not_retried_again() -> None:
    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        if request.url.path.endswith("/access_tokens"):
            return token_response(request, f"secret-token-{request_count}")
        return httpx.Response(
            401,
            json={"message": "SENTINEL_PROVIDER_BODY secret-token"},
            request=request,
        )

    provider, client = make_provider(handler)
    with pytest.raises(GitHubAuthenticationError) as raised:
        provider.validate()

    assert request_count == 4
    assert "SENTINEL" not in str(raised.value)
    assert "secret-token" not in str(raised.value)
    client.close()


def test_redirect_is_refused_without_following_location() -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(
            302,
            headers={"Location": "http://169.254.169.254/latest/meta-data"},
            request=request,
        )

    provider, client = make_provider(handler)
    with pytest.raises(GitHubRedirectError):
        provider.validate()

    assert requested == ["https://api.github.com/app/installations/67890/access_tokens"]
    client.close()


class ChunkedBody(httpx.SyncByteStream):
    def __iter__(self) -> Iterator[bytes]:
        yield b"a" * 40
        yield b"b" * 40


@pytest.mark.parametrize("use_content_length", [True, False])
def test_response_body_is_capped_by_header_and_streamed_bytes(use_content_length: bool) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if use_content_length:
            return httpx.Response(201, content=b"x" * 80, request=request)
        return httpx.Response(201, stream=ChunkedBody(), request=request)

    provider, client = make_provider(
        handler,
        limits=GitHubClientLimits(max_response_bytes=64),
    )
    with pytest.raises(GitHubResponseTooLargeError):
        provider.validate()

    client.close()


def test_request_budget_stops_before_extra_network_io() -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return token_response(request)

    provider, client = make_provider(handler, limits=GitHubClientLimits(request_budget=2))
    with pytest.raises(GitHubRequestBudgetExceededError):
        provider.validate()

    assert requests == 1
    client.close()


@pytest.mark.parametrize("status_code", [403, 429])
def test_rate_limit_errors_are_typed_and_never_echo_provider_body_or_token(
    status_code: int,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/access_tokens"):
            return token_response(request, token="SENTINEL_INSTALLATION_TOKEN")
        return httpx.Response(
            status_code,
            headers={"Retry-After": "120", "X-RateLimit-Reset": "1784220000"},
            json={"message": "SENTINEL_PROVIDER_BODY"},
            request=request,
        )

    provider, client = make_provider(handler)
    with pytest.raises(GitHubRateLimitError) as raised:
        provider.validate()

    assert raised.value.retryable is True
    assert raised.value.status_code == status_code
    assert raised.value.retry_after_seconds == 120
    assert raised.value.reset_at is not None
    assert "SENTINEL" not in str(raised.value)
    client.close()


def test_plain_forbidden_response_is_non_retryable_and_sanitized() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/access_tokens"):
            return token_response(request)
        return httpx.Response(
            403,
            json={"message": "SENTINEL_PROVIDER_BODY"},
            request=request,
        )

    provider, client = make_provider(handler)
    with pytest.raises(GitHubProviderError) as raised:
        provider.validate()

    assert not isinstance(raised.value, GitHubRateLimitError)
    assert raised.value.retryable is False
    assert "SENTINEL" not in str(raised.value)
    client.close()


def test_oversized_json_integer_is_mapped_to_a_sanitized_provider_shape_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/access_tokens"):
            return token_response(request)
        return httpx.Response(
            200,
            content=b'{"integer":' + (b"1" * 5_000) + b"}",
            request=request,
        )

    provider, client = make_provider(handler)
    with pytest.raises(GitHubProviderResponseError) as raised:
        provider.validate()

    assert "integer" not in str(raised.value)
    client.close()


def test_installation_token_length_and_control_characters_are_bounded() -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return token_response(request, token="t" * 8_193)

    provider, client = make_provider(handler)
    with pytest.raises(GitHubProviderResponseError):
        provider.validate()

    assert requests == 1
    client.close()


def test_server_failure_is_typed_retryable_and_sanitized() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/access_tokens"):
            return token_response(request)
        return httpx.Response(
            503,
            json={"message": "SENTINEL_PROVIDER_BODY"},
            request=request,
        )

    provider, client = make_provider(handler)
    with pytest.raises(GitHubRetryableError) as raised:
        provider.validate()

    assert raised.value.retryable is True
    assert "SENTINEL" not in str(raised.value)
    client.close()


@pytest.mark.parametrize(
    "repository",
    ["../pageragent", "octo-org/..", "octo-org/%2e%2e", "octo-org/repo\\admin"],
)
def test_repository_traversal_segments_are_rejected_before_io(repository: str) -> None:
    with pytest.raises((GitHubConfigurationError, ValueError)):
        GitHubAppEvidenceProvider(configuration(repository=repository), credentials())


def test_only_exact_public_github_api_origin_is_supported() -> None:
    with pytest.raises((GitHubConfigurationError, ValueError)):
        GitHubAppEvidenceProvider(
            configuration(api_url="https://api.github.com.evil.example"),
            credentials(),
        )


def test_invalid_active_sha_is_rejected_before_network_io() -> None:
    request_called = False

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal request_called
        request_called = True
        raise AssertionError("invalid caller input must be rejected before HTTP")

    provider, client = make_provider(handler)
    with pytest.raises(Exception, match="SHA was malformed"):
        provider.collect_evidence(NOW, "checkout-api", "../../admin")

    assert request_called is False
    client.close()
