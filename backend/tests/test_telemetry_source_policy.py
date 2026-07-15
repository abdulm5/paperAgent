import socket
from typing import Any

import httpx
import pytest

from app.investigation.collectors import (
    HttpTelemetryCollector,
    TelemetrySourcePolicy,
    TelemetrySourceRejectedError,
)


def resolver_for(*addresses: str):
    def resolve(
        _host: str,
        port: int,
        _family: int,
        _socket_type: int,
    ) -> list[tuple[Any, ...]]:
        return [
            (
                socket.AF_INET6 if ":" in address else socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                (
                    (address, port, 0, 0)
                    if ":" in address
                    else (address, port)
                ),
            )
            for address in addresses
        ]

    return resolve


def json_client(payload: dict[str, object]) -> httpx.Client:
    return httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=payload, request=request)
        )
    )


def test_collector_fetches_only_an_exact_public_allowlisted_origin() -> None:
    with json_client({"service": "checkout-api"}) as client:
        collector = HttpTelemetryCollector(
            allowed_origins="https://telemetry.example, https://other.example:8443",
            resolver=resolver_for("1.1.1.1", "2606:4700:4700::1111"),
            client=client,
        )

        payload = collector.collect("https://telemetry.example/v1/snapshot?window=300")

    assert payload == {"service": "checkout-api"}


def test_unconfigured_origin_and_port_are_rejected_before_network_io() -> None:
    dns_called = False
    request_called = False

    def resolve(*_args: object) -> list[tuple[Any, ...]]:
        nonlocal dns_called
        dns_called = True
        return resolver_for("1.1.1.1")("ignored", 443, 0, 0)

    def request(_request: httpx.Request) -> httpx.Response:
        nonlocal request_called
        request_called = True
        raise AssertionError("The rejected source must not reach HTTP")

    with httpx.Client(transport=httpx.MockTransport(request)) as client:
        collector = HttpTelemetryCollector(
            allowed_origins="https://telemetry.example",
            resolver=resolve,
            client=client,
        )
        with pytest.raises(TelemetrySourceRejectedError, match="not server allow-listed"):
            collector.collect("https://attacker.example/snapshot")
        with pytest.raises(TelemetrySourceRejectedError, match="not server allow-listed"):
            collector.collect("https://telemetry.example:444/snapshot")

    assert dns_called is False
    assert request_called is False


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("file:///etc/passwd", "HTTP or HTTPS"),
        ("https://user:secret@telemetry.example/data", "cannot contain credentials"),
        ("https://telemetry.example/data#internal", "cannot contain a fragment"),
        ("https://bad_host.example/data", "invalid host"),
        ("https://telemetry.example:0/data", "invalid port"),
    ],
)
def test_ambiguous_or_unsafe_urls_are_rejected(source: str, message: str) -> None:
    policy = TelemetrySourcePolicy("https://telemetry.example")

    with pytest.raises(TelemetrySourceRejectedError, match=message):
        policy.validate(source)


@pytest.mark.parametrize(
    "addresses",
    [
        ("127.0.0.1",),
        ("10.20.30.40",),
        ("169.254.169.254",),
        ("0.0.0.0",),
        ("224.0.0.1",),
        ("1.1.1.1", "10.20.30.40"),
        ("::1",),
        ("fe80::1",),
    ],
)
def test_production_policy_rejects_every_non_public_dns_answer(
    addresses: tuple[str, ...],
) -> None:
    policy = TelemetrySourcePolicy(
        "https://telemetry.example",
        resolver=resolver_for(*addresses),
    )

    with pytest.raises(TelemetrySourceRejectedError, match="address"):
        policy.validate("https://telemetry.example/snapshot")


def test_local_mode_requires_an_explicit_origin_but_allows_private_docker_dns() -> None:
    with json_client({"service": "checkout-api"}) as client:
        collector = HttpTelemetryCollector(
            allowed_origins="http://checkout-api:8100",
            allow_private_networks=True,
            resolver=resolver_for("172.20.0.7"),
            client=client,
        )

        payload = collector.collect("http://checkout-api:8100/telemetry")

    assert payload["service"] == "checkout-api"


@pytest.mark.parametrize("address", ["127.0.0.1", "::1"])
def test_local_explicit_allowlist_can_use_loopback(address: str) -> None:
    rendered = f"[{address}]" if ":" in address else address
    policy = TelemetrySourcePolicy(
        f"http://{rendered}:8100",
        allow_private_networks=True,
    )

    validated = policy.validate(f"http://{rendered}:8100/telemetry")

    assert validated.path == "/telemetry"


def test_local_private_exception_never_allows_link_local_metadata() -> None:
    policy = TelemetrySourcePolicy(
        "http://metadata.internal",
        allow_private_networks=True,
        resolver=resolver_for("169.254.169.254"),
    )

    with pytest.raises(TelemetrySourceRejectedError, match="forbidden address"):
        policy.validate("http://metadata.internal/latest/meta-data")


def test_redirect_is_rejected_without_following_its_internal_location() -> None:
    requested_urls: list[str] = []

    def redirect(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        return httpx.Response(
            302,
            headers={"Location": "http://127.0.0.1/admin"},
            request=request,
        )

    with httpx.Client(
        transport=httpx.MockTransport(redirect),
        follow_redirects=True,
    ) as client:
        collector = HttpTelemetryCollector(
            allowed_origins="https://telemetry.example",
            resolver=resolver_for("1.1.1.1"),
            client=client,
        )

        with pytest.raises(TelemetrySourceRejectedError, match="redirects are not allowed"):
            collector.collect("https://telemetry.example/snapshot")

    assert requested_urls == ["https://telemetry.example/snapshot"]


def test_configured_allowlist_entries_must_be_origins() -> None:
    with pytest.raises(ValueError, match="must be origins"):
        TelemetrySourcePolicy("https://telemetry.example/private/path")
