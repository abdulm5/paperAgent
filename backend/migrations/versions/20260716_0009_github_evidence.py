"""Add a durable, replay-safe inbox for normalized GitHub webhook evidence.

Revision ID: 20260716_0009
Revises: 20260715_0008
Create Date: 2026-07-16
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260716_0009"
down_revision: str | None = "20260715_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

json_document = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def _assert_github_delivery_data_is_empty() -> None:
    """Refuse a downgrade that would silently erase verified provider evidence."""

    connection = op.get_bind()
    deliveries = sa.table("github_webhook_deliveries", sa.column("connector_id"))
    row_exists = connection.execute(
        sa.select(sa.literal(1)).select_from(deliveries).limit(1)
    ).first()
    if row_exists is not None:
        raise RuntimeError(
            "Cannot downgrade GitHub evidence migration while webhook deliveries exist. "
            "Preserve or explicitly migrate the verified delivery ledger before downgrading."
        )


def _disable_github_connectors(*, actor: str) -> None:
    """Require GitHub envelopes crossing this contract boundary to revalidate."""

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
        ).where(connectors.c.provider == "github")
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
            raise RuntimeError("GitHub connector changed during Phase 9B migration")
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
    # The composite key makes tenant ownership an at-rest invariant instead of
    # relying only on the runtime's derived organization binding.
    with op.batch_alter_table("connectors") as batch_op:
        batch_op.create_unique_constraint(
            "uq_connectors_organization_id",
            ["organization_id", "id"],
        )
    _disable_github_connectors(actor="migration:phase-9b")
    op.create_table(
        "github_webhook_deliveries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("connector_id", sa.Uuid(), nullable=False),
        sa.Column("delivery_id", sa.String(length=36), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=True),
        sa.Column("repository", sa.String(length=201), nullable=False),
        sa.Column("installation_id", sa.BigInteger(), nullable=False),
        sa.Column("connector_version", sa.Integer(), nullable=False),
        sa.Column("credential_version", sa.Integer(), nullable=False),
        sa.Column("body_sha256", sa.String(length=64), nullable=False),
        sa.Column("normalized_payload", json_document, nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "event_type IN ('push', 'pull_request', 'deployment', 'deployment_status', 'release')",
            name="ck_github_webhook_deliveries_event_type",
        ),
        sa.CheckConstraint(
            "action IS NULL OR (length(action) >= 1 AND length(action) <= 32)",
            name="ck_github_webhook_deliveries_action_length",
        ),
        sa.CheckConstraint(
            "length(delivery_id) = 36",
            name="ck_github_webhook_deliveries_delivery_id_length",
        ),
        sa.CheckConstraint(
            "length(body_sha256) = 64",
            name="ck_github_webhook_deliveries_body_hash_length",
        ),
        sa.CheckConstraint(
            "installation_id > 0",
            name="ck_github_webhook_deliveries_installation_positive",
        ),
        sa.CheckConstraint(
            "connector_version > 0 AND credential_version > 0",
            name="ck_github_webhook_deliveries_versions_positive",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "connector_id"],
            ["connectors.organization_id", "connectors.id"],
            name="fk_github_webhook_deliveries_tenant_connector",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "connector_id",
            "delivery_id",
            name="uq_github_webhook_deliveries_connector_delivery",
        ),
    )
    op.create_index(
        "ix_github_webhook_deliveries_organization_received",
        "github_webhook_deliveries",
        ["organization_id", "received_at"],
    )
    op.create_index(
        "ix_github_webhook_deliveries_connector_repository_received",
        "github_webhook_deliveries",
        ["connector_id", "repository", "received_at"],
    )


def downgrade() -> None:
    _assert_github_delivery_data_is_empty()
    _disable_github_connectors(actor="migration:phase-9b-downgrade")
    op.drop_index(
        "ix_github_webhook_deliveries_connector_repository_received",
        table_name="github_webhook_deliveries",
    )
    op.drop_index(
        "ix_github_webhook_deliveries_organization_received",
        table_name="github_webhook_deliveries",
    )
    op.drop_table("github_webhook_deliveries")
    with op.batch_alter_table("connectors") as batch_op:
        batch_op.drop_constraint(
            "uq_connectors_organization_id",
            type_="unique",
        )
    # Deliberately do not restore GitHub validation or enablement. A downgrade
    # must not silently re-authorize credentials across the contract boundary.
