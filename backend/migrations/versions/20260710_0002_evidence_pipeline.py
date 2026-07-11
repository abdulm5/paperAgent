"""Create evidence and investigation tables.

Revision ID: 20260710_0002
Revises: 20260710_0001
Create Date: 2026-07-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260710_0002"
down_revision: str | None = "20260710_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

json_document = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "investigation_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("incident_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("collector_version", sa.String(length=64), nullable=False),
        sa.Column("clusterer_version", sa.String(length=64), nullable=False),
        sa.Column("ranker_version", sa.String(length=64), nullable=False),
        sa.Column("retrieval_version", sa.String(length=64), nullable=False),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["incident_id"], ["incidents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_investigation_runs_incident_started",
        "investigation_runs",
        ["incident_id", "started_at"],
    )

    op.create_table(
        "evidence_artifacts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("investigation_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("source_uri", sa.String(length=500), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("payload", json_document, nullable=False),
        sa.Column(
            "collected_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["investigation_id"], ["investigation_runs.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_evidence_artifacts_investigation_kind",
        "evidence_artifacts",
        ["investigation_id", "kind"],
    )
    op.create_index(
        "ix_evidence_artifacts_content_hash", "evidence_artifacts", ["content_hash"]
    )

    op.create_table(
        "error_clusters",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("investigation_id", sa.Uuid(), nullable=False),
        sa.Column("signature", sa.String(length=64), nullable=False),
        sa.Column("error_type", sa.String(length=200), nullable=False),
        sa.Column("endpoint", sa.String(length=200), nullable=False),
        sa.Column("affected_attributes", json_document, nullable=False),
        sa.Column("failure_count", sa.Integer(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sample_request_ids", json_document, nullable=False),
        sa.Column("evidence_ids", json_document, nullable=False),
        sa.ForeignKeyConstraint(
            ["investigation_id"], ["investigation_runs.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_error_clusters_investigation_count",
        "error_clusters",
        ["investigation_id", "failure_count"],
    )

    op.create_table(
        "commit_candidates",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("investigation_id", sa.Uuid(), nullable=False),
        sa.Column("commit_sha", sa.String(length=40), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("total_score", sa.Float(), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("author", sa.String(length=200), nullable=False),
        sa.Column("committed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("files_changed", json_document, nullable=False),
        sa.Column("diff_summary", sa.Text(), nullable=False),
        sa.Column("feature_scores", json_document, nullable=False),
        sa.Column("explanation", json_document, nullable=False),
        sa.Column("evidence_ids", json_document, nullable=False),
        sa.ForeignKeyConstraint(
            ["investigation_id"], ["investigation_runs.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_commit_candidates_investigation_rank",
        "commit_candidates",
        ["investigation_id", "rank"],
    )

    op.create_table(
        "runbook_matches",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("investigation_id", sa.Uuid(), nullable=False),
        sa.Column("runbook_id", sa.String(length=200), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("service", sa.String(length=100), nullable=False),
        sa.Column("failure_mode", sa.String(length=100), nullable=False),
        sa.Column("total_score", sa.Float(), nullable=False),
        sa.Column("score_breakdown", json_document, nullable=False),
        sa.Column("matched_sections", json_document, nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("evidence_ids", json_document, nullable=False),
        sa.ForeignKeyConstraint(
            ["investigation_id"], ["investigation_runs.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_runbook_matches_investigation_rank",
        "runbook_matches",
        ["investigation_id", "rank"],
    )


def downgrade() -> None:
    op.drop_index("ix_runbook_matches_investigation_rank", table_name="runbook_matches")
    op.drop_table("runbook_matches")
    op.drop_index("ix_commit_candidates_investigation_rank", table_name="commit_candidates")
    op.drop_table("commit_candidates")
    op.drop_index("ix_error_clusters_investigation_count", table_name="error_clusters")
    op.drop_table("error_clusters")
    op.drop_index("ix_evidence_artifacts_content_hash", table_name="evidence_artifacts")
    op.drop_index(
        "ix_evidence_artifacts_investigation_kind", table_name="evidence_artifacts"
    )
    op.drop_table("evidence_artifacts")
    op.drop_index("ix_investigation_runs_incident_started", table_name="investigation_runs")
    op.drop_table("investigation_runs")
