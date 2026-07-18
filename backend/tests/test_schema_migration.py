from collections.abc import Iterator

import pytest

from app.db import migrate
from app.db.migrate import SchemaSnapshot
from app.db.schema import (
    APPLICATION_SCHEMA_GENERATION,
    SCHEMA_HEAD_REVISION,
    classify_schema_revisions,
)


def test_schema_contract_does_not_infer_future_compatibility_from_a_revision() -> None:
    assert classify_schema_revisions({"20260719_0014"}, None) == (
        "application_incompatible"
    )
    assert classify_schema_revisions(
        {"20260719_0014"}, APPLICATION_SCHEMA_GENERATION
    ) == "forward_compatible"
    assert classify_schema_revisions(
        {"20260719_0014"}, APPLICATION_SCHEMA_GENERATION + 1
    ) == "application_incompatible"


def _snapshots(*snapshots: SchemaSnapshot) -> Iterator[SchemaSnapshot]:
    yield from snapshots


def test_migration_gate_noops_for_a_compatible_newer_database(monkeypatch) -> None:
    upgrade_called = False
    snapshots = _snapshots(SchemaSnapshot({"20260719_0014"}, 12))
    monkeypatch.setattr(migrate, "inspect_schema", lambda _engine: next(snapshots))

    def upgrade() -> None:
        nonlocal upgrade_called
        upgrade_called = True

    assert migrate.migrate_database(object(), upgrade) == "forward_compatible"
    assert upgrade_called is False


def test_migration_gate_rejects_a_database_that_requires_newer_code(monkeypatch) -> None:
    snapshots = _snapshots(SchemaSnapshot({"20260719_0014"}, 13))
    monkeypatch.setattr(migrate, "inspect_schema", lambda _engine: next(snapshots))

    with pytest.raises(RuntimeError, match="newer PagerAgent"):
        migrate.migrate_database(object(), lambda: None)


def test_migration_gate_upgrades_an_older_database_and_rechecks(monkeypatch) -> None:
    snapshots = _snapshots(
        SchemaSnapshot({"20260718_0012"}, None),
        SchemaSnapshot({SCHEMA_HEAD_REVISION}, APPLICATION_SCHEMA_GENERATION),
    )
    monkeypatch.setattr(migrate, "inspect_schema", lambda _engine: next(snapshots))
    calls = 0

    def upgrade() -> None:
        nonlocal calls
        calls += 1

    assert migrate.migrate_database(object(), upgrade) == "current"
    assert calls == 1
