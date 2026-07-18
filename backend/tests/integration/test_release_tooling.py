"""Fast contract tests for the runnable release-gate entry points."""

import argparse
import runpy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


def _script(name: str) -> dict[str, object]:
    return runpy.run_path(str(ROOT / "scripts" / name))


def test_load_report_cannot_pass_without_a_successful_response() -> None:
    script = _script("run-load-gate.py")
    request_result = script["RequestResult"]
    build_report = script["build_report"]
    args = argparse.Namespace(
        requests=1,
        concurrency=1,
        unique_fingerprints=1,
        timeout_seconds=1.0,
        max_error_rate=1.0,
        max_p95_ms=10_000.0,
        min_throughput_rps=0.01,
        telemetry_url="http://checkout-api:8100/metrics",
    )

    report = build_report(
        args,
        "http://localhost:8000",
        "release-test",
        [
            request_result(
                fingerprint="pageragent-release-load:release-test:0",
                status=None,
                latency_ms=1.0,
                deduplicated=None,
                incident_id=None,
                error_type="URLError",
            )
        ],
        1.0,
    )

    assert report["assertions"]["successful_response_observed"] is False
    assert report["passed"] is False


def test_load_gate_refuses_embedded_credentials_and_remote_targets() -> None:
    validate_args = _script("run-load-gate.py")["validate_args"]
    baseline = {
        "api_key": "test-key",
        "requests": 10,
        "concurrency": 2,
        "unique_fingerprints": 2,
        "timeout_seconds": 1.0,
        "max_error_rate": 0.01,
        "max_p95_ms": 1_000.0,
        "min_throughput_rps": 1.0,
        "allow_remote": False,
        "telemetry_url": "http://checkout-api:8100/metrics",
    }

    with pytest.raises(ValueError, match="absolute HTTP"):
        validate_args(
            argparse.Namespace(
                **baseline,
                base_url="http://user:password@localhost:8000",
            )
        )
    with pytest.raises(ValueError, match="Refusing"):
        validate_args(
            argparse.Namespace(
                **baseline,
                base_url="https://pageragent.example.com",
            )
        )

    with pytest.raises(ValueError, match="must use HTTPS"):
        validate_args(
            argparse.Namespace(
                **{**baseline, "allow_remote": True},
                base_url="http://pageragent.example.com",
            )
        )


def test_load_gate_rejects_reserved_remote_target_and_telemetry_hosts() -> None:
    validate_args = _script("run-load-gate.py")["validate_args"]
    baseline = {
        "api_key": "test-key",
        "requests": 10,
        "concurrency": 2,
        "unique_fingerprints": 2,
        "timeout_seconds": 1.0,
        "max_error_rate": 0.01,
        "max_p95_ms": 1_000.0,
        "min_throughput_rps": 1.0,
        "allow_remote": True,
    }

    with pytest.raises(ValueError, match="non-reserved host"):
        validate_args(
            argparse.Namespace(
                **baseline,
                base_url="https://pageragent.example.com",
                telemetry_url="https://telemetry.acme.dev/metrics",
            )
        )
    with pytest.raises(ValueError, match="non-reserved telemetry host"):
        validate_args(
            argparse.Namespace(
                **baseline,
                base_url="https://pageragent.acme.dev",
                telemetry_url="https://telemetry.example.net/metrics",
            )
        )


def test_load_report_requires_one_distinct_incident_per_fingerprint() -> None:
    script = _script("run-load-gate.py")
    request_result = script["RequestResult"]
    build_report = script["build_report"]
    args = argparse.Namespace(
        requests=2,
        concurrency=1,
        unique_fingerprints=2,
        timeout_seconds=1.0,
        max_error_rate=0.0,
        max_p95_ms=10_000.0,
        min_throughput_rps=0.01,
        telemetry_url="http://checkout-api:8100/metrics",
    )
    results = [
        request_result(
            fingerprint=f"pageragent-release-load:release-test:{bucket}",
            status=201,
            latency_ms=1.0,
            deduplicated=False,
            incident_id="same-incident",
            error_type=None,
        )
        for bucket in range(2)
    ]

    report = build_report(
        args,
        "http://localhost:8000",
        "release-test",
        results,
        1.0,
    )

    assert report["assertions"]["one_stable_incident_per_fingerprint"] is False
    assert report["passed"] is False


def test_security_environment_parser_rejects_duplicate_keys(tmp_path: Path) -> None:
    script = _script("run-security-gate.py")
    environment_file = tmp_path / "duplicate.env"
    environment_file.write_text(
        "PAGERAGENT_ENV=production\nPAGERAGENT_ENV=staging\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Duplicate environment key"):
        script["parse_environment_file"](environment_file)


def test_security_gate_requires_hosted_trust_boundary() -> None:
    required = _script("run-security-gate.py")["REQUIRED_PRODUCTION_FIELDS"]

    assert "PAGERAGENT_TRUSTED_HOSTS" in required
    assert "PAGERAGENT_CONNECTOR_KMS_KEY_ARN" in required
    assert "PAGERAGENT_OIDC_REDIRECT_URI" in required
    assert "PAGERAGENT_INGEST_ORGANIZATION_SLUG" in required
    assert "DATABASE_URL" in required
    assert "REDIS_URL" in required
    assert "DURABLE_MITIGATION_ENABLED" in required


def test_security_gate_requires_https_for_remote_targets() -> None:
    validate_base_url = _script("run-security-gate.py")["validate_base_url"]

    with pytest.raises(ValueError, match="must use HTTPS"):
        validate_base_url("http://pageragent.example.com", True)

    with pytest.raises(ValueError, match="Hosted environment"):
        validate_base_url("http://localhost:8000", False, "production")


def test_security_gate_rejects_reserved_remote_targets() -> None:
    validate_base_url = _script("run-security-gate.py")["validate_base_url"]

    with pytest.raises(ValueError, match="non-reserved host"):
        validate_base_url("https://pageragent.example.org", True)


@pytest.mark.parametrize(
    ("setting", "value", "message"),
    [
        (
            "DATABASE_URL",
            (
                "postgresql+psycopg://pageragent:secret@db.acme.dev/pageragent"
                "?sslmode=require&sslmode=disable"
            ),
            "exactly one secure sslmode",
        ),
        (
            "DATABASE_URL",
            (
                "postgresql+psycopg://pageragent:secret@db.acme.dev/pageragent"
                "?sslmode=verify-full&host=localhost"
            ),
            "override its authority",
        ),
        (
            "REDIS_URL",
            "rediss://cache.acme.dev:6380/0?ssl_check_hostname=false",
            "ssl_check_hostname=true",
        ),
        (
            "REDIS_URL",
            (
                "rediss://cache.acme.dev:6380/0"
                "?ssl_check_hostname=true&ssl_cert_reqs=none"
            ),
            "certificate verification",
        ),
        (
            "DATABASE_URL",
            (
                "postgresql+psycopg://pageragent:secret@db.example.net/pageragent"
                "?sslmode=verify-full"
            ),
            "non-reserved public endpoint",
        ),
        (
            "REDIS_URL",
            "rediss://cache.example.org:6380/0?ssl_check_hostname=true",
            "non-reserved public endpoint",
        ),
        (
            "DATABASE_URL",
            (
                "postgresql+psycopg://pageragent:secret@%6cocalhost/pageragent"
                "?sslmode=verify-full"
            ),
            "canonical DNS name or IP address",
        ),
        (
            "REDIS_URL",
            "rediss://%2Ftmp%2Fredis.sock:6380/0?ssl_check_hostname=true",
            "canonical DNS name or IP address",
        ),
    ],
)
def test_security_gate_reuses_runtime_store_url_policy(
    setting: str,
    value: str,
    message: str,
    tmp_path: Path,
) -> None:
    script = _script("run-security-gate.py")
    environment_file = tmp_path / "unsafe.env"
    environment_file.write_text(f"{setting}={value}\n", encoding="utf-8")

    report = script["validate_production_environment"](environment_file)

    assert report["passed"] is False
    assert any(message in failure for failure in report["failures"])


@pytest.mark.parametrize(
    ("setting", "value"),
    [
        ("PAGERAGENT_OIDC_ISSUER", "https://identity.example.com"),
        (
            "PAGERAGENT_CONNECTOR_ALLOWED_ORIGINS",
            "https://api.github.com,https://prometheus.example.com",
        ),
        (
            "PAGERAGENT_TELEMETRY_ALLOWED_ORIGINS",
            "https://telemetry.example.net",
        ),
        ("BACKEND_CORS_ORIGINS", "https://pageragent.example.org"),
        ("PAGERAGENT_TRUSTED_HOSTS", "pageragent.example.com"),
    ],
)
def test_security_gate_rejects_reserved_hosted_configuration(
    setting: str,
    value: str,
    tmp_path: Path,
) -> None:
    script = _script("run-security-gate.py")
    environment_file = tmp_path / "reserved.env"
    environment_file.write_text(f"{setting}={value}\n", encoding="utf-8")

    report = script["validate_production_environment"](environment_file)

    assert report["passed"] is False
    assert any(
        setting in failure and "non-reserved public host" in failure
        for failure in report["failures"]
    )


def test_security_gate_requires_distinct_session_and_ingest_secrets(tmp_path: Path) -> None:
    script = _script("run-security-gate.py")
    environment_file = tmp_path / "reused-secret.env"
    environment_file.write_text(
        "PAGERAGENT_SESSION_SECRET=same-production-secret-value\n"
        "PAGERAGENT_INGEST_API_KEY=same-production-secret-value\n",
        encoding="utf-8",
    )

    report = script["validate_production_environment"](environment_file)

    assert report["passed"] is False
    assert any("must be distinct" in failure for failure in report["failures"])


def test_security_gate_rejects_extra_internal_trusted_hosts(tmp_path: Path) -> None:
    script = _script("run-security-gate.py")
    environment_file = tmp_path / "expanded-trust.env"
    environment_file.write_text(
        "PAGERAGENT_OIDC_FRONTEND_URL=https://pageragent.acme.dev/\n"
        "PAGERAGENT_TRUSTED_HOSTS=pageragent.acme.dev,localhost,pageragent-api\n",
        encoding="utf-8",
    )

    report = script["validate_production_environment"](environment_file)

    assert report["passed"] is False
    assert any(
        "only the exact OIDC frontend hostname" in failure
        for failure in report["failures"]
    )


def test_security_gate_cannot_treat_a_local_server_as_the_hosted_environment() -> None:
    script = _script("run-security-gate.py")
    run_live_checks = script["run_live_checks"]

    def fake_request(url: str, **options):
        headers = {
            "x-content-type-options": "nosniff",
            "x-frame-options": "DENY",
            "referrer-policy": "no-referrer",
            "permissions-policy": "camera=()",
        }
        if options.get("headers", {}).get("Host") == "attacker.invalid":
            return 400, headers, b"Invalid host header", None
        if url.endswith("/health"):
            return 200, headers, b'{"status":"ok","environment":"local"}', None
        if url.endswith("/health/live"):
            return 200, {**headers, "cache-control": "no-store"}, b'{"status":"alive"}', None
        if url.endswith("/health/ready"):
            return (
                200,
                {**headers, "cache-control": "no-store"},
                b'{"status":"ready","checks":{"database":"ok","schema":"current"}}',
                None,
            )
        if url.endswith("/docs"):
            return 200, headers, b"local docs", None
        if url.endswith("/incidents") and options.get("method", "GET") == "OPTIONS":
            return 400, headers, b"", None
        if url.endswith("/incidents"):
            return 401, {**headers, "www-authenticate": "Bearer"}, b"", None
        ingest_key = options.get("headers", {}).get("X-PagerAgent-Ingest-Key", "")
        if str(ingest_key).startswith("invalid-"):
            return 401, headers, b'{"detail":"Authentication required"}', None
        return 422, headers, b'{"detail":"Request validation failed"}', None

    run_live_checks.__globals__["request"] = fake_request
    report = run_live_checks(
        "http://localhost:8000",
        "valid-test-key",
        1.0,
        expected_environment="production",
    )

    assert report["checks"]["health_contract"] is False
    assert report["checks"]["untrusted_host_is_denied"] is True
    assert report["checks"]["hosted_transport_and_content_policy"] is False
    assert report["checks"]["interactive_docs_disabled_when_hosted"] is False
    assert report["passed"] is False


@pytest.mark.parametrize(
    "script_name",
    ["run-load-gate.py", "run-security-gate.py", "run-chaos-drill.py"],
)
def test_release_http_gates_never_follow_redirects_with_credentials(
    script_name: str,
) -> None:
    script = _script(script_name)
    reject_type = script["RejectRedirects"]
    handlers = [
        handler
        for handler in script["HTTP_OPENER"].handlers
        if isinstance(handler, reject_type)
    ]

    assert len(handlers) == 1
    assert (
        handlers[0].redirect_request(
            script["Request"](
                "https://pageragent.acme.dev/api/v1/alerts",
                headers={"X-PagerAgent-Ingest-Key": "must-not-cross-origins"},
            ),
            None,
            302,
            "Found",
            {},
            "https://attacker.acme.dev/capture",
        )
        is None
    )


def test_chaos_gate_requires_confirmation_before_compose_access(tmp_path: Path) -> None:
    validate_args = _script("run-chaos-drill.py")["validate_args"]
    args = argparse.Namespace(
        confirm=False,
        base_url="http://localhost:8000",
        api_key="test-key",
        stream_name="pageragent.workflows",
        consumer_group="pageragent-workers",
        dead_letter_stream="pageragent.workflows.dlq",
        recovery_timeout_seconds=30.0,
        compose_file=tmp_path / "missing-compose.yml",
        project_directory=tmp_path,
    )

    with pytest.raises(ValueError, match="--confirm"):
        validate_args(args)


def test_chaos_gate_correlates_completion_ack_and_dlq_to_exact_job(tmp_path: Path) -> None:
    script = _script("run-chaos-drill.py")
    drill_type = script["ChaosDrill"]
    args = argparse.Namespace(project_directory=tmp_path, compose_file=tmp_path / "compose.yml")
    drill = drill_type(args)
    workflow = {
        "id": "workflow-1",
        "status": "completed",
        "jobs": [
            {
                "id": "job-1",
                "status": "completed",
                "deliveries": [{"stream_message_id": "1700000000000-0"}],
            }
        ],
    }
    drill.pending_message_ids = lambda: set()
    drill.dead_letter_job_ids = lambda: set()

    assert drill.workflow_effect_is_settled(workflow) is True
    assert drill.workflow_receipt(workflow) == {
        "workflow_id": "workflow-1",
        "job_ids": ["job-1"],
        "stream_message_ids": ["1700000000000-0"],
    }

    drill.pending_message_ids = lambda: {"1700000000000-0"}
    assert drill.workflow_effect_is_settled(workflow) is False
    drill.pending_message_ids = lambda: set()
    drill.dead_letter_job_ids = lambda: {"job-1"}
    with pytest.raises(script["DrillFailure"], match="dead-letter"):
        drill.workflow_effect_is_settled(workflow)
