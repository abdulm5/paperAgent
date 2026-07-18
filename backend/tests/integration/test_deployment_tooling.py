"""Contract tests for the production deployment scaffold."""

import argparse
import copy
import runpy
import shutil
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]


def _script(name: str) -> dict[str, object]:
    return runpy.run_path(str(ROOT / "deploy" / name))


@pytest.mark.parametrize(
    "host",
    [
        "example",
        "example.com",
        "pageragent.example.com",
        "pageragent.example.net",
        "pageragent.example.org",
        "pageragent.example",
        "pageragent.invalid",
        "pageragent.local",
        "pageragent.localhost",
        "pageragent.test",
        "invalid",
        "local",
        "test",
        "127.0.0.1",
    ],
)
def test_environment_config_rejects_reserved_hosts(host: str) -> None:
    script = _script("configure_environment.py")

    with pytest.raises(argparse.ArgumentTypeError, match="fully qualified"):
        script["public_host"](host)
    with pytest.raises(argparse.ArgumentTypeError, match="fully qualified"):
        script["exact_https_origin"](f"https://{host}")


def test_environment_config_normalizes_real_origins_and_revisions() -> None:
    script = _script("configure_environment.py")

    assert script["public_host"](" PagerAgent.ACME.dev. ") == "pageragent.acme.dev"
    assert (
        script["exact_https_origin"]("https://PROMETHEUS.ACME.dev.:8443/")
        == "https://prometheus.acme.dev:8443"
    )
    assert script["revision_token"](" v1.2.3-rc.1 ") == "v1.2.3-rc.1"
    with pytest.raises(argparse.ArgumentTypeError, match="revision"):
        script["revision_token"]("release/unsafe")
    with pytest.raises(argparse.ArgumentTypeError, match="revision"):
        script["revision_token"]("x" * 129)
    with pytest.raises(argparse.ArgumentTypeError, match="fully qualified"):
        script["exact_https_origin"]("https://*.acme.dev")


def test_environment_config_stamps_every_pod_template(tmp_path: Path) -> None:
    script = _script("configure_environment.py")
    deployment_root = tmp_path / "kubernetes"
    shutil.copytree(ROOT / "deploy" / "kubernetes", deployment_root)
    script["configure"].__globals__["ROOT"] = deployment_root
    args = argparse.Namespace(
        public_host="pageragent.acme.dev",
        prometheus_origin="https://prometheus.acme.dev",
        telemetry_origin="https://telemetry.acme.dev",
        release_revision="v1.2.3",
        runtime_secret_revision="secret-42",
    )

    script["configure"](args)

    first_render = {
        path.relative_to(deployment_root): path.read_bytes()
        for path in deployment_root.rglob("*.yaml")
    }
    script["configure"](args)
    second_render = {
        path.relative_to(deployment_root): path.read_bytes()
        for path in deployment_root.rglob("*.yaml")
    }
    assert second_render == first_render

    configured = yaml.safe_load(
        (deployment_root / "foundation" / "configmap.yaml").read_text()
    )
    assert configured["data"]["PAGERAGENT_TRUSTED_HOSTS"] == "pageragent.acme.dev"
    assert configured["data"]["PAGERAGENT_HEALTH_HOST"] == "pageragent.acme.dev"

    for relative_path in (
        "workloads/api.yaml",
        "workloads/workflow-worker.yaml",
        "workloads/outbox-relay.yaml",
        "workloads/frontend.yaml",
        "migration/job.yaml",
    ):
        workload = yaml.safe_load((deployment_root / relative_path).read_text())
        annotations = workload["spec"]["template"]["metadata"]["annotations"]
        assert annotations == {
            "pageragent.dev/release-revision": "v1.2.3",
            "pageragent.dev/runtime-secret-revision": "secret-42",
        }

    api = yaml.safe_load((deployment_root / "workloads" / "api.yaml").read_text())
    api_container = api["spec"]["template"]["spec"]["containers"][0]
    for probe_name in ("startupProbe", "readinessProbe", "livenessProbe"):
        assert api_container[probe_name]["httpGet"]["httpHeaders"] == [
            {"name": "Host", "value": "pageragent.acme.dev"}
        ]


def test_deployment_contract_enforces_least_privilege_runtime_identity() -> None:
    script = _script("validate_manifests.py")
    paths = script["manifest_paths"]([str(ROOT / "deploy" / "kubernetes")])
    documents = script["load_documents"](paths)

    assert script["validate"](copy.deepcopy(documents)) == []

    api = next(
        document
        for document in documents
        if document.get("kind") == "Deployment"
        and (document.get("metadata") or {}).get("name") == "pageragent-api"
    )
    api["spec"]["template"]["spec"]["serviceAccountName"] = "pageragent"
    errors = script["validate"](documents)

    assert any("service account must be pageragent-api" in error for error in errors)


def test_deployment_contract_requires_the_compatibility_aware_migration_gate() -> None:
    script = _script("validate_manifests.py")
    paths = script["manifest_paths"]([str(ROOT / "deploy" / "kubernetes")])
    documents = script["load_documents"](paths)
    migration = next(
        document
        for document in documents
        if document.get("kind") == "Job"
        and (document.get("metadata") or {}).get("name") == "pageragent-migrate"
    )
    migration["spec"]["template"]["spec"]["containers"][0]["command"] = [
        "alembic",
        "upgrade",
        "head",
    ]

    errors = script["validate"](documents)

    assert "migration must use the compatibility-aware application gate" in errors


def test_deployment_contract_rejects_internal_trusted_host_expansion() -> None:
    script = _script("validate_manifests.py")
    paths = script["manifest_paths"]([str(ROOT / "deploy" / "kubernetes")])
    documents = script["load_documents"](paths)
    config = next(
        document
        for document in documents
        if document.get("kind") == "ConfigMap"
        and (document.get("metadata") or {}).get("name") == "pageragent-runtime"
    )
    config["data"]["PAGERAGENT_TRUSTED_HOSTS"] += ",pageragent-api"

    errors = script["validate"](documents)

    assert "trusted hosts must contain only the exact public Ingress host" in errors


def test_deployment_contract_rejects_secret_scope_expansion() -> None:
    script = _script("validate_manifests.py")
    paths = script["manifest_paths"]([str(ROOT / "deploy" / "kubernetes")])
    documents = script["load_documents"](paths)
    relay = next(
        document
        for document in documents
        if document.get("kind") == "Deployment"
        and (document.get("metadata") or {}).get("name") == "pageragent-outbox-relay"
    )
    relay["spec"]["template"]["spec"]["containers"][0]["envFrom"].append(
        {"secretRef": {"name": "pageragent-api-secrets"}}
    )

    errors = script["validate"](documents)

    assert any("secret refs must be exactly" in error for error in errors)
