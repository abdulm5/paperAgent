"""Pure configuration checks for production data-store connection URLs.

This module deliberately uses only the Python standard library so release tooling can
reuse the exact runtime policy without importing the application or installing its
database drivers.
"""

from __future__ import annotations

import re
from ipaddress import ip_address
from urllib.parse import SplitResult, parse_qsl, urlsplit

POSTGRES_TLS_MODES = frozenset({"require", "verify-ca", "verify-full"})
POSTGRES_QUERY_OVERRIDES = frozenset(
    {
        "dbname",
        "host",
        "hostaddr",
        "password",
        "port",
        "service",
        "servicefile",
        "user",
    }
)
REDIS_TLS_CERT_REQUIREMENTS = frozenset({"required"})
REDIS_QUERY_OVERRIDES = frozenset({"host", "port"})
RESERVED_PUBLIC_HOSTS = frozenset(
    {
        "example",
        "example.com",
        "example.net",
        "example.org",
        "invalid",
        "local",
        "localhost",
        "test",
    }
)
RESERVED_PUBLIC_HOST_SUFFIXES = (
    ".example",
    ".example.com",
    ".example.net",
    ".example.org",
    ".invalid",
    ".local",
    ".localhost",
    ".test",
)
DNS_HOST_PATTERN = re.compile(
    r"(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
)


def is_reserved_public_host(hostname: str) -> bool:
    """Return whether a host is reserved for examples, tests, or local naming."""

    normalized = hostname.rstrip(".").lower()
    return normalized in RESERVED_PUBLIC_HOSTS or normalized.endswith(
        RESERVED_PUBLIC_HOST_SUFFIXES
    )


def _is_local_host(hostname: str) -> bool:
    normalized = hostname.rstrip(".").lower()
    if normalized in {"localhost", "postgres", "redis"} or normalized.endswith(
        ".localhost"
    ):
        return True
    try:
        return ip_address(normalized).is_loopback
    except ValueError:
        return False


def _is_canonical_host(hostname: str) -> bool:
    """Accept only literal IPs or ASCII DNS names shared parsers interpret equally."""

    if "%" in hostname:
        return False
    try:
        ip_address(hostname)
    except ValueError:
        return DNS_HOST_PATTERN.fullmatch(hostname) is not None
    return True


def _query_values(query: str, *, setting: str) -> dict[str, list[str]]:
    try:
        pairs = parse_qsl(query, keep_blank_values=True, strict_parsing=True)
    except ValueError as error:
        raise ValueError(f"{setting} contains an invalid query string") from error

    values: dict[str, list[str]] = {}
    for key, value in pairs:
        if not key or key != key.lower():
            raise ValueError(f"{setting} query parameter names must be lowercase")
        values.setdefault(key, []).append(value)
    return values


def _validate_origin_shape(url: str, *, setting: str) -> tuple[SplitResult, str]:
    parsed = urlsplit(url)
    try:
        parsed.port
    except ValueError as error:
        raise ValueError(f"{setting} contains an invalid port") from error
    hostname = (parsed.hostname or "").rstrip(".").lower()
    if (
        not hostname
        or "," in hostname
        or any(character.isspace() for character in hostname)
        or parsed.fragment
    ):
        raise ValueError(f"{setting} must contain one exact data-store host")
    if not _is_canonical_host(hostname):
        raise ValueError(f"{setting} host must be a canonical DNS name or IP address")
    if _is_local_host(hostname):
        raise ValueError(f"{setting} must use a non-local endpoint")
    if is_reserved_public_host(hostname):
        raise ValueError(f"{setting} must use a non-reserved public endpoint")
    return parsed, hostname


def validate_production_database_url(url: str) -> None:
    """Require one non-local psycopg target and one effective secure TLS mode.

    SQLAlchemy passes PostgreSQL URI query parameters through to libpq, where query
    values such as ``host`` or the last duplicate ``sslmode`` can override the URI
    authority. Rejecting those alternate sources makes the validated target the
    one the driver will actually use.
    """

    parsed, _ = _validate_origin_shape(url, setting="DATABASE_URL")
    if parsed.scheme != "postgresql+psycopg":
        raise ValueError("DATABASE_URL must use postgresql+psycopg")

    query = _query_values(parsed.query, setting="DATABASE_URL")
    overrides = sorted(POSTGRES_QUERY_OVERRIDES & query.keys())
    if overrides:
        raise ValueError(
            "DATABASE_URL must not override its authority through query parameters: "
            + ", ".join(overrides)
        )
    ssl_modes = query.get("sslmode", [])
    if len(ssl_modes) != 1 or ssl_modes[0] not in POSTGRES_TLS_MODES:
        raise ValueError(
            "DATABASE_URL must contain exactly one secure sslmode "
            "(require, verify-ca, or verify-full)"
        )


def validate_production_redis_url(url: str) -> None:
    """Require Redis TLS with peer and hostname verification that cannot be weakened."""

    parsed, _ = _validate_origin_shape(url, setting="REDIS_URL")
    if parsed.scheme != "rediss":
        raise ValueError("REDIS_URL must use rediss")

    query = _query_values(parsed.query, setting="REDIS_URL")
    overrides = sorted(REDIS_QUERY_OVERRIDES & query.keys())
    if overrides:
        raise ValueError(
            "REDIS_URL must not override its authority through query parameters: "
            + ", ".join(overrides)
        )
    for key, values in query.items():
        if len(values) != 1:
            raise ValueError(f"REDIS_URL must not repeat query parameter {key}")

    certificate_requirements = query.get("ssl_cert_reqs")
    if (
        certificate_requirements is not None
        and certificate_requirements[0].lower() not in REDIS_TLS_CERT_REQUIREMENTS
    ):
        raise ValueError("REDIS_URL must not weaken TLS certificate verification")
    hostname_checks = query.get("ssl_check_hostname")
    if hostname_checks != ["true"]:
        raise ValueError("REDIS_URL must set ssl_check_hostname=true")
