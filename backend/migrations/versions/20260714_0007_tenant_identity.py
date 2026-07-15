"""Add tenant identity, memberships, and incident ownership.

Revision ID: 20260714_0007
Revises: 20260714_0006
Create Date: 2026-07-14
"""

from collections.abc import Sequence
from uuid import UUID

import sqlalchemy as sa
from alembic import op

revision: str = "20260714_0007"
down_revision: str | None = "20260714_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DEFAULT_ORGANIZATION_ID = UUID("00000000-0000-0000-0000-000000000001")
DEFAULT_ORGANIZATION_SLUG = "pageragent-labs"
DEFAULT_ORGANIZATION_NAME = "PagerAgent Labs"


def _legacy_active_fingerprint_constraint() -> str:
    """Return the reflected name, including a convention name for SQLite."""
    constraints = sa.inspect(op.get_bind()).get_unique_constraints("incidents")
    for constraint in constraints:
        if constraint.get("column_names") == ["active_fingerprint"]:
            return constraint.get("name") or "uq_incidents_active_fingerprint"
    raise RuntimeError("Expected legacy active_fingerprint uniqueness constraint")


def _assert_legacy_fingerprint_uniqueness_can_be_restored() -> None:
    """Refuse a lossy downgrade before SQLite or PostgreSQL executes any DDL."""
    incidents = sa.table(
        "incidents",
        sa.column("active_fingerprint", sa.String()),
    )
    duplicate = (
        op.get_bind()
        .execute(
            sa.select(incidents.c.active_fingerprint)
            .where(incidents.c.active_fingerprint.is_not(None))
            .group_by(incidents.c.active_fingerprint)
            .having(sa.func.count() > 1)
            .limit(1)
        )
        .scalar_one_or_none()
    )
    if duplicate is not None:
        raise RuntimeError(
            "Cannot downgrade tenant identity migration: multiple organizations "
            "contain the same non-null active_fingerprint. Resolve or close the "
            "duplicate active incidents before downgrading."
        )


def upgrade() -> None:
    op.create_table(
        "organizations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("slug", sa.String(length=100), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug", name="uq_organizations_slug"),
    )
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("issuer", sa.String(length=500), nullable=False),
        sa.Column("subject", sa.String(length=500), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("issuer", "subject", name="uq_users_issuer_subject"),
    )
    op.create_table(
        "organization_memberships",
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("organization_id", "user_id"),
    )
    op.create_index(
        "ix_organization_memberships_user",
        "organization_memberships",
        ["user_id"],
    )

    organizations = sa.table(
        "organizations",
        sa.column("id", sa.Uuid()),
        sa.column("slug", sa.String()),
        sa.column("name", sa.String()),
    )
    op.bulk_insert(
        organizations,
        [
            {
                "id": DEFAULT_ORGANIZATION_ID,
                "slug": DEFAULT_ORGANIZATION_SLUG,
                "name": DEFAULT_ORGANIZATION_NAME,
            }
        ],
    )

    op.add_column("incidents", sa.Column("organization_id", sa.Uuid(), nullable=True))
    incidents = sa.table("incidents", sa.column("organization_id", sa.Uuid()))
    op.execute(incidents.update().values(organization_id=DEFAULT_ORGANIZATION_ID))

    legacy_unique_name = _legacy_active_fingerprint_constraint()
    with op.batch_alter_table(
        "incidents",
        naming_convention={"uq": "uq_%(table_name)s_%(column_0_name)s"},
    ) as batch_op:
        batch_op.drop_constraint(legacy_unique_name, type_="unique")
        batch_op.alter_column("organization_id", nullable=False)
        batch_op.create_foreign_key(
            "fk_incidents_organization_id_organizations",
            "organizations",
            ["organization_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_unique_constraint(
            "uq_incidents_organization_active_fingerprint",
            ["organization_id", "active_fingerprint"],
        )
    op.create_index(
        "ix_incidents_organization_received_at",
        "incidents",
        ["organization_id", "received_at"],
    )


def downgrade() -> None:
    _assert_legacy_fingerprint_uniqueness_can_be_restored()

    op.drop_index("ix_incidents_organization_received_at", table_name="incidents")
    with op.batch_alter_table(
        "incidents",
        naming_convention={"uq": "uq_%(table_name)s_%(column_0_name)s"},
    ) as batch_op:
        batch_op.drop_constraint(
            "uq_incidents_organization_active_fingerprint",
            type_="unique",
        )
        batch_op.drop_constraint(
            "fk_incidents_organization_id_organizations",
            type_="foreignkey",
        )
        batch_op.drop_column("organization_id")
        batch_op.create_unique_constraint(
            "uq_incidents_active_fingerprint",
            ["active_fingerprint"],
        )

    op.drop_index("ix_organization_memberships_user", table_name="organization_memberships")
    op.drop_table("organization_memberships")
    op.drop_table("users")
    op.drop_table("organizations")
