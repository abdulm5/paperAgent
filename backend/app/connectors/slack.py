from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import RLock
from uuid import UUID

import httpx

from app.domain.connectors import SlackConfiguration, SlackCredentials

type JsonValue = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
type Clock = Callable[[], datetime]

SLACK_API_ORIGIN = "https://slack.com"
SLACK_PROVIDER_VERSION = "slack-web-api-v1"

_AUTH_TEST_PATH = "/api/auth.test"
_HISTORY_PATH = "/api/conversations.history"
_POST_MESSAGE_PATH = "/api/chat.postMessage"
_DELIVERY_EVENT_TYPE = "pageragent_delivery"
_CHANNEL_PATTERN = re.compile(r"^[CG][A-Z0-9]{8,31}$")
_MESSAGE_TS_PATTERN = re.compile(r"^[0-9]{1,20}\.[0-9]{6}$")
_CONTROL_CHARACTER_PATTERN = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

_AUTHENTICATION_ERRORS = frozenset(
    {
        "account_inactive",
        "invalid_auth",
        "not_authed",
        "token_expired",
        "token_revoked",
    }
)
_PERMISSION_ERRORS = frozenset(
    {
        "access_denied",
        "channel_not_found",
        "ekm_access_denied",
        "method_not_supported_for_channel_type",
        "missing_scope",
        "no_permission",
        "not_in_channel",
        "org_login_required",
        "restricted_action",
        "restricted_action_non_threadable_channel",
    }
)
_TRANSIENT_ERRORS = frozenset(
    {
        "fatal_error",
        "internal_error",
        "request_timeout",
        "service_unavailable",
    }
)


class SlackProviderError(RuntimeError):
    """Sanitized Slack failure with workflow-safe classification metadata."""

    code = "slack_provider_failure"
    permanent = False
    retryable = False
    ambiguous = False

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds


class SlackPermanentError(SlackProviderError):
    """A failure that should move a delivery toward its dead-letter path."""

    code = "slack_permanent_failure"
    permanent = True


class SlackConfigurationError(SlackPermanentError):
    code = "slack_configuration_invalid"


class SlackAuthenticationError(SlackPermanentError):
    code = "slack_authentication_failed"


class SlackPermissionError(SlackPermanentError):
    code = "slack_permission_denied"


class SlackRedirectError(SlackPermanentError):
    code = "slack_redirect_rejected"


class SlackRequestTooLargeError(SlackPermanentError):
    code = "slack_request_too_large"


class SlackResponseTooLargeError(SlackPermanentError):
    code = "slack_response_too_large"


class SlackRequestBudgetExceededError(SlackPermanentError):
    code = "slack_request_budget_exhausted"


class SlackProviderResponseError(SlackPermanentError):
    code = "slack_response_invalid"


class SlackRetryableError(SlackProviderError):
    code = "slack_transient_failure"
    retryable = True


class SlackRateLimitError(SlackRetryableError):
    code = "slack_rate_limited"


class SlackAmbiguousDeliveryError(SlackRetryableError):
    """A write may have succeeded, so a retry must reconcile before posting."""

    code = "slack_delivery_ambiguous"
    ambiguous = True


class SlackReconciliationAmbiguityError(SlackPermanentError):
    """History contains contradictory receipts that cannot be retried safely."""

    code = "slack_reconciliation_ambiguous"
    ambiguous = True


@dataclass(frozen=True)
class SlackDeliveryReceipt:
    delivery_id: str
    channel_id: str
    message_ts: str
    reconciled: bool

    def to_dict(self) -> dict[str, str | bool]:
        return {
            "delivery_id": self.delivery_id,
            "channel_id": self.channel_id,
            "message_ts": self.message_ts,
            "reconciled": self.reconciled,
        }


@dataclass(frozen=True)
class SlackClientLimits:
    request_budget: int = 2
    timeout_seconds: float = 5.0
    max_request_bytes: int = 64 * 1024
    max_response_bytes: int = 512 * 1024
    history_message_limit: int = 100
    history_lookback_seconds: int = 86_400
    max_text_length: int = 40_000

    def __post_init__(self) -> None:
        if not 1 <= self.request_budget <= 8:
            raise ValueError("Slack request budget must be between 1 and 8")
        if not 0.1 <= self.timeout_seconds <= 30.0:
            raise ValueError("Slack timeout must be between 0.1 and 30 seconds")
        if not 128 <= self.max_request_bytes <= 1024 * 1024:
            raise ValueError("Slack request byte limit is invalid")
        if not 128 <= self.max_response_bytes <= 4 * 1024 * 1024:
            raise ValueError("Slack response byte limit is invalid")
        if not 1 <= self.history_message_limit <= 100:
            raise ValueError("Slack history limit must be between 1 and 100")
        if not 60 <= self.history_lookback_seconds <= 7 * 86_400:
            raise ValueError("Slack history lookback must be between 1 minute and 7 days")
        if not 1 <= self.max_text_length <= 40_000:
            raise ValueError("Slack text limit must be between 1 and 40000")


@dataclass
class _RequestBudget:
    remaining: int

    def consume(self) -> None:
        if self.remaining <= 0:
            raise SlackRequestBudgetExceededError("Slack request budget was exhausted")
        self.remaining -= 1


class _DuplicateKeyError(ValueError):
    pass


class SlackIncidentPublisher:
    """Bounded Slack Web API publisher with reconciliation-before-write semantics."""

    version = SLACK_PROVIDER_VERSION

    def __init__(
        self,
        configuration: SlackConfiguration,
        credentials: SlackCredentials,
        *,
        limits: SlackClientLimits = SlackClientLimits(),
        client: httpx.Client | None = None,
        now: Clock = lambda: datetime.now(UTC),
    ) -> None:
        if configuration.api_url != SLACK_API_ORIGIN:
            raise SlackConfigurationError("Slack API origin is invalid")
        if _CHANNEL_PATTERN.fullmatch(configuration.channel) is None:
            raise SlackConfigurationError("Slack channel binding is invalid")

        token = credentials.bot_token.get_secret_value()
        if (
            not 1 <= len(token) <= 8_192
            or not token.isascii()
            or token != token.strip()
            or any(character.isspace() for character in token)
            or _CONTROL_CHARACTER_PATTERN.search(token) is not None
        ):
            raise SlackConfigurationError("Slack bot token is invalid")

        self.service = configuration.service
        self.channel_id = configuration.channel
        self.limits = limits
        self._now = now
        self._bot_token = token
        connect_timeout = min(3.0, limits.timeout_seconds)
        self._timeout = httpx.Timeout(
            connect=connect_timeout,
            read=limits.timeout_seconds,
            write=limits.timeout_seconds,
            pool=connect_timeout,
        )
        self._request_lock = RLock()
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=SLACK_API_ORIGIN,
            timeout=self._timeout,
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
            follow_redirects=False,
            trust_env=False,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> SlackIncidentPublisher:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def validate(self) -> None:
        """Validate the token and prove bounded read access to the bound channel."""

        budget = _RequestBudget(self.limits.request_budget)
        authentication = self._call_api(_AUTH_TEST_PATH, {}, budget=budget)
        self._require_ok(authentication)
        self._bounded_identity(authentication.get("team_id"), field="team")
        self._bounded_identity(authentication.get("user_id"), field="user")
        self._history(budget, require_complete=False)

    def publish(self, text: str, delivery_id: UUID) -> SlackDeliveryReceipt:
        """Return an existing receipt or publish exactly one marked incident update."""

        normalized_text = self._validate_text(text)
        if not isinstance(delivery_id, UUID):
            raise SlackConfigurationError("Slack delivery identifier is invalid")
        normalized_delivery_id = str(delivery_id)
        budget = _RequestBudget(self.limits.request_budget)

        history = self._history(budget, require_complete=True)
        existing_receipt = self._reconciled_receipt(history, normalized_delivery_id)
        if existing_receipt is not None:
            return existing_receipt

        request_payload: dict[str, JsonValue] = {
            "channel": self.channel_id,
            "client_msg_id": normalized_delivery_id,
            "metadata": {
                "event_type": _DELIVERY_EVENT_TYPE,
                "event_payload": {"delivery_id": normalized_delivery_id},
            },
            "text": normalized_text,
            "unfurl_links": False,
            "unfurl_media": False,
        }
        response = self._call_api(
            _POST_MESSAGE_PATH,
            request_payload,
            budget=budget,
            ambiguous_on_transport=True,
        )
        self._require_ok(response, ambiguous_if_transient=True)
        try:
            channel_id = self._normalized_channel(response.get("channel"))
            message_ts = self._normalized_message_ts(response.get("ts"))
        except SlackProviderResponseError as error:
            raise SlackAmbiguousDeliveryError(
                "Slack acknowledged a delivery without a usable receipt",
                status_code=error.status_code,
            ) from None

        if channel_id != self.channel_id:
            raise SlackAmbiguousDeliveryError(
                "Slack acknowledged a delivery for an unexpected channel"
            )
        return SlackDeliveryReceipt(
            delivery_id=normalized_delivery_id,
            channel_id=channel_id,
            message_ts=message_ts,
            reconciled=False,
        )

    def _history(
        self,
        budget: _RequestBudget,
        *,
        require_complete: bool,
    ) -> list[dict[str, JsonValue]]:
        oldest = self._history_oldest()
        response = self._call_api(
            _HISTORY_PATH,
            {
                "channel": self.channel_id,
                "include_all_metadata": True,
                "limit": self.limits.history_message_limit,
                "oldest": oldest,
            },
            budget=budget,
        )
        self._require_ok(response)
        if self._history_is_incomplete(response):
            if require_complete:
                raise SlackReconciliationAmbiguityError(
                    "Slack history window was incomplete for reconciliation"
                )
        raw_messages = response.get("messages")
        if not isinstance(raw_messages, list):
            raise SlackProviderResponseError("Slack history response omitted its messages")
        if len(raw_messages) > self.limits.history_message_limit:
            raise SlackProviderResponseError(
                "Slack history response exceeded the configured message limit"
            )
        messages: list[dict[str, JsonValue]] = []
        for raw_message in raw_messages:
            if not isinstance(raw_message, dict):
                raise SlackProviderResponseError(
                    "Slack history response contained a malformed message"
                )
            messages.append(raw_message)
        return messages

    def _reconciled_receipt(
        self,
        messages: list[dict[str, JsonValue]],
        delivery_id: str,
    ) -> SlackDeliveryReceipt | None:
        message_timestamps: list[str] = []
        for message in messages:
            metadata = message.get("metadata")
            if metadata is None:
                continue
            if not isinstance(metadata, dict):
                raise SlackProviderResponseError(
                    "Slack history response contained malformed metadata"
                )
            if metadata.get("event_type") != _DELIVERY_EVENT_TYPE:
                continue
            event_payload = metadata.get("event_payload")
            if not isinstance(event_payload, dict):
                raise SlackProviderResponseError(
                    "Slack history response contained malformed delivery metadata"
                )
            if event_payload.get("delivery_id") != delivery_id:
                continue
            try:
                message_timestamps.append(self._normalized_message_ts(message.get("ts")))
            except SlackProviderResponseError:
                raise SlackReconciliationAmbiguityError(
                    "Slack history contained an unusable matching delivery receipt"
                ) from None

        if len(message_timestamps) > 1:
            raise SlackReconciliationAmbiguityError(
                "Slack history contained multiple matching delivery receipts"
            )
        if not message_timestamps:
            return None
        return SlackDeliveryReceipt(
            delivery_id=delivery_id,
            channel_id=self.channel_id,
            message_ts=message_timestamps[0],
            reconciled=True,
        )

    def _call_api(
        self,
        path: str,
        payload: Mapping[str, JsonValue],
        *,
        budget: _RequestBudget,
        ambiguous_on_transport: bool = False,
    ) -> dict[str, JsonValue]:
        if path not in {_AUTH_TEST_PATH, _HISTORY_PATH, _POST_MESSAGE_PATH}:
            raise SlackConfigurationError("Slack API path is not allowed")
        budget.consume()
        request_body = self._encode_request(payload)
        url = f"{SLACK_API_ORIGIN}{path}"

        try:
            with self._request_lock:
                with self._client.stream(
                    "POST",
                    url,
                    headers={
                        "Accept": "application/json",
                        "Authorization": f"Bearer {self._bot_token}",
                        "Content-Type": "application/json; charset=utf-8",
                    },
                    content=request_body,
                    timeout=self._timeout,
                    follow_redirects=False,
                ) as response:
                    status_code = response.status_code
                    if 300 <= status_code < 400:
                        raise SlackRedirectError(
                            "Slack API redirects are not allowed",
                            status_code=status_code,
                        )
                    if status_code == 429:
                        raise SlackRateLimitError(
                            "Slack API rate limit prevented the request",
                            status_code=status_code,
                            retry_after_seconds=self._retry_after_seconds(response.headers),
                        )
                    if status_code in {401, 403}:
                        raise SlackAuthenticationError(
                            "Slack API rejected the bot authentication",
                            status_code=status_code,
                        )
                    if status_code >= 500 or status_code in {408, 425}:
                        error_type = (
                            SlackAmbiguousDeliveryError
                            if ambiguous_on_transport
                            else SlackRetryableError
                        )
                        raise error_type(
                            "Slack API request failed transiently",
                            status_code=status_code,
                        )
                    if not 200 <= status_code < 300:
                        raise SlackPermanentError(
                            "Slack API rejected the request",
                            status_code=status_code,
                        )
                    try:
                        response_body = self._read_bounded_body(response)
                    except (SlackResponseTooLargeError, SlackProviderResponseError):
                        if ambiguous_on_transport:
                            raise SlackAmbiguousDeliveryError(
                                "Slack delivery response exceeded the configured byte limit",
                                status_code=status_code,
                            ) from None
                        raise
        except SlackProviderError:
            raise
        except (httpx.TimeoutException, httpx.TransportError):
            error_type = (
                SlackAmbiguousDeliveryError if ambiguous_on_transport else SlackRetryableError
            )
            raise error_type("Slack API request failed transiently") from None

        if not response_body:
            if ambiguous_on_transport:
                raise SlackAmbiguousDeliveryError(
                    "Slack delivery response was empty",
                    status_code=status_code,
                )
            raise SlackProviderResponseError(
                "Slack API returned an empty response",
                status_code=status_code,
            )
        try:
            parsed = json.loads(
                response_body,
                object_pairs_hook=self._reject_duplicate_keys,
                parse_constant=self._reject_json_constant,
            )
        except (
            json.JSONDecodeError,
            UnicodeDecodeError,
            _DuplicateKeyError,
            ValueError,
            RecursionError,
        ):
            if ambiguous_on_transport:
                raise SlackAmbiguousDeliveryError(
                    "Slack delivery response contained malformed JSON",
                    status_code=status_code,
                ) from None
            raise SlackProviderResponseError(
                "Slack API returned malformed JSON",
                status_code=status_code,
            ) from None
        if not isinstance(parsed, dict):
            if ambiguous_on_transport:
                raise SlackAmbiguousDeliveryError(
                    "Slack delivery response contained an invalid envelope",
                    status_code=status_code,
                )
            raise SlackProviderResponseError(
                "Slack API returned an invalid response envelope",
                status_code=status_code,
            )
        return parsed

    def _require_ok(
        self,
        payload: Mapping[str, JsonValue],
        *,
        ambiguous_if_transient: bool = False,
    ) -> None:
        ok = payload.get("ok")
        if ok is True:
            return
        provider_code = payload.get("error")
        if ok is not False or not isinstance(provider_code, str):
            error_type = (
                SlackAmbiguousDeliveryError
                if ambiguous_if_transient
                else SlackProviderResponseError
            )
            raise error_type("Slack API returned an invalid response envelope")
        if provider_code in _AUTHENTICATION_ERRORS:
            raise SlackAuthenticationError("Slack API rejected the bot authentication")
        if provider_code in _PERMISSION_ERRORS:
            raise SlackPermissionError("Slack API rejected access to the bound channel")
        if provider_code == "ratelimited":
            raise SlackRateLimitError("Slack API rate limit prevented the request")
        if provider_code in _TRANSIENT_ERRORS:
            error_type = (
                SlackAmbiguousDeliveryError
                if ambiguous_if_transient
                else SlackRetryableError
            )
            raise error_type("Slack API request failed transiently")
        raise SlackProviderResponseError("Slack API rejected the request")

    def _encode_request(self, payload: Mapping[str, JsonValue]) -> bytes:
        try:
            body = json.dumps(
                payload,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeError):
            raise SlackConfigurationError("Slack request payload is invalid") from None
        if len(body) > self.limits.max_request_bytes:
            raise SlackRequestTooLargeError(
                "Slack request exceeded the configured byte limit"
            )
        return body

    def _read_bounded_body(self, response: httpx.Response) -> bytes:
        content_length = response.headers.get("content-length")
        if content_length is not None:
            if not content_length.isascii() or not content_length.isdigit():
                raise SlackProviderResponseError(
                    "Slack API returned an invalid response length"
                )
            if int(content_length) > self.limits.max_response_bytes:
                raise SlackResponseTooLargeError(
                    "Slack API response exceeded the configured byte limit"
                )
        body = bytearray()
        for chunk in response.iter_bytes():
            if len(body) + len(chunk) > self.limits.max_response_bytes:
                raise SlackResponseTooLargeError(
                    "Slack API response exceeded the configured byte limit"
                )
            body.extend(chunk)
        return bytes(body)

    def _validate_text(self, text: str) -> str:
        if not isinstance(text, str) or not text or len(text) > self.limits.max_text_length:
            raise SlackConfigurationError("Slack incident update text is invalid")
        if _CONTROL_CHARACTER_PATTERN.search(text) is not None:
            raise SlackConfigurationError("Slack incident update text contains invalid controls")
        return text

    def _history_oldest(self) -> str:
        current_time = self._now()
        if (
            not isinstance(current_time, datetime)
            or current_time.tzinfo is None
            or current_time.utcoffset() is None
        ):
            raise SlackConfigurationError("Slack reconciliation clock is invalid")
        oldest = current_time.astimezone(UTC) - timedelta(
            seconds=self.limits.history_lookback_seconds
        )
        return f"{oldest.timestamp():.6f}"

    @staticmethod
    def _history_is_incomplete(payload: Mapping[str, JsonValue]) -> bool:
        has_more = payload.get("has_more", False)
        if not isinstance(has_more, bool):
            raise SlackProviderResponseError(
                "Slack history response contained an invalid pagination flag"
            )
        raw_metadata = payload.get("response_metadata")
        if raw_metadata is None:
            next_cursor = ""
        else:
            if not isinstance(raw_metadata, dict):
                raise SlackProviderResponseError(
                    "Slack history response contained invalid pagination metadata"
                )
            raw_cursor = raw_metadata.get("next_cursor", "")
            if (
                not isinstance(raw_cursor, str)
                or len(raw_cursor) > 1_024
                or _CONTROL_CHARACTER_PATTERN.search(raw_cursor) is not None
            ):
                raise SlackProviderResponseError(
                    "Slack history response contained an invalid pagination cursor"
                )
            next_cursor = raw_cursor
        return has_more or bool(next_cursor)

    def _normalized_channel(self, raw_channel: JsonValue) -> str:
        if not isinstance(raw_channel, str) or _CHANNEL_PATTERN.fullmatch(raw_channel) is None:
            raise SlackProviderResponseError(
                "Slack API returned an invalid channel receipt"
            )
        return raw_channel

    @staticmethod
    def _normalized_message_ts(raw_timestamp: JsonValue) -> str:
        if (
            not isinstance(raw_timestamp, str)
            or _MESSAGE_TS_PATTERN.fullmatch(raw_timestamp) is None
        ):
            raise SlackProviderResponseError(
                "Slack API returned an invalid message timestamp"
            )
        return raw_timestamp

    @staticmethod
    def _bounded_identity(raw_identity: JsonValue, *, field: str) -> str:
        if (
            not isinstance(raw_identity, str)
            or not 1 <= len(raw_identity) <= 64
            or any(ord(character) < 33 or ord(character) == 127 for character in raw_identity)
        ):
            raise SlackProviderResponseError(
                f"Slack authentication response contained an invalid {field} identity"
            )
        return raw_identity

    @staticmethod
    def _retry_after_seconds(headers: httpx.Headers) -> int | None:
        raw_value = headers.get("retry-after")
        if raw_value is None or not raw_value.isascii() or not raw_value.isdigit():
            return None
        retry_after = int(raw_value)
        return retry_after if 0 <= retry_after <= 86_400 else None

    @staticmethod
    def _reject_duplicate_keys(
        pairs: list[tuple[str, JsonValue]],
    ) -> dict[str, JsonValue]:
        result: dict[str, JsonValue] = {}
        for key, value in pairs:
            if key in result:
                raise _DuplicateKeyError
            result[key] = value
        return result

    @staticmethod
    def _reject_json_constant(_constant: str) -> JsonValue:
        raise ValueError
