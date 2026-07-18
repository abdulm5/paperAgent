#!/usr/bin/env python3
"""Validate PagerAgent's rendered deployment contract without contacting a cluster."""

from __future__ import annotations

import re
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml

EXPECTED_NAMESPACE = "pageragent"
EXPECTED_CONFIG_MAP = "pageragent-runtime"
ROLLOUT_ANNOTATIONS = (
    "pageragent.dev/release-revision",
    "pageragent.dev/runtime-secret-revision",
)
REVISION_TOKEN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,126}[A-Za-z0-9])?$")
RUNTIME_CONTRACTS = {
    ("Deployment", "pageragent-api"): {
        "container": "api",
        "service_account": "pageragent-api",
        "secrets": {
            "pageragent-api-secrets",
            "pageragent-connector-secrets",
            "pageragent-database-secrets",
            "pageragent-transport-secrets",
        },
    },
    ("Deployment", "pageragent-workflow-worker"): {
        "container": "worker",
        "service_account": "pageragent-workflow-worker",
        "secrets": {
            "pageragent-connector-secrets",
            "pageragent-database-secrets",
            "pageragent-transport-secrets",
        },
    },
    ("Deployment", "pageragent-outbox-relay"): {
        "container": "relay",
        "service_account": "pageragent-outbox-relay",
        "secrets": {
            "pageragent-database-secrets",
            "pageragent-transport-secrets",
        },
    },
    ("Deployment", "pageragent-frontend"): {
        "container": "frontend",
        "service_account": "pageragent-frontend",
        "secrets": set(),
    },
    ("Job", "pageragent-migrate"): {
        "container": "migration",
        "service_account": "pageragent-migration",
        "secrets": {"pageragent-database-secrets"},
    },
}


def manifest_paths(arguments: list[str]) -> list[Path]:
    paths: list[Path] = []
    for argument in arguments:
        candidate = Path(argument)
        if candidate.is_dir():
            paths.extend(
                path
                for path in sorted(candidate.rglob("*.yaml"))
                if path.name != "kustomization.yaml"
            )
        elif candidate.is_file():
            paths.append(candidate)
        else:
            raise ValueError(f"manifest path does not exist: {candidate}")
    if not paths:
        raise ValueError("no manifest files were provided")
    return paths


def load_documents(paths: Iterable[Path]) -> list[dict[str, Any]]:
    documents: list[dict[str, Any]] = []
    for path in paths:
        for index, document in enumerate(yaml.safe_load_all(path.read_text()), start=1):
            if document is None:
                continue
            if not isinstance(document, dict):
                raise ValueError(f"{path}:{index} is not a YAML object")
            document["__source"] = f"{path}:{index}"
            documents.append(document)
    return documents


def resource_id(document: dict[str, Any]) -> tuple[str, str, str]:
    metadata = document.get("metadata") or {}
    return (
        str(document.get("kind", "")),
        str(metadata.get("namespace", "")),
        str(metadata.get("name", "")),
    )


def pod_spec(document: dict[str, Any]) -> dict[str, Any] | None:
    kind = document.get("kind")
    spec = document.get("spec") or {}
    if kind == "Deployment":
        return ((spec.get("template") or {}).get("spec") or {})
    if kind == "Job":
        return ((spec.get("template") or {}).get("spec") or {})
    return None


def require(condition: bool, message: str, errors: list[str]) -> None:
    if not condition:
        errors.append(message)


def validate_pod_security(document: dict[str, Any], errors: list[str]) -> None:
    spec = pod_spec(document)
    if spec is None:
        return
    source = document["__source"]
    security = spec.get("securityContext") or {}
    require(security.get("runAsNonRoot") is True, f"{source}: runAsNonRoot must be true", errors)
    require(
        (security.get("seccompProfile") or {}).get("type") == "RuntimeDefault",
        f"{source}: seccompProfile must be RuntimeDefault",
        errors,
    )
    require(
        spec.get("automountServiceAccountToken") is False,
        f"{source}: service-account token automount must be disabled",
        errors,
    )
    require(bool(spec.get("serviceAccountName")), f"{source}: service account is required", errors)

    for container in spec.get("containers") or []:
        name = str(container.get("name", "<unnamed>"))
        prefix = f"{source}: container {name}"
        image = str(container.get("image", ""))
        require(bool(image), f"{prefix} has no image", errors)
        require(
            not image.endswith(":latest") and ":latest@" not in image,
            f"{prefix} must not use the latest tag",
            errors,
        )
        container_security = container.get("securityContext") or {}
        require(
            container_security.get("allowPrivilegeEscalation") is False,
            f"{prefix} must disable privilege escalation",
            errors,
        )
        require(
            container_security.get("readOnlyRootFilesystem") is True,
            f"{prefix} must use a read-only root filesystem",
            errors,
        )
        dropped = ((container_security.get("capabilities") or {}).get("drop") or [])
        require("ALL" in dropped, f"{prefix} must drop all Linux capabilities", errors)
        resources = container.get("resources") or {}
        require(bool(resources.get("requests")), f"{prefix} needs resource requests", errors)
        require(bool(resources.get("limits")), f"{prefix} needs resource limits", errors)

        if name in {"api", "frontend"}:
            for probe_name in ("startupProbe", "readinessProbe", "livenessProbe"):
                require(bool(container.get(probe_name)), f"{prefix} needs {probe_name}", errors)


def validate_runtime_references(document: dict[str, Any], errors: list[str]) -> None:
    spec = pod_spec(document)
    if spec is None:
        return
    kind, _, name = resource_id(document)
    contract = RUNTIME_CONTRACTS.get((kind, name))
    if contract is None:
        return
    source = document["__source"]
    containers = spec.get("containers") or []
    require(len(containers) == 1, f"{source}: workload must have exactly one container", errors)
    if not containers:
        return
    container = containers[0]
    expected_container = str(contract["container"])
    require(
        container.get("name") == expected_container,
        f"{source}: expected container {expected_container}",
        errors,
    )
    expected_service_account = str(contract["service_account"])
    require(
        spec.get("serviceAccountName") == expected_service_account,
        f"{source}: service account must be {expected_service_account}",
        errors,
    )

    env_from = container.get("envFrom") or []
    config_names = [
        (entry.get("configMapRef") or {}).get("name")
        for entry in env_from
        if entry.get("configMapRef") is not None
    ]
    secret_names = [
        (entry.get("secretRef") or {}).get("name")
        for entry in env_from
        if entry.get("secretRef") is not None
    ]
    expected_secrets = set(contract["secrets"])
    expected_configs = set() if expected_container == "frontend" else {EXPECTED_CONFIG_MAP}
    require(
        set(config_names) == expected_configs and len(config_names) == len(expected_configs),
        f"{source}: config-map refs must be exactly {sorted(expected_configs)}",
        errors,
    )
    require(
        set(secret_names) == expected_secrets and len(secret_names) == len(expected_secrets),
        f"{source}: secret refs must be exactly {sorted(expected_secrets)}",
        errors,
    )

    if name in {"pageragent-outbox-relay", "pageragent-migrate"}:
        environment = {
            str(entry.get("name")): entry.get("value") for entry in container.get("env") or []
        }
        require(
            environment.get("PAGERAGENT_CONNECTOR_CIPHER_PROVIDER") == "local",
            f"{source}: non-connector workload must override the connector cipher to local",
            errors,
        )


def validate_rollout_annotations(
    document: dict[str, Any],
    values: dict[str, set[str]],
    errors: list[str],
) -> None:
    if pod_spec(document) is None:
        return
    source = document["__source"]
    template = ((document.get("spec") or {}).get("template") or {})
    annotations = (template.get("metadata") or {}).get("annotations") or {}
    for annotation in ROLLOUT_ANNOTATIONS:
        value = str(annotations.get(annotation, ""))
        require(
            bool(REVISION_TOKEN.fullmatch(value)),
            f"{source}: {annotation} must contain a deterministic revision token",
            errors,
        )
        if value:
            values[annotation].add(value)


def validate(documents: list[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    seen: dict[tuple[str, str, str], str] = {}
    resources: dict[tuple[str, str], dict[str, Any]] = {}
    rollout_values = {annotation: set() for annotation in ROLLOUT_ANNOTATIONS}

    for document in documents:
        source = document["__source"]
        kind, namespace, name = resource_id(document)
        require(bool(document.get("apiVersion")), f"{source}: apiVersion is required", errors)
        require(bool(kind), f"{source}: kind is required", errors)
        require(bool(name), f"{source}: metadata.name is required", errors)
        identifier = (kind, namespace, name)
        if identifier in seen:
            errors.append(f"{source}: duplicates {identifier} from {seen[identifier]}")
        seen[identifier] = source
        resources[(kind, name)] = document

        require(kind != "Secret", f"{source}: secret values must not be committed", errors)
        if kind != "Namespace":
            require(
                namespace == EXPECTED_NAMESPACE,
                f"{source}: namespace must be {EXPECTED_NAMESPACE}",
                errors,
            )
        validate_pod_security(document, errors)
        validate_runtime_references(document, errors)
        validate_rollout_annotations(document, rollout_values, errors)
    expected = {
        ("Namespace", "pageragent"),
        ("ServiceAccount", "pageragent-api"),
        ("ServiceAccount", "pageragent-workflow-worker"),
        ("ServiceAccount", "pageragent-outbox-relay"),
        ("ServiceAccount", "pageragent-migration"),
        ("ServiceAccount", "pageragent-frontend"),
        ("ConfigMap", EXPECTED_CONFIG_MAP),
        ("Ingress", "pageragent"),
        ("Deployment", "pageragent-api"),
        ("Deployment", "pageragent-frontend"),
        ("Deployment", "pageragent-workflow-worker"),
        ("Deployment", "pageragent-outbox-relay"),
        ("Job", "pageragent-migrate"),
    }
    missing = sorted(expected - resources.keys())
    require(not missing, f"missing required resources: {missing}", errors)

    for service_account in (
        "pageragent-api",
        "pageragent-workflow-worker",
        "pageragent-outbox-relay",
        "pageragent-migration",
        "pageragent-frontend",
    ):
        resource = resources.get(("ServiceAccount", service_account)) or {}
        require(
            resource.get("automountServiceAccountToken") is False,
            f"ServiceAccount {service_account} must disable token automount",
            errors,
        )

    for annotation, values in rollout_values.items():
        require(
            len(values) == 1,
            f"all workloads must share one {annotation} value: {sorted(values)}",
            errors,
        )

    namespace = resources.get(("Namespace", EXPECTED_NAMESPACE)) or {}
    namespace_labels = (namespace.get("metadata") or {}).get("labels") or {}
    require(
        namespace_labels.get("pod-security.kubernetes.io/enforce") == "restricted",
        "Namespace must enforce the restricted Pod Security Standard",
        errors,
    )
    require(
        namespace_labels.get("pod-security.kubernetes.io/enforce-version") == "latest",
        "Namespace must enforce the latest Pod Security Standard version",
        errors,
    )

    config = ((resources.get(("ConfigMap", EXPECTED_CONFIG_MAP)) or {}).get("data") or {})
    required_config = {
        "PAGERAGENT_ENV": "production",
        "PAGERAGENT_AUTH_MODE": "oidc",
        "PAGERAGENT_SESSION_COOKIE_SECURE": "true",
        "PAGERAGENT_CONNECTOR_CIPHER_PROVIDER": "aws_kms",
        "GITHUB_EVIDENCE_MODE": "connector",
        "PROMETHEUS_EVIDENCE_MODE": "connector",
    }
    for key, expected_value in required_config.items():
        require(
            str(config.get(key, "")).lower() == expected_value,
            f"{EXPECTED_CONFIG_MAP}: {key} must be {expected_value}",
            errors,
        )
    ingress = resources.get(("Ingress", "pageragent")) or {}
    ingress_spec = ingress.get("spec") or {}
    require(bool(ingress_spec.get("tls")), "Ingress must terminate TLS", errors)
    ingress_rules = ingress_spec.get("rules") or []
    public_host = str((ingress_rules[0] if ingress_rules else {}).get("host", ""))
    trusted_hosts = str(config.get("PAGERAGENT_TRUSTED_HOSTS", ""))
    require(
        bool(public_host) and trusted_hosts == public_host,
        "trusted hosts must contain only the exact public Ingress host",
        errors,
    )
    require(
        config.get("PAGERAGENT_HEALTH_HOST") == public_host,
        "image health checks must send the exact public Host header",
        errors,
    )

    api = resources.get(("Deployment", "pageragent-api")) or {}
    api_containers = (((api.get("spec") or {}).get("template") or {}).get("spec") or {}).get(
        "containers", []
    )
    if api_containers:
        api_container = api_containers[0]
        readiness_path = (
            ((api_container.get("readinessProbe") or {}).get("httpGet") or {}).get("path")
        )
        liveness_path = (
            ((api_container.get("livenessProbe") or {}).get("httpGet") or {}).get("path")
        )
        for probe_name in ("startupProbe", "readinessProbe", "livenessProbe"):
            headers = (
                ((api_container.get(probe_name) or {}).get("httpGet") or {}).get(
                    "httpHeaders", []
                )
            )
            host_values = [
                header.get("value")
                for header in headers
                if str(header.get("name", "")).lower() == "host"
            ]
            require(
                host_values == [public_host],
                f"API {probe_name} must send the exact public Host header",
                errors,
            )
        require(
            readiness_path == "/api/v1/health/ready",
            "API readiness must use the dependency-aware endpoint",
            errors,
        )
        require(
            liveness_path == "/api/v1/health/live",
            "API liveness must not depend on external services",
            errors,
        )

    frontend = resources.get(("Deployment", "pageragent-frontend")) or {}
    frontend_containers = (
        (((frontend.get("spec") or {}).get("template") or {}).get("spec") or {}).get(
            "containers", []
        )
    )
    if frontend_containers:
        frontend_container = frontend_containers[0]
        for probe_name in ("startupProbe", "readinessProbe", "livenessProbe"):
            probe_path = (
                ((frontend_container.get(probe_name) or {}).get("httpGet") or {}).get(
                    "path"
                )
            )
            require(
                probe_path == "/healthz",
                f"frontend {probe_name} must use the static health endpoint",
                errors,
            )

    migration = resources.get(("Job", "pageragent-migrate")) or {}
    migration_spec = migration.get("spec") or {}
    require(migration_spec.get("backoffLimit") is not None, "migration needs backoffLimit", errors)
    require(
        migration_spec.get("activeDeadlineSeconds") is not None,
        "migration needs activeDeadlineSeconds",
        errors,
    )

    backend_images: set[str] = set()
    for name in (
        "pageragent-api",
        "pageragent-workflow-worker",
        "pageragent-outbox-relay",
    ):
        deployment = resources.get(("Deployment", name)) or {}
        containers = (
            (((deployment.get("spec") or {}).get("template") or {}).get("spec") or {}).get(
                "containers", []
            )
        )
        if containers:
            backend_images.add(str(containers[0].get("image")))
    migration_containers = (
        ((migration_spec.get("template") or {}).get("spec") or {}).get("containers") or []
    )
    if migration_containers:
        migration_container = migration_containers[0]
        backend_images.add(str(migration_container.get("image")))
        require(
            migration_container.get("command") == ["python", "-m", "app.db.migrate"],
            "migration must use the compatibility-aware application gate",
            errors,
        )
    require(
        len(backend_images) == 1,
        f"migration, API, relay, and worker must use one backend image: {backend_images}",
        errors,
    )
    return errors


def main() -> int:
    try:
        paths = manifest_paths(sys.argv[1:] or ["deploy/kubernetes"])
        documents = load_documents(paths)
        errors = validate(documents)
    except (OSError, ValueError, yaml.YAMLError) as error:
        print(f"deployment validation failed: {error}", file=sys.stderr)
        return 2
    if errors:
        print("deployment validation failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print(f"validated {len(documents)} deployment resources from {len(paths)} file(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
