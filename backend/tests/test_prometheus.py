from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs
from uuid import UUID

import httpx
import pytest
from pydantic import SecretStr, ValidationError

import app.connectors.prometheus as prometheus_module
from app.connectors.prometheus import (
    PrometheusAuthenticationError,
    PrometheusClientLimits,
    PrometheusConfigurationError,
    PrometheusHttpApiProvider,
    PrometheusProviderError,
    PrometheusProviderResponseError,
    PrometheusRateLimitError,
    PrometheusRedirectError,
    PrometheusResponseTooLargeError,
    PrometheusRetryableError,
    PrometheusUnsupportedMetricError,
)
from app.domain.connectors import PrometheusConfiguration, PrometheusCredentials
from app.domain.prometheus import PrometheusEvidenceBundle

NOW = datetime(2026, 7, 16, 16, 30, tzinfo=UTC)
TOKEN = "prometheus-read-token-SENTINEL"


def configuration(base_url: str = "https://prometheus.example") -> PrometheusConfiguration:
    return PrometheusConfiguration(service="checkout-api", base_url=base_url)


def credentials(token: str = TOKEN) -> PrometheusCredentials:
    return PrometheusCredentials(bearer_token=SecretStr(token))


def make_provider(
    handler,
    *,
    limits: PrometheusClientLimits = PrometheusClientLimits(),
    base_url: str = "https://prometheus.example",
) -> tuple[PrometheusHttpApiProvider, httpx.Client]:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return (
        PrometheusHttpApiProvider(
            configuration(base_url),
            credentials(),
            limits=limits,
            client=client,
            now=lambda: NOW,
        ),
        client,
    )


def matrix_response(
    request: httpx.Request,
    result: list[dict[str, object]],
) -> httpx.Response:
    return httpx.Response(
        200,
        json={"status": "success", "data": {"resultType": "matrix", "result": result}},
        request=request,
    )


def test_collects_one_catalogued_metric_with_bounded_deterministic_normalization() -> None:
    requested: list[httpx.Request] = []
    start = NOW - timedelta(seconds=300)

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request)
        return matrix_response(
            request,
            [
                {
                    "metric": {"instance": "west", "service": "checkout-api"},
                    "values": [
                        [start.timestamp(), "0.01"],
                        [NOW.timestamp(), "0.12"],
                    ],
                },
                {
                    "metric": {"instance": "east", "service": "checkout-api"},
                    "values": [[start.timestamp(), "0.02"]],
                },
            ],
        )

    provider, client = make_provider(handler)
    result = provider.collect_metric(
        metric_name="http_server_error_rate",
        service="checkout-api",
        observed_at=NOW,
        window_seconds=300,
    )

    assert len(requested) == 1
    request = requested[0]
    assert request.method == "POST"
    assert request.url == "https://prometheus.example/api/v1/query_range"
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    assert request.headers["content-type"].startswith("application/x-www-form-urlencoded")
    submitted = parse_qs(request.content.decode())
    assert submitted == {
        "query": ['http_server_error_rate{service="checkout-api"}'],
        "start": ["2026-07-16T16:25:00Z"],
        "end": ["2026-07-16T16:30:00Z"],
        "step": ["15"],
        "limit": ["20"],
        "timeout": ["5s"],
    }
    assert result.model_dump(mode="json") == {
        "provider": "prometheus",
        "provider_version": "prometheus-http-api-v1",
        "catalog_version": "prometheus-query-catalog-v1",
        "query_id": "alert.http-server-error-rate.v1",
        "metric_name": "http_server_error_rate",
        "service": "checkout-api",
        "window_started_at": "2026-07-16T16:25:00Z",
        "window_ended_at": "2026-07-16T16:30:00Z",
        "step_seconds": 15,
        "series_count": 2,
        "sample_count": 3,
        "truncated": False,
        "series": [
            {
                "labels": {"instance": "east", "service": "checkout-api"},
                "samples": [
                    {"observed_at": "2026-07-16T16:25:00Z", "value": 0.02}
                ],
            },
            {
                "labels": {"instance": "west", "service": "checkout-api"},
                "samples": [
                    {"observed_at": "2026-07-16T16:25:00Z", "value": 0.01},
                    {"observed_at": "2026-07-16T16:30:00Z", "value": 0.12},
                ],
            },
        ],
    }
    serialized = result.model_dump_json()
    assert "prometheus.example" not in serialized
    assert "query" not in result.model_fields_set
    assert TOKEN not in serialized
    client.close()


def test_validate_uses_one_fixed_vector_query() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "resultType": "vector",
                    "result": [{"metric": {}, "value": [NOW.timestamp(), "1"]}],
                },
            },
            request=request,
        )

    provider, client = make_provider(handler)
    provider.validate()

    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert request.url.path == "/api/v1/query"
    assert parse_qs(request.content.decode()) == {
        "query": ["vector(1)"],
        "time": ["2026-07-16T16:30:00Z"],
        "limit": ["1"],
        "timeout": ["5s"],
    }
    client.close()


def test_owned_client_disables_environment_redirects_and_parallel_connections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeClient:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

        def close(self) -> None:
            captured["closed"] = True

    monkeypatch.setattr(prometheus_module.httpx, "Client", FakeClient)
    provider = PrometheusHttpApiProvider(
        configuration(),
        credentials(),
        limits=PrometheusClientLimits(timeout_seconds=4.0),
    )
    provider.close()

    assert captured["base_url"] == "https://prometheus.example"
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


@pytest.mark.parametrize(
    "base_url",
    [
        "https://prometheus.example/private",
        "https://prometheus.example?query=secret",
        "https://prometheus.example/#fragment",
        "https://user:secret@prometheus.example",
        "file:///etc/passwd",
    ],
)
def test_base_url_must_be_an_exact_http_origin(base_url: str) -> None:
    with pytest.raises(PrometheusConfigurationError):
        PrometheusHttpApiProvider(configuration(base_url), credentials())


@pytest.mark.parametrize(
    ("status_code", "error_type"),
    [
        (302, PrometheusRedirectError),
        (401, PrometheusAuthenticationError),
        (403, PrometheusAuthenticationError),
        (429, PrometheusRateLimitError),
        (503, PrometheusRetryableError),
        (422, PrometheusProviderError),
    ],
)
def test_provider_status_failures_are_typed_and_never_reflect_response_bodies(
    status_code: int,
    error_type: type[PrometheusProviderError],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code,
            text=f"provider leaked {TOKEN}",
            headers={"Location": "http://169.254.169.254/latest"},
            request=request,
        )

    provider, client = make_provider(handler)

    with pytest.raises(error_type) as raised:
        provider.collect_metric(
            metric_name="http_server_error_rate",
            service="checkout-api",
            observed_at=NOW,
            window_seconds=300,
        )

    assert TOKEN not in str(raised.value)
    assert "169.254" not in str(raised.value)
    client.close()


def test_timeout_is_retryable_and_sanitized() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout(f"timeout included {TOKEN}", request=request)

    provider, client = make_provider(handler)
    with pytest.raises(PrometheusRetryableError) as raised:
        provider.validate()

    assert TOKEN not in str(raised.value)
    client.close()


@pytest.mark.parametrize(
    "response",
    [
        lambda request: httpx.Response(
            200,
            content=b"x" * 1_025,
            request=request,
        ),
        lambda request: httpx.Response(
            200,
            content=b"{}",
            headers={"Content-Length": "1025"},
            request=request,
        ),
    ],
)
def test_response_body_is_streamed_under_a_hard_byte_cap(response) -> None:
    provider, client = make_provider(
        response,
        limits=PrometheusClientLimits(max_response_bytes=1_024),
    )

    with pytest.raises(PrometheusResponseTooLargeError):
        provider.validate()

    client.close()


@pytest.mark.parametrize(
    "body",
    [
        b'{"status":"success","status":"error","data":{}}',
        b'{"status":"success","data":{"resultType":"vector","result":[],"result":[]}}',
        b"not-json",
        b"[]",
    ],
)
def test_json_envelope_rejects_duplicate_keys_and_malformed_shapes(body: bytes) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, request=request)

    provider, client = make_provider(handler)
    with pytest.raises(PrometheusProviderResponseError):
        provider.validate()
    client.close()


def test_range_collection_accepts_only_matrix_results() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {"resultType": "vector", "result": []},
            },
            request=request,
        )

    provider, client = make_provider(handler)
    with pytest.raises(PrometheusProviderResponseError, match="result type"):
        provider.collect_metric(
            metric_name="http_server_error_rate",
            service="checkout-api",
            observed_at=NOW,
            window_seconds=300,
        )
    client.close()


@pytest.mark.parametrize("notice_field", ["warnings", "infos"])
def test_provider_notices_cannot_silently_qualify_causal_evidence(
    notice_field: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "success",
                notice_field: ["SENTINEL provider-specific text"],
                "data": {"resultType": "matrix", "result": []},
            },
            request=request,
        )

    provider, client = make_provider(handler)
    with pytest.raises(PrometheusProviderResponseError, match="provider notices") as raised:
        provider.collect_metric(
            metric_name="http_server_error_rate",
            service="checkout-api",
            observed_at=NOW,
            window_seconds=300,
        )

    assert "SENTINEL" not in str(raised.value)
    client.close()


@pytest.mark.parametrize("histogram_field", ["histogram", "histograms"])
def test_native_histogram_payloads_are_rejected(histogram_field: str) -> None:
    result = [
        {
            "metric": {"service": "checkout-api"},
            "values": [],
            histogram_field: [],
        }
    ]
    provider, client = make_provider(lambda request: matrix_response(request, result))

    with pytest.raises(PrometheusProviderResponseError, match="histogram"):
        provider.collect_metric(
            metric_name="http_server_error_rate",
            service="checkout-api",
            observed_at=NOW,
            window_seconds=300,
        )

    client.close()


def test_unapproved_high_cardinality_labels_are_not_persisted() -> None:
    result = [
        {
            "metric": {
                "service": "checkout-api",
                "request_id": "SENTINEL-HIGH-CARDINALITY",
            },
            "values": [[NOW.timestamp(), "0.2"]],
        }
    ]
    provider, client = make_provider(lambda request: matrix_response(request, result))

    with pytest.raises(PrometheusProviderResponseError, match="label name") as raised:
        provider.collect_metric(
            metric_name="http_server_error_rate",
            service="checkout-api",
            observed_at=NOW,
            window_seconds=300,
        )

    assert "request_id" not in str(raised.value)
    assert "SENTINEL" not in str(raised.value)
    client.close()


@pytest.mark.parametrize(
    "result",
    [
        [
            {
                "metric": {"service": "checkout-api"},
                "values": [[NOW.timestamp(), "NaN"]],
            }
        ],
        [
            {
                "metric": {"service": "checkout-api"},
                "values": [[10**400, "0.2"]],
            }
        ],
        [
            {
                "metric": {"service": "checkout-api"},
                "values": [
                    [NOW.timestamp(), "0.2"],
                    [(NOW - timedelta(seconds=15)).timestamp(), "0.1"],
                ],
            }
        ],
        [
            {
                "metric": {"service": "checkout-api"},
                "values": [[(NOW + timedelta(seconds=1)).timestamp(), "0.2"]],
            }
        ],
        [
            {
                "metric": {"service": "checkout-api\nSENTINEL"},
                "values": [[NOW.timestamp(), "0.2"]],
            }
        ],
        [
            {"metric": {"service": "checkout-api"}, "values": []},
            {"metric": {"service": "checkout-api"}, "values": []},
        ],
    ],
)
def test_nonfinite_unordered_out_of_window_or_ambiguous_series_fail_closed(
    result: list[dict[str, object]],
) -> None:
    provider, client = make_provider(lambda request: matrix_response(request, result))

    with pytest.raises(PrometheusProviderResponseError):
        provider.collect_metric(
            metric_name="http_server_error_rate",
            service="checkout-api",
            observed_at=NOW,
            window_seconds=300,
        )

    client.close()


def test_series_sample_and_label_limits_fail_instead_of_truncating() -> None:
    cases = [
        (
            PrometheusClientLimits(max_series=1),
            [
                {"metric": {"instance": "a"}, "values": []},
                {"metric": {"instance": "b"}, "values": []},
            ],
        ),
        (
            PrometheusClientLimits(
                max_samples_per_series=2,
                max_total_samples=2,
                step_seconds=300,
            ),
            [
                {
                    "metric": {"instance": "a"},
                    "values": [
                        [(NOW - timedelta(seconds=300)).timestamp(), "0.1"],
                        [(NOW - timedelta(seconds=150)).timestamp(), "0.2"],
                        [NOW.timestamp(), "0.3"],
                    ],
                }
            ],
        ),
        (
            PrometheusClientLimits(max_labels_per_series=1),
            [
                {
                    "metric": {"service": "checkout-api", "instance": "a"},
                    "values": [],
                }
            ],
        ),
    ]
    for limits, result in cases:
        provider, client = make_provider(
            lambda request, result=result: matrix_response(request, result),
            limits=limits,
        )
        with pytest.raises(PrometheusProviderResponseError):
            provider.collect_metric(
                metric_name="http_server_error_rate",
                service="checkout-api",
                observed_at=NOW,
                window_seconds=300,
            )
        client.close()


def test_catalog_service_window_and_timezone_rejections_happen_before_io() -> None:
    called = False

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        raise AssertionError("Invalid query input must not reach the network")

    provider, client = make_provider(handler)
    with pytest.raises(PrometheusUnsupportedMetricError):
        provider.collect_metric(
            metric_name='up{job="attacker"}',
            service="checkout-api",
            observed_at=NOW,
            window_seconds=300,
        )
    with pytest.raises(PrometheusConfigurationError):
        provider.collect_metric(
            metric_name="http_server_error_rate",
            service='checkout-api"} or vector(1)',
            observed_at=NOW,
            window_seconds=300,
        )
    with pytest.raises(PrometheusConfigurationError, match="connector binding"):
        provider.collect_metric(
            metric_name="http_server_error_rate",
            service="payments-api",
            observed_at=NOW,
            window_seconds=300,
        )
    with pytest.raises(PrometheusConfigurationError):
        provider.collect_metric(
            metric_name="http_server_error_rate",
            service="checkout-api",
            observed_at=NOW,
            window_seconds=3_601,
        )
    with pytest.raises(PrometheusConfigurationError):
        provider.collect_metric(
            metric_name="http_server_error_rate",
            service="checkout-api",
            observed_at=NOW.replace(tzinfo=None),
            window_seconds=300,
        )
    with pytest.raises(PrometheusConfigurationError, match="future skew"):
        provider.collect_metric(
            metric_name="http_server_error_rate",
            service="checkout-api",
            observed_at=NOW + timedelta(minutes=5, seconds=1),
            window_seconds=300,
        )
    assert called is False
    client.close()


def test_service_layer_bundle_requires_the_sanitized_connector_source() -> None:
    provider, client = make_provider(
        lambda request: matrix_response(request, [])
    )
    result = provider.collect_metric(
        metric_name="http_server_error_rate",
        service="checkout-api",
        observed_at=NOW,
        window_seconds=300,
    )
    connector_id = UUID("22222222-2222-4222-8222-222222222222")
    values = {
        **result.model_dump(),
        "source_uri": f"prometheus://connector/{connector_id}/checkout-api",
        "connector_id": connector_id,
        "connector_version": 4,
        "credential_version": 2,
    }

    bundle = PrometheusEvidenceBundle.model_validate(values)
    assert bundle.source_uri == (
        "prometheus://connector/22222222-2222-4222-8222-222222222222/checkout-api"
    )
    assert "https://" not in bundle.model_dump_json()

    with pytest.raises(ValidationError):
        PrometheusEvidenceBundle.model_validate(
            {**values, "source_uri": "https://prometheus.example/api/v1/query_range"}
        )
    client.close()
