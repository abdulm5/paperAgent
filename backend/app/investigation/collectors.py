import re
import socket
from collections.abc import Callable, Iterable
from ipaddress import IPv4Address, IPv6Address, ip_address
from typing import Any, Protocol

import httpx

ResolvedAddress = IPv4Address | IPv6Address
Resolver = Callable[..., list[tuple[Any, ...]]]


class TelemetrySourceRejectedError(ValueError):
    """Raised before I/O when a telemetry URL crosses the configured network boundary."""


class TelemetrySourcePolicy:
    """Allow only server-configured telemetry origins with safe DNS answers."""

    _HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", re.IGNORECASE)

    def __init__(
        self,
        allowed_origins: str | Iterable[str],
        *,
        allow_private_networks: bool = False,
        resolver: Resolver = socket.getaddrinfo,
    ) -> None:
        configured = (
            allowed_origins.split(",")
            if isinstance(allowed_origins, str)
            else list(allowed_origins)
        )
        self.allowed_origins = frozenset(
            self._parse_configured_origin(value.strip())
            for value in configured
            if value.strip()
        )
        self.allow_private_networks = allow_private_networks
        self.resolver = resolver

    def validate(self, source_uri: str) -> httpx.URL:
        url, host, port, origin = self._parse_url(source_uri)
        if origin not in self.allowed_origins:
            raise TelemetrySourceRejectedError(
                f"Telemetry origin {origin!r} is not server allow-listed"
            )
        addresses = self._resolve(host, port)
        for address in addresses:
            self._validate_address(address)
        return url

    def _parse_configured_origin(self, raw_origin: str) -> str:
        url, _, _, origin = self._parse_url(raw_origin)
        if url.path not in {"", "/"} or url.query or url.fragment:
            raise ValueError(
                "PAGERAGENT_TELEMETRY_ALLOWED_ORIGINS entries must be origins without "
                "paths, queries, or fragments"
            )
        return origin

    @classmethod
    def _parse_url(cls, raw_url: str) -> tuple[httpx.URL, str, int, str]:
        try:
            url = httpx.URL(raw_url)
        except (TypeError, httpx.InvalidURL) as error:
            raise TelemetrySourceRejectedError("Telemetry source is not a valid URL") from error
        if url.scheme not in {"http", "https"}:
            raise TelemetrySourceRejectedError("Telemetry source must use HTTP or HTTPS")
        if url.userinfo:
            raise TelemetrySourceRejectedError("Telemetry source cannot contain credentials")
        if url.fragment:
            raise TelemetrySourceRejectedError("Telemetry source cannot contain a fragment")
        if not url.host:
            raise TelemetrySourceRejectedError("Telemetry source must include a host")

        host = cls._canonical_host(url.host)
        port = url.port
        if port is None:
            port = 443 if url.scheme == "https" else 80
        if not 1 <= port <= 65_535:
            raise TelemetrySourceRejectedError("Telemetry source contains an invalid port")
        rendered_host = f"[{host}]" if ":" in host else host
        default_port = 443 if url.scheme == "https" else 80
        rendered_port = "" if port == default_port else f":{port}"
        return url, host, port, f"{url.scheme}://{rendered_host}{rendered_port}"

    @classmethod
    def _canonical_host(cls, raw_host: str) -> str:
        host = raw_host.rstrip(".").lower()
        if not host or "%" in host:
            raise TelemetrySourceRejectedError("Telemetry source contains an invalid host")
        try:
            return str(ip_address(host))
        except ValueError:
            try:
                ascii_host = host.encode("idna").decode("ascii")
            except UnicodeError as error:
                raise TelemetrySourceRejectedError(
                    "Telemetry source contains an invalid host"
                ) from error
            labels = ascii_host.split(".")
            if len(ascii_host) > 253 or any(
                not label or cls._HOST_LABEL.fullmatch(label) is None for label in labels
            ):
                raise TelemetrySourceRejectedError(
                    "Telemetry source contains an invalid host"
                )
            return ascii_host.lower()

    def _resolve(self, host: str, port: int) -> frozenset[ResolvedAddress]:
        try:
            literal = ip_address(host)
        except ValueError:
            try:
                results = self.resolver(
                    host,
                    port,
                    socket.AF_UNSPEC,
                    socket.SOCK_STREAM,
                )
            except OSError as error:
                raise TelemetrySourceRejectedError(
                    f"Telemetry host {host!r} could not be resolved"
                ) from error
            addresses: set[ResolvedAddress] = set()
            for result in results:
                try:
                    sockaddr = result[4]
                    addresses.add(ip_address(str(sockaddr[0]).split("%", 1)[0]))
                except (IndexError, TypeError, ValueError) as error:
                    raise TelemetrySourceRejectedError(
                        f"Telemetry host {host!r} returned an invalid DNS answer"
                    ) from error
            if not addresses:
                raise TelemetrySourceRejectedError(
                    f"Telemetry host {host!r} did not resolve to an address"
                )
            return frozenset(addresses)
        return frozenset({literal})

    def _validate_address(self, address: ResolvedAddress) -> None:
        # Link-local and non-routable special ranges stay forbidden even in local
        # mode. Local/test may explicitly allow-list loopback or private Docker
        # origins, but production accepts only globally routable DNS answers.
        if self.allow_private_networks and address.is_loopback:
            return
        if (
            address.is_link_local
            or address.is_multicast
            or address.is_unspecified
            or address.is_reserved
        ):
            raise TelemetrySourceRejectedError(
                f"Telemetry host resolves to forbidden address {address}"
            )
        if address.is_global:
            return
        if self.allow_private_networks and address.is_private:
            return
        raise TelemetrySourceRejectedError(
            f"Telemetry host resolves to non-public address {address}"
        )


class TelemetryCollector(Protocol):
    version: str

    def collect(self, source_uri: str) -> dict[str, Any]: ...


class HttpTelemetryCollector:
    version = "http-telemetry-v1"

    def __init__(
        self,
        timeout_seconds: float = 5.0,
        *,
        allowed_origins: str | Iterable[str] = (),
        allow_private_networks: bool = False,
        resolver: Resolver = socket.getaddrinfo,
        client: httpx.Client | None = None,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.policy = TelemetrySourcePolicy(
            allowed_origins,
            allow_private_networks=allow_private_networks,
            resolver=resolver,
        )
        self.client = client

    def collect(self, source_uri: str) -> dict[str, Any]:
        validated_url = self.policy.validate(source_uri)
        if self.client is None:
            # Environment proxy variables could resolve a validated hostname on
            # another network boundary, so production collection ignores them.
            with httpx.Client(
                timeout=self.timeout_seconds,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                response = client.get(validated_url, follow_redirects=False)
        else:
            response = self.client.get(
                validated_url,
                timeout=self.timeout_seconds,
                follow_redirects=False,
            )
        if 300 <= response.status_code < 400:
            raise TelemetrySourceRejectedError("Telemetry source redirects are not allowed")
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Telemetry source must return a JSON object")
        return payload


class StaticTelemetryCollector:
    version = "static-telemetry-v1"

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def collect(self, source_uri: str) -> dict[str, Any]:
        del source_uri
        return self.payload
