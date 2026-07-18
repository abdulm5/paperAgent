#!/usr/bin/env python3
"""Apply non-secret, environment-specific values to the deployment scaffold."""

from __future__ import annotations

import argparse
import re
from ipaddress import ip_address
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

ROOT = Path(__file__).resolve().parent / "kubernetes"
DNS_NAME = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)
REVISION_TOKEN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,126}[A-Za-z0-9])?$")
RESERVED_HOSTS = {
    "example",
    "example.com",
    "example.net",
    "example.org",
    "invalid",
    "local",
    "localhost",
    "test",
}
RESERVED_HOST_SUFFIXES = (
    ".example",
    ".example.com",
    ".example.net",
    ".example.org",
    ".invalid",
    ".local",
    ".localhost",
    ".test",
)
ROLLOUT_ANNOTATIONS = {
    "pageragent.dev/release-revision": "release_revision",
    "pageragent.dev/runtime-secret-revision": "runtime_secret_revision",
}


def reserved_host(value: str) -> bool:
    normalized = value.strip().lower().rstrip(".")
    return normalized in RESERVED_HOSTS or normalized.endswith(RESERVED_HOST_SUFFIXES)


def ip_literal(value: str) -> bool:
    try:
        ip_address(value)
    except ValueError:
        return False
    return True


def exact_https_origin(value: str) -> str:
    parsed = urlsplit(value)
    try:
        parsed.port
    except ValueError as error:
        raise argparse.ArgumentTypeError("origin contains an invalid port") from error
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if (
        parsed.scheme != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or not DNS_NAME.fullmatch(hostname)
        or ip_literal(hostname)
        or reserved_host(hostname)
    ):
        raise argparse.ArgumentTypeError(
            "origin must be an exact HTTPS origin on a real, fully qualified DNS host"
        )
    rendered_host = hostname
    if parsed.port is not None:
        rendered_host = f"{rendered_host}:{parsed.port}"
    return f"https://{rendered_host}"


def public_host(value: str) -> str:
    normalized = value.strip().lower().rstrip(".")
    if (
        not DNS_NAME.fullmatch(normalized)
        or ip_literal(normalized)
        or reserved_host(normalized)
    ):
        raise argparse.ArgumentTypeError("public host must be a real, fully qualified DNS name")
    return normalized


def revision_token(value: str) -> str:
    normalized = value.strip()
    if not REVISION_TOKEN.fullmatch(normalized):
        raise argparse.ArgumentTypeError(
            "revision must be 1-128 characters using only letters, digits, '.', '_' or '-'"
        )
    return normalized


def load(path: Path) -> dict[str, Any]:
    document = yaml.safe_load(path.read_text())
    if not isinstance(document, dict):
        raise ValueError(f"{path} is not a YAML object")
    return document


def write(path: Path, document: dict[str, Any]) -> None:
    path.write_text(yaml.safe_dump(document, sort_keys=False, width=120))


def configure(args: argparse.Namespace) -> None:
    config_path = ROOT / "foundation" / "configmap.yaml"
    config = load(config_path)
    data = config["data"]
    browser_origin = f"https://{args.public_host}"
    data["PAGERAGENT_TRUSTED_HOSTS"] = args.public_host
    data["PAGERAGENT_HEALTH_HOST"] = args.public_host
    data["PAGERAGENT_CONNECTOR_ALLOWED_ORIGINS"] = ",".join(
        ("https://api.github.com", "https://slack.com", args.prometheus_origin)
    )
    data["PAGERAGENT_TELEMETRY_ALLOWED_ORIGINS"] = args.telemetry_origin
    data["BACKEND_CORS_ORIGINS"] = browser_origin
    write(config_path, config)

    ingress_path = ROOT / "foundation" / "ingress.yaml"
    ingress = load(ingress_path)
    ingress["spec"]["tls"][0]["hosts"] = [args.public_host]
    ingress["spec"]["rules"][0]["host"] = args.public_host
    write(ingress_path, ingress)

    api_path = ROOT / "workloads" / "api.yaml"
    api = load(api_path)
    container = api["spec"]["template"]["spec"]["containers"][0]
    for probe_name in ("startupProbe", "readinessProbe", "livenessProbe"):
        headers = container[probe_name]["httpGet"]["httpHeaders"]
        host_header = next(header for header in headers if header["name"].lower() == "host")
        host_header["value"] = args.public_host
    write(api_path, api)

    for relative_path in (
        Path("workloads/api.yaml"),
        Path("workloads/workflow-worker.yaml"),
        Path("workloads/outbox-relay.yaml"),
        Path("workloads/frontend.yaml"),
        Path("migration/job.yaml"),
    ):
        path = ROOT / relative_path
        workload = load(path)
        template_metadata = workload["spec"]["template"].setdefault("metadata", {})
        annotations = template_metadata.setdefault("annotations", {})
        for annotation, argument_name in ROLLOUT_ANNOTATIONS.items():
            annotations[annotation] = getattr(args, argument_name)
        write(path, workload)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    result.add_argument("--public-host", required=True, type=public_host)
    result.add_argument("--prometheus-origin", required=True, type=exact_https_origin)
    result.add_argument("--telemetry-origin", required=True, type=exact_https_origin)
    result.add_argument("--release-revision", required=True, type=revision_token)
    result.add_argument("--runtime-secret-revision", required=True, type=revision_token)
    return result


if __name__ == "__main__":
    configure(parser().parse_args())
