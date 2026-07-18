"""Add the explicit application/schema compatibility marker.

Revision ID: 20260718_0013
Revises: 20260718_0012
Create Date: 2026-07-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260718_0013"
down_revision: str | None = "20260718_0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "pageragent_schema_contract",
        sa.Column("singleton_id", sa.SmallInteger(), nullable=False),
        sa.Column("minimum_application_generation", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "singleton_id = 1",
            name="ck_pageragent_schema_contract_singleton",
        ),
        sa.CheckConstraint(
            "minimum_application_generation > 0",
            name="ck_pageragent_schema_contract_generation_positive",
        ),
        sa.PrimaryKeyConstraint("singleton_id"),
    )
    contract = sa.table(
        "pageragent_schema_contract",
        sa.column("singleton_id", sa.SmallInteger()),
        sa.column("minimum_application_generation", sa.Integer()),
    )
    op.bulk_insert(
        contract,
        [{"singleton_id": 1, "minimum_application_generation": 12}],
    )


def downgrade() -> None:
    op.drop_table("pageragent_schema_contract")
