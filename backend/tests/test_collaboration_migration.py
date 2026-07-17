from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, Table, create_engine, inspect

from app.auth.constants import DEFAULT_ORGANIZATION_ID
from app.core.config import settings


def _migration_config() -> Config:
    backend_root = Path(__file__).parents[1]
    config = Config(str(backend_root / "alembic.ini"))
    config.set_main_option("script_location", str(backend_root / "migrations"))
    return config


def test_empty_collaboration_migration_round_trips_on_sqlite(tmp_path: Path) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'collaboration-empty.db'}"
    previous_url = settings.database_url
    engine = None
    try:
        settings.database_url = database_url
        config = _migration_config()
        command.upgrade(config, "20260717_0011")
        engine = create_engine(database_url)
        inspector = inspect(engine)
        assert {
            "collaboration_outputs",
            "collaboration_decisions",
            "collaboration_deliveries",
        } <= set(inspector.get_table_names())
        assert {
            "id",
            "organization_id",
            "incident_id",
            "proposal_id",
            "connector_id",
            "workflow_run_id",
            "content_sha256",
        } <= {
            column["name"]
            for column in inspector.get_columns("collaboration_outputs")
        }
        foreign_keys = {
            (
                tuple(constraint["constrained_columns"]),
                constraint["referred_table"],
                tuple(constraint["referred_columns"]),
            )
            for constraint in inspector.get_foreign_keys("collaboration_outputs")
        }
        assert {
            (
                ("organization_id", "connector_id"),
                "connectors",
                ("organization_id", "id"),
            ),
            (
                ("organization_id", "incident_id"),
                "incidents",
                ("organization_id", "id"),
            ),
            (
                ("incident_id", "proposal_id"),
                "mitigation_proposals",
                ("incident_id", "id"),
            ),
            (
                ("incident_id", "workflow_run_id"),
                "workflow_runs",
                ("incident_id", "id"),
            ),
        } <= foreign_keys
        with engine.connect() as connection:
            assert (
                connection.exec_driver_sql(
                    "SELECT version_num FROM alembic_version"
                ).scalar_one()
                == "20260717_0011"
            )

        command.downgrade(config, "20260716_0010")
        assert "collaboration_outputs" not in inspect(engine).get_table_names()
        command.upgrade(config, "20260717_0011")
        assert "collaboration_outputs" in inspect(engine).get_table_names()
    finally:
        settings.database_url = previous_url
        if engine is not None:
            engine.dispose()


def test_phase9d_disables_legacy_slack_on_upgrade_and_downgrade(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'collaboration-slack.db'}"
    previous_url = settings.database_url
    engine = None
    try:
        settings.database_url = database_url
        config = _migration_config()
        command.upgrade(config, "20260716_0010")
        engine = create_engine(database_url)
        metadata = MetaData()
        connectors = Table("connectors", metadata, autoload_with=engine)
        events = Table("connector_audit_events", metadata, autoload_with=engine)
        connector_id = uuid4()
        now = datetime.now(UTC)
        configuration = {
            "channel": "incident-room-name",
            "api_url": "https://slack.com",
            "token_hint": "must-not-enter-the-audit-event",
        }
        with engine.begin() as connection:
            connection.execute(
                connectors.insert().values(
                    id=connector_id.hex,
                    organization_id=DEFAULT_ORGANIZATION_ID.hex,
                    name="Legacy Slack",
                    provider="slack",
                    configuration=configuration,
                    enabled=True,
                    status="configured",
                    version=3,
                    last_validated_at=now,
                    last_validation_ok=True,
                    last_validation_message="Legacy local validation passed.",
                    created_at=now,
                    updated_at=now,
                )
            )

        command.upgrade(config, "20260717_0011")
        with engine.connect() as connection:
            upgraded = connection.execute(
                connectors.select().where(connectors.c.id == connector_id.hex)
            ).one()
            upgrade_event = connection.execute(
                events.select().where(
                    events.c.connector_id == connector_id.hex,
                    events.c.connector_version == 4,
                )
            ).one()
        assert upgraded.enabled is False
        assert upgraded.status == "disabled"
        assert upgraded.version == 4
        assert upgraded.last_validation_ok is None
        assert upgrade_event.actor == "migration:phase-9d"
        assert upgrade_event.payload == {
            "changed_fields": ["enabled", "status", "validation"],
            "enabled": False,
            "status": "disabled",
            "configuration_fields": sorted(configuration),
        }
        assert "must-not-enter" not in str(upgrade_event.payload)

        command.downgrade(config, "20260716_0010")
        with engine.connect() as connection:
            downgraded = connection.execute(
                connectors.select().where(connectors.c.id == connector_id.hex)
            ).one()
            downgrade_event = connection.execute(
                events.select().where(
                    events.c.connector_id == connector_id.hex,
                    events.c.connector_version == 5,
                )
            ).one()
        assert downgraded.enabled is False
        assert downgraded.version == 5
        assert downgrade_event.actor == "migration:phase-9d-downgrade"
    finally:
        settings.database_url = previous_url
        if engine is not None:
            engine.dispose()


def test_phase9d_downgrade_refuses_to_discard_a_populated_approval_ledger(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'collaboration-ledger.db'}"
    previous_url = settings.database_url
    engine = None
    try:
        settings.database_url = database_url
        config = _migration_config()
        command.upgrade(config, "20260717_0011")
        engine = create_engine(database_url)
        metadata = MetaData()
        outputs = Table("collaboration_outputs", metadata, autoload_with=engine)
        now = datetime.now(UTC)
        with engine.begin() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys = OFF")
            connection.execute(
                outputs.insert().values(
                    id=uuid4().hex,
                    organization_id=uuid4().hex,
                    incident_id=uuid4().hex,
                    proposal_id=uuid4().hex,
                    connector_id=uuid4().hex,
                    workflow_run_id=None,
                    kind="slack_update",
                    provider="slack",
                    status="pending_approval",
                    version=1,
                    destination="C0123456789",
                    payload={"text": "bounded migration fixture"},
                    content_sha256="a" * 64,
                    connector_version=1,
                    credential_version=1,
                    requested_by="test:migration",
                    requested_at=now,
                )
            )

        with pytest.raises(
            RuntimeError,
            match="Cannot downgrade while collaboration approvals or delivery receipts exist",
        ):
            command.downgrade(config, "20260716_0010")

        with engine.connect() as connection:
            assert (
                connection.exec_driver_sql(
                    "SELECT version_num FROM alembic_version"
                ).scalar_one()
                == "20260717_0011"
            )
            assert connection.execute(outputs.select()).first() is not None
    finally:
        settings.database_url = previous_url
        if engine is not None:
            engine.dispose()
