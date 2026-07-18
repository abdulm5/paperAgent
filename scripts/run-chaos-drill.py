#!/usr/bin/env python3
"""Run a self-restoring local Redis/outbox/worker recovery drill."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4

LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
REQUIRED_SERVICES = frozenset(
    {"backend", "checkout-api", "redis", "outbox-relay", "workflow-worker"}
)


class RejectRedirects(HTTPRedirectHandler):
    """Turn every redirect into an HTTPError before local credentials can escape."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


HTTP_OPENER = build_opener(RejectRedirects)


def parse_args() -> argparse.Namespace:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-directory", type=Path, default=root)
    parser.add_argument("--compose-file", type=Path, default=root / "docker-compose.yml")
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument(
        "--stream-name",
        default=os.getenv("WORKFLOW_STREAM_NAME", "pageragent.workflows"),
    )
    parser.add_argument(
        "--consumer-group",
        default=os.getenv("WORKFLOW_CONSUMER_GROUP", "pageragent-workers"),
    )
    parser.add_argument(
        "--dead-letter-stream",
        default=os.getenv("WORKFLOW_DEAD_LETTER_STREAM", "pageragent.workflows.dlq"),
    )
    parser.add_argument(
        "--api-key",
        default=os.getenv("PAGERAGENT_INGEST_API_KEY", "pageragent-local-ingest-key"),
    )
    parser.add_argument("--recovery-timeout-seconds", type=float, default=60.0)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Acknowledge that local services will restart and two incidents will be created",
    )
    return parser.parse_args()


class DrillFailure(RuntimeError):
    pass


class ChaosDrill:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.compose_prefix = [
            "docker",
            "compose",
            "--project-directory",
            str(args.project_directory.resolve()),
            "--file",
            str(args.compose_file.resolve()),
        ]
        self.phases: list[dict[str, Any]] = []
        self.auth_token: str | None = None

    def compose(self, *arguments: str, capture: bool = False) -> str:
        completed = subprocess.run(
            [*self.compose_prefix, *arguments],
            cwd=self.args.project_directory,
            check=False,
            capture_output=capture,
            text=True,
            timeout=120,
        )
        if completed.returncode != 0:
            raise DrillFailure(
                f"docker compose {' '.join(arguments[:2])} failed; inspect local compose logs"
            )
        return completed.stdout.strip() if capture else ""

    def record(self, name: str, started: float, **evidence: Any) -> None:
        self.phases.append(
            {
                "name": name,
                "passed": True,
                "duration_seconds": round(time.perf_counter() - started, 3),
                **evidence,
            }
        )

    def wait_until(self, description: str, predicate) -> Any:
        deadline = time.monotonic() + self.args.recovery_timeout_seconds
        last_value: Any = None
        while time.monotonic() < deadline:
            try:
                last_value = predicate()
                if last_value:
                    return last_value
            except (OSError, ValueError, json.JSONDecodeError):
                pass
            time.sleep(0.5)
        raise DrillFailure(f"Timed out waiting for {description}")

    def http_alert(self, phase: str) -> str:
        marker = uuid4().hex
        detected_at = datetime.now(UTC)
        started_at = detected_at - timedelta(minutes=2)
        body = json.dumps(
            {
                "fingerprint": f"pageragent-chaos:{phase}:{marker}",
                "source": "pageragent-release-chaos-drill",
                "service": "checkout-api",
                "severity": "critical",
                "summary": f"Synthetic {phase} durability alert",
                "started_at": started_at.isoformat(),
                "detected_at": detected_at.isoformat(),
                "metric": {
                    "name": "http_server_error_rate",
                    "value": 0.2,
                    "threshold": 0.05,
                    "window_seconds": 300,
                    "request_count": 500,
                    "failed_request_count": 100,
                },
                "release": {
                    "name": "release-chaos-drill",
                    "commit_sha": "0000000",
                    "deployed_at": started_at.isoformat(),
                },
                "telemetry_url": "http://checkout-api:8100/metrics",
            },
            separators=(",", ":"),
        ).encode()
        outbound = Request(
            f"{self.args.base_url.rstrip('/')}/api/v1/alerts",
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-PagerAgent-Ingest-Key": self.args.api_key,
            },
        )
        try:
            with HTTP_OPENER.open(outbound, timeout=10) as response:
                response_body = response.read(1_048_577)
                if response.status != 201 or len(response_body) > 1_048_576:
                    raise DrillFailure("Alert ingest did not return a bounded 201 response")
                document = json.loads(response_body)
                return str(document["incident"]["id"])
        except HTTPError as error:
            error.read(65_537)
            raise DrillFailure(f"Alert ingest returned HTTP {error.code}") from None
        except (OSError, URLError, KeyError, ValueError, json.JSONDecodeError) as error:
            raise DrillFailure(f"Alert ingest failed with {type(error).__name__}") from None

    def api_ready(self) -> bool:
        try:
            with HTTP_OPENER.open(
                f"{self.args.base_url.rstrip('/')}/api/v1/health/ready",
                timeout=5,
            ) as response:
                body = response.read(65_537)
                document = json.loads(body)
                checks = document.get("checks") if isinstance(document, dict) else None
                return (
                    isinstance(document, dict)
                    and len(body) <= 65_536
                    and response.status == 200
                    and document.get("status") == "ready"
                    and isinstance(checks, dict)
                    and checks.get("database") == "ok"
                    and checks.get("schema") in {"current", "forward_compatible"}
                )
        except (HTTPError, OSError, URLError, ValueError, json.JSONDecodeError):
            return False

    def authenticate(self) -> None:
        body = json.dumps(
            {"persona": "admin", "organization_slug": "pageragent-labs"},
            separators=(",", ":"),
        ).encode()
        request = Request(
            f"{self.args.base_url.rstrip('/')}/api/v1/auth/dev/session",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with HTTP_OPENER.open(request, timeout=10) as response:
                document = json.loads(response.read(262_145))
        except (HTTPError, OSError, URLError, ValueError, json.JSONDecodeError):
            raise DrillFailure(
                "The chaos drill requires the local development identity boundary"
            ) from None
        token = document.get("access_token") if isinstance(document, dict) else None
        if not isinstance(token, str) or not token:
            raise DrillFailure("PagerAgent did not issue a local drill session")
        self.auth_token = token

    def incident_workflow(self, incident_id: str) -> dict[str, Any] | None:
        if self.auth_token is None:
            raise DrillFailure("The local drill session is unavailable")
        request = Request(
            f"{self.args.base_url.rstrip('/')}/api/v1/incidents/{incident_id}/workflows",
            headers={"Authorization": f"Bearer {self.auth_token}"},
        )
        try:
            with HTTP_OPENER.open(request, timeout=10) as response:
                document = json.loads(response.read(1_048_577))
        except HTTPError as error:
            error.read(65_537)
            if error.code == 404:
                return None
            raise DrillFailure(f"Workflow inspection returned HTTP {error.code}") from None
        except (OSError, URLError, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(document, list):
            raise DrillFailure("Workflow inspection returned an invalid envelope")
        return next(
            (
                workflow
                for workflow in document
                if isinstance(workflow, dict)
                and workflow.get("workflow_type") == "incident_response"
            ),
            None,
        )

    @staticmethod
    def workflow_receipt(workflow: dict[str, Any]) -> dict[str, Any]:
        jobs = workflow.get("jobs")
        if not isinstance(jobs, list):
            raise DrillFailure("Workflow receipt has no jobs")
        job_ids: list[str] = []
        stream_ids: list[str] = []
        for job in jobs:
            if not isinstance(job, dict) or not isinstance(job.get("id"), str):
                raise DrillFailure("Workflow receipt contains an invalid job")
            job_ids.append(job["id"])
            deliveries = job.get("deliveries")
            if not isinstance(deliveries, list):
                continue
            stream_ids.extend(
                delivery["stream_message_id"]
                for delivery in deliveries
                if isinstance(delivery, dict)
                and isinstance(delivery.get("stream_message_id"), str)
            )
        return {
            "workflow_id": workflow.get("id"),
            "job_ids": job_ids,
            "stream_message_ids": stream_ids,
        }

    def published_workflow(self, incident_id: str) -> dict[str, Any] | None:
        workflow = self.incident_workflow(incident_id)
        if workflow is None:
            return None
        if workflow.get("status") == "dead_lettered":
            raise DrillFailure("The exact drill workflow entered the dead letter state")
        receipt = self.workflow_receipt(workflow)
        return workflow if receipt["stream_message_ids"] else None

    def completed_workflow(self, incident_id: str) -> dict[str, Any] | None:
        workflow = self.incident_workflow(incident_id)
        if workflow is None:
            return None
        if workflow.get("status") == "dead_lettered":
            raise DrillFailure("The exact drill workflow entered the dead letter state")
        jobs = workflow.get("jobs")
        if workflow.get("status") != "completed" or not isinstance(jobs, list):
            return None
        if not jobs or any(
            not isinstance(job, dict) or job.get("status") != "completed" for job in jobs
        ):
            return None
        return workflow

    def pending_message_ids(self) -> set[str]:
        raw = self.compose(
            "exec",
            "-T",
            "redis",
            "redis-cli",
            "--json",
            "XPENDING",
            self.args.stream_name,
            self.args.consumer_group,
            "-",
            "+",
            "1000",
            capture=True,
        )
        entries = json.loads(raw)
        return {
            str(entry[0])
            for entry in entries
            if isinstance(entry, list) and entry
        }

    def dead_letter_job_ids(self) -> set[str]:
        raw = self.compose(
            "exec",
            "-T",
            "redis",
            "redis-cli",
            "--json",
            "XREVRANGE",
            self.args.dead_letter_stream,
            "+",
            "-",
            "COUNT",
            "1000",
            capture=True,
        )
        entries = json.loads(raw)
        job_ids: set[str] = set()
        for entry in entries:
            if not isinstance(entry, list) or len(entry) != 2:
                continue
            raw_fields = entry[1]
            fields = (
                raw_fields
                if isinstance(raw_fields, dict)
                else dict(zip(raw_fields[::2], raw_fields[1::2], strict=True))
            )
            job_id = fields.get("workflow_job_id")
            if isinstance(job_id, str):
                job_ids.add(job_id)
        return job_ids

    def workflow_effect_is_settled(self, workflow: dict[str, Any]) -> bool:
        receipt = self.workflow_receipt(workflow)
        stream_ids = set(receipt["stream_message_ids"])
        job_ids = set(receipt["job_ids"])
        if not stream_ids:
            return False
        if stream_ids & self.pending_message_ids():
            return False
        if job_ids & self.dead_letter_job_ids():
            raise DrillFailure("The exact drill job was copied to the dead-letter stream")
        return True

    def stream_length(self) -> int:
        value = self.compose(
            "exec",
            "-T",
            "redis",
            "redis-cli",
            "XLEN",
            self.args.stream_name,
            capture=True,
        )
        return int(value)

    def redis_ready(self) -> bool:
        return self.compose("exec", "-T", "redis", "redis-cli", "PING", capture=True) == "PONG"

    def run(self) -> dict[str, Any]:
        baseline_started = time.perf_counter()
        running_services = set(
            self.compose("ps", "--status", "running", "--services", capture=True).splitlines()
        )
        missing = sorted(REQUIRED_SERVICES - running_services)
        if missing:
            raise DrillFailure("Required compose services are not running: " + ", ".join(missing))
        if not self.api_ready():
            raise DrillFailure("PagerAgent database/schema readiness check did not pass")
        self.authenticate()
        baseline_length = self.stream_length()
        self.record(
            "preflight",
            baseline_started,
            running_services=sorted(REQUIRED_SERVICES),
            workflow_stream_length=baseline_length,
        )

        outage_started = time.perf_counter()
        self.compose("stop", "redis")
        durable_incident_id = self.http_alert("redis-outage")
        self.record(
            "postgres_accepts_alert_while_redis_is_down",
            outage_started,
            incident_id=durable_incident_id,
        )

        repair_started = time.perf_counter()
        self.compose("start", "redis")
        self.wait_until("Redis readiness", self.redis_ready)
        self.compose("restart", "outbox-relay", "workflow-worker")
        repaired_workflow = self.wait_until(
            "the exact Redis-outage workflow to complete",
            lambda: self.completed_workflow(durable_incident_id),
        )
        self.wait_until(
            "the exact Redis-outage delivery to be acknowledged",
            lambda: self.workflow_effect_is_settled(repaired_workflow),
        )
        repaired_receipt = self.workflow_receipt(repaired_workflow)
        self.record(
            "redis_restart_repairs_postgres_outbox",
            repair_started,
            incident_id=durable_incident_id,
            **repaired_receipt,
        )

        worker_outage_started = time.perf_counter()
        self.compose("stop", "workflow-worker")
        worker_incident_id = self.http_alert("worker-outage")
        queued_workflow = self.wait_until(
            "the exact stopped-worker workflow to receive a stream delivery",
            lambda: self.published_workflow(worker_incident_id),
        )
        queued_receipt = self.workflow_receipt(queued_workflow)
        self.record(
            "outbox_continues_while_worker_is_down",
            worker_outage_started,
            incident_id=worker_incident_id,
            **queued_receipt,
        )

        worker_recovery_started = time.perf_counter()
        self.compose("start", "workflow-worker")
        recovered_workflow = self.wait_until(
            "the exact stopped-worker workflow to complete",
            lambda: self.completed_workflow(worker_incident_id),
        )
        self.wait_until(
            "the exact stopped-worker delivery to be acknowledged",
            lambda: self.workflow_effect_is_settled(recovered_workflow),
        )
        self.record(
            "worker_restart_consumes_backlog",
            worker_recovery_started,
            incident_id=worker_incident_id,
            **self.workflow_receipt(recovered_workflow),
        )
        return {
            "schema_version": "pageragent.chaos-drill.v1",
            "generated_at": datetime.now(UTC).isoformat(),
            "source_revision": source_revision(),
            "source_dirty": source_dirty(),
            "passed": True,
            "phases": self.phases,
        }

    def restore(self) -> None:
        try:
            subprocess.run(
                [
                    *self.compose_prefix,
                    "start",
                    "redis",
                    "outbox-relay",
                    "workflow-worker",
                ],
                cwd=self.args.project_directory,
                check=False,
                capture_output=True,
                timeout=120,
            )
        except (OSError, subprocess.SubprocessError):
            pass


def validate_args(args: argparse.Namespace) -> None:
    if not (args.confirm or os.getenv("PAGERAGENT_CHAOS_CONFIRM") == "1"):
        raise ValueError("Pass --confirm or set PAGERAGENT_CHAOS_CONFIRM=1")
    parsed = urlsplit(args.base_url)
    try:
        parsed.port
    except ValueError as error:
        raise ValueError("--base-url contains an invalid port") from error
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname not in LOOPBACK_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or args.base_url.rstrip("/") != f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
    ):
        raise ValueError("Chaos drills are restricted to a loopback PagerAgent origin")
    if not args.api_key:
        raise ValueError("An ingest API key is required")
    if not args.stream_name or not args.consumer_group or not args.dead_letter_stream:
        raise ValueError("Workflow stream, consumer group, and dead-letter stream are required")
    if not 10 <= args.recovery_timeout_seconds <= 300:
        raise ValueError("Recovery timeout must be between 10 and 300 seconds")
    if not args.compose_file.is_file():
        raise ValueError("Compose file does not exist")
    if not args.project_directory.is_dir():
        raise ValueError("Compose project directory does not exist")


def main() -> int:
    args = parse_args()
    try:
        validate_args(args)
    except ValueError as error:
        print(f"configuration error: {error}", file=sys.stderr)
        return 2

    drill = ChaosDrill(args)
    try:
        report = drill.run()
    except (DrillFailure, OSError, subprocess.SubprocessError) as error:
        report = {
            "schema_version": "pageragent.chaos-drill.v1",
            "generated_at": datetime.now(UTC).isoformat(),
            "source_revision": source_revision(),
            "source_dirty": source_dirty(),
            "passed": False,
            "phases": drill.phases,
            "failure": str(error),
        }
    finally:
        drill.restore()

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
