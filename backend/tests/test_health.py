from collections.abc import Iterator
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from app.core.config import settings
from app.db.schema import (
    APPLICATION_SCHEMA_GENERATION,
    SCHEMA_CONTRACT_INTRODUCTION_GENERATION,
    SCHEMA_HEAD_REVISION,
    classify_schema_revisions,
)
from app.db.session import get_db
from app.main import app


def test_health_check_returns_service_status() -> None:
    response = TestClient(app).get("/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "pageragent-api",
        "environment": "local",
    }


def test_readiness_schema_contract_tracks_the_single_alembic_head() -> None:
    backend_directory = Path(__file__).resolve().parents[1]
    migration_scripts = ScriptDirectory.from_config(
        Config(str(backend_directory / "alembic.ini"))
    )

    assert migration_scripts.get_heads() == [SCHEMA_HEAD_REVISION]
    assert SCHEMA_HEAD_REVISION.endswith(
        f"_{SCHEMA_CONTRACT_INTRODUCTION_GENERATION:04d}"
    )
    assert classify_schema_revisions(
        {"20260719_0014"}, APPLICATION_SCHEMA_GENERATION
    ) == "forward_compatible"


class ReadySession:
    def execute(self, _statement) -> None:
        return None

    def scalars(self, _statement):
        return RevisionValues([SCHEMA_HEAD_REVISION])

    def scalar(self, _statement) -> int:
        return APPLICATION_SCHEMA_GENERATION

    def rollback(self) -> None:
        return None


class RevisionValues:
    def __init__(self, values: list[str]) -> None:
        self.values = values

    def all(self) -> list[str]:
        return self.values


class UnavailableSession:
    def execute(self, _statement) -> None:
        raise OperationalError("SELECT 1", {}, RuntimeError("database details"))


class StaleSchemaSession(ReadySession):
    def scalars(self, _statement) -> RevisionValues:
        return RevisionValues(["20260717_0011", SCHEMA_HEAD_REVISION])


class ForwardCompatibleSchemaSession(ReadySession):
    def scalars(self, _statement) -> RevisionValues:
        return RevisionValues(["20260719_0014"])


class IncompatibleSchemaSession(ForwardCompatibleSchemaSession):
    def scalar(self, _statement) -> int:
        return APPLICATION_SCHEMA_GENERATION + 1


class UnexpectedFailureSession:
    def execute(self, _statement) -> None:
        raise RuntimeError("sensitive unexpected database failure")


def _ready_database() -> Iterator[ReadySession]:
    yield ReadySession()


def _unavailable_database() -> Iterator[UnavailableSession]:
    yield UnavailableSession()


def _stale_schema() -> Iterator[StaleSchemaSession]:
    yield StaleSchemaSession()


def _forward_compatible_schema() -> Iterator[ForwardCompatibleSchemaSession]:
    yield ForwardCompatibleSchemaSession()


def _incompatible_schema() -> Iterator[IncompatibleSchemaSession]:
    yield IncompatibleSchemaSession()


def _unexpected_failure() -> Iterator[UnexpectedFailureSession]:
    yield UnexpectedFailureSession()


def test_liveness_does_not_claim_dependency_readiness() -> None:
    response = TestClient(app).get("/api/v1/health/live")

    assert response.status_code == 200
    assert response.json()["status"] == "alive"
    assert response.headers["cache-control"] == "no-store"


def test_readiness_requires_database_and_current_schema() -> None:
    app.dependency_overrides[get_db] = _ready_database
    response = TestClient(app).get("/api/v1/health/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "service": "pageragent-api",
        "environment": "local",
        "checks": {"database": "ok", "schema": "current"},
    }
    assert response.headers["cache-control"] == "no-store"


def test_readiness_sanitizes_database_failure() -> None:
    app.dependency_overrides[get_db] = _unavailable_database
    response = TestClient(app).get("/api/v1/health/ready")

    assert response.status_code == 503
    assert response.json()["checks"] == {
        "database": "unavailable",
        "schema": "unknown",
    }
    assert "database details" not in response.text


def test_readiness_rejects_a_process_ahead_of_its_database_schema() -> None:
    app.dependency_overrides[get_db] = _stale_schema
    response = TestClient(app).get("/api/v1/health/ready")

    assert response.status_code == 503
    assert response.json()["checks"] == {
        "database": "ok",
        "schema": "migration_required",
    }


def test_readiness_accepts_one_newer_expand_contract_generation() -> None:
    app.dependency_overrides[get_db] = _forward_compatible_schema
    response = TestClient(app).get("/api/v1/health/ready")

    assert response.status_code == 200
    assert response.json()["checks"] == {
        "database": "ok",
        "schema": "forward_compatible",
    }


def test_readiness_rejects_a_schema_contract_that_requires_newer_code() -> None:
    app.dependency_overrides[get_db] = _incompatible_schema
    response = TestClient(app).get("/api/v1/health/ready")

    assert response.status_code == 503
    assert response.json()["checks"] == {
        "database": "ok",
        "schema": "application_incompatible",
    }


def test_api_responses_receive_security_headers(monkeypatch) -> None:
    response = TestClient(app).get("/api/v1/health/live")

    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "content-security-policy" not in response.headers
    assert "strict-transport-security" not in response.headers

    monkeypatch.setattr(settings, "environment", "production")
    hosted = TestClient(app).get("/api/v1/health/live")
    assert hosted.headers["content-security-policy"] == (
        "default-src 'none'; base-uri 'none'; frame-ancestors 'none'"
    )
    assert hosted.headers["strict-transport-security"] == (
        "max-age=31536000; includeSubDomains"
    )


def test_untrusted_host_is_rejected_before_route_handling() -> None:
    response = TestClient(app).get(
        "/api/v1/health/live",
        headers={"Host": "attacker.example"},
    )

    assert response.status_code == 400
    assert response.text == "Invalid host header"


def test_host_matching_is_case_insensitive() -> None:
    response = TestClient(app).get(
        "/api/v1/health/live",
        headers={"Host": "PAGERAGENT.TEST"},
    )

    assert response.status_code == 200


def test_unexpected_errors_are_sanitized_and_keep_hosted_headers(monkeypatch) -> None:
    monkeypatch.setattr(settings, "environment", "production")
    app.dependency_overrides[get_db] = _unexpected_failure
    response = TestClient(app, raise_server_exceptions=False).get(
        "/api/v1/health/ready"
    )

    assert response.status_code == 500
    assert response.json() == {"detail": "Internal server error"}
    assert "sensitive unexpected database failure" not in response.text
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["strict-transport-security"] == (
        "max-age=31536000; includeSubDomains"
    )
