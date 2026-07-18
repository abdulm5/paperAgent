#!/usr/bin/env python3
"""Validate PagerAgent production configuration and live HTTP security contracts."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4

BACKEND_DIRECTORY = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIRECTORY))

from app.core.runtime_urls import (  # noqa: E402
    is_reserved_public_host,
    validate_production_database_url,
    validate_production_redis_url,
)

LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
REQUIRED_PRODUCTION_FIELDS = frozenset(
    {
        "PAGERAGENT_ENV",
        "PAGERAGENT_AUTH_MODE",
        "PAGERAGENT_SESSION_SECRET",
        "PAGERAGENT_SESSION_COOKIE_NAME",
        "PAGERAGENT_SESSION_COOKIE_SECURE",
        "PAGERAGENT_OIDC_ISSUER",
        "PAGERAGENT_OIDC_AUDIENCE",
        "PAGERAGENT_OIDC_JWKS_URL",
        "PAGERAGENT_OIDC_CLIENT_ID",
        "PAGERAGENT_OIDC_CLIENT_SECRET",
        "PAGERAGENT_OIDC_AUTHORIZATION_URL",
        "PAGERAGENT_OIDC_TOKEN_URL",
        "PAGERAGENT_OIDC_REDIRECT_URI",
        "PAGERAGENT_OIDC_FRONTEND_URL",
        "PAGERAGENT_OIDC_DEFAULT_ORGANIZATION_SLUG",
        "PAGERAGENT_OIDC_TRANSACTION_KEY",
        "PAGERAGENT_OIDC_LOGIN_COOKIE_NAME",
        "PAGERAGENT_INGEST_API_KEY",
        "PAGERAGENT_INGEST_ORGANIZATION_SLUG",
        "PAGERAGENT_CONNECTOR_CIPHER_PROVIDER",
        "PAGERAGENT_CONNECTOR_KMS_KEY_ARN",
        "PAGERAGENT_CONNECTOR_KMS_REGION",
        "PAGERAGENT_CONNECTOR_ALLOWED_ORIGINS",
        "PAGERAGENT_TELEMETRY_ALLOWED_ORIGINS",
        "GITHUB_EVIDENCE_MODE",
        "PROMETHEUS_EVIDENCE_MODE",
        "BACKEND_CORS_ORIGINS",
        "PAGERAGENT_TRUSTED_HOSTS",
        "DATABASE_URL",
        "REDIS_URL",
        "DURABLE_MITIGATION_ENABLED",
    }
)
PUBLIC_URL_FIELDS = frozenset(
    {
        "PAGERAGENT_OIDC_ISSUER",
        "PAGERAGENT_OIDC_JWKS_URL",
        "PAGERAGENT_OIDC_AUTHORIZATION_URL",
        "PAGERAGENT_OIDC_TOKEN_URL",
        "PAGERAGENT_OIDC_REDIRECT_URI",
        "PAGERAGENT_OIDC_FRONTEND_URL",
    }
)
PUBLIC_ORIGIN_LIST_FIELDS = frozenset(
    {
        "PAGERAGENT_CONNECTOR_ALLOWED_ORIGINS",
        "PAGERAGENT_TELEMETRY_ALLOWED_ORIGINS",
        "BACKEND_CORS_ORIGINS",
    }
)


class RejectRedirects(HTTPRedirectHandler):
    """Turn every redirect into an HTTPError before credentials can cross origins."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


HTTP_OPENER = build_opener(RejectRedirects)
DEVELOPMENT_VALUES = frozenset(
    {
        "pageragent-local-ingest-key",
        "pageragent-local-session-secret-change-me",
        "replace-this-before-running-outside-local-development",
        "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=",
        "fJIrHdOnYNQ6If5g9sz8nNVTsN2I6uav5FRHX24GQMs=",
    }
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default=os.getenv("PAGERAGENT_SECURITY_BASE_URL", "http://localhost:8000"),
        help="PagerAgent API origin for non-mutating live checks",
    )
    parser.add_argument(
        "--api-key",
        default=os.getenv("PAGERAGENT_INGEST_API_KEY", "pageragent-local-ingest-key"),
        help="Valid ingest key used only for a rejected malformed request",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        help="Materialized production environment file to validate without printing values",
    )
    parser.add_argument("--skip-live", action="store_true")
    parser.add_argument("--allow-remote", action="store_true")
    parser.add_argument("--timeout-seconds", type=float, default=5.0)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def parse_environment_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, raw_value = line.partition("=")
        key = key.strip()
        if not separator or not key or not key.replace("_", "").isalnum():
            raise ValueError(f"Invalid environment assignment on line {line_number}")
        value = raw_value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key in values:
            raise ValueError(f"Duplicate environment key: {key}")
        values[key] = value
    return values


def validate_production_environment(path: Path) -> dict[str, Any]:
    try:
        values = parse_environment_file(path)
    except (OSError, UnicodeError, ValueError) as error:
        return {"passed": False, "failures": [str(error)]}

    failures: list[str] = []
    missing = sorted(field for field in REQUIRED_PRODUCTION_FIELDS if not values.get(field))
    if missing:
        failures.append("Missing required fields: " + ", ".join(missing))

    weak_fields = sorted(key for key, value in values.items() if value in DEVELOPMENT_VALUES)
    if weak_fields:
        failures.append("Development values remain in: " + ", ".join(weak_fields))
    unresolved_fields = sorted(
        key
        for key, value in values.items()
        if any(marker in value for marker in ("${", "{{", "REPLACE_ME", "CHANGE_ME"))
    )
    if unresolved_fields:
        failures.append(
            "Unresolved secret or template references remain in: " + ", ".join(unresolved_fields)
        )
    if values.get("PAGERAGENT_ENV", "").strip().lower() in {"", "local", "test"}:
        failures.append("PAGERAGENT_ENV must identify a hosted environment")
    if values.get("PAGERAGENT_AUTH_MODE") != "oidc":
        failures.append("PAGERAGENT_AUTH_MODE must be oidc")
    if values.get("PAGERAGENT_CONNECTOR_CIPHER_PROVIDER") != "aws_kms":
        failures.append("PAGERAGENT_CONNECTOR_CIPHER_PROVIDER must be aws_kms")
    if values.get("PAGERAGENT_SESSION_COOKIE_SECURE", "").lower() != "true":
        failures.append("PAGERAGENT_SESSION_COOKIE_SECURE must be true")
    if values.get("PAGERAGENT_CONNECTOR_KMS_ENDPOINT_URL"):
        failures.append("PAGERAGENT_CONNECTOR_KMS_ENDPOINT_URL must be unset in production")
    if values.get("PAGERAGENT_CONNECTOR_DECRYPTION_KEYS", "{}").strip() not in {"", "{}"}:
        failures.append("PAGERAGENT_CONNECTOR_DECRYPTION_KEYS must be empty with KMS custody")
    if values.get("DURABLE_MITIGATION_ENABLED", "").lower() != "false":
        failures.append(
            "DURABLE_MITIGATION_ENABLED must remain false without a production action adapter"
        )
    session_secret = values.get("PAGERAGENT_SESSION_SECRET")
    if session_secret and session_secret == values.get("PAGERAGENT_INGEST_API_KEY"):
        failures.append("PAGERAGENT_SESSION_SECRET and PAGERAGENT_INGEST_API_KEY must be distinct")

    try:
        validate_production_database_url(values.get("DATABASE_URL", ""))
    except ValueError as error:
        failures.append(str(error))

    try:
        validate_production_redis_url(values.get("REDIS_URL", ""))
    except ValueError as error:
        failures.append(str(error))

    for field in sorted(PUBLIC_URL_FIELDS):
        value = values.get(field, "")
        try:
            hostname = urlsplit(value).hostname or ""
        except ValueError:
            continue
        if hostname and is_reserved_public_host(hostname):
            failures.append(f"{field} must use a non-reserved public host")
    for field in sorted(PUBLIC_ORIGIN_LIST_FIELDS):
        candidates = [value.strip() for value in values.get(field, "").split(",")]
        for candidate in candidates:
            try:
                hostname = urlsplit(candidate).hostname or ""
            except ValueError:
                continue
            if hostname and is_reserved_public_host(hostname):
                failures.append(f"{field} must use non-reserved public hosts")
                break
    trusted_hosts = [
        host.strip() for host in values.get("PAGERAGENT_TRUSTED_HOSTS", "").split(",")
        if host.strip()
    ]
    if any(host and is_reserved_public_host(host) for host in trusted_hosts):
        failures.append("PAGERAGENT_TRUSTED_HOSTS must use non-reserved public hosts")
    frontend_host = (
        urlsplit(values.get("PAGERAGENT_OIDC_FRONTEND_URL", "")).hostname or ""
    ).lower()
    if frontend_host and trusted_hosts != [frontend_host]:
        failures.append(
            "PAGERAGENT_TRUSTED_HOSTS must contain only the exact OIDC frontend hostname"
        )

    child_environment = {
        **values,
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(BACKEND_DIRECTORY),
        "PYTHONNOUSERSITE": "1",
    }
    try:
        validation = subprocess.run(
            [
                sys.executable,
                "-c",
                "from app.core.config import Settings; Settings()",
            ],
            cwd=BACKEND_DIRECTORY,
            env=child_environment,
            check=False,
            capture_output=True,
            text=False,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        validation = None
    if validation is None or validation.returncode != 0:
        failures.append(
            "Application Settings rejected the production environment; inspect it in a "
            "secure shell because secret-bearing validation output is intentionally hidden"
        )
    return {
        "passed": not failures,
        "checked_field_count": len(values),
        "environment": values.get("PAGERAGENT_ENV", "unknown"),
        "failures": failures,
    }


def request(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    timeout_seconds: float,
) -> tuple[int | None, dict[str, str], bytes, str | None]:
    outbound = Request(url, data=body, method=method, headers=headers or {})
    try:
        with HTTP_OPENER.open(outbound, timeout=timeout_seconds) as response:
            return (
                response.status,
                {key.lower(): value for key, value in response.headers.items()},
                response.read(1_048_577),
                None,
            )
    except HTTPError as error:
        return (
            error.code,
            {key.lower(): value for key, value in error.headers.items()},
            error.read(1_048_577),
            None,
        )
    except (OSError, URLError) as error:
        return None, {}, b"", type(error).__name__


def valid_alert(marker: str) -> bytes:
    detected_at = datetime.now(UTC)
    started_at = detected_at - timedelta(minutes=2)
    return json.dumps(
        {
            "fingerprint": f"security-gate:{marker}",
            "source": "pageragent-security-gate",
            "service": "checkout-api",
            "severity": "high",
            "summary": "Synthetic request that authentication must reject",
            "started_at": started_at.isoformat(),
            "detected_at": detected_at.isoformat(),
            "metric": {
                "name": "http_server_error_rate",
                "value": 0.1,
                "threshold": 0.05,
                "window_seconds": 300,
                "request_count": 100,
                "failed_request_count": 10,
            },
            "release": {
                "name": "security-gate",
                "commit_sha": "0000000",
                "deployed_at": started_at.isoformat(),
            },
            "telemetry_url": "http://checkout-api:8100/metrics",
        },
        separators=(",", ":"),
    ).encode()


def run_live_checks(
    base_url: str,
    api_key: str,
    timeout_seconds: float,
    *,
    expected_environment: str | None = None,
) -> dict[str, Any]:
    marker = f"security-canary-{uuid4().hex}"
    checks: dict[str, bool] = {}
    failures: list[str] = []

    health_status, health_headers, health_body, health_error = request(
        f"{base_url}/api/v1/health",
        timeout_seconds=timeout_seconds,
    )
    try:
        health_document = json.loads(health_body)
    except (UnicodeError, json.JSONDecodeError):
        health_document = {}
    if not isinstance(health_document, dict):
        health_document = {}
    checks["health_contract"] = (
        health_error is None
        and health_status == 200
        and health_document.get("status") == "ok"
        and (
            expected_environment is None
            or health_document.get("environment") == expected_environment
        )
    )
    checks["baseline_security_headers"] = (
        health_headers.get("x-content-type-options") == "nosniff"
        and health_headers.get("x-frame-options") == "DENY"
        and health_headers.get("referrer-policy") == "no-referrer"
        and "camera=()" in health_headers.get("permissions-policy", "")
    )
    hostile_host_status, _, hostile_host_body, hostile_host_error = request(
        f"{base_url}/api/v1/health",
        headers={"Host": "attacker.invalid"},
        timeout_seconds=timeout_seconds,
    )
    checks["untrusted_host_is_denied"] = (
        hostile_host_error is None
        and hostile_host_status == 400
        and b"attacker.invalid" not in hostile_host_body
    )
    hosted_environment = (
        expected_environment not in {"local", "test"}
        if expected_environment is not None
        else health_document.get("environment") not in {"local", "test"}
    )
    checks["hosted_transport_and_content_policy"] = not hosted_environment or (
        urlsplit(base_url).scheme == "https"
        and health_headers.get("strict-transport-security")
        == "max-age=31536000; includeSubDomains"
        and "default-src 'none'" in health_headers.get("content-security-policy", "")
    )
    live_status, live_headers, live_body, live_error = request(
        f"{base_url}/api/v1/health/live",
        timeout_seconds=timeout_seconds,
    )
    try:
        live_document = json.loads(live_body)
    except (UnicodeError, json.JSONDecodeError):
        live_document = {}
    if not isinstance(live_document, dict):
        live_document = {}
    checks["liveness_contract"] = (
        live_error is None
        and live_status == 200
        and live_document.get("status") == "alive"
        and live_headers.get("cache-control") == "no-store"
    )

    ready_status, ready_headers, ready_body, ready_error = request(
        f"{base_url}/api/v1/health/ready",
        timeout_seconds=timeout_seconds,
    )
    try:
        ready_document = json.loads(ready_body)
    except (UnicodeError, json.JSONDecodeError):
        ready_document = {}
    if not isinstance(ready_document, dict):
        ready_document = {}
    ready_checks = ready_document.get("checks")
    checks["database_and_schema_readiness_contract"] = (
        ready_error is None
        and ready_status == 200
        and ready_document.get("status") == "ready"
        and isinstance(ready_checks, dict)
        and ready_checks.get("database") == "ok"
        and ready_checks.get("schema") in {"current", "forward_compatible"}
        and ready_headers.get("cache-control") == "no-store"
    )

    docs_status, _, _, _ = request(
        f"{base_url}/docs",
        timeout_seconds=timeout_seconds,
    )
    checks["interactive_docs_disabled_when_hosted"] = not hosted_environment or docs_status == 404

    protected_status, protected_headers, protected_body, _ = request(
        f"{base_url}/api/v1/incidents",
        timeout_seconds=timeout_seconds,
    )
    checks["protected_route_requires_authentication"] = (
        protected_status == 401
        and protected_headers.get("www-authenticate") == "Bearer"
        and marker.encode() not in protected_body
    )

    invalid_key = f"invalid-{marker}"
    ingest_status, _, ingest_body, _ = request(
        f"{base_url}/api/v1/alerts",
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-PagerAgent-Ingest-Key": invalid_key,
        },
        body=valid_alert(marker),
        timeout_seconds=timeout_seconds,
    )
    checks["invalid_ingest_key_is_rejected_without_reflection"] = (
        ingest_status == 401
        and invalid_key.encode() not in ingest_body
        and marker.encode() not in ingest_body
    )

    malformed_body = json.dumps({"fingerprint": marker, "attacker_controlled": marker}).encode()
    malformed_status, _, malformed_response, _ = request(
        f"{base_url}/api/v1/alerts",
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-PagerAgent-Ingest-Key": api_key,
        },
        body=malformed_body,
        timeout_seconds=timeout_seconds,
    )
    try:
        malformed_document = json.loads(malformed_response)
    except (UnicodeError, json.JSONDecodeError):
        malformed_document = {}
    checks["validation_errors_are_sanitized"] = (
        malformed_status == 422
        and malformed_document == {"detail": "Request validation failed"}
        and marker.encode() not in malformed_response
    )

    cors_status, cors_headers, _, _ = request(
        f"{base_url}/api/v1/incidents",
        method="OPTIONS",
        headers={
            "Origin": "https://attacker.invalid",
            "Access-Control-Request-Method": "GET",
        },
        timeout_seconds=timeout_seconds,
    )
    checks["untrusted_cors_origin_is_denied"] = (
        cors_status in {400, 403} and "access-control-allow-origin" not in cors_headers
    )

    for name, passed in checks.items():
        if not passed:
            failures.append(name)
    return {"passed": not failures, "checks": checks, "failures": failures}


def validate_base_url(
    base_url: str,
    allow_remote: bool,
    expected_environment: str | None = None,
) -> str:
    parsed = urlsplit(base_url)
    try:
        parsed.port
    except ValueError as error:
        raise ValueError("--base-url contains an invalid port") from error
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or base_url.rstrip("/") != f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
    ):
        raise ValueError("--base-url must be an absolute HTTP(S) origin")
    remote_target = parsed.hostname not in LOOPBACK_HOSTS
    if remote_target and not allow_remote:
        raise ValueError("Refusing a remote security target without --allow-remote")
    if remote_target and parsed.scheme != "https":
        raise ValueError("Remote security targets must use HTTPS")
    if remote_target and is_reserved_public_host(parsed.hostname):
        raise ValueError("Remote security targets must use a real, non-reserved host")
    if (
        expected_environment is not None
        and expected_environment.strip().lower() not in {"local", "test"}
        and parsed.scheme != "https"
    ):
        raise ValueError("Hosted environment live checks must use HTTPS")
    return base_url.rstrip("/")


def main() -> int:
    args = parse_args()
    if args.skip_live and args.env_file is None:
        print("configuration error: --skip-live requires --env-file", file=sys.stderr)
        return 2
    if not 0 < args.timeout_seconds <= 30:
        print("configuration error: timeout must be greater than 0 and at most 30", file=sys.stderr)
        return 2
    if not args.skip_live and not args.api_key:
        print("configuration error: a live ingest API key is required", file=sys.stderr)
        return 2

    report: dict[str, Any] = {
        "schema_version": "pageragent.security-gate.v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "source_revision": source_revision(),
        "source_dirty": source_dirty(),
    }
    expected_environment: str | None = None
    if args.env_file is not None:
        production_environment = validate_production_environment(args.env_file)
        report["production_environment"] = production_environment
        expected_environment = str(production_environment.get("environment", "")) or None
    if not args.skip_live:
        try:
            base_url = validate_base_url(
                args.base_url,
                args.allow_remote,
                expected_environment,
            )
        except ValueError as error:
            print(f"configuration error: {error}", file=sys.stderr)
            return 2
        report["live_api"] = run_live_checks(
            base_url,
            args.api_key,
            args.timeout_seconds,
            expected_environment=expected_environment,
        )

    sections = [value for value in report.values() if isinstance(value, dict)]
    report["passed"] = all(section.get("passed", True) for section in sections)
    rendered = json.dumps(report, indent=2, sort_keys=True)
    print(rendered)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(f"{rendered}\n", encoding="utf-8")
    return 0 if report["passed"] else 1


def source_revision() -> str:
    root = Path(__file__).resolve().parents[1]
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    revision = completed.stdout.strip()
    return revision if completed.returncode == 0 and len(revision) == 40 else "unknown"


def source_dirty() -> bool | None:
    root = Path(__file__).resolve().parents[1]
    try:
        completed = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return bool(completed.stdout.strip()) if completed.returncode == 0 else None


if __name__ == "__main__":
    raise SystemExit(main())
