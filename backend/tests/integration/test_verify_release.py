"""Tests for the immutable release-asset verification boundary."""

import runpy
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = runpy.run_path(str(ROOT / "deploy" / "verify_release.py"))
REVISION = "a" * 40
BACKEND_DIGEST = "sha256:" + "b" * 64
FRONTEND_DIGEST = "sha256:" + "c" * 64
BACKEND_IMAGE = f"ghcr.io/example/pageragent-backend@{BACKEND_DIGEST}"
FRONTEND_IMAGE = f"ghcr.io/example/pageragent-frontend@{FRONTEND_DIGEST}"


def _workload(kind: str, name: str, image: str) -> dict[str, object]:
    return {
        "apiVersion": "apps/v1" if kind == "Deployment" else "batch/v1",
        "kind": kind,
        "metadata": {"name": name, "namespace": "pageragent"},
        "spec": {"template": {"spec": {"containers": [{"name": "app", "image": image}]}}},
    }


def _assets(tmp_path: Path) -> tuple[Path, Path]:
    manifest = tmp_path / "pageragent-release.yaml"
    manifest.write_text(
        yaml.safe_dump_all(
            [
                _workload("Deployment", "pageragent-api", BACKEND_IMAGE),
                _workload("Deployment", "pageragent-workflow-worker", BACKEND_IMAGE),
                _workload("Deployment", "pageragent-outbox-relay", BACKEND_IMAGE),
                _workload("Job", "pageragent-migrate", BACKEND_IMAGE),
                _workload("Deployment", "pageragent-frontend", FRONTEND_IMAGE),
            ]
        ),
        encoding="utf-8",
    )
    metadata = tmp_path / "release-metadata.txt"
    metadata.write_text(
        "\n".join(
            (
                "release=v1.2.3",
                f"revision={REVISION}",
                f"backend_image={BACKEND_IMAGE}",
                f"frontend_image={FRONTEND_IMAGE}",
                "",
            )
        ),
        encoding="utf-8",
    )
    return manifest, metadata


def _verify(manifest: Path, metadata: Path):
    return SCRIPT["verify_release"](
        manifest_path=manifest,
        metadata_path=metadata,
        expected_tag="v1.2.3",
        expected_revision=REVISION,
        repository="example/paperAgent",
    )


def test_verifies_full_repository_images_and_exact_release_workloads(tmp_path: Path) -> None:
    manifest, metadata = _assets(tmp_path)

    release = _verify(manifest, metadata)

    assert release.backend_image == BACKEND_IMAGE
    assert release.frontend_image == FRONTEND_IMAGE
    assert len(release.manifest_sha256) == 64


def test_rejects_metadata_revision_that_is_not_the_checked_out_tag(tmp_path: Path) -> None:
    manifest, metadata = _assets(tmp_path)

    with pytest.raises(ValueError, match="checked-out tag"):
        SCRIPT["verify_release"](
            manifest_path=manifest,
            metadata_path=metadata,
            expected_tag="v1.2.3",
            expected_revision="d" * 40,
            repository="example/paperAgent",
        )


def test_rejects_image_from_an_untrusted_registry_or_repository(tmp_path: Path) -> None:
    manifest, metadata = _assets(tmp_path)
    metadata.write_text(
        metadata.read_text(encoding="utf-8").replace(
            "ghcr.io/example/pageragent-backend", "registry.example/pageragent-backend"
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="release repository"):
        _verify(manifest, metadata)


def test_rejects_manifest_image_that_differs_from_attested_metadata(tmp_path: Path) -> None:
    manifest, metadata = _assets(tmp_path)
    documents = list(yaml.safe_load_all(manifest.read_text(encoding="utf-8")))
    documents[0]["spec"]["template"]["spec"]["containers"][0]["image"] = (
        f"ghcr.io/example/pageragent-backend@sha256:{'d' * 64}"
    )
    manifest.write_text(yaml.safe_dump_all(documents), encoding="utf-8")

    with pytest.raises(ValueError, match="not pinned to backend_image"):
        _verify(manifest, metadata)


def test_rejects_an_unexpected_workload_or_init_container(tmp_path: Path) -> None:
    manifest, metadata = _assets(tmp_path)
    documents = list(yaml.safe_load_all(manifest.read_text(encoding="utf-8")))
    documents.append(_workload("Deployment", "unreviewed-service", BACKEND_IMAGE))
    manifest.write_text(yaml.safe_dump_all(documents), encoding="utf-8")

    with pytest.raises(ValueError, match="unexpected=.*unreviewed-service"):
        _verify(manifest, metadata)

    manifest, metadata = _assets(tmp_path)
    documents = list(yaml.safe_load_all(manifest.read_text(encoding="utf-8")))
    documents[0]["spec"]["template"]["spec"]["initContainers"] = [
        {"name": "unreviewed", "image": BACKEND_IMAGE}
    ]
    manifest.write_text(yaml.safe_dump_all(documents), encoding="utf-8")

    with pytest.raises(ValueError, match="unexpected container"):
        _verify(manifest, metadata)


def test_rejects_duplicate_or_unknown_metadata_keys(tmp_path: Path) -> None:
    manifest, metadata = _assets(tmp_path)
    metadata.write_text(
        metadata.read_text(encoding="utf-8") + "backend_image=duplicate\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate metadata key"):
        _verify(manifest, metadata)

    _, metadata = _assets(tmp_path)
    metadata.write_text(
        metadata.read_text(encoding="utf-8") + "untrusted=value\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match=r"unexpected=\['untrusted'\]"):
        _verify(manifest, metadata)
