from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import isfinite
from threading import RLock
from urllib.parse import urlsplit

import httpx

from app.domain.connectors import PrometheusConfiguration, PrometheusCredentials
from app.domain.prometheus import (
    PrometheusQueryResult,
    PrometheusSample,
    PrometheusSeries,
)

Clock = Callable[[], datetime]
JsonValue = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]

PROMETHEUS_PROVIDER_VERSION = "prometheus-http-api-v1"
PROMETHEUS_QUERY_CATALOG_VERSION = "prometheus-query-catalog-v1"
_INSTANT_QUERY_PATH = "/api/v1/query"
_RANGE_QUERY_PATH = "/api/v1/query_range"
_SERVICE_PATTERN = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,98}[A-Za-z0-9])?$"
)
_LABEL_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_CONTROL_CHARACTER_PATTERN = re.compile(r"[\x00-\x1f\x7f]")
_PERSISTED_LABEL_ALLOWLIST = frozenset(
    {"__name__", "service", "job", "instance", "cluster", "namespace"}
)
_MAX_FUTURE_SKEW = timedelta(minutes=5)


class PrometheusProviderError(RuntimeError):
    """Sanitized provider failure suitable for workflow and API receipts."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        self.status_code = status_code
        super().__init__(message)


class PrometheusConfigurationError(PrometheusProviderError):
    pass


class PrometheusUnsupportedMetricError(PrometheusConfigurationError):
    pass


class PrometheusAuthenticationError(PrometheusProviderError):
    pass


class PrometheusRetryableError(PrometheusProviderError):
    pass


class PrometheusRateLimitError(PrometheusRetryableError):
    pass


class PrometheusRedirectError(PrometheusProviderError):
    pass


class PrometheusResponseTooLargeError(PrometheusProviderError):
    pass


class PrometheusRequestBudgetExceededError(PrometheusProviderError):
    pass


class PrometheusProviderResponseError(PrometheusProviderError):
    pass


@dataclass(frozen=True)
class PrometheusClientLimits:
    request_budget: int = 1
    timeout_seconds: float = 5.0
    max_response_bytes: int = 1_048_576
    max_window_seconds: int = 3_600
    step_seconds: int = 15
    max_series: int = 20
    max_samples_per_series: int = 256
    max_total_samples: int = 2_048
    max_labels_per_series: int = 32
    max_label_name_length: int = 100
    max_label_value_length: int = 500

    def __post_init__(self) -> None:
        if not 1 <= self.request_budget <= 10:
            raise ValueError("Prometheus request budget must be between 1 and 10")
        if not 0 < self.timeout_seconds <= 30:
            raise ValueError("Prometheus timeout must be between 0 and 30 seconds")
        if not 1_024 <= self.max_response_bytes <= 4_194_304:
            raise ValueError("Prometheus response limit must be between 1 KiB and 4 MiB")
        if not 1 <= self.max_window_seconds <= 86_400:
            raise ValueError("Prometheus query window must be between 1 second and 1 day")
        if not 1 <= self.step_seconds <= self.max_window_seconds:
            raise ValueError("Prometheus query step must fit inside the query window")
        if not 1 <= self.max_series <= 10_000:
            raise ValueError("Prometheus series limit is invalid")
        if not 1 <= self.max_samples_per_series <= 10_000:
            raise ValueError("Prometheus per-series sample limit is invalid")
        if not 1 <= self.max_total_samples <= 1_000_000:
            raise ValueError("Prometheus total sample limit is invalid")
        if self.max_total_samples < self.max_samples_per_series:
            raise ValueError("Prometheus total sample limit cannot be smaller than one series")
        if not 1 <= self.max_labels_per_series <= 64:
            raise ValueError("Prometheus label-count limit is invalid")
        if not 1 <= self.max_label_name_length <= 100:
            raise ValueError("Prometheus label-name limit is invalid")
        if not 1 <= self.max_label_value_length <= 500:
            raise ValueError("Prometheus label-value limit is invalid")


@dataclass(frozen=True)
class _CatalogEntry:
    query_id: str
    metric_name: str

    def render(self, service: str) -> str:
        # Service names are validated against a grammar without quotes or
        # backslashes, keeping this server-owned selector non-extensible.
        return f'{self.metric_name}{{service="{service}"}}'


_QUERY_CATALOG: Mapping[str, _CatalogEntry] = {
    "http_server_error_rate": _CatalogEntry(
        query_id="alert.http-server-error-rate.v1",
        metric_name="http_server_error_rate",
    )
}


class _RequestBudget:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.used = 0

    def consume(self) -> None:
        if self.used >= self.limit:
            raise PrometheusRequestBudgetExceededError(
                "Prometheus request budget was exhausted"
            )
        self.used += 1


class _DuplicateKeyError(ValueError):
    pass


class PrometheusHttpApiProvider:
    """Bounded read-only Prometheus HTTP API adapter."""

    version = PROMETHEUS_PROVIDER_VERSION
    catalog_version = PROMETHEUS_QUERY_CATALOG_VERSION

    def __init__(
        self,
        configuration: PrometheusConfiguration,
        credentials: PrometheusCredentials,
        *,
        limits: PrometheusClientLimits = PrometheusClientLimits(),
        client: httpx.Client | None = None,
        now: Clock = lambda: datetime.now(UTC),
    ) -> None:
        self.base_url = self._validated_origin(configuration.base_url)
        self.service = configuration.service
        token = credentials.bearer_token.get_secret_value()
        if (
            not 1 <= len(token) <= 8_192
            or token != token.strip()
            or _CONTROL_CHARACTER_PATTERN.search(token) is not None
        ):
            raise PrometheusConfigurationError("Prometheus bearer token is invalid")
        self._bearer_token = token
        self.limits = limits
        self._now = now
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
            base_url=self.base_url,
            timeout=self._timeout,
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
            follow_redirects=False,
            trust_env=False,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> PrometheusHttpApiProvider:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def validate(self) -> None:
        """Prove bearer-authenticated read access with one fixed instant query."""

        payload = self._request(
            _INSTANT_QUERY_PATH,
            data={
                "query": "vector(1)",
                "time": self._format_timestamp(self._aware_utc(self._now())),
                "limit": "1",
                "timeout": f"{self.limits.timeout_seconds:g}s",
            },
            budget=_RequestBudget(self.limits.request_budget),
        )
        data = self._success_data(payload, expected_result_type="vector")
        result = self._as_list(data.get("result"))
        if len(result) != 1:
            raise PrometheusProviderResponseError(
                "Prometheus validation query returned an unexpected result"
            )
        item = self._as_object(result[0])
        labels = self._normalize_labels(item.get("metric"))
        if labels:
            raise PrometheusProviderResponseError(
                "Prometheus validation query returned unexpected labels"
            )
        _, value = self._normalize_sample(item.get("value"))
        if value != 1.0:
            raise PrometheusProviderResponseError(
                "Prometheus validation query returned an unexpected value"
            )

    def collect_metric(
        self,
        *,
        metric_name: str,
        service: str,
        observed_at: datetime,
        window_seconds: int,
    ) -> PrometheusQueryResult:
        entry = _QUERY_CATALOG.get(metric_name)
        if entry is None:
            raise PrometheusUnsupportedMetricError(
                "The alert metric is not in the Prometheus query catalog"
            )
        if _SERVICE_PATTERN.fullmatch(service) is None:
            raise PrometheusConfigurationError("Prometheus service binding is invalid")
        if service != self.service:
            raise PrometheusConfigurationError(
                "Prometheus service does not match the connector binding"
            )
        if isinstance(window_seconds, bool) or not isinstance(window_seconds, int):
            raise PrometheusConfigurationError("Prometheus query window is invalid")
        if not 1 <= window_seconds <= self.limits.max_window_seconds:
            raise PrometheusConfigurationError("Prometheus query window exceeds its limit")
        maximum_samples = (window_seconds // self.limits.step_seconds) + 1
        if maximum_samples > self.limits.max_samples_per_series:
            raise PrometheusConfigurationError(
                "Prometheus query window exceeds its sample limit"
            )

        ended_at = self._aware_utc(observed_at)
        current_time = self._aware_utc(self._now())
        if ended_at > current_time + _MAX_FUTURE_SKEW:
            raise PrometheusConfigurationError(
                "Prometheus query timestamp exceeds the allowed future skew"
            )
        started_at = ended_at - timedelta(seconds=window_seconds)
        payload = self._request(
            _RANGE_QUERY_PATH,
            data={
                "query": entry.render(service),
                "start": self._format_timestamp(started_at),
                "end": self._format_timestamp(ended_at),
                "step": str(self.limits.step_seconds),
                "limit": str(self.limits.max_series),
                "timeout": f"{self.limits.timeout_seconds:g}s",
            },
            budget=_RequestBudget(self.limits.request_budget),
        )
        data = self._success_data(payload, expected_result_type="matrix")
        raw_series = self._as_list(data.get("result"))
        if len(raw_series) > self.limits.max_series:
            raise PrometheusProviderResponseError(
                "Prometheus response exceeded the configured series limit"
            )

        normalized: list[PrometheusSeries] = []
        identities: set[tuple[tuple[str, str], ...]] = set()
        total_samples = 0
        for raw_item in raw_series:
            item = self._as_object(raw_item)
            if "histogram" in item or "histograms" in item:
                raise PrometheusProviderResponseError(
                    "Prometheus native histogram results are not supported"
                )
            labels = self._normalize_labels(item.get("metric"))
            if labels.get("service") != service:
                raise PrometheusProviderResponseError(
                    "Prometheus series did not match the selected service binding"
                )
            identity = tuple(sorted(labels.items()))
            if identity in identities:
                raise PrometheusProviderResponseError(
                    "Prometheus response contained duplicate series"
                )
            identities.add(identity)
            values = self._as_list(item.get("values"))
            if len(values) > self.limits.max_samples_per_series:
                raise PrometheusProviderResponseError(
                    "Prometheus response exceeded the per-series sample limit"
                )
            samples: list[PrometheusSample] = []
            previous_timestamp: datetime | None = None
            for raw_sample in values:
                timestamp, value = self._normalize_sample(raw_sample)
                if timestamp < started_at or timestamp > ended_at:
                    raise PrometheusProviderResponseError(
                        "Prometheus response contained an out-of-window sample"
                    )
                if previous_timestamp is not None and timestamp <= previous_timestamp:
                    raise PrometheusProviderResponseError(
                        "Prometheus response samples were not strictly ordered"
                    )
                previous_timestamp = timestamp
                samples.append(PrometheusSample(observed_at=timestamp, value=value))
            total_samples += len(samples)
            if total_samples > self.limits.max_total_samples:
                raise PrometheusProviderResponseError(
                    "Prometheus response exceeded the total sample limit"
                )
            normalized.append(PrometheusSeries(labels=labels, samples=samples))

        normalized.sort(key=lambda item: tuple(sorted(item.labels.items())))
        return PrometheusQueryResult(
            provider_version=self.version,
            catalog_version=self.catalog_version,
            query_id=entry.query_id,
            metric_name=entry.metric_name,
            service=service,
            window_started_at=started_at,
            window_ended_at=ended_at,
            step_seconds=self.limits.step_seconds,
            series_count=len(normalized),
            sample_count=total_samples,
            truncated=False,
            series=normalized,
        )

    def _request(
        self,
        path: str,
        *,
        data: Mapping[str, str],
        budget: _RequestBudget,
    ) -> dict[str, JsonValue]:
        if path not in {_INSTANT_QUERY_PATH, _RANGE_QUERY_PATH}:
            raise PrometheusConfigurationError("Unsupported Prometheus API path")
        budget.consume()
        url = f"{self.base_url}{path}"
        try:
            with self._request_lock:
                with self._client.stream(
                    "POST",
                    url,
                    headers={
                        "Accept": "application/json",
                        "Authorization": f"Bearer {self._bearer_token}",
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                    data=data,
                    timeout=self._timeout,
                    follow_redirects=False,
                ) as response:
                    if 300 <= response.status_code < 400:
                        raise PrometheusRedirectError(
                            "Prometheus API redirects are not allowed",
                            status_code=response.status_code,
                        )
                    body = self._read_bounded_body(response)
                    status_code = response.status_code
        except PrometheusProviderError:
            raise
        except (httpx.TimeoutException, httpx.TransportError):
            raise PrometheusRetryableError(
                "Prometheus API request failed transiently"
            ) from None

        if not 200 <= status_code < 300:
            self._raise_for_status(status_code)
        if not body:
            raise PrometheusProviderResponseError("Prometheus API returned an empty response")

        def reject_duplicate_keys(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
            result: dict[str, JsonValue] = {}
            for key, value in pairs:
                if key in result:
                    raise _DuplicateKeyError
                result[key] = value
            return result

        try:
            payload = json.loads(body, object_pairs_hook=reject_duplicate_keys)
        except (
            json.JSONDecodeError,
            UnicodeDecodeError,
            _DuplicateKeyError,
            ValueError,
            RecursionError,
        ):
            raise PrometheusProviderResponseError(
                "Prometheus API returned malformed JSON"
            ) from None
        if not isinstance(payload, dict):
            raise PrometheusProviderResponseError(
                "Prometheus API returned an invalid response envelope"
            )
        return payload

    def _read_bounded_body(self, response: httpx.Response) -> bytes:
        content_length = response.headers.get("content-length")
        if content_length is not None:
            if not content_length.isascii() or not content_length.isdigit():
                raise PrometheusProviderResponseError(
                    "Prometheus API returned an invalid response length"
                )
            if int(content_length) > self.limits.max_response_bytes:
                raise PrometheusResponseTooLargeError(
                    "Prometheus API response exceeded the configured byte limit"
                )
        body = bytearray()
        for chunk in response.iter_bytes():
            if len(body) + len(chunk) > self.limits.max_response_bytes:
                raise PrometheusResponseTooLargeError(
                    "Prometheus API response exceeded the configured byte limit"
                )
            body.extend(chunk)
        return bytes(body)

    @staticmethod
    def _success_data(
        payload: Mapping[str, JsonValue],
        *,
        expected_result_type: str,
    ) -> dict[str, JsonValue]:
        if payload.get("status") != "success":
            raise PrometheusProviderResponseError(
                "Prometheus API returned an unsuccessful response"
            )
        warnings = payload.get("warnings")
        infos = payload.get("infos")
        if (warnings is not None and warnings != []) or (infos is not None and infos != []):
            # Partial or qualified results must not silently become causal
            # evidence. Provider text stays outside errors and persisted data.
            raise PrometheusProviderResponseError(
                "Prometheus API qualified the result with provider notices"
            )
        data = PrometheusHttpApiProvider._as_object(payload.get("data"))
        if data.get("resultType") != expected_result_type:
            raise PrometheusProviderResponseError(
                "Prometheus API returned an unexpected result type"
            )
        if "result" not in data:
            raise PrometheusProviderResponseError(
                "Prometheus API response omitted its result"
            )
        return data

    def _normalize_labels(self, raw_labels: JsonValue) -> dict[str, str]:
        labels = self._as_object(raw_labels)
        if len(labels) > self.limits.max_labels_per_series:
            raise PrometheusProviderResponseError(
                "Prometheus response exceeded the label-count limit"
            )
        normalized: dict[str, str] = {}
        for name, value in labels.items():
            if (
                len(name) > self.limits.max_label_name_length
                or _LABEL_NAME_PATTERN.fullmatch(name) is None
                or name not in _PERSISTED_LABEL_ALLOWLIST
            ):
                raise PrometheusProviderResponseError(
                    "Prometheus response contained an invalid label name"
                )
            if (
                not isinstance(value, str)
                or len(value) > self.limits.max_label_value_length
                or _CONTROL_CHARACTER_PATTERN.search(value) is not None
            ):
                raise PrometheusProviderResponseError(
                    "Prometheus response contained an invalid label value"
                )
            normalized[name] = value
        return dict(sorted(normalized.items()))

    @staticmethod
    def _normalize_sample(raw_sample: JsonValue) -> tuple[datetime, float]:
        if not isinstance(raw_sample, list) or len(raw_sample) != 2:
            raise PrometheusProviderResponseError(
                "Prometheus response contained a malformed sample"
            )
        raw_timestamp, raw_value = raw_sample
        if (
            isinstance(raw_timestamp, bool)
            or not isinstance(raw_timestamp, int | float)
            or not isinstance(raw_value, str)
        ):
            raise PrometheusProviderResponseError(
                "Prometheus response contained a malformed sample"
            )
        try:
            numeric_timestamp = float(raw_timestamp)
            if not isfinite(numeric_timestamp):
                raise ValueError
            timestamp = datetime.fromtimestamp(numeric_timestamp, UTC)
            value = float(raw_value)
        except (OverflowError, OSError, ValueError):
            raise PrometheusProviderResponseError(
                "Prometheus response contained a malformed sample"
            ) from None
        if not isfinite(value):
            raise PrometheusProviderResponseError(
                "Prometheus response contained a non-finite sample"
            )
        return timestamp, value

    @staticmethod
    def _as_object(value: JsonValue) -> dict[str, JsonValue]:
        if not isinstance(value, dict):
            raise PrometheusProviderResponseError(
                "Prometheus API returned a malformed object"
            )
        return value

    @staticmethod
    def _as_list(value: JsonValue) -> list[JsonValue]:
        if not isinstance(value, list):
            raise PrometheusProviderResponseError(
                "Prometheus API returned a malformed list"
            )
        return value

    @staticmethod
    def _raise_for_status(status_code: int) -> None:
        if status_code in {401, 403}:
            raise PrometheusAuthenticationError(
                "Prometheus authentication was rejected",
                status_code=status_code,
            )
        if status_code == 429:
            raise PrometheusRateLimitError(
                "Prometheus rate limit was exceeded",
                status_code=status_code,
            )
        if status_code >= 500:
            raise PrometheusRetryableError(
                "Prometheus API is temporarily unavailable",
                status_code=status_code,
            )
        raise PrometheusProviderError(
            "Prometheus API rejected the evidence request",
            status_code=status_code,
        )

    @staticmethod
    def _validated_origin(raw_url: str) -> str:
        try:
            parsed = urlsplit(raw_url)
            port = parsed.port
        except (TypeError, ValueError):
            raise PrometheusConfigurationError("Prometheus base URL is invalid") from None
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise PrometheusConfigurationError("Prometheus base URL is invalid")
        host = parsed.hostname.lower()
        rendered_host = f"[{host}]" if ":" in host else host
        default_port = 443 if parsed.scheme == "https" else 80
        rendered_port = "" if port in {None, default_port} else f":{port}"
        return f"{parsed.scheme.lower()}://{rendered_host}{rendered_port}"

    @staticmethod
    def _aware_utc(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise PrometheusConfigurationError(
                "Prometheus query timestamps must include a timezone"
            )
        return value.astimezone(UTC)

    @staticmethod
    def _format_timestamp(value: datetime) -> str:
        return value.isoformat().replace("+00:00", "Z")
