"""Require Prometheus connectors to revalidate for live evidence collection.

Revision ID: 20260716_0010
Revises: 20260716_0009
Create Date: 2026-07-16
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260716_0010"
down_revision: str | None = "20260716_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

json_document = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def _disable_prometheus_connectors(*, actor: str) -> None:
    """Fail closed whenever a Prometheus connector crosses this contract boundary."""

    connection = op.get_bind()
    connectors = sa.table(
        "connectors",
        sa.column("id", sa.Uuid()),
        sa.column("organization_id", sa.Uuid()),
        sa.column("provider", sa.String()),
        sa.column("configuration", json_document),
        sa.column("enabled", sa.Boolean()),
        sa.column("status", sa.String()),
        sa.column("version", sa.Integer()),
        sa.column("last_validated_at", sa.DateTime(timezone=True)),
        sa.column("last_validation_ok", sa.Boolean()),
        sa.column("last_validation_message", sa.String()),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    events = sa.table(
        "connector_audit_events",
        sa.column("id", sa.Uuid()),
        sa.column("organization_id", sa.Uuid()),
        sa.column("connector_id", sa.Uuid()),
        sa.column("event_type", sa.String()),
        sa.column("actor", sa.String()),
        sa.column("connector_version", sa.Integer()),
        sa.column("payload", json_document),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    records = connection.execute(
        sa.select(
            connectors.c.id,
            connectors.c.organization_id,
            connectors.c.configuration,
            connectors.c.version,
        ).where(connectors.c.provider == "prometheus")
    ).mappings().all()
    now = datetime.now(UTC)
    for record in records:
        configuration = record["configuration"]
        configuration_fields = (
            sorted(
                key
                for key in configuration
                if isinstance(key, str) and len(key) <= 100
            )[:32]
            if isinstance(configuration, dict)
            else []
        )
        new_version = record["version"] + 1
        result = connection.execute(
            connectors.update()
            .where(
                connectors.c.id == record["id"],
                connectors.c.version == record["version"],
            )
            .values(
                enabled=False,
                status="disabled",
                version=new_version,
                last_validated_at=None,
                last_validation_ok=None,
                last_validation_message=None,
                updated_at=now,
            )
        )
        if result.rowcount != 1:
            raise RuntimeError("Prometheus connector changed during Phase 9C migration")
        connection.execute(
            events.insert().values(
                id=uuid4(),
                organization_id=record["organization_id"],
                connector_id=record["id"],
                event_type="connector.updated",
                actor=actor,
                connector_version=new_version,
                payload={
                    "changed_fields": ["enabled", "status", "validation"],
                    "enabled": False,
                    "status": "disabled",
                    "configuration_fields": configuration_fields,
                },
                created_at=now,
            )
        )


def upgrade() -> None:
    _disable_prometheus_connectors(actor="migration:phase-9c")


def downgrade() -> None:
    _disable_prometheus_connectors(actor="migration:phase-9c-downgrade")
    # Deliberately do not restore Prometheus validation or enablement. Either
    # direction crosses a runtime evidence contract and therefore requires a
    # fresh provider handshake before the connector can regain authority.
