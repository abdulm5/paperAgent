import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from pydantic import SecretStr

import app.connectors.github_issues as github_issues_module
from app.connectors.github_issues import (
    GitHubIssueAmbiguousDeliveryError,
    GitHubIssueAuthenticationError,
    GitHubIssueConfigurationError,
    GitHubIssueDeliveryReceipt,
    GitHubIssuePermanentError,
    GitHubIssuePermissionError,
    GitHubIssueProviderResponseError,
    GitHubIssuePublisher,
    GitHubIssuePublisherError,
    GitHubIssuePublisherLimits,
    GitHubIssueRateLimitError,
    GitHubIssueReconciliationAmbiguityError,
    GitHubIssueRedirectError,
    GitHubIssueRequestBudgetExceededError,
    GitHubIssueRequestTooLargeError,
    GitHubIssueResponseTooLargeError,
    GitHubIssueRetryableError,
)
from app.domain.connectors import GithubConfiguration, GithubCredentials

NOW = datetime(2026, 7, 16, 19, 30, tzinfo=UTC)
DELIVERY_ID = UUID("6ecde4d7-9eae-4da3-97ec-f3a7ab66b23f")
INSTALLATION_ID = 67_890
REPOSITORY = "octo-org/pageragent"
TOKEN = "ghs_SENTINEL-secret-token"
PRIVATE_KEY = rsa.generate_private_key(public_exponent=65_537, key_size=2_048)
PRIVATE_KEY_PEM = PRIVATE_KEY.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption(),
).decode()


def configuration(**overrides: object) -> GithubConfiguration:
    values: dict[str, object] = {
        "service": "checkout-api",
        "repository": REPOSITORY,
        "app_id": 12_345,
        "installation_id": INSTALLATION_ID,
        "issue_creation_enabled": True,
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
    *,
    permissions: dict[str, str] | None = None,
    repositories: list[dict[str, object]] | None = None,
    expires_at: datetime | None = None,
) -> httpx.Response:
    return httpx.Response(
        201,
        json={
            "token": TOKEN,
            "expires_at": (expires_at or NOW + timedelta(hours=1))
            .isoformat()
            .replace("+00:00", "Z"),
            "permissions": (
                permissions
                if permissions is not None
                else {"issues": "write", "metadata": "read"}
            ),
            "repositories": (
                repositories
                if repositories is not None
                else [{"full_name": REPOSITORY}]
            ),
        },
        request=request,
    )


def issue_payload(
    *,
    number: int = 42,
    body: str | None = None,
    html_url: str | None = None,
    pull_request: bool = False,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "number": number,
        "html_url": html_url or f"https://github.com/{REPOSITORY}/issues/{number}",
        "body": body,
    }
    if pull_request:
        payload["pull_request"] = {
            "url": f"https://api.github.com/repos/{REPOSITORY}/pulls/{number}"
        }
    return payload


def marker(delivery_id: UUID = DELIVERY_ID) -> str:
    return f"<!-- pageragent-delivery:{delivery_id} -->"


def make_publisher(
    handler,
    *,
    limits: GitHubIssuePublisherLimits = GitHubIssuePublisherLimits(),
    config: GithubConfiguration | None = None,
) -> tuple[GitHubIssuePublisher, httpx.Client]:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    publisher = GitHubIssuePublisher(
        config or configuration(),
        credentials(),
        client=client,
        limits=limits,
        now=lambda: NOW,
    )
    return publisher, client


def provider_handler(
    issue_handler,
    *,
    requests: list[httpx.Request] | None = None,
    installation_id: int = INSTALLATION_ID,
    token_permissions: dict[str, str] | None = None,
    token_repositories: list[dict[str, object]] | None = None,
):
    def handler(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)
        if request.url.path == f"/repos/{REPOSITORY}/installation":
            return httpx.Response(200, json={"id": installation_id}, request=request)
        if request.url.path == f"/app/installations/{INSTALLATION_ID}/access_tokens":
            return token_response(
                request,
                permissions=token_permissions,
                repositories=token_repositories,
            )
        return issue_handler(request)

    return handler


def test_validate_uses_rs256_repo_scoped_issue_write_token_and_never_creates() -> None:
    requests: list[httpx.Request] = []

    def issues(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        return httpx.Response(200, json=[], request=request)

    publisher, client = make_publisher(provider_handler(issues, requests=requests))
    publisher.validate()

    assert [request.url.path for request in requests] == [
        f"/repos/{REPOSITORY}/installation",
        f"/app/installations/{INSTALLATION_ID}/access_tokens",
        f"/repos/{REPOSITORY}/issues",
    ]
    assert [request.method for request in requests] == ["GET", "POST", "GET"]
    token_request = requests[1]
    assert json.loads(token_request.content) == {
        "permissions": {"issues": "write"},
        "repositories": ["pageragent"],
    }
    app_token = requests[0].headers["authorization"].removeprefix("Bearer ")
    claims = jwt.decode(
        app_token,
        PRIVATE_KEY.public_key(),
        algorithms=["RS256"],
        options={"verify_exp": False, "verify_iat": False},
    )
    assert claims == {
        "iat": int(NOW.timestamp()) - 60,
        "exp": int(NOW.timestamp()) + 540,
        "iss": "12345",
    }
    assert requests[2].headers["authorization"] == f"Bearer {TOKEN}"
    assert dict(requests[2].url.params) == {
        "state": "all",
        "sort": "created",
        "direction": "desc",
        "per_page": "1",
        "page": "1",
    }
    assert all(request.url.host == "api.github.com" for request in requests)
    client.close()


def test_publish_reconciles_before_creating_an_exactly_marked_issue() -> None:
    requests: list[httpx.Request] = []

    def issues(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=[], request=request)
        return httpx.Response(201, json=issue_payload(), request=request)

    publisher, client = make_publisher(provider_handler(issues, requests=requests))
    receipt = publisher.publish("Checkout incident", "Grounded evidence.", DELIVERY_ID)

    issue_requests = [request for request in requests if request.url.path.endswith("/issues")]
    assert [request.method for request in issue_requests] == ["GET", "POST"]
    assert json.loads(issue_requests[1].content) == {
        "body": f"Grounded evidence.\n\n{marker()}",
        "title": "Checkout incident",
    }
    assert receipt == GitHubIssueDeliveryReceipt(
        delivery_id=str(DELIVERY_ID),
        repository=REPOSITORY,
        issue_number=42,
        issue_url=f"https://github.com/{REPOSITORY}/issues/42",
        reconciled=False,
    )
    assert receipt.to_dict() == {
        "delivery_id": str(DELIVERY_ID),
        "repository": REPOSITORY,
        "issue_number": 42,
        "issue_url": f"https://github.com/{REPOSITORY}/issues/42",
        "reconciled": False,
    }
    client.close()


def test_existing_exact_marker_is_not_duplicated_in_created_body() -> None:
    submitted_body = ""
    original_body = f"Grounded evidence.\n\n{marker()}"

    def issues(request: httpx.Request) -> httpx.Response:
        nonlocal submitted_body
        if request.method == "GET":
            return httpx.Response(200, json=[], request=request)
        submitted_body = json.loads(request.content)["body"]
        return httpx.Response(201, json=issue_payload(), request=request)

    publisher, client = make_publisher(provider_handler(issues))
    publisher.publish("Checkout incident", original_body, DELIVERY_ID)

    assert submitted_body == original_body
    assert submitted_body.count(marker()) == 1
    client.close()


@pytest.mark.parametrize(
    "body",
    [
        "conflict <!-- pageragent-delivery:00000000-0000-0000-0000-000000000000 -->",
        f"{marker()}\n{marker()}",
        "malformed pageragent-delivery:marker",
    ],
)
def test_conflicting_or_duplicate_delivery_markers_fail_before_network(body: str) -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(500, request=request)

    publisher, client = make_publisher(handler)
    with pytest.raises(GitHubIssueConfigurationError):
        publisher.publish("Checkout incident", body, DELIVERY_ID)

    assert requests == 0
    client.close()


def test_reconciliation_returns_existing_issue_and_excludes_pull_requests() -> None:
    requests: list[httpx.Request] = []

    def issues(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        return httpx.Response(
            200,
            json=[
                issue_payload(number=41, body=marker(), pull_request=True),
                issue_payload(number=42, body=marker()),
            ],
            request=request,
        )

    publisher, client = make_publisher(provider_handler(issues, requests=requests))
    receipt = publisher.publish("Checkout incident", "Body", DELIVERY_ID)

    assert receipt.issue_number == 42
    assert receipt.reconciled is True
    assert not any(
        request.method == "POST" and request.url.path.endswith("/issues")
        for request in requests
    )
    client.close()


def test_pull_request_marker_alone_does_not_prevent_issue_creation() -> None:
    issue_methods: list[str] = []

    def issues(request: httpx.Request) -> httpx.Response:
        issue_methods.append(request.method)
        if request.method == "GET":
            return httpx.Response(
                200,
                json=[issue_payload(body=marker(), pull_request=True)],
                request=request,
            )
        return httpx.Response(201, json=issue_payload(number=43), request=request)

    publisher, client = make_publisher(provider_handler(issues))
    receipt = publisher.publish("Checkout incident", "Body", DELIVERY_ID)

    assert issue_methods == ["GET", "POST"]
    assert receipt.issue_number == 43
    client.close()


def test_reconciliation_follows_only_a_bounded_generated_page_sequence() -> None:
    pages: list[str] = []

    def issues(request: httpx.Request) -> httpx.Response:
        page = request.url.params["page"]
        pages.append(page)
        if page == "1":
            return httpx.Response(
                200,
                json=[],
                headers={
                    "Link": (
                        f'<https://api.github.com/repos/{REPOSITORY}/issues?page=999>; '
                        'rel="next"'
                    )
                },
                request=request,
            )
        return httpx.Response(
            200,
            json=[issue_payload(number=88, body=marker())],
            request=request,
        )

    publisher, client = make_publisher(provider_handler(issues))
    receipt = publisher.publish("Checkout incident", "Body", DELIVERY_ID)

    assert pages == ["1", "2"]
    assert receipt.issue_number == 88
    assert receipt.reconciled is True
    client.close()


def test_incomplete_bounded_reconciliation_fails_closed_before_create() -> None:
    issue_requests = 0

    def issues(request: httpx.Request) -> httpx.Response:
        nonlocal issue_requests
        issue_requests += 1
        return httpx.Response(
            200,
            json=[],
            headers={
                "Link": f'<https://api.github.com/repos/{REPOSITORY}/issues>; rel="next"'
            },
            request=request,
        )

    publisher, client = make_publisher(provider_handler(issues))
    with pytest.raises(GitHubIssueReconciliationAmbiguityError) as raised:
        publisher.publish("Checkout incident", "Body", DELIVERY_ID)

    assert issue_requests == 2
    assert raised.value.ambiguous is True
    assert raised.value.permanent is True
    client.close()


def test_duplicate_reconciliation_matches_fail_closed() -> None:
    def issues(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[issue_payload(number=41, body=marker()), issue_payload(body=marker())],
            request=request,
        )

    publisher, client = make_publisher(provider_handler(issues))
    with pytest.raises(GitHubIssueReconciliationAmbiguityError):
        publisher.publish("Checkout incident", "Body", DELIVERY_ID)
    client.close()


def test_issue_creation_must_be_explicitly_enabled() -> None:
    with pytest.raises(GitHubIssueConfigurationError) as raised:
        GitHubIssuePublisher(
            configuration(issue_creation_enabled=False),
            credentials(),
        )

    assert raised.value.permanent is True
    assert raised.value.code == "github_issue_configuration_invalid"


@pytest.mark.parametrize(
    "private_key",
    [
        "not a private key SENTINEL",
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
def test_invalid_private_keys_fail_closed_without_reflection(private_key: str) -> None:
    with pytest.raises(GitHubIssueConfigurationError) as raised:
        GitHubIssuePublisher(configuration(), credentials(private_key))

    assert "SENTINEL" not in str(raised.value)
    assert "PRIVATE KEY" not in str(raised.value)


def test_repository_installation_mismatch_fails_before_token_exchange() -> None:
    requests: list[httpx.Request] = []
    publisher, client = make_publisher(
        provider_handler(
            lambda request: httpx.Response(500, request=request),
            requests=requests,
            installation_id=99_999,
        )
    )

    with pytest.raises(GitHubIssueAuthenticationError):
        publisher.validate()

    assert [request.url.path for request in requests] == [
        f"/repos/{REPOSITORY}/installation"
    ]
    client.close()


@pytest.mark.parametrize(
    ("permissions", "repositories", "error_type"),
    [
        ({"issues": "read"}, [{"full_name": REPOSITORY}], GitHubIssuePermissionError),
        (
            {"issues": "write", "contents": "read"},
            [{"full_name": REPOSITORY}],
            GitHubIssuePermissionError,
        ),
        ({"issues": "write"}, [], GitHubIssueAuthenticationError),
        (
            {"issues": "write"},
            [{"full_name": "other/repository"}],
            GitHubIssueAuthenticationError,
        ),
    ],
)
def test_token_response_must_prove_write_permission_and_exact_repo_scope(
    permissions: dict[str, str],
    repositories: list[dict[str, object]],
    error_type: type[GitHubIssuePublisherError],
) -> None:
    publisher, client = make_publisher(
        provider_handler(
            lambda request: httpx.Response(200, json=[], request=request),
            token_permissions=permissions,
            token_repositories=repositories,
        )
    )

    with pytest.raises(error_type):
        publisher.validate()
    client.close()


def test_token_is_cached_only_until_its_refresh_boundary() -> None:
    current = NOW
    token_requests = 0

    def clock() -> datetime:
        return current

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_requests
        if request.url.path.endswith("/installation"):
            return httpx.Response(200, json={"id": INSTALLATION_ID}, request=request)
        if request.url.path.endswith("/access_tokens"):
            token_requests += 1
            return token_response(request)
        return httpx.Response(200, json=[], request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    publisher = GitHubIssuePublisher(
        configuration(), credentials(), client=client, now=clock
    )
    publisher.validate()
    publisher.validate()
    current = NOW + timedelta(minutes=59, seconds=1)
    publisher.validate()

    assert token_requests == 2
    client.close()


def test_create_transport_failure_is_retryable_ambiguity_and_sanitized() -> None:
    def issues(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=[], request=request)
        raise httpx.ReadTimeout(f"SENTINEL {TOKEN}", request=request)

    publisher, client = make_publisher(provider_handler(issues))
    with pytest.raises(GitHubIssueAmbiguousDeliveryError) as raised:
        publisher.publish("Checkout incident", "Body", DELIVERY_ID)

    assert raised.value.retryable is True
    assert raised.value.ambiguous is True
    assert raised.value.permanent is False
    assert "SENTINEL" not in str(raised.value)
    assert TOKEN not in str(raised.value)
    client.close()


def test_reconciliation_transport_failure_is_retryable_not_ambiguous() -> None:
    def issues(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"SENTINEL {TOKEN}", request=request)

    publisher, client = make_publisher(provider_handler(issues))
    with pytest.raises(GitHubIssueRetryableError) as raised:
        publisher.publish("Checkout incident", "Body", DELIVERY_ID)

    assert raised.value.retryable is True
    assert raised.value.ambiguous is False
    assert "SENTINEL" not in str(raised.value)
    client.close()


def test_rate_limit_exposes_only_a_bounded_retry_hint() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers={"Retry-After": "90"},
            text=f"SENTINEL {TOKEN}",
            request=request,
        )

    publisher, client = make_publisher(handler)
    with pytest.raises(GitHubIssueRateLimitError) as raised:
        publisher.validate()

    assert raised.value.retry_after_seconds == 90
    assert raised.value.status_code == 429
    assert raised.value.retryable is True
    assert "SENTINEL" not in str(raised.value)
    client.close()


@pytest.mark.parametrize(
    ("status", "error_type", "retryable"),
    [
        (302, GitHubIssueRedirectError, False),
        (401, GitHubIssueAuthenticationError, False),
        (403, GitHubIssuePermissionError, False),
        (503, GitHubIssueRetryableError, True),
    ],
)
def test_read_status_failures_are_typed_and_sanitized(
    status: int,
    error_type: type[GitHubIssuePublisherError],
    retryable: bool,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status,
            headers={"Location": "https://SENTINEL.invalid"},
            text=f"SENTINEL {TOKEN}",
            request=request,
        )

    publisher, client = make_publisher(handler)
    with pytest.raises(error_type) as raised:
        publisher.validate()

    assert raised.value.retryable is retryable
    assert "SENTINEL" not in str(raised.value)
    assert TOKEN not in str(raised.value)
    client.close()


@pytest.mark.parametrize(
    ("status", "error_type"),
    [
        (503, GitHubIssueAmbiguousDeliveryError),
        (422, GitHubIssuePermanentError),
    ],
)
def test_create_status_distinguishes_ambiguity_from_rejection(
    status: int,
    error_type: type[GitHubIssuePublisherError],
) -> None:
    def issues(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=[], request=request)
        return httpx.Response(status, text="SENTINEL", request=request)

    publisher, client = make_publisher(provider_handler(issues))
    with pytest.raises(error_type) as raised:
        publisher.publish("Checkout incident", "Body", DELIVERY_ID)

    assert "SENTINEL" not in str(raised.value)
    client.close()


@pytest.mark.parametrize(
    "body",
    [
        b'{"id":1,"id":2}',
        b'{"id":NaN}',
        b"not-json",
        b"[]",
    ],
)
def test_malformed_duplicate_or_wrong_shape_json_is_rejected(body: bytes) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, request=request)

    publisher, client = make_publisher(handler)
    with pytest.raises(GitHubIssueProviderResponseError):
        publisher.validate()
    client.close()


@pytest.mark.parametrize(
    "response",
    [
        lambda request: httpx.Response(200, content=b"x" * 257, request=request),
        lambda request: httpx.Response(
            200,
            content=b"{}",
            headers={"Content-Length": "257"},
            request=request,
        ),
    ],
)
def test_response_body_is_streamed_under_a_hard_cap(response) -> None:
    publisher, client = make_publisher(
        response,
        limits=GitHubIssuePublisherLimits(max_response_bytes=256),
    )
    with pytest.raises(GitHubIssueResponseTooLargeError):
        publisher.validate()
    client.close()


@pytest.mark.parametrize(
    "response",
    [
        lambda request: httpx.Response(201, content=b"not-json", request=request),
        lambda request: httpx.Response(
            201,
            content=b"{}",
            headers={"Content-Length": "invalid"},
            request=request,
        ),
    ],
)
def test_unusable_successful_create_receipt_is_delivery_ambiguity(response) -> None:
    def issues(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=[], request=request)
        return response(request)

    publisher, client = make_publisher(provider_handler(issues))
    with pytest.raises(GitHubIssueAmbiguousDeliveryError):
        publisher.publish("Checkout incident", "Body", DELIVERY_ID)
    client.close()


def test_receipt_rejects_cross_repository_or_noncanonical_issue_urls() -> None:
    def issues(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=[], request=request)
        return httpx.Response(
            201,
            json=issue_payload(html_url="https://github.com/other/repo/issues/42"),
            request=request,
        )

    publisher, client = make_publisher(provider_handler(issues))
    with pytest.raises(GitHubIssueAmbiguousDeliveryError):
        publisher.publish("Checkout incident", "Body", DELIVERY_ID)
    client.close()


def test_request_and_text_bounds_are_enforced_before_issue_create() -> None:
    issue_methods: list[str] = []

    def issues(request: httpx.Request) -> httpx.Response:
        issue_methods.append(request.method)
        return httpx.Response(200, json=[], request=request)

    publisher, client = make_publisher(
        provider_handler(issues),
        limits=GitHubIssuePublisherLimits(max_request_bytes=256),
    )
    with pytest.raises(GitHubIssueRequestTooLargeError):
        publisher.publish("Checkout incident", "é" * 200, DELIVERY_ID)
    with pytest.raises(GitHubIssueConfigurationError):
        publisher.publish("", "Body", DELIVERY_ID)
    with pytest.raises(GitHubIssueConfigurationError):
        publisher.publish("Unsafe\x01title", "Body", DELIVERY_ID)

    assert issue_methods == ["GET"]
    client.close()


def test_request_budget_stops_validation_before_token_exchange() -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, json={"id": INSTALLATION_ID}, request=request)

    publisher, client = make_publisher(
        handler,
        limits=GitHubIssuePublisherLimits(request_budget=1),
    )
    with pytest.raises(GitHubIssueRequestBudgetExceededError):
        publisher.validate()

    assert requests == 1
    client.close()


def test_owned_client_disables_environment_redirects_and_connection_fanout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    clients: list[httpx.Client] = []
    real_client = httpx.Client

    def client_factory(**kwargs: object) -> httpx.Client:
        captured.update(kwargs)
        client = real_client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(500, request=request)
            ),
            **kwargs,
        )
        clients.append(client)
        return client

    monkeypatch.setattr(github_issues_module.httpx, "Client", client_factory)
    publisher = GitHubIssuePublisher(configuration(), credentials())

    assert captured["base_url"] == "https://api.github.com"
    assert captured["trust_env"] is False
    assert captured["follow_redirects"] is False
    connection_limits = captured["limits"]
    assert isinstance(connection_limits, httpx.Limits)
    assert connection_limits.max_connections == 1
    assert connection_limits.max_keepalive_connections == 1

    publisher.close()
    assert clients[0].is_closed is True


def test_injected_client_is_not_closed_by_publisher() -> None:
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(500, request=request)
        )
    )
    publisher = GitHubIssuePublisher(configuration(), credentials(), client=client)

    publisher.close()

    assert client.is_closed is False
    client.close()
