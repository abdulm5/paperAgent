#!/usr/bin/env python3
"""Bounded HTTP load gate for PagerAgent alert ingestion.

The gate targets loopback by default because it creates real incident records. Pass
--allow-remote only when the target environment is an explicitly approved test stack.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
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

from app.core.runtime_urls import is_reserved_public_host  # noqa: E402

LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class RejectRedirects(HTTPRedirectHandler):
    """Turn every redirect into an HTTPError before the ingest key can be forwarded."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


HTTP_OPENER = build_opener(RejectRedirects)


@dataclass(frozen=True)
class RequestResult:
    fingerprint: str
    status: int | None
    latency_ms: float
    deduplicated: bool | None
    incident_id: str | None
    error_type: str | None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default=os.getenv("PAGERAGENT_LOAD_BASE_URL", "http://localhost:8000"),
        help="PagerAgent API origin (default: http://localhost:8000)",
    )
    parser.add_argument(
        "--api-key",
        default=os.getenv("PAGERAGENT_INGEST_API_KEY", "pageragent-local-ingest-key"),
        help="Alert ingest key; defaults to PAGERAGENT_INGEST_API_KEY",
    )
    parser.add_argument(
        "--telemetry-url",
        default=os.getenv(
            "PAGERAGENT_LOAD_TELEMETRY_URL",
            "http://checkout-api:8100/metrics",
        ),
        help="Evidence URL stored on synthetic alerts; remote runs require HTTPS",
    )
    parser.add_argument("--requests", type=int, default=200)
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument(
        "--unique-fingerprints",
        type=int,
        default=40,
        help="Number of incident keys shared across requests to exercise deduplication",
    )
    parser.add_argument("--timeout-seconds", type=float, default=10.0)
    parser.add_argument("--max-error-rate", type=float, default=0.01)
    parser.add_argument("--max-p95-ms", type=float, default=1_500.0)
    parser.add_argument("--min-throughput-rps", type=float, default=5.0)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--allow-remote",
        action="store_true",
        help="Allow a non-loopback target that will receive synthetic incidents",
    )
    return parser.parse_args()


def validate_args(args: argparse.Namespace) -> str:
    parsed = urlsplit(args.base_url)
    try:
        parsed.port
    except ValueError as error:
        raise ValueError("--base-url contains an invalid port") from error
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("--base-url must be an absolute HTTP(S) origin")
    canonical_origin = f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
    if (
        parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or args.base_url.rstrip("/") != canonical_origin
    ):
        raise ValueError("--base-url must not include a path, query, or fragment")
    remote_target = parsed.hostname not in LOOPBACK_HOSTS
    if remote_target and not args.allow_remote:
        raise ValueError(
            "Refusing to create load-test incidents on a remote host; pass --allow-remote "
            "only for an approved test environment"
        )
    if remote_target and parsed.scheme != "https":
        raise ValueError("Remote load targets must use HTTPS")
    if remote_target and is_reserved_public_host(parsed.hostname):
        raise ValueError("Remote load targets must use a real, non-reserved host")

    telemetry = urlsplit(args.telemetry_url)
    try:
        telemetry.port
    except ValueError as error:
        raise ValueError("--telemetry-url contains an invalid port") from error
    if (
        telemetry.scheme not in {"http", "https"}
        or not telemetry.hostname
        or telemetry.username is not None
        or telemetry.password is not None
        or telemetry.query
        or telemetry.fragment
    ):
        raise ValueError("--telemetry-url must be an absolute HTTP(S) URL without credentials")
    if remote_target and telemetry.scheme != "https":
        raise ValueError("Remote load evidence must use an allow-listed HTTPS telemetry URL")
    if remote_target and is_reserved_public_host(telemetry.hostname):
        raise ValueError("Remote load evidence must use a real, non-reserved telemetry host")
    if not args.api_key:
        raise ValueError("An ingest API key is required")
    if not 1 <= args.requests <= 10_000:
        raise ValueError("--requests must be between 1 and 10000")
    if not 1 <= args.concurrency <= min(args.requests, 256):
        raise ValueError("--concurrency must be between 1 and min(requests, 256)")
    if not 1 <= args.unique_fingerprints <= args.requests:
        raise ValueError("--unique-fingerprints must be between 1 and requests")
    if not 0 < args.timeout_seconds <= 60:
        raise ValueError("--timeout-seconds must be greater than 0 and at most 60")
    if not 0 <= args.max_error_rate <= 1:
        raise ValueError("--max-error-rate must be between 0 and 1")
    if args.max_p95_ms <= 0 or args.min_throughput_rps <= 0:
        raise ValueError("Latency and throughput thresholds must be positive")
    return args.base_url.rstrip("/")


def build_payload(
    run_id: str,
    bucket: int,
    detected_at: datetime,
    telemetry_url: str,
) -> tuple[str, bytes]:
    started_at = detected_at - timedelta(minutes=2)
    fingerprint = f"pageragent-release-load:{run_id}:{bucket}"
    payload = {
        "fingerprint": fingerprint,
        "source": "pageragent-release-load-gate",
        "service": "checkout-api",
        "severity": "high",
        "summary": "Synthetic release-gate error-rate alert",
        "started_at": started_at.isoformat(),
        "detected_at": detected_at.isoformat(),
        "metric": {
            "name": "http_server_error_rate",
            "value": 0.12,
            "threshold": 0.05,
            "window_seconds": 300,
            "request_count": 1_000,
            "failed_request_count": 120,
        },
        "release": {
            "name": "release-load-gate",
            "commit_sha": "0000000",
            "deployed_at": started_at.isoformat(),
        },
        "telemetry_url": telemetry_url,
    }
    return fingerprint, json.dumps(payload, separators=(",", ":")).encode()


def send_alert(
    endpoint: str,
    api_key: str,
    fingerprint: str,
    payload: bytes,
    timeout_seconds: float,
) -> RequestResult:
    request = Request(
        endpoint,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-PagerAgent-Ingest-Key": api_key,
            "User-Agent": "pageragent-release-load-gate/1",
        },
    )
    started = time.perf_counter()
    try:
        with HTTP_OPENER.open(request, timeout=timeout_seconds) as response:
            body = response.read(1_048_577)
            if len(body) > 1_048_576:
                raise ValueError("Response exceeded the 1 MiB release-gate bound")
            document = json.loads(body)
            if not isinstance(document, dict) or not isinstance(document.get("incident"), dict):
                raise ValueError("Response did not match the alert-ingest envelope")
            return RequestResult(
                fingerprint=fingerprint,
                status=response.status,
                latency_ms=(time.perf_counter() - started) * 1_000,
                deduplicated=document.get("deduplicated"),
                incident_id=document.get("incident", {}).get("id"),
                error_type=None,
            )
    except HTTPError as error:
        error.read(65_537)
        return RequestResult(
            fingerprint=fingerprint,
            status=error.code,
            latency_ms=(time.perf_counter() - started) * 1_000,
            deduplicated=None,
            incident_id=None,
            error_type="HTTPError",
        )
    except (OSError, URLError, ValueError, json.JSONDecodeError) as error:
        return RequestResult(
            fingerprint=fingerprint,
            status=None,
            latency_ms=(time.perf_counter() - started) * 1_000,
            deduplicated=None,
            incident_id=None,
            error_type=type(error).__name__,
        )


def percentile(values: list[float], percentile_value: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(0, math.ceil(percentile_value * len(ordered)) - 1)
    return ordered[rank]


def build_report(
    args: argparse.Namespace,
    base_url: str,
    run_id: str,
    results: list[RequestResult],
    elapsed_seconds: float,
) -> dict[str, Any]:
    successful = [result for result in results if result.status == 201]
    latencies = [result.latency_ms for result in results]
    error_count = len(results) - len(successful)
    error_rate = error_count / len(results)
    throughput_rps = len(results) / elapsed_seconds
    p95_ms = percentile(latencies, 0.95)
    expected_fingerprints = {
        f"pageragent-release-load:{run_id}:{bucket}"
        for bucket in range(args.unique_fingerprints)
    }
    incidents_by_fingerprint: dict[str, set[str]] = {}
    for result in successful:
        if result.incident_id is None:
            continue
        incidents_by_fingerprint.setdefault(result.fingerprint, set()).add(
            result.incident_id
        )
    stable_incident_ids = {
        next(iter(incident_ids))
        for incident_ids in incidents_by_fingerprint.values()
        if len(incident_ids) == 1
    }
    exact_fingerprint_cardinality = (
        set(incidents_by_fingerprint) == expected_fingerprints
        and all(len(incident_ids) == 1 for incident_ids in incidents_by_fingerprint.values())
        and len(stable_incident_ids) == args.unique_fingerprints
    )
    assertions = {
        "successful_response_observed": bool(successful),
        "error_rate": error_rate <= args.max_error_rate,
        "p95_latency": p95_ms <= args.max_p95_ms,
        "throughput": throughput_rps >= args.min_throughput_rps,
        "deduplication_observed": (
            args.requests == args.unique_fingerprints
            or any(result.deduplicated is True for result in successful)
        ),
        "one_stable_incident_per_fingerprint": exact_fingerprint_cardinality,
    }
    return {
        "schema_version": "pageragent.release-load.v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "source_revision": source_revision(),
        "source_dirty": source_dirty(),
        "run_id": run_id,
        "target_origin": base_url,
        "configuration": {
            "requests": args.requests,
            "concurrency": args.concurrency,
            "unique_fingerprints": args.unique_fingerprints,
            "timeout_seconds": args.timeout_seconds,
        },
        "thresholds": {
            "max_error_rate": args.max_error_rate,
            "max_p95_ms": args.max_p95_ms,
            "min_throughput_rps": args.min_throughput_rps,
        },
        "measurements": {
            "elapsed_seconds": round(elapsed_seconds, 3),
            "throughput_rps": round(throughput_rps, 3),
            "error_rate": round(error_rate, 6),
            "latency_ms": {
                "p50": round(percentile(latencies, 0.50), 3),
                "p95": round(p95_ms, 3),
                "p99": round(percentile(latencies, 0.99), 3),
                "max": round(max(latencies, default=0.0), 3),
            },
            "status_counts": dict(
                sorted(Counter(str(result.status) for result in results).items())
            ),
            "error_type_counts": dict(
                sorted(
                    Counter(
                        result.error_type for result in results if result.error_type is not None
                    ).items()
                )
            ),
            "deduplicated_responses": sum(result.deduplicated is True for result in successful),
            "distinct_incidents": len(
                {result.incident_id for result in successful if result.incident_id}
            ),
        },
        "assertions": assertions,
        "passed": all(assertions.values()),
    }


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


def main() -> int:
    args = parse_args()
    try:
        base_url = validate_args(args)
    except ValueError as error:
        print(f"configuration error: {error}", file=sys.stderr)
        return 2

    endpoint = f"{base_url}/api/v1/alerts"
    run_id = uuid4().hex
    detected_at = datetime.now(UTC)
    payloads = [
        build_payload(
            run_id,
            index % args.unique_fingerprints,
            detected_at,
            args.telemetry_url,
        )
        for index in range(args.requests)
    ]
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        futures = [
            executor.submit(
                send_alert,
                endpoint,
                args.api_key,
                fingerprint,
                payload,
                args.timeout_seconds,
            )
            for fingerprint, payload in payloads
        ]
        results = [future.result() for future in as_completed(futures)]
    elapsed_seconds = max(time.perf_counter() - started, 0.000_001)
    report = build_report(args, base_url, run_id, results, elapsed_seconds)
    rendered = json.dumps(report, indent=2, sort_keys=True)
    print(rendered)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(f"{rendered}\n", encoding="utf-8")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
