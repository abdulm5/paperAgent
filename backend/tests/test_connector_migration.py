from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, Table, create_engine, inspect
from sqlalchemy.engine import Engine

from app.auth.constants import DEFAULT_ORGANIZATION_ID
from app.core.config import settings


def migration_config() -> Config:
    return Config(str(Path(__file__).parents[1] / "alembic.ini"))


def schema_signature(engine: Engine) -> dict[str, object]:
    inspector = inspect(engine)
    with engine.connect() as connection:
        revision = connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one()
    return {
        "revision": revision,
        "tables": tuple(sorted(inspector.get_table_names())),
        "connector_columns": tuple(
            column["name"] for column in inspector.get_columns("connectors")
        ),
        "connector_uniques": tuple(
            sorted(
                tuple(constraint["column_names"])
                for constraint in inspector.get_unique_constraints("connectors")
            )
        ),
        "event_uniques": tuple(
            sorted(
                tuple(constraint["column_names"])
                for constraint in inspector.get_unique_constraints(
                    "connector_audit_events"
                )
            )
        ),
    }


def test_empty_connector_migration_round_trips_and_matches_orm_constraints(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'connectors-empty.db'}"
    previous_url = settings.database_url
    engine = None
    try:
        settings.database_url = database_url
        config = migration_config()
        command.upgrade(config, "20260715_0008")
        engine = create_engine(database_url)
        signature = schema_signature(engine)
        assert signature["revision"] == "20260715_0008"
        assert {
            "connectors",
            "connector_credentials",
            "connector_audit_events",
        }.issubset(signature["tables"])
        assert ("organization_id", "name") in signature["connector_uniques"]
        assert ("connector_id", "connector_version") in signature["event_uniques"]

        command.downgrade(config, "20260714_0007")
        downgraded_tables = inspect(engine).get_table_names()
        assert "connectors" not in downgraded_tables
        assert "connector_credentials" not in downgraded_tables
        assert "connector_audit_events" not in downgraded_tables

        command.upgrade(config, "20260715_0008")
        assert schema_signature(engine)["revision"] == "20260715_0008"
    finally:
        settings.database_url = previous_url
        if engine is not None:
            engine.dispose()


def test_connector_migration_refuses_nonempty_downgrade_before_any_ddl(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'connectors-populated.db'}"
    previous_url = settings.database_url
    engine = None
    try:
        settings.database_url = database_url
        config = migration_config()
        command.upgrade(config, "20260715_0008")
        engine = create_engine(database_url)
        metadata = MetaData()
        connectors = Table("connectors", metadata, autoload_with=engine)
        credentials = Table("connector_credentials", metadata, autoload_with=engine)
        events = Table("connector_audit_events", metadata, autoload_with=engine)
        connector_id = uuid4()
        now = datetime.now(UTC)
        with engine.begin() as connection:
            connection.execute(
                connectors.insert().values(
                    id=connector_id.hex,
                    organization_id=DEFAULT_ORGANIZATION_ID.hex,
                    name="Migration GitHub",
                    provider="github",
                    configuration={
                        "repository": "pageragent/checkout",
                        "app_id": 1,
                        "installation_id": 2,
                    },
                    enabled=False,
                    status="disabled",
                    version=1,
                    created_at=now,
                    updated_at=now,
                )
            )
            connection.execute(
                credentials.insert().values(
                    connector_id=connector_id.hex,
                    credential_version=1,
                    ciphertext=b"x" * 32,
                    ciphertext_nonce=b"n" * 12,
                    wrapped_data_key=b"w" * 48,
                    wrapped_key_nonce=b"k" * 12,
                    key_version="local-v1",
                    credential_field_names=["private_key"],
                    created_at=now,
                    updated_at=now,
                )
            )
            connection.execute(
                events.insert().values(
                    id=uuid4().hex,
                    organization_id=DEFAULT_ORGANIZATION_ID.hex,
                    connector_id=connector_id.hex,
                    event_type="connector.created",
                    actor="user:00000000-0000-0000-0000-000000000101",
                    connector_version=1,
                    payload={
                        "provider": "github",
                        "enabled": False,
                        "status": "disabled",
                        "configuration_fields": [
                            "app_id",
                            "installation_id",
                            "repository",
                        ],
                        "credential_fields": ["private_key"],
                        "credential_version": 1,
                    },
                    created_at=now,
                )
            )

        before = schema_signature(engine)
        with pytest.raises(RuntimeError, match="Cannot downgrade connector control plane"):
            command.downgrade(config, "20260714_0007")
        after = schema_signature(engine)

        assert after == before
        assert after["revision"] == "20260715_0008"
        with engine.connect() as connection:
            stored = connection.execute(credentials.select()).one()
        assert UUID(str(stored.connector_id)) == connector_id
        assert stored.ciphertext == b"x" * 32
    finally:
        settings.database_url = previous_url
        if engine is not None:
            engine.dispose()
