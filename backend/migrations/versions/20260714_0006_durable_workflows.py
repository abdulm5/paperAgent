"""Create durable workflow, event, and transactional outbox records.

Revision ID: 20260714_0006
Revises: 20260714_0005
Create Date: 2026-07-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260714_0006"
down_revision: str | None = "20260714_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

json_document = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
event_id_type = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "workflow_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("incident_id", sa.Uuid(), nullable=False),
        sa.Column("workflow_type", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("current_step", sa.String(length=64), nullable=True),
        sa.Column("dedupe_key", sa.String(length=200), nullable=False),
        sa.Column("trace_id", sa.String(length=128), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("failure_reason", sa.Text(), nullable=True),
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
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["incident_id"], ["incidents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dedupe_key", name="uq_workflow_runs_dedupe_key"),
    )
    op.create_index(
        "ix_workflow_runs_incident_created",
        "workflow_runs",
        ["incident_id", "created_at"],
    )
    op.create_index(
        "ix_workflow_runs_status_updated",
        "workflow_runs",
        ["status", "updated_at"],
    )

    op.create_table(
        "workflow_jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workflow_run_id", sa.Uuid(), nullable=False),
        sa.Column("step_type", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("payload", json_document, nullable=False),
        sa.Column("result", json_document, nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("lease_owner", sa.String(length=100), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
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
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["workflow_run_id"], ["workflow_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_workflow_jobs_idempotency_key"),
        sa.UniqueConstraint("workflow_run_id", "step_type", name="uq_workflow_jobs_run_step"),
    )
    op.create_index("ix_workflow_jobs_due", "workflow_jobs", ["status", "available_at"])
    op.create_index("ix_workflow_jobs_lease", "workflow_jobs", ["status", "lease_expires_at"])

    op.create_table(
        "workflow_events",
        sa.Column("id", event_id_type, autoincrement=True, nullable=False),
        sa.Column("workflow_run_id", sa.Uuid(), nullable=False),
        sa.Column("workflow_job_id", sa.Uuid(), nullable=True),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("payload", json_document, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["workflow_job_id"], ["workflow_jobs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["workflow_run_id"], ["workflow_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workflow_run_id", "sequence", name="uq_workflow_events_run_sequence"),
    )
    op.create_index(
        "ix_workflow_events_run_sequence",
        "workflow_events",
        ["workflow_run_id", "sequence"],
    )

    op.create_table(
        "outbox_messages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workflow_job_id", sa.Uuid(), nullable=False),
        sa.Column("topic", sa.String(length=100), nullable=False),
        sa.Column("payload", json_document, nullable=False),
        sa.Column("dispatch_attempt", sa.Integer(), nullable=False),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("publish_attempts", sa.Integer(), nullable=False),
        sa.Column("stream_message_id", sa.String(length=128), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
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
        sa.ForeignKeyConstraint(["workflow_job_id"], ["workflow_jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workflow_job_id",
            "dispatch_attempt",
            name="uq_outbox_messages_job_dispatch",
        ),
    )
    op.create_index(
        "ix_outbox_messages_due",
        "outbox_messages",
        ["published_at", "available_at"],
    )

    with op.batch_alter_table("investigation_runs") as batch_op:
        batch_op.add_column(sa.Column("workflow_job_id", sa.Uuid(), nullable=True))
        batch_op.create_foreign_key(
            "fk_investigation_runs_workflow_job_id",
            "workflow_jobs",
            ["workflow_job_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_unique_constraint(
            "uq_investigation_runs_workflow_job_id", ["workflow_job_id"]
        )

    with op.batch_alter_table("mitigation_proposals") as batch_op:
        batch_op.add_column(sa.Column("workflow_job_id", sa.Uuid(), nullable=True))
        batch_op.create_foreign_key(
            "fk_mitigation_proposals_workflow_job_id",
            "workflow_jobs",
            ["workflow_job_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_unique_constraint(
            "uq_mitigation_proposals_workflow_job_id", ["workflow_job_id"]
        )


def downgrade() -> None:
    with op.batch_alter_table("mitigation_proposals") as batch_op:
        batch_op.drop_constraint("uq_mitigation_proposals_workflow_job_id", type_="unique")
        batch_op.drop_constraint("fk_mitigation_proposals_workflow_job_id", type_="foreignkey")
        batch_op.drop_column("workflow_job_id")

    with op.batch_alter_table("investigation_runs") as batch_op:
        batch_op.drop_constraint("uq_investigation_runs_workflow_job_id", type_="unique")
        batch_op.drop_constraint("fk_investigation_runs_workflow_job_id", type_="foreignkey")
        batch_op.drop_column("workflow_job_id")

    op.drop_index("ix_outbox_messages_due", table_name="outbox_messages")
    op.drop_table("outbox_messages")
    op.drop_index("ix_workflow_events_run_sequence", table_name="workflow_events")
    op.drop_table("workflow_events")
    op.drop_index("ix_workflow_jobs_lease", table_name="workflow_jobs")
    op.drop_index("ix_workflow_jobs_due", table_name="workflow_jobs")
    op.drop_table("workflow_jobs")
    op.drop_index("ix_workflow_runs_status_updated", table_name="workflow_runs")
    op.drop_index("ix_workflow_runs_incident_created", table_name="workflow_runs")
    op.drop_table("workflow_runs")
