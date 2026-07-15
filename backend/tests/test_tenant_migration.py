from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, Table, create_engine, func, inspect, select
from sqlalchemy.engine import Engine

from app.auth.constants import DEFAULT_ORGANIZATION_ID
from app.core.config import settings


def _tenant_schema_signature(engine: Engine) -> dict[str, object]:
    inspector = inspect(engine)

    def constraints(kind: str) -> tuple[tuple[str, tuple[str, ...]], ...]:
        values = getattr(inspector, kind)("incidents")
        return tuple(
            sorted(
                (
                    str(value.get("name") or ""),
                    tuple(value.get("column_names") or value.get("constrained_columns") or ()),
                )
                for value in values
            )
        )

    with engine.connect() as connection:
        revision = connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one()
    return {
        "revision": revision,
        "tables": tuple(sorted(inspector.get_table_names())),
        "incident_columns": tuple(
            (column["name"], str(column["type"]), column["nullable"])
            for column in inspector.get_columns("incidents")
        ),
        "incident_indexes": tuple(
            sorted(index["name"] for index in inspector.get_indexes("incidents"))
        ),
        "incident_unique_constraints": constraints("get_unique_constraints"),
        "incident_foreign_keys": constraints("get_foreign_keys"),
        "membership_indexes": tuple(
            sorted(index["name"] for index in inspector.get_indexes("organization_memberships"))
        ),
    }


def test_populated_phase_seven_database_backfills_and_round_trips_tenant_schema(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "phase8a-migration.db"
    database_url = f"sqlite+pysqlite:///{database_path}"
    config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    previous_url = settings.database_url
    engine = None
    try:
        settings.database_url = database_url
        command.upgrade(config, "20260714_0006")
        engine = create_engine(database_url)
        metadata = MetaData()
        incidents = Table("incidents", metadata, autoload_with=engine)
        incident_id = uuid4()
        now = datetime.now(UTC)
        with engine.begin() as connection:
            connection.execute(
                incidents.insert().values(
                    id=incident_id.hex,
                    fingerprint="legacy:checkout-error-rate",
                    active_fingerprint="legacy:checkout-error-rate",
                    status="detected",
                    service="checkout-api",
                    severity="critical",
                    summary="Incident created before tenant migration.",
                    started_at=now,
                    detected_at=now,
                    version=1,
                )
            )

        command.upgrade(config, "head")
        upgraded = MetaData()
        upgraded_incidents = Table("incidents", upgraded, autoload_with=engine)
        organizations = Table("organizations", upgraded, autoload_with=engine)
        with engine.connect() as connection:
            row = connection.execute(
                select(upgraded_incidents).where(upgraded_incidents.c.id == incident_id.hex)
            ).one()
            organization_count = connection.scalar(select(func.count()).select_from(organizations))
        assert UUID(str(row.organization_id)) == DEFAULT_ORGANIZATION_ID
        assert organization_count == 1
        assert "organization_id" in upgraded_incidents.c

        command.downgrade(config, "20260714_0006")
        downgraded_inspector = inspect(engine)
        assert "organizations" not in downgraded_inspector.get_table_names()
        assert "organization_id" not in {
            column["name"] for column in downgraded_inspector.get_columns("incidents")
        }

        command.upgrade(config, "head")
        recovered = Table("incidents", MetaData(), autoload_with=engine)
        with engine.connect() as connection:
            recovered_row = connection.execute(
                select(recovered).where(recovered.c.id == incident_id.hex)
            ).one()
        assert UUID(str(recovered_row.organization_id)) == DEFAULT_ORGANIZATION_ID
    finally:
        settings.database_url = previous_url
        if engine is not None:
            engine.dispose()


def test_downgrade_refuses_cross_tenant_active_fingerprint_collision_before_ddl(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "phase8a-unsafe-downgrade.db"
    database_url = f"sqlite+pysqlite:///{database_path}"
    config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    previous_url = settings.database_url
    engine = None
    try:
        settings.database_url = database_url
        command.upgrade(config, "20260714_0007")
        engine = create_engine(database_url)
        metadata = MetaData()
        incidents = Table("incidents", metadata, autoload_with=engine)
        organizations = Table("organizations", metadata, autoload_with=engine)
        other_organization_id = UUID("00000000-0000-0000-0000-000000000002")
        now = datetime.now(UTC)
        shared_active_fingerprint = "shared:active-checkout-failure"
        with engine.begin() as connection:
            connection.execute(
                organizations.insert().values(
                    id=other_organization_id.hex,
                    slug="other-operations",
                    name="Other Operations",
                )
            )
            connection.execute(
                incidents.insert(),
                [
                    {
                        "id": uuid4().hex,
                        "organization_id": DEFAULT_ORGANIZATION_ID.hex,
                        "fingerprint": "default:checkout-failure",
                        "active_fingerprint": shared_active_fingerprint,
                        "status": "detected",
                        "service": "checkout-api",
                        "severity": "critical",
                        "summary": "Default tenant active incident.",
                        "started_at": now,
                        "detected_at": now,
                        "version": 1,
                    },
                    {
                        "id": uuid4().hex,
                        "organization_id": other_organization_id.hex,
                        "fingerprint": "other:checkout-failure",
                        "active_fingerprint": shared_active_fingerprint,
                        "status": "detected",
                        "service": "checkout-api",
                        "severity": "critical",
                        "summary": "Other tenant active incident.",
                        "started_at": now,
                        "detected_at": now,
                        "version": 1,
                    },
                ],
            )

        before = _tenant_schema_signature(engine)
        with pytest.raises(
            RuntimeError,
            match="Cannot downgrade tenant identity migration",
        ):
            command.downgrade(config, "20260714_0006")
        after = _tenant_schema_signature(engine)

        assert after == before
        assert after["revision"] == "20260714_0007"
        with engine.connect() as connection:
            collision_count = connection.scalar(
                select(func.count())
                .select_from(incidents)
                .where(incidents.c.active_fingerprint == shared_active_fingerprint)
            )
        assert collision_count == 2
    finally:
        settings.database_url = previous_url
        if engine is not None:
            engine.dispose()
