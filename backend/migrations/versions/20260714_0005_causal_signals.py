"""Create generalized causal signal candidates.

Revision ID: 20260714_0005
Revises: 20260711_0004
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.db.models import json_document

revision: str = "20260714_0005"
down_revision: str | None = "20260711_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "cause_candidates",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("investigation_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("reference", sa.String(length=200), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("explanation", json_document, nullable=False),
        sa.Column("evidence_ids", json_document, nullable=False),
        sa.ForeignKeyConstraint(
            ["investigation_id"], ["investigation_runs.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_cause_candidates_investigation_rank",
        "cause_candidates",
        ["investigation_id", "rank"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_cause_candidates_investigation_rank", table_name="cause_candidates"
    )
    op.drop_table("cause_candidates")
