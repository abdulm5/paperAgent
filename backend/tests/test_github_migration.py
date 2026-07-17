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


def github_schema_signature(engine: Engine) -> dict[str, object]:
    inspector = inspect(engine)
    with engine.connect() as connection:
        revision = connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one()
    return {
        "revision": revision,
        "tables": tuple(sorted(inspector.get_table_names())),
        "columns": tuple(
            column["name"]
            for column in inspector.get_columns("github_webhook_deliveries")
        ),
        "uniques": tuple(
            sorted(
                tuple(constraint["column_names"])
                for constraint in inspector.get_unique_constraints(
                    "github_webhook_deliveries"
                )
            )
        ),
        "connector_uniques": tuple(
            sorted(
                tuple(constraint["column_names"])
                for constraint in inspector.get_unique_constraints("connectors")
            )
        ),
        "foreign_keys": tuple(
            sorted(
                (
                    tuple(constraint["constrained_columns"]),
                    constraint["referred_table"],
                    tuple(constraint["referred_columns"]),
                )
                for constraint in inspector.get_foreign_keys(
                    "github_webhook_deliveries"
                )
            )
        ),
        "indexes": tuple(
            sorted(
                tuple(index["column_names"])
                for index in inspector.get_indexes("github_webhook_deliveries")
            )
        ),
        "checks": tuple(
            sorted(
                constraint["name"]
                for constraint in inspector.get_check_constraints(
                    "github_webhook_deliveries"
                )
            )
        ),
    }


def test_empty_github_evidence_migration_round_trips_on_sqlite(tmp_path: Path) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'github-evidence-empty.db'}"
    previous_url = settings.database_url
    engine = None
    try:
        settings.database_url = database_url
        config = migration_config()
        command.upgrade(config, "20260716_0010")
        engine = create_engine(database_url)
        signature = github_schema_signature(engine)

        assert signature["revision"] == "20260716_0010"
        assert "github_webhook_deliveries" in signature["tables"]
        assert signature["columns"] == (
            "id",
            "organization_id",
            "connector_id",
            "delivery_id",
            "event_type",
            "action",
            "repository",
            "installation_id",
            "connector_version",
            "credential_version",
            "body_sha256",
            "normalized_payload",
            "received_at",
        )
        assert ("connector_id", "delivery_id") in signature["uniques"]
        assert ("organization_id", "id") in signature["connector_uniques"]
        assert (
            ("organization_id", "connector_id"),
            "connectors",
            ("organization_id", "id"),
        ) in signature["foreign_keys"]
        assert (
            "connector_id",
            "repository",
            "received_at",
        ) in signature["indexes"]
        assert (
            "organization_id",
            "received_at",
        ) in signature["indexes"]
        assert "ck_github_webhook_deliveries_body_hash_length" in signature["checks"]

        command.downgrade(config, "20260715_0008")
        assert "github_webhook_deliveries" not in inspect(engine).get_table_names()
        with engine.connect() as connection:
            assert (
                connection.exec_driver_sql(
                    "SELECT version_num FROM alembic_version"
                ).scalar_one()
                == "20260715_0008"
            )

        command.upgrade(config, "20260716_0010")
        assert github_schema_signature(engine)["revision"] == "20260716_0010"
    finally:
        settings.database_url = previous_url
        if engine is not None:
            engine.dispose()


def test_upgrade_disables_and_audits_populated_phase9a_github_connector(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'github-evidence-legacy.db'}"
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
        configuration = {
            "repository": "pageragent/checkout",
            "app_id": 1,
            "installation_id": 2,
        }
        with engine.begin() as connection:
            connection.execute(
                connectors.insert().values(
                    id=connector_id.hex,
                    organization_id=DEFAULT_ORGANIZATION_ID.hex,
                    name="Legacy GitHub",
                    provider="github",
                    configuration=configuration,
                    enabled=True,
                    status="configured",
                    version=1,
                    last_validated_at=now,
                    last_validation_ok=True,
                    last_validation_message="Phase 9A local validation passed.",
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
                        "enabled": True,
                        "status": "configured",
                        "configuration_fields": sorted(configuration),
                        "credential_fields": ["private_key"],
                        "credential_version": 1,
                    },
                    created_at=now,
                )
            )

        command.upgrade(config, "20260716_0009")

        migrated_metadata = MetaData()
        migrated_connectors = Table(
            "connectors", migrated_metadata, autoload_with=engine
        )
        migrated_events = Table(
            "connector_audit_events", migrated_metadata, autoload_with=engine
        )
        with engine.connect() as connection:
            connector = connection.execute(
                migrated_connectors.select().where(
                    migrated_connectors.c.id == connector_id.hex
                )
            ).one()
            migration_event = connection.execute(
                migrated_events.select().where(
                    migrated_events.c.connector_id == connector_id.hex,
                    migrated_events.c.connector_version == 2,
                )
            ).one()

        assert connector.enabled is False
        assert connector.status == "disabled"
        assert connector.version == 2
        assert connector.last_validated_at is None
        assert connector.last_validation_ok is None
        assert connector.last_validation_message is None
        assert migration_event.event_type == "connector.updated"
        assert migration_event.actor == "migration:phase-9b"
        assert migration_event.payload == {
            "changed_fields": ["enabled", "status", "validation"],
            "enabled": False,
            "status": "disabled",
            "configuration_fields": sorted(configuration),
        }

        command.downgrade(config, "20260715_0008")
        downgraded_metadata = MetaData()
        downgraded_connectors = Table(
            "connectors", downgraded_metadata, autoload_with=engine
        )
        downgraded_events = Table(
            "connector_audit_events", downgraded_metadata, autoload_with=engine
        )
        with engine.connect() as connection:
            downgraded = connection.execute(
                downgraded_connectors.select().where(
                    downgraded_connectors.c.id == connector_id.hex
                )
            ).one()
            downgrade_event = connection.execute(
                downgraded_events.select().where(
                    downgraded_events.c.connector_id == connector_id.hex,
                    downgraded_events.c.connector_version == 3,
                )
            ).one()
        assert downgraded.enabled is False
        assert downgraded.status == "disabled"
        assert downgraded.version == 3
        assert downgrade_event.actor == "migration:phase-9b-downgrade"
    finally:
        settings.database_url = previous_url
        if engine is not None:
            engine.dispose()


def test_github_evidence_migration_refuses_nonempty_downgrade_before_ddl(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'github-evidence-populated.db'}"
    previous_url = settings.database_url
    engine = None
    try:
        settings.database_url = database_url
        config = migration_config()
        command.upgrade(config, "20260716_0009")
        engine = create_engine(database_url)
        metadata = MetaData()
        connectors = Table("connectors", metadata, autoload_with=engine)
        deliveries = Table(
            "github_webhook_deliveries",
            metadata,
            autoload_with=engine,
        )
        connector_id = uuid4()
        delivery_id = uuid4()
        record_id = uuid4()
        now = datetime.now(UTC)
        with engine.begin() as connection:
            connection.execute(
                connectors.insert().values(
                    id=connector_id.hex,
                    organization_id=DEFAULT_ORGANIZATION_ID.hex,
                    name="Migration GitHub",
                    provider="github",
                    configuration={
                        "service": "checkout-api",
                        "repository": "pageragent/checkout",
                        "app_id": 1,
                        "installation_id": 2,
                        "api_url": "https://api.github.com",
                    },
                    enabled=True,
                    status="configured",
                    version=1,
                    created_at=now,
                    updated_at=now,
                )
            )
            connection.execute(
                deliveries.insert().values(
                    id=record_id.hex,
                    organization_id=DEFAULT_ORGANIZATION_ID.hex,
                    connector_id=connector_id.hex,
                    delivery_id=str(delivery_id),
                    event_type="release",
                    action="published",
                    repository="pageragent/checkout",
                    installation_id=2,
                    connector_version=1,
                    credential_version=1,
                    body_sha256="a" * 64,
                    normalized_payload={
                        "service": "checkout-api",
                        "release_id": 1,
                        "tag_name": "v1",
                        "name": "v1",
                        "draft": False,
                        "prerelease": False,
                        "created_at": now.isoformat(),
                        "published_at": now.isoformat(),
                    },
                    received_at=now,
                )
            )

        before = github_schema_signature(engine)
        with pytest.raises(RuntimeError, match="Cannot downgrade GitHub evidence migration"):
            command.downgrade(config, "20260715_0008")
        after = github_schema_signature(engine)

        assert after == before
        assert after["revision"] == "20260716_0009"
        with engine.connect() as connection:
            stored = connection.execute(deliveries.select()).one()
        assert UUID(str(stored.id)) == record_id
        assert UUID(str(stored.connector_id)) == connector_id
        assert stored.body_sha256 == "a" * 64
    finally:
        settings.database_url = previous_url
        if engine is not None:
            engine.dispose()
