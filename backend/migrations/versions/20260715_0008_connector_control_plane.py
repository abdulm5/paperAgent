"""Add tenant-scoped connector control plane and encrypted credential custody.

Revision ID: 20260715_0008
Revises: 20260714_0007
Create Date: 2026-07-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260715_0008"
down_revision: str | None = "20260714_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

json_document = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def _assert_connector_data_is_empty() -> None:
    """Refuse a downgrade that would silently erase credential or audit history."""
    connection = op.get_bind()
    for table_name in (
        "connectors",
        "connector_credentials",
        "connector_audit_events",
    ):
        table = sa.table(table_name, sa.column("id"))
        if table_name == "connector_credentials":
            table = sa.table(table_name, sa.column("connector_id"))
        row_exists = connection.execute(
            sa.select(sa.literal(1)).select_from(table).limit(1)
        ).first()
        if row_exists is not None:
            raise RuntimeError(
                "Cannot downgrade connector control plane migration while connector, "
                "credential, or audit data exists. Preserve or explicitly migrate the "
                "connector ledger before downgrading."
            )


def upgrade() -> None:
    op.create_table(
        "connectors",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("configuration", json_document, nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("last_validated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_validation_ok", sa.Boolean(), nullable=True),
        sa.Column("last_validation_message", sa.String(length=500), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "provider IN ('github', 'prometheus', 'slack')",
            name="ck_connectors_provider",
        ),
        sa.CheckConstraint(
            "status IN ('configured', 'disabled', 'invalid')",
            name="ck_connectors_status",
        ),
        sa.CheckConstraint("version > 0", name="ck_connectors_version_positive"),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organization_id",
            "name",
            name="uq_connectors_organization_name",
        ),
    )
    op.create_index(
        "ix_connectors_organization_created",
        "connectors",
        ["organization_id", "created_at"],
    )

    op.create_table(
        "connector_credentials",
        sa.Column("connector_id", sa.Uuid(), nullable=False),
        sa.Column("credential_version", sa.Integer(), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("ciphertext_nonce", sa.LargeBinary(), nullable=False),
        sa.Column("wrapped_data_key", sa.LargeBinary(), nullable=False),
        sa.Column("wrapped_key_nonce", sa.LargeBinary(), nullable=False),
        sa.Column("key_version", sa.String(length=100), nullable=False),
        sa.Column("credential_field_names", json_document, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "credential_version > 0",
            name="ck_connector_credentials_version_positive",
        ),
        sa.CheckConstraint(
            "length(ciphertext_nonce) = 12",
            name="ck_connector_credentials_ciphertext_nonce_length",
        ),
        sa.CheckConstraint(
            "length(wrapped_key_nonce) = 12",
            name="ck_connector_credentials_wrapped_key_nonce_length",
        ),
        sa.CheckConstraint(
            "length(ciphertext) >= 16",
            name="ck_connector_credentials_ciphertext_length",
        ),
        sa.CheckConstraint(
            "length(wrapped_data_key) = 48",
            name="ck_connector_credentials_wrapped_key_length",
        ),
        sa.CheckConstraint(
            "length(ciphertext) <= 262144",
            name="ck_connector_credentials_ciphertext_max_length",
        ),
        sa.ForeignKeyConstraint(["connector_id"], ["connectors.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("connector_id"),
    )

    op.create_table(
        "connector_audit_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("connector_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("actor", sa.String(length=100), nullable=False),
        sa.Column("connector_version", sa.Integer(), nullable=False),
        sa.Column("payload", json_document, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "connector_version > 0",
            name="ck_connector_audit_events_version_positive",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["connector_id"],
            ["connectors.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "connector_id",
            "connector_version",
            name="uq_connector_audit_events_connector_version",
        ),
    )
    op.create_index(
        "ix_connector_audit_events_connector_created",
        "connector_audit_events",
        ["connector_id", "created_at"],
    )
    op.create_index(
        "ix_connector_audit_events_organization_created",
        "connector_audit_events",
        ["organization_id", "created_at"],
    )


def downgrade() -> None:
    _assert_connector_data_is_empty()
    op.drop_index(
        "ix_connector_audit_events_organization_created",
        table_name="connector_audit_events",
    )
    op.drop_index(
        "ix_connector_audit_events_connector_created",
        table_name="connector_audit_events",
    )
    op.drop_table("connector_audit_events")
    op.drop_table("connector_credentials")
    op.drop_index("ix_connectors_organization_created", table_name="connectors")
    op.drop_table("connectors")
