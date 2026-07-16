import json
from datetime import UTC, datetime
from uuid import UUID

import httpx
import pytest
from pydantic import SecretStr

import app.connectors.slack as slack_module
from app.connectors.slack import (
    SlackAmbiguousDeliveryError,
    SlackAuthenticationError,
    SlackClientLimits,
    SlackConfigurationError,
    SlackIncidentPublisher,
    SlackPermissionError,
    SlackProviderError,
    SlackProviderResponseError,
    SlackRateLimitError,
    SlackReconciliationAmbiguityError,
    SlackRedirectError,
    SlackRequestBudgetExceededError,
    SlackRequestTooLargeError,
    SlackResponseTooLargeError,
    SlackRetryableError,
)
from app.domain.connectors import SlackConfiguration, SlackCredentials

CHANNEL_ID = "C012345678"
TOKEN = "xoxb-SENTINEL-secret-token"
DELIVERY_ID = UUID("5f17385c-4102-44b8-b53d-341ae50cc25f")
MESSAGE_TS = "1784220000.123456"
NOW = datetime(2026, 7, 16, 18, 0, tzinfo=UTC)


def configuration() -> SlackConfiguration:
    return SlackConfiguration(
        service="checkout-api",
        channel=CHANNEL_ID,
        api_url="https://slack.com",
    )


def credentials(token: str = TOKEN) -> SlackCredentials:
    return SlackCredentials(bot_token=SecretStr(token))


def make_publisher(
    handler,
    *,
    limits: SlackClientLimits = SlackClientLimits(),
) -> tuple[SlackIncidentPublisher, httpx.Client]:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return (
        SlackIncidentPublisher(
            configuration(),
            credentials(),
            limits=limits,
            client=client,
            now=lambda: NOW,
        ),
        client,
    )


def auth_response(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={"ok": True, "team_id": "T012345678", "user_id": "U012345678"},
        request=request,
    )


def history_response(
    request: httpx.Request,
    messages: list[dict[str, object]] | None = None,
) -> httpx.Response:
    return httpx.Response(
        200,
        json={"ok": True, "messages": messages or []},
        request=request,
    )


def delivery_message(
    *,
    delivery_id: str = str(DELIVERY_ID),
    timestamp: str = MESSAGE_TS,
) -> dict[str, object]:
    return {
        "ts": timestamp,
        "metadata": {
            "event_type": "pageragent_delivery",
            "event_payload": {"delivery_id": delivery_id},
        },
    }


def test_validate_proves_authentication_and_bounded_channel_history_access() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/auth.test":
            return auth_response(request)
        assert request.url.path == "/api/conversations.history"
        return history_response(request)

    publisher, client = make_publisher(handler)
    publisher.validate()

    assert [request.url for request in requests] == [
        httpx.URL("https://slack.com/api/auth.test"),
        httpx.URL("https://slack.com/api/conversations.history"),
    ]
    assert all(request.method == "POST" for request in requests)
    assert all(request.headers["authorization"] == f"Bearer {TOKEN}" for request in requests)
    assert json.loads(requests[0].content) == {}
    assert json.loads(requests[1].content) == {
        "channel": CHANNEL_ID,
        "include_all_metadata": True,
        "limit": 100,
        "oldest": "1784138400.000000",
    }
    client.close()


def test_publish_reconciles_then_posts_a_stably_marked_bounded_message() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/conversations.history":
            return history_response(request)
        assert request.url.path == "/api/chat.postMessage"
        return httpx.Response(
            200,
            json={"ok": True, "channel": CHANNEL_ID, "ts": MESSAGE_TS},
            request=request,
        )

    publisher, client = make_publisher(handler)
    receipt = publisher.publish("Checkout errors are elevated.", DELIVERY_ID)

    assert [request.url.path for request in requests] == [
        "/api/conversations.history",
        "/api/chat.postMessage",
    ]
    submitted = json.loads(requests[1].content)
    assert submitted == {
        "channel": CHANNEL_ID,
        "client_msg_id": str(DELIVERY_ID),
        "metadata": {
            "event_type": "pageragent_delivery",
            "event_payload": {"delivery_id": str(DELIVERY_ID)},
        },
        "text": "Checkout errors are elevated.",
        "unfurl_links": False,
        "unfurl_media": False,
    }
    assert receipt.to_dict() == {
        "delivery_id": str(DELIVERY_ID),
        "channel_id": CHANNEL_ID,
        "message_ts": MESSAGE_TS,
        "reconciled": False,
    }
    assert all(isinstance(value, str | bool) for value in receipt.to_dict().values())
    client.close()


def test_publish_returns_reconciled_receipt_without_a_second_write() -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        assert request.url.path == "/api/conversations.history"
        return history_response(request, [delivery_message()])

    publisher, client = make_publisher(handler)
    receipt = publisher.publish("The original message must not be repeated.", DELIVERY_ID)

    assert requests == 1
    assert receipt.to_dict() == {
        "delivery_id": str(DELIVERY_ID),
        "channel_id": CHANNEL_ID,
        "message_ts": MESSAGE_TS,
        "reconciled": True,
    }
    client.close()


def test_multiple_reconciliation_matches_fail_closed_as_permanent_ambiguity() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return history_response(
            request,
            [delivery_message(), delivery_message(timestamp="1784220001.123456")],
        )

    publisher, client = make_publisher(handler)
    with pytest.raises(SlackReconciliationAmbiguityError) as raised:
        publisher.publish("Do not create a third message.", DELIVERY_ID)

    assert raised.value.permanent is True
    assert raised.value.retryable is False
    assert raised.value.ambiguous is True
    assert raised.value.code == "slack_reconciliation_ambiguous"
    client.close()


@pytest.mark.parametrize(
    "pagination",
    [
        {"has_more": True},
        {"response_metadata": {"next_cursor": "opaque-next-page"}},
    ],
)
def test_incomplete_history_fails_closed_before_posting(
    pagination: dict[str, object],
) -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(
            200,
            json={"ok": True, "messages": [], **pagination},
            request=request,
        )

    publisher, client = make_publisher(handler)
    with pytest.raises(SlackReconciliationAmbiguityError):
        publisher.publish("Do not post past an incomplete scan.", DELIVERY_ID)

    assert requests == 1
    client.close()


def test_post_transport_failure_is_retryable_delivery_ambiguity_and_sanitized() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/conversations.history":
            return history_response(request)
        raise httpx.ReadTimeout(
            f"SENTINEL provider timeout leaked {TOKEN}",
            request=request,
        )

    publisher, client = make_publisher(handler)
    with pytest.raises(SlackAmbiguousDeliveryError) as raised:
        publisher.publish("Checkout errors are elevated.", DELIVERY_ID)

    assert raised.value.permanent is False
    assert raised.value.retryable is True
    assert raised.value.ambiguous is True
    assert raised.value.code == "slack_delivery_ambiguous"
    assert raised.value.retry_after_seconds is None
    assert TOKEN not in str(raised.value)
    assert "SENTINEL" not in str(raised.value)
    client.close()


@pytest.mark.parametrize(
    "response",
    [
        lambda request: httpx.Response(
            200,
            content=b"{}",
            headers={"Content-Length": "invalid"},
            request=request,
        ),
        lambda request: httpx.Response(200, content=b"x" * 513, request=request),
    ],
)
def test_unreadable_successful_post_receipt_is_delivery_ambiguity(response) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/conversations.history":
            return history_response(request)
        return response(request)

    publisher, client = make_publisher(
        handler,
        limits=SlackClientLimits(max_response_bytes=512),
    )
    with pytest.raises(SlackAmbiguousDeliveryError) as raised:
        publisher.publish("Checkout errors are elevated.", DELIVERY_ID)

    assert raised.value.ambiguous is True
    assert raised.value.retryable is True
    client.close()


def test_history_transport_failure_is_retryable_but_not_ambiguous() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"SENTINEL {TOKEN}", request=request)

    publisher, client = make_publisher(handler)
    with pytest.raises(SlackRetryableError) as raised:
        publisher.publish("Checkout errors are elevated.", DELIVERY_ID)

    assert raised.value.retryable is True
    assert raised.value.ambiguous is False
    assert raised.value.code == "slack_transient_failure"
    assert "SENTINEL" not in str(raised.value)
    assert TOKEN not in str(raised.value)
    client.close()


def test_rate_limit_preserves_only_a_bounded_retry_hint() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers={"Retry-After": "120"},
            text=f"SENTINEL provider body {TOKEN}",
            request=request,
        )

    publisher, client = make_publisher(handler)
    with pytest.raises(SlackRateLimitError) as raised:
        publisher.validate()

    assert raised.value.retryable is True
    assert raised.value.ambiguous is False
    assert raised.value.status_code == 429
    assert raised.value.retry_after_seconds == 120
    assert raised.value.code == "slack_rate_limited"
    assert "SENTINEL" not in str(raised.value)
    assert TOKEN not in str(raised.value)
    client.close()


@pytest.mark.parametrize(
    ("status_code", "error_type", "retryable"),
    [
        (302, SlackRedirectError, False),
        (401, SlackAuthenticationError, False),
        (503, SlackRetryableError, True),
    ],
)
def test_http_status_errors_are_typed_without_reflecting_provider_data(
    status_code: int,
    error_type: type[SlackProviderError],
    retryable: bool,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code,
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
    ("provider_code", "error_type"),
    [
        ("invalid_auth", SlackAuthenticationError),
        ("missing_scope", SlackPermissionError),
        ("internal_error", SlackRetryableError),
        ("provider-SENTINEL-private-detail", SlackProviderResponseError),
    ],
)
def test_slack_error_envelopes_map_to_sanitized_stable_types(
    provider_code: str,
    error_type: type[SlackProviderError],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"ok": False, "error": provider_code},
            request=request,
        )

    publisher, client = make_publisher(handler)
    with pytest.raises(error_type) as raised:
        publisher.validate()

    assert provider_code not in str(raised.value)
    assert "SENTINEL" not in str(raised.value)
    client.close()


@pytest.mark.parametrize(
    "body",
    [
        b'{"ok":true,"ok":false}',
        b'{"ok":true,"nested":{"id":"one","id":"two"}}',
        b'{"ok":NaN}',
        b"not-json",
        b"[]",
    ],
)
def test_malformed_or_duplicate_json_is_rejected_strictly(body: bytes) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, request=request)

    publisher, client = make_publisher(handler)
    with pytest.raises(SlackProviderResponseError):
        publisher.validate()
    client.close()


@pytest.mark.parametrize(
    "response",
    [
        lambda request: httpx.Response(200, content=b"x" * 129, request=request),
        lambda request: httpx.Response(
            200,
            content=b"{}",
            headers={"Content-Length": "129"},
            request=request,
        ),
    ],
)
def test_response_body_is_streamed_under_a_hard_byte_cap(response) -> None:
    publisher, client = make_publisher(
        response,
        limits=SlackClientLimits(max_response_bytes=128),
    )
    with pytest.raises(SlackResponseTooLargeError):
        publisher.validate()
    client.close()


def test_publish_enforces_text_and_encoded_request_bounds_before_write() -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        return history_response(request)

    publisher, client = make_publisher(
        handler,
        limits=SlackClientLimits(max_request_bytes=256),
    )
    with pytest.raises(SlackRequestTooLargeError):
        publisher.publish("é" * 200, DELIVERY_ID)

    assert requests == ["/api/conversations.history"]
    with pytest.raises(SlackConfigurationError):
        publisher.publish("", DELIVERY_ID)
    with pytest.raises(SlackConfigurationError):
        publisher.publish("unsafe\x01control", DELIVERY_ID)
    client.close()


def test_request_budget_caps_validation_before_second_network_call() -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return auth_response(request)

    publisher, client = make_publisher(
        handler,
        limits=SlackClientLimits(request_budget=1),
    )
    with pytest.raises(SlackRequestBudgetExceededError):
        publisher.validate()

    assert requests == 1
    client.close()


def test_invalid_post_receipt_is_delivery_ambiguity() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/conversations.history":
            return history_response(request)
        return httpx.Response(
            200,
            json={
                "ok": True,
                "channel": "C999999999",
                "ts": "SENTINEL-invalid-ts",
            },
            request=request,
        )

    publisher, client = make_publisher(handler)
    with pytest.raises(SlackAmbiguousDeliveryError) as raised:
        publisher.publish("Checkout errors are elevated.", DELIVERY_ID)

    assert raised.value.retryable is True
    assert raised.value.ambiguous is True
    assert "SENTINEL" not in str(raised.value)
    client.close()


def test_owned_client_disables_environment_redirects_and_connection_fanout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    created_clients: list[httpx.Client] = []
    real_client = httpx.Client

    def client_factory(**kwargs: object) -> httpx.Client:
        captured.update(kwargs)
        client = real_client(
            transport=httpx.MockTransport(auth_response),
            **kwargs,
        )
        created_clients.append(client)
        return client

    monkeypatch.setattr(slack_module.httpx, "Client", client_factory)
    publisher = SlackIncidentPublisher(configuration(), credentials())

    assert captured["base_url"] == "https://slack.com"
    assert captured["trust_env"] is False
    assert captured["follow_redirects"] is False
    limits = captured["limits"]
    assert isinstance(limits, httpx.Limits)
    assert limits.max_connections == 1
    assert limits.max_keepalive_connections == 1

    publisher.close()
    assert created_clients[0].is_closed is True


def test_injected_client_is_not_closed_by_publisher() -> None:
    client = httpx.Client(transport=httpx.MockTransport(auth_response))
    publisher = SlackIncidentPublisher(configuration(), credentials(), client=client)

    publisher.close()

    assert client.is_closed is False
    client.close()


@pytest.mark.parametrize(
    "token",
    [
        " xoxb-token",
        "xoxb-token\n",
        "xoxb token",
        "xoxb-\x00-token",
        "xoxb-tökén",
    ],
)
def test_unsafe_tokens_are_rejected_before_network_io(token: str) -> None:
    with pytest.raises(SlackConfigurationError) as raised:
        SlackIncidentPublisher(configuration(), credentials(token))

    assert token not in str(raised.value)
