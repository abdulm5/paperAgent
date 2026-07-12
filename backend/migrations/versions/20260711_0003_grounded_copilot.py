"""Create grounded mitigation proposal and approval tables.

Revision ID: 20260711_0003
Revises: 20260710_0002
Create Date: 2026-07-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260711_0003"
down_revision: str | None = "20260710_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

json_document = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "mitigation_proposals",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("incident_id", sa.Uuid(), nullable=False),
        sa.Column("investigation_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("synthesizer_version", sa.String(length=64), nullable=False),
        sa.Column("model_name", sa.String(length=100), nullable=False),
        sa.Column("prompt_version", sa.String(length=64), nullable=False),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("root_cause_summary", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("impact_summary", sa.Text(), nullable=False),
        sa.Column("recommended_action", sa.Text(), nullable=False),
        sa.Column("risk_summary", sa.Text(), nullable=False),
        sa.Column("verification_steps", json_document, nullable=False),
        sa.Column("slack_update", sa.Text(), nullable=False),
        sa.Column("claims", json_document, nullable=False),
        sa.Column("action_type", sa.String(length=64), nullable=False),
        sa.Column("action_target", sa.String(length=100), nullable=False),
        sa.Column("action_parameters", json_document, nullable=False),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["incident_id"], ["incidents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["investigation_id"], ["investigation_runs.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_mitigation_proposals_incident_created",
        "mitigation_proposals",
        ["incident_id", "created_at"],
    )

    op.create_table(
        "proposal_decisions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("proposal_id", sa.Uuid(), nullable=False),
        sa.Column("incident_id", sa.Uuid(), nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("actor", sa.String(length=100), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["incident_id"], ["incidents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["proposal_id"], ["mitigation_proposals.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_proposal_decisions_proposal_created",
        "proposal_decisions",
        ["proposal_id", "created_at"],
    )

    op.create_table(
        "mitigation_executions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("proposal_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("executor_version", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=100), nullable=False),
        sa.Column("request_payload", json_document, nullable=False),
        sa.Column("response_payload", json_document, nullable=False),
        sa.Column("before_telemetry", json_document, nullable=False),
        sa.Column("after_telemetry", json_document, nullable=False),
        sa.Column("recovery_verified", sa.Boolean(), nullable=False),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["proposal_id"], ["mitigation_proposals.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
        sa.UniqueConstraint("proposal_id"),
    )


def downgrade() -> None:
    op.drop_table("mitigation_executions")
    op.drop_index("ix_proposal_decisions_proposal_created", table_name="proposal_decisions")
    op.drop_table("proposal_decisions")
    op.drop_index("ix_mitigation_proposals_incident_created", table_name="mitigation_proposals")
    op.drop_table("mitigation_proposals")
