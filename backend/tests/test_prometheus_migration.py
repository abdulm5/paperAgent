from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, Table, create_engine

from app.auth.constants import DEFAULT_ORGANIZATION_ID
from app.core.config import settings


def migration_config() -> Config:
    return Config(str(Path(__file__).parents[1] / "alembic.ini"))


def test_prometheus_contract_boundary_disables_and_audits_both_directions(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'prometheus-evidence.db'}"
    previous_url = settings.database_url
    engine = None
    try:
        settings.database_url = database_url
        config = migration_config()
        command.upgrade(config, "20260716_0009")
        engine = create_engine(database_url)
        metadata = MetaData()
        connectors = Table("connectors", metadata, autoload_with=engine)
        events = Table("connector_audit_events", metadata, autoload_with=engine)
        prometheus_id = uuid4()
        github_id = uuid4()
        now = datetime.now(UTC)
        configuration = {
            "base_url": "https://prometheus.example",
            "service": "checkout-api",
            "authorization_hint": "must-not-appear-in-the-audit-event",
        }
        with engine.begin() as connection:
            connection.execute(
                connectors.insert(),
                [
                    {
                        "id": prometheus_id.hex,
                        "organization_id": DEFAULT_ORGANIZATION_ID.hex,
                        "name": "Checkout Prometheus",
                        "provider": "prometheus",
                        "configuration": configuration,
                        "enabled": True,
                        "status": "configured",
                        "version": 7,
                        "last_validated_at": now,
                        "last_validation_ok": True,
                        "last_validation_message": "Legacy local validation passed.",
                        "created_at": now,
                        "updated_at": now,
                    },
                    {
                        "id": github_id.hex,
                        "organization_id": DEFAULT_ORGANIZATION_ID.hex,
                        "name": "Unrelated GitHub",
                        "provider": "github",
                        "configuration": {
                            "service": "checkout-api",
                            "repository": "pageragent/checkout",
                        },
                        "enabled": True,
                        "status": "configured",
                        "version": 4,
                        "last_validated_at": now,
                        "last_validation_ok": True,
                        "last_validation_message": "GitHub validation passed.",
                        "created_at": now,
                        "updated_at": now,
                    },
                ],
            )

        command.upgrade(config, "20260716_0010")

        with engine.connect() as connection:
            upgraded = connection.execute(
                connectors.select().where(connectors.c.id == prometheus_id.hex)
            ).one()
            unrelated = connection.execute(
                connectors.select().where(connectors.c.id == github_id.hex)
            ).one()
            upgrade_event = connection.execute(
                events.select().where(
                    events.c.connector_id == prometheus_id.hex,
                    events.c.connector_version == 8,
                )
            ).one()

        assert upgraded.enabled is False
        assert upgraded.status == "disabled"
        assert upgraded.version == 8
        assert upgraded.last_validated_at is None
        assert upgraded.last_validation_ok is None
        assert upgraded.last_validation_message is None
        assert unrelated.enabled is True
        assert unrelated.status == "configured"
        assert unrelated.version == 4
        assert upgrade_event.event_type == "connector.updated"
        assert upgrade_event.actor == "migration:phase-9c"
        assert upgrade_event.payload == {
            "changed_fields": ["enabled", "status", "validation"],
            "enabled": False,
            "status": "disabled",
            "configuration_fields": sorted(configuration),
        }
        assert "must-not-appear-in-the-audit-event" not in str(upgrade_event.payload)

        command.downgrade(config, "20260716_0009")

        with engine.connect() as connection:
            downgraded = connection.execute(
                connectors.select().where(connectors.c.id == prometheus_id.hex)
            ).one()
            downgrade_event = connection.execute(
                events.select().where(
                    events.c.connector_id == prometheus_id.hex,
                    events.c.connector_version == 9,
                )
            ).one()

        assert downgraded.enabled is False
        assert downgraded.status == "disabled"
        assert downgraded.version == 9
        assert downgraded.last_validated_at is None
        assert downgraded.last_validation_ok is None
        assert downgraded.last_validation_message is None
        assert downgrade_event.event_type == "connector.updated"
        assert downgrade_event.actor == "migration:phase-9c-downgrade"
        assert downgrade_event.payload == {
            "changed_fields": ["enabled", "status", "validation"],
            "enabled": False,
            "status": "disabled",
            "configuration_fields": sorted(configuration),
        }
    finally:
        settings.database_url = previous_url
        if engine is not None:
            engine.dispose()
