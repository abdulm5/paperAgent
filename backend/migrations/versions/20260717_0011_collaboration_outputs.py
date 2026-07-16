"""Add approval-gated durable Slack and GitHub collaboration outputs.

Revision ID: 20260717_0011
Revises: 20260716_0010
Create Date: 2026-07-17
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260717_0011"
down_revision: str | None = "20260716_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

json_document = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def _disable_slack_connectors(*, actor: str) -> None:
    """Require Slack rows to cross the service-binding and live-handshake boundary."""

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
        ).where(connectors.c.provider == "slack")
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
            raise RuntimeError("Slack connector changed during Phase 9D migration")
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


def _assert_collaboration_ledger_is_empty() -> None:
    connection = op.get_bind()
    for table_name, identifier in (
        ("collaboration_decisions", "id"),
        ("collaboration_deliveries", "output_id"),
        ("collaboration_outputs", "id"),
    ):
        table = sa.table(table_name, sa.column(identifier))
        if connection.execute(
            sa.select(sa.literal(1)).select_from(table).limit(1)
        ).first() is not None:
            raise RuntimeError(
                "Cannot downgrade while collaboration approvals or delivery receipts exist"
            )


def upgrade() -> None:
    op.create_table(
        "collaboration_outputs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("incident_id", sa.Uuid(), nullable=False),
        sa.Column("proposal_id", sa.Uuid(), nullable=False),
        sa.Column("connector_id", sa.Uuid(), nullable=False),
        sa.Column("workflow_run_id", sa.Uuid(), nullable=True),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("destination", sa.String(length=500), nullable=False),
        sa.Column("payload", json_document, nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("connector_version", sa.Integer(), nullable=False),
        sa.Column("credential_version", sa.Integer(), nullable=False),
        sa.Column("requested_by", sa.String(length=100), nullable=False),
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_reason", sa.String(length=500), nullable=True),
        sa.CheckConstraint(
            "kind IN ('slack_update', 'github_issue')",
            name="ck_collaboration_outputs_kind",
        ),
        sa.CheckConstraint(
            "provider IN ('slack', 'github')",
            name="ck_collaboration_outputs_provider",
        ),
        sa.CheckConstraint(
            "(kind = 'slack_update' AND provider = 'slack') OR "
            "(kind = 'github_issue' AND provider = 'github')",
            name="ck_collaboration_outputs_kind_provider",
        ),
        sa.CheckConstraint(
            "status IN ('pending_approval', 'rejected', 'queued', 'delivering', "
            "'retry_scheduled', 'delivered', 'dead_lettered')",
            name="ck_collaboration_outputs_status",
        ),
        sa.CheckConstraint(
            "version > 0",
            name="ck_collaboration_outputs_version_positive",
        ),
        sa.CheckConstraint(
            "connector_version > 0 AND credential_version > 0",
            name="ck_collaboration_outputs_connector_versions_positive",
        ),
        sa.CheckConstraint(
            "length(content_sha256) = 64",
            name="ck_collaboration_outputs_content_hash_length",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["incident_id"], ["incidents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["proposal_id"], ["mitigation_proposals.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "connector_id"],
            ["connectors.organization_id", "connectors.id"],
            name="fk_collaboration_outputs_tenant_connector",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workflow_run_id"], ["workflow_runs.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "proposal_id",
            "kind",
            name="uq_collaboration_outputs_proposal_kind",
        ),
        sa.UniqueConstraint("workflow_run_id"),
    )
    op.create_index(
        "ix_collaboration_outputs_incident_requested",
        "collaboration_outputs",
        ["incident_id", "requested_at"],
    )
    op.create_index(
        "ix_collaboration_outputs_status_requested",
        "collaboration_outputs",
        ["status", "requested_at"],
    )

    op.create_table(
        "collaboration_decisions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("output_id", sa.Uuid(), nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("actor", sa.String(length=100), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["output_id"], ["collaboration_outputs.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_collaboration_decisions_output_created",
        "collaboration_decisions",
        ["output_id", "created_at"],
    )

    op.create_table(
        "collaboration_deliveries",
        sa.Column("output_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("provider_receipt", json_document, nullable=False),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('prepared', 'delivering', 'retry_scheduled', 'delivered', "
            "'dead_lettered')",
            name="ck_collaboration_deliveries_status",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="ck_collaboration_deliveries_attempt_count_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["output_id"], ["collaboration_outputs.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("output_id"),
        sa.UniqueConstraint("idempotency_key"),
    )
    _disable_slack_connectors(actor="migration:phase-9d")


def downgrade() -> None:
    _assert_collaboration_ledger_is_empty()
    _disable_slack_connectors(actor="migration:phase-9d-downgrade")
    op.drop_table("collaboration_deliveries")
    op.drop_index(
        "ix_collaboration_decisions_output_created",
        table_name="collaboration_decisions",
    )
    op.drop_table("collaboration_decisions")
    op.drop_index(
        "ix_collaboration_outputs_status_requested",
        table_name="collaboration_outputs",
    )
    op.drop_index(
        "ix_collaboration_outputs_incident_requested",
        table_name="collaboration_outputs",
    )
    op.drop_table("collaboration_outputs")
