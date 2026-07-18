#!/usr/bin/env python3
"""Verify that downloaded release assets describe one immutable PagerAgent build."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

SEMANTIC_TAG = re.compile(r"^v\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")
REVISION = re.compile(r"^[0-9a-f]{40}$")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
REQUIRED_METADATA = {"release", "revision", "backend_image", "frontend_image"}
BACKEND_WORKLOADS = {
    ("Deployment", "pageragent-api"),
    ("Deployment", "pageragent-workflow-worker"),
    ("Deployment", "pageragent-outbox-relay"),
    ("Job", "pageragent-migrate"),
}
FRONTEND_WORKLOADS = {("Deployment", "pageragent-frontend")}
POD_WORKLOAD_KINDS = {
    "CronJob",
    "DaemonSet",
    "Deployment",
    "Job",
    "Pod",
    "ReplicaSet",
    "StatefulSet",
}


@dataclass(frozen=True)
class VerifiedRelease:
    release: str
    revision: str
    backend_image: str
    frontend_image: str
    manifest_sha256: str
    metadata_sha256: str


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def parse_metadata(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw_line or raw_line.startswith("#") or "=" not in raw_line:
            raise ValueError(f"{path}:{line_number}: expected a non-empty key=value line")
        key, value = raw_line.split("=", 1)
        if not key or key.strip() != key or not value or value.strip() != value:
            raise ValueError(f"{path}:{line_number}: metadata must not contain padding")
        if key in values:
            raise ValueError(f"{path}:{line_number}: duplicate metadata key {key!r}")
        values[key] = value

    missing = REQUIRED_METADATA - values.keys()
    unexpected = values.keys() - REQUIRED_METADATA
    if missing or unexpected:
        raise ValueError(
            f"release metadata keys differ from the contract; "
            f"missing={sorted(missing)}, unexpected={sorted(unexpected)}"
        )
    return values


def expected_image(repository: str, component: str, digest: str) -> str:
    if not REPOSITORY.fullmatch(repository):
        raise ValueError("repository must have the form owner/name")
    if not DIGEST.fullmatch(digest):
        raise ValueError(f"invalid OCI digest for {component}")
    owner = repository.split("/", 1)[0].lower()
    return f"ghcr.io/{owner}/pageragent-{component}@{digest}"


def split_image(image: str, *, repository: str, component: str) -> tuple[str, str]:
    prefix = expected_image(repository, component, "sha256:" + "0" * 64).rsplit("@", 1)[0]
    if not image.startswith(prefix + "@"):
        raise ValueError(f"{component}_image must use the release repository {prefix}")
    digest = image.rsplit("@", 1)[1]
    expected_image(repository, component, digest)
    return image, digest


def load_manifest(path: Path) -> list[dict[str, Any]]:
    documents: list[dict[str, Any]] = []
    for index, document in enumerate(yaml.safe_load_all(path.read_text(encoding="utf-8")), start=1):
        if document is None:
            continue
        if not isinstance(document, dict):
            raise ValueError(f"{path}:{index}: release manifest document is not an object")
        documents.append(document)
    if not documents:
        raise ValueError("release manifest is empty")
    return documents


def workload_images(documents: list[dict[str, Any]]) -> dict[tuple[str, str], list[str]]:
    result: dict[tuple[str, str], list[str]] = {}
    for document in documents:
        kind = str(document.get("kind", ""))
        name = str((document.get("metadata") or {}).get("name", ""))
        if kind in POD_WORKLOAD_KINDS and kind not in {"Deployment", "Job"}:
            raise ValueError(f"release manifest contains unexpected workload kind {kind}/{name}")
        if kind not in {"Deployment", "Job"}:
            continue
        identifier = (kind, name)
        if identifier in result:
            raise ValueError(f"release manifest contains duplicate workload {kind}/{name}")
        spec = document.get("spec") or {}
        pod_spec = ((spec.get("template") or {}).get("spec") or {})
        if pod_spec.get("initContainers") or pod_spec.get("ephemeralContainers"):
            raise ValueError(f"release workload {kind}/{name} contains an unexpected container")
        containers = pod_spec.get("containers") or []
        if not containers:
            raise ValueError(f"release workload {kind}/{name} has no containers")
        result[identifier] = [str(container.get("image", "")) for container in containers]
    return result


def verify_workloads(
    documents: list[dict[str, Any]], backend_image: str, frontend_image: str
) -> None:
    images = workload_images(documents)
    expected_workloads = BACKEND_WORKLOADS | FRONTEND_WORKLOADS
    missing = expected_workloads - images.keys()
    unexpected = images.keys() - expected_workloads
    if missing or unexpected:
        raise ValueError(
            f"release workload set differs from the contract; "
            f"missing={sorted(missing)}, unexpected={sorted(unexpected)}"
        )
    for identifier in sorted(BACKEND_WORKLOADS):
        if images[identifier] != [backend_image]:
            raise ValueError(f"{identifier[0]}/{identifier[1]} is not pinned to backend_image")
    for identifier in sorted(FRONTEND_WORKLOADS):
        if images[identifier] != [frontend_image]:
            raise ValueError(f"{identifier[0]}/{identifier[1]} is not pinned to frontend_image")


def verify_release(
    *,
    manifest_path: Path,
    metadata_path: Path,
    expected_tag: str,
    expected_revision: str,
    repository: str,
) -> VerifiedRelease:
    if not SEMANTIC_TAG.fullmatch(expected_tag):
        raise ValueError("expected tag must be a semantic release tag such as v1.2.3")
    if not REVISION.fullmatch(expected_revision):
        raise ValueError("expected revision must be a lowercase, full 40-character Git SHA")

    metadata = parse_metadata(metadata_path)
    if metadata["release"] != expected_tag:
        raise ValueError("release metadata tag does not match the requested release")
    if metadata["revision"] != expected_revision:
        raise ValueError("release metadata revision does not match the checked-out tag")
    backend_image, _ = split_image(
        metadata["backend_image"], repository=repository, component="backend"
    )
    frontend_image, _ = split_image(
        metadata["frontend_image"], repository=repository, component="frontend"
    )
    verify_workloads(load_manifest(manifest_path), backend_image, frontend_image)

    return VerifiedRelease(
        release=expected_tag,
        revision=expected_revision,
        backend_image=backend_image,
        frontend_image=frontend_image,
        manifest_sha256=file_sha256(manifest_path),
        metadata_sha256=file_sha256(metadata_path),
    )


def write_github_output(path: Path, release: VerifiedRelease) -> None:
    values = asdict(release)
    with path.open("a", encoding="utf-8") as output:
        for key, value in values.items():
            output.write(f"{key}={value}\n")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    result.add_argument("--manifest", required=True, type=Path)
    result.add_argument("--metadata", required=True, type=Path)
    result.add_argument("--expected-tag", required=True)
    result.add_argument("--expected-revision", required=True)
    result.add_argument("--repository", required=True)
    result.add_argument(
        "--github-output",
        type=Path,
        default=Path(value) if (value := os.environ.get("GITHUB_OUTPUT")) else None,
    )
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        release = verify_release(
            manifest_path=args.manifest,
            metadata_path=args.metadata,
            expected_tag=args.expected_tag,
            expected_revision=args.expected_revision,
            repository=args.repository,
        )
        if args.github_output is not None:
            write_github_output(args.github_output, release)
    except (OSError, ValueError, yaml.YAMLError) as error:
        print(f"release verification failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(asdict(release), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
