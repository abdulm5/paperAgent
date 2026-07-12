from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

json_document = JSON().with_variant(JSONB(), "postgresql")


class IncidentRecord(Base):
    __tablename__ = "incidents"
    __table_args__ = (
        Index("ix_incidents_status_received_at", "status", "received_at"),
        Index("ix_incidents_service", "service"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    fingerprint: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    active_fingerprint: Mapped[str | None] = mapped_column(String(200), unique=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    service: Mapped[str] = mapped_column(String(100), nullable=False)
    severity: Mapped[str] = mapped_column(String(32), nullable=False)
    summary: Mapped[str] = mapped_column(String(500), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    alerts: Mapped[list["AlertRecord"]] = relationship(
        back_populates="incident",
        cascade="all, delete-orphan",
        order_by="AlertRecord.received_at",
        passive_deletes=True,
    )
    events: Mapped[list["IncidentEventRecord"]] = relationship(
        back_populates="incident",
        cascade="all, delete-orphan",
        order_by="IncidentEventRecord.created_at",
        passive_deletes=True,
    )
    investigations: Mapped[list["InvestigationRunRecord"]] = relationship(
        back_populates="incident",
        cascade="all, delete-orphan",
        order_by="InvestigationRunRecord.started_at",
        passive_deletes=True,
    )
    proposals: Mapped[list["MitigationProposalRecord"]] = relationship(
        back_populates="incident",
        cascade="all, delete-orphan",
        order_by="MitigationProposalRecord.created_at",
        passive_deletes=True,
    )
    postmortem: Mapped["PostmortemRecord | None"] = relationship(
        back_populates="incident",
        cascade="all, delete-orphan",
        uselist=False,
        passive_deletes=True,
    )


class AlertRecord(Base):
    __tablename__ = "alerts"
    __table_args__ = (Index("ix_alerts_incident_received_at", "incident_id", "received_at"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    incident_id: Mapped[UUID] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False
    )
    source: Mapped[str] = mapped_column(String(100), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(200), nullable=False)
    deduplicated: Mapped[bool] = mapped_column(nullable=False, default=False)
    payload: Mapped[dict[str, Any]] = mapped_column(json_document, nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    incident: Mapped[IncidentRecord] = relationship(back_populates="alerts")


class IncidentEventRecord(Base):
    __tablename__ = "incident_events"
    __table_args__ = (Index("ix_incident_events_incident_created", "incident_id", "created_at"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    incident_id: Mapped[UUID] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    actor: Mapped[str] = mapped_column(String(100), nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(32))
    to_status: Mapped[str | None] = mapped_column(String(32))
    note: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(json_document, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    incident: Mapped[IncidentRecord] = relationship(back_populates="events")


class InvestigationRunRecord(Base):
    __tablename__ = "investigation_runs"
    __table_args__ = (
        Index("ix_investigation_runs_incident_started", "incident_id", "started_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    incident_id: Mapped[UUID] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    collector_version: Mapped[str] = mapped_column(String(64), nullable=False)
    clusterer_version: Mapped[str] = mapped_column(String(64), nullable=False)
    ranker_version: Mapped[str] = mapped_column(String(64), nullable=False)
    retrieval_version: Mapped[str] = mapped_column(String(64), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    failure_reason: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    incident: Mapped[IncidentRecord] = relationship(back_populates="investigations")
    evidence: Mapped[list["EvidenceArtifactRecord"]] = relationship(
        back_populates="investigation",
        cascade="all, delete-orphan",
        order_by="EvidenceArtifactRecord.collected_at",
        passive_deletes=True,
    )
    error_clusters: Mapped[list["ErrorClusterRecord"]] = relationship(
        back_populates="investigation",
        cascade="all, delete-orphan",
        order_by="ErrorClusterRecord.failure_count.desc()",
        passive_deletes=True,
    )
    commit_candidates: Mapped[list["CommitCandidateRecord"]] = relationship(
        back_populates="investigation",
        cascade="all, delete-orphan",
        order_by="CommitCandidateRecord.rank",
        passive_deletes=True,
    )
    runbook_matches: Mapped[list["RunbookMatchRecord"]] = relationship(
        back_populates="investigation",
        cascade="all, delete-orphan",
        order_by="RunbookMatchRecord.rank",
        passive_deletes=True,
    )
    proposals: Mapped[list["MitigationProposalRecord"]] = relationship(
        back_populates="investigation",
        cascade="all, delete-orphan",
        order_by="MitigationProposalRecord.created_at",
        passive_deletes=True,
    )


class EvidenceArtifactRecord(Base):
    __tablename__ = "evidence_artifacts"
    __table_args__ = (
        Index("ix_evidence_artifacts_investigation_kind", "investigation_id", "kind"),
        Index("ix_evidence_artifacts_content_hash", "content_hash"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    investigation_id: Mapped[UUID] = mapped_column(
        ForeignKey("investigation_runs.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    source_uri: Mapped[str] = mapped_column(String(500), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(json_document, nullable=False)
    collected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    investigation: Mapped[InvestigationRunRecord] = relationship(back_populates="evidence")


class ErrorClusterRecord(Base):
    __tablename__ = "error_clusters"
    __table_args__ = (
        Index("ix_error_clusters_investigation_count", "investigation_id", "failure_count"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    investigation_id: Mapped[UUID] = mapped_column(
        ForeignKey("investigation_runs.id", ondelete="CASCADE"), nullable=False
    )
    signature: Mapped[str] = mapped_column(String(64), nullable=False)
    error_type: Mapped[str] = mapped_column(String(200), nullable=False)
    endpoint: Mapped[str] = mapped_column(String(200), nullable=False)
    affected_attributes: Mapped[dict[str, Any]] = mapped_column(json_document, nullable=False)
    failure_count: Mapped[int] = mapped_column(Integer, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sample_request_ids: Mapped[list[str]] = mapped_column(json_document, nullable=False)
    evidence_ids: Mapped[list[str]] = mapped_column(json_document, nullable=False)

    investigation: Mapped[InvestigationRunRecord] = relationship(back_populates="error_clusters")


class CommitCandidateRecord(Base):
    __tablename__ = "commit_candidates"
    __table_args__ = (
        Index("ix_commit_candidates_investigation_rank", "investigation_id", "rank"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    investigation_id: Mapped[UUID] = mapped_column(
        ForeignKey("investigation_runs.id", ondelete="CASCADE"), nullable=False
    )
    commit_sha: Mapped[str] = mapped_column(String(40), nullable=False)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    total_score: Mapped[float] = mapped_column(Float, nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    author: Mapped[str] = mapped_column(String(200), nullable=False)
    committed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    files_changed: Mapped[list[str]] = mapped_column(json_document, nullable=False)
    diff_summary: Mapped[str] = mapped_column(Text, nullable=False)
    feature_scores: Mapped[dict[str, float]] = mapped_column(json_document, nullable=False)
    explanation: Mapped[list[str]] = mapped_column(json_document, nullable=False)
    evidence_ids: Mapped[list[str]] = mapped_column(json_document, nullable=False)

    investigation: Mapped[InvestigationRunRecord] = relationship(
        back_populates="commit_candidates"
    )


class RunbookMatchRecord(Base):
    __tablename__ = "runbook_matches"
    __table_args__ = (
        Index("ix_runbook_matches_investigation_rank", "investigation_id", "rank"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    investigation_id: Mapped[UUID] = mapped_column(
        ForeignKey("investigation_runs.id", ondelete="CASCADE"), nullable=False
    )
    runbook_id: Mapped[str] = mapped_column(String(200), nullable=False)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    service: Mapped[str] = mapped_column(String(100), nullable=False)
    failure_mode: Mapped[str] = mapped_column(String(100), nullable=False)
    total_score: Mapped[float] = mapped_column(Float, nullable=False)
    score_breakdown: Mapped[dict[str, float]] = mapped_column(json_document, nullable=False)
    matched_sections: Mapped[list[dict[str, str]]] = mapped_column(json_document, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence_ids: Mapped[list[str]] = mapped_column(json_document, nullable=False)

    investigation: Mapped[InvestigationRunRecord] = relationship(
        back_populates="runbook_matches"
    )


class MitigationProposalRecord(Base):
    __tablename__ = "mitigation_proposals"
    __table_args__ = (
        Index("ix_mitigation_proposals_incident_created", "incident_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    incident_id: Mapped[UUID] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False
    )
    investigation_id: Mapped[UUID] = mapped_column(
        ForeignKey("investigation_runs.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    synthesizer_version: Mapped[str] = mapped_column(String(64), nullable=False)
    model_name: Mapped[str] = mapped_column(String(100), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    root_cause_summary: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    impact_summary: Mapped[str] = mapped_column(Text, nullable=False)
    recommended_action: Mapped[str] = mapped_column(Text, nullable=False)
    risk_summary: Mapped[str] = mapped_column(Text, nullable=False)
    verification_steps: Mapped[list[str]] = mapped_column(json_document, nullable=False)
    slack_update: Mapped[str] = mapped_column(Text, nullable=False)
    claims: Mapped[list[dict[str, Any]]] = mapped_column(json_document, nullable=False)
    action_type: Mapped[str] = mapped_column(String(64), nullable=False)
    action_target: Mapped[str] = mapped_column(String(100), nullable=False)
    action_parameters: Mapped[dict[str, Any]] = mapped_column(json_document, nullable=False)
    failure_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    incident: Mapped[IncidentRecord] = relationship(back_populates="proposals")
    investigation: Mapped[InvestigationRunRecord] = relationship(back_populates="proposals")
    decisions: Mapped[list["ProposalDecisionRecord"]] = relationship(
        back_populates="proposal",
        cascade="all, delete-orphan",
        order_by="ProposalDecisionRecord.created_at",
        passive_deletes=True,
    )
    execution: Mapped["MitigationExecutionRecord | None"] = relationship(
        back_populates="proposal",
        cascade="all, delete-orphan",
        uselist=False,
        passive_deletes=True,
    )


class ProposalDecisionRecord(Base):
    __tablename__ = "proposal_decisions"
    __table_args__ = (
        Index("ix_proposal_decisions_proposal_created", "proposal_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    proposal_id: Mapped[UUID] = mapped_column(
        ForeignKey("mitigation_proposals.id", ondelete="CASCADE"), nullable=False
    )
    incident_id: Mapped[UUID] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False
    )
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    actor: Mapped[str] = mapped_column(String(100), nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    proposal: Mapped[MitigationProposalRecord] = relationship(back_populates="decisions")


class MitigationExecutionRecord(Base):
    __tablename__ = "mitigation_executions"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    proposal_id: Mapped[UUID] = mapped_column(
        ForeignKey("mitigation_proposals.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    executor_version: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    request_payload: Mapped[dict[str, Any]] = mapped_column(json_document, nullable=False)
    response_payload: Mapped[dict[str, Any]] = mapped_column(json_document, nullable=False)
    before_telemetry: Mapped[dict[str, Any]] = mapped_column(json_document, nullable=False)
    after_telemetry: Mapped[dict[str, Any]] = mapped_column(json_document, nullable=False)
    recovery_verified: Mapped[bool] = mapped_column(nullable=False, default=False)
    failure_reason: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    proposal: Mapped[MitigationProposalRecord] = relationship(back_populates="execution")


class PostmortemRecord(Base):
    __tablename__ = "postmortems"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    incident_id: Mapped[UUID] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    generator_version: Mapped[str] = mapped_column(String(64), nullable=False)
    model_name: Mapped[str] = mapped_column(String(100), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    content: Mapped[dict[str, Any]] = mapped_column(json_document, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finalized_by: Mapped[str | None] = mapped_column(String(100))

    incident: Mapped[IncidentRecord] = relationship(back_populates="postmortem")
    revisions: Mapped[list["PostmortemRevisionRecord"]] = relationship(
        back_populates="postmortem",
        cascade="all, delete-orphan",
        order_by="PostmortemRevisionRecord.version",
        passive_deletes=True,
    )


class PostmortemRevisionRecord(Base):
    __tablename__ = "postmortem_revisions"
    __table_args__ = (
        Index("ix_postmortem_revisions_postmortem_version", "postmortem_id", "version"),
        UniqueConstraint(
            "postmortem_id", "version", name="uq_postmortem_revisions_version"
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    postmortem_id: Mapped[UUID] = mapped_column(
        ForeignKey("postmortems.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    editor: Mapped[str] = mapped_column(String(100), nullable=False)
    change_note: Mapped[str] = mapped_column(String(500), nullable=False)
    snapshot: Mapped[dict[str, Any]] = mapped_column(json_document, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    postmortem: Mapped[PostmortemRecord] = relationship(back_populates="revisions")
