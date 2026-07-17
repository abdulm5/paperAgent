from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
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


class OrganizationRecord(Base):
    __tablename__ = "organizations"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    slug: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    memberships: Mapped[list["OrganizationMembershipRecord"]] = relationship(
        back_populates="organization",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    incidents: Mapped[list["IncidentRecord"]] = relationship(back_populates="organization")
    connectors: Mapped[list["ConnectorRecord"]] = relationship(
        back_populates="organization",
    )


class UserRecord(Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("issuer", "subject", name="uq_users_issuer_subject"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    issuer: Mapped[str] = mapped_column(String(500), nullable=False)
    subject: Mapped[str] = mapped_column(String(500), nullable=False)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    is_active: Mapped[bool] = mapped_column(nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    memberships: Mapped[list["OrganizationMembershipRecord"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    sessions: Mapped[list["AuthSessionRecord"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class OrganizationMembershipRecord(Base):
    __tablename__ = "organization_memberships"
    __table_args__ = (
        Index("ix_organization_memberships_user", "user_id"),
        CheckConstraint(
            "role IN ('viewer', 'responder', 'incident_commander', 'admin')",
            name="ck_organization_memberships_role",
        ),
        CheckConstraint(
            "version > 0",
            name="ck_organization_memberships_version_positive",
        ),
    )

    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    is_active: Mapped[bool] = mapped_column(nullable=False, default=True)
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    organization: Mapped[OrganizationRecord] = relationship(back_populates="memberships")
    user: Mapped[UserRecord] = relationship(back_populates="memberships")


class AuthSessionRecord(Base):
    __tablename__ = "auth_sessions"
    __table_args__ = (
        Index("ix_auth_sessions_user_expires", "user_id", "expires_at"),
        Index("ix_auth_sessions_active_expires", "revoked_at", "expires_at"),
        Index("ix_auth_sessions_expires", "expires_at"),
        ForeignKeyConstraint(
            ["organization_id", "user_id"],
            [
                "organization_memberships.organization_id",
                "organization_memberships.user_id",
            ],
            name="fk_auth_sessions_membership",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "auth_method IN ('local', 'oidc')",
            name="ck_auth_sessions_method",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    auth_method: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[UserRecord] = relationship(back_populates="sessions")


class OidcLoginTransactionRecord(Base):
    __tablename__ = "oidc_login_transactions"
    __table_args__ = (
        Index("ix_oidc_login_transactions_expires", "expires_at"),
        Index(
            "ix_oidc_login_transactions_org_pending",
            "organization_slug",
            "consumed_at",
            "expires_at",
        ),
        CheckConstraint(
            "length(state_hash) = 64 AND length(browser_binding_hash) = 64 "
            "AND length(nonce_hash) = 64",
            name="ck_oidc_login_transactions_hash_lengths",
        ),
        CheckConstraint(
            "length(verifier_nonce) = 12",
            name="ck_oidc_login_transactions_nonce_length",
        ),
        CheckConstraint(
            "length(verifier_ciphertext) BETWEEN 59 AND 272",
            name="ck_oidc_login_transactions_ciphertext_length",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    state_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    browser_binding_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    nonce_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    verifier_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    verifier_nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    organization_slug: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class IdentityAuditEventRecord(Base):
    __tablename__ = "identity_audit_events"
    __table_args__ = (
        Index(
            "ix_identity_audit_events_organization_created",
            "organization_id",
            "created_at",
        ),
        UniqueConstraint(
            "organization_id",
            "target_user_id",
            "membership_version",
            name="uq_identity_audit_events_membership_version",
        ),
        ForeignKeyConstraint(
            ["organization_id", "target_user_id"],
            [
                "organization_memberships.organization_id",
                "organization_memberships.user_id",
            ],
            name="fk_identity_audit_events_membership",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "event_type IN ('membership.provisioned', 'membership.updated')",
            name="ck_identity_audit_events_type",
        ),
        CheckConstraint(
            "membership_version > 0",
            name="ck_identity_audit_events_version_positive",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    target_user_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    actor: Mapped[str] = mapped_column(String(100), nullable=False)
    membership_version: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(json_document, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ConnectorRecord(Base):
    __tablename__ = "connectors"
    __table_args__ = (
        Index("ix_connectors_organization_created", "organization_id", "created_at"),
        UniqueConstraint("organization_id", "name", name="uq_connectors_organization_name"),
        UniqueConstraint(
            "organization_id",
            "id",
            name="uq_connectors_organization_id",
        ),
        CheckConstraint(
            "provider IN ('github', 'prometheus', 'slack')",
            name="ck_connectors_provider",
        ),
        CheckConstraint(
            "status IN ('configured', 'disabled', 'invalid')",
            name="ck_connectors_status",
        ),
        CheckConstraint("version > 0", name="ck_connectors_version_positive"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    configuration: Mapped[dict[str, Any]] = mapped_column(json_document, nullable=False)
    enabled: Mapped[bool] = mapped_column(nullable=False, default=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    last_validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_validation_ok: Mapped[bool | None] = mapped_column()
    last_validation_message: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    organization: Mapped[OrganizationRecord] = relationship(back_populates="connectors")
    credential: Mapped["ConnectorCredentialRecord | None"] = relationship(
        back_populates="connector",
        cascade="all, delete-orphan",
        uselist=False,
        passive_deletes=True,
    )
    events: Mapped[list["ConnectorAuditEventRecord"]] = relationship(
        back_populates="connector",
        order_by="(ConnectorAuditEventRecord.created_at, ConnectorAuditEventRecord.id)",
    )


class ConnectorCredentialRecord(Base):
    __tablename__ = "connector_credentials"
    __table_args__ = (
        CheckConstraint(
            "credential_version > 0",
            name="ck_connector_credentials_version_positive",
        ),
        CheckConstraint(
            "length(ciphertext_nonce) = 12",
            name="ck_connector_credentials_ciphertext_nonce_length",
        ),
        CheckConstraint(
            "length(ciphertext) >= 16",
            name="ck_connector_credentials_ciphertext_length",
        ),
        CheckConstraint(
            "cipher_scheme IN ('local-aesgcm-v1', 'aws-kms-v1')",
            name="ck_connector_credentials_cipher_scheme",
        ),
        CheckConstraint(
            "(cipher_scheme = 'local-aesgcm-v1' "
            "AND length(wrapped_key_nonce) = 12 "
            "AND length(wrapped_data_key) = 48 "
            "AND length(key_version) BETWEEN 1 AND 100) OR "
            "(cipher_scheme = 'aws-kms-v1' "
            "AND wrapped_key_nonce IS NULL "
            "AND length(wrapped_data_key) BETWEEN 1 AND 6144 "
            "AND length(key_version) BETWEEN 20 AND 2048)",
            name="ck_connector_credentials_wrap_contract",
        ),
        CheckConstraint(
            "length(ciphertext) <= 262144",
            name="ck_connector_credentials_ciphertext_max_length",
        ),
    )

    connector_id: Mapped[UUID] = mapped_column(
        ForeignKey("connectors.id", ondelete="CASCADE"), primary_key=True
    )
    credential_version: Mapped[int] = mapped_column(Integer, nullable=False)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    ciphertext_nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    wrapped_data_key: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    wrapped_key_nonce: Mapped[bytes | None] = mapped_column(LargeBinary)
    key_version: Mapped[str] = mapped_column(String(2048), nullable=False)
    cipher_scheme: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="local-aesgcm-v1",
        server_default="local-aesgcm-v1",
    )
    credential_field_names: Mapped[list[str]] = mapped_column(json_document, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    connector: Mapped[ConnectorRecord] = relationship(back_populates="credential")


class ConnectorAuditEventRecord(Base):
    __tablename__ = "connector_audit_events"
    __table_args__ = (
        Index("ix_connector_audit_events_connector_created", "connector_id", "created_at"),
        Index("ix_connector_audit_events_organization_created", "organization_id", "created_at"),
        CheckConstraint(
            "connector_version > 0",
            name="ck_connector_audit_events_version_positive",
        ),
        UniqueConstraint(
            "connector_id",
            "connector_version",
            name="uq_connector_audit_events_connector_version",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    connector_id: Mapped[UUID] = mapped_column(
        ForeignKey("connectors.id", ondelete="RESTRICT"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    actor: Mapped[str] = mapped_column(String(100), nullable=False)
    connector_version: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(json_document, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    connector: Mapped[ConnectorRecord] = relationship(back_populates="events")


class GithubWebhookDeliveryRecord(Base):
    """Verified, normalized GitHub evidence; raw webhook bodies are never retained."""

    __tablename__ = "github_webhook_deliveries"
    __table_args__ = (
        Index(
            "ix_github_webhook_deliveries_organization_received",
            "organization_id",
            "received_at",
        ),
        Index(
            "ix_github_webhook_deliveries_connector_repository_received",
            "connector_id",
            "repository",
            "received_at",
        ),
        UniqueConstraint(
            "connector_id",
            "delivery_id",
            name="uq_github_webhook_deliveries_connector_delivery",
        ),
        ForeignKeyConstraint(
            ["organization_id", "connector_id"],
            ["connectors.organization_id", "connectors.id"],
            name="fk_github_webhook_deliveries_tenant_connector",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "event_type IN ('push', 'pull_request', 'deployment', 'deployment_status', 'release')",
            name="ck_github_webhook_deliveries_event_type",
        ),
        CheckConstraint(
            "action IS NULL OR (length(action) >= 1 AND length(action) <= 32)",
            name="ck_github_webhook_deliveries_action_length",
        ),
        CheckConstraint(
            "length(delivery_id) = 36",
            name="ck_github_webhook_deliveries_delivery_id_length",
        ),
        CheckConstraint(
            "length(body_sha256) = 64",
            name="ck_github_webhook_deliveries_body_hash_length",
        ),
        CheckConstraint(
            "installation_id > 0",
            name="ck_github_webhook_deliveries_installation_positive",
        ),
        CheckConstraint(
            "connector_version > 0 AND credential_version > 0",
            name="ck_github_webhook_deliveries_versions_positive",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    connector_id: Mapped[UUID] = mapped_column(
        Uuid, nullable=False
    )
    delivery_id: Mapped[str] = mapped_column(String(36), nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    action: Mapped[str | None] = mapped_column(String(32))
    repository: Mapped[str] = mapped_column(String(201), nullable=False)
    installation_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    connector_version: Mapped[int] = mapped_column(Integer, nullable=False)
    credential_version: Mapped[int] = mapped_column(Integer, nullable=False)
    body_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    normalized_payload: Mapped[dict[str, Any]] = mapped_column(json_document, nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class IncidentRecord(Base):
    __tablename__ = "incidents"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "id",
            name="uq_incidents_organization_id",
        ),
        UniqueConstraint(
            "organization_id",
            "active_fingerprint",
            name="uq_incidents_organization_active_fingerprint",
        ),
        Index("ix_incidents_organization_received_at", "organization_id", "received_at"),
        Index("ix_incidents_status_received_at", "status", "received_at"),
        Index("ix_incidents_service", "service"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    fingerprint: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    active_fingerprint: Mapped[str | None] = mapped_column(String(200))
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

    organization: Mapped[OrganizationRecord] = relationship(back_populates="incidents")
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
    collaboration_outputs: Mapped[list["CollaborationOutputRecord"]] = relationship(
        back_populates="incident",
        cascade="all, delete-orphan",
        order_by="CollaborationOutputRecord.requested_at",
        passive_deletes=True,
        foreign_keys="CollaborationOutputRecord.incident_id",
    )
    postmortem: Mapped["PostmortemRecord | None"] = relationship(
        back_populates="incident",
        cascade="all, delete-orphan",
        uselist=False,
        passive_deletes=True,
    )
    workflows: Mapped[list["WorkflowRunRecord"]] = relationship(
        back_populates="incident",
        cascade="all, delete-orphan",
        order_by="WorkflowRunRecord.created_at",
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
        UniqueConstraint("workflow_job_id", name="uq_investigation_runs_workflow_job_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    incident_id: Mapped[UUID] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False
    )
    workflow_job_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("workflow_jobs.id", ondelete="SET NULL")
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
    cause_candidates: Mapped[list["CauseCandidateRecord"]] = relationship(
        back_populates="investigation",
        cascade="all, delete-orphan",
        order_by="CauseCandidateRecord.rank",
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
    __table_args__ = (Index("ix_commit_candidates_investigation_rank", "investigation_id", "rank"),)

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

    investigation: Mapped[InvestigationRunRecord] = relationship(back_populates="commit_candidates")


class CauseCandidateRecord(Base):
    __tablename__ = "cause_candidates"
    __table_args__ = (Index("ix_cause_candidates_investigation_rank", "investigation_id", "rank"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    investigation_id: Mapped[UUID] = mapped_column(
        ForeignKey("investigation_runs.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    reference: Mapped[str] = mapped_column(String(200), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    explanation: Mapped[list[str]] = mapped_column(json_document, nullable=False)
    evidence_ids: Mapped[list[str]] = mapped_column(json_document, nullable=False)

    investigation: Mapped[InvestigationRunRecord] = relationship(back_populates="cause_candidates")


class RunbookMatchRecord(Base):
    __tablename__ = "runbook_matches"
    __table_args__ = (Index("ix_runbook_matches_investigation_rank", "investigation_id", "rank"),)

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

    investigation: Mapped[InvestigationRunRecord] = relationship(back_populates="runbook_matches")


class MitigationProposalRecord(Base):
    __tablename__ = "mitigation_proposals"
    __table_args__ = (
        Index("ix_mitigation_proposals_incident_created", "incident_id", "created_at"),
        UniqueConstraint(
            "incident_id",
            "id",
            name="uq_mitigation_proposals_incident_id",
        ),
        UniqueConstraint("workflow_job_id", name="uq_mitigation_proposals_workflow_job_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    incident_id: Mapped[UUID] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False
    )
    investigation_id: Mapped[UUID] = mapped_column(
        ForeignKey("investigation_runs.id", ondelete="CASCADE"), nullable=False
    )
    workflow_job_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("workflow_jobs.id", ondelete="SET NULL")
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
    collaboration_outputs: Mapped[list["CollaborationOutputRecord"]] = relationship(
        back_populates="proposal",
        cascade="all, delete-orphan",
        order_by="CollaborationOutputRecord.requested_at",
        passive_deletes=True,
        foreign_keys="CollaborationOutputRecord.proposal_id",
    )


class ProposalDecisionRecord(Base):
    __tablename__ = "proposal_decisions"
    __table_args__ = (Index("ix_proposal_decisions_proposal_created", "proposal_id", "created_at"),)

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


class CollaborationOutputRecord(Base):
    __tablename__ = "collaboration_outputs"
    __table_args__ = (
        Index(
            "ix_collaboration_outputs_incident_requested",
            "incident_id",
            "requested_at",
        ),
        Index(
            "ix_collaboration_outputs_status_requested",
            "status",
            "requested_at",
        ),
        UniqueConstraint(
            "proposal_id",
            "kind",
            name="uq_collaboration_outputs_proposal_kind",
        ),
        ForeignKeyConstraint(
            ["organization_id", "connector_id"],
            ["connectors.organization_id", "connectors.id"],
            name="fk_collaboration_outputs_tenant_connector",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "incident_id"],
            ["incidents.organization_id", "incidents.id"],
            name="fk_collaboration_outputs_tenant_incident",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["incident_id", "proposal_id"],
            ["mitigation_proposals.incident_id", "mitigation_proposals.id"],
            name="fk_collaboration_outputs_incident_proposal",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["incident_id", "workflow_run_id"],
            ["workflow_runs.incident_id", "workflow_runs.id"],
            name="fk_collaboration_outputs_incident_workflow",
        ),
        CheckConstraint(
            "kind IN ('slack_update', 'github_issue')",
            name="ck_collaboration_outputs_kind",
        ),
        CheckConstraint(
            "provider IN ('slack', 'github')",
            name="ck_collaboration_outputs_provider",
        ),
        CheckConstraint(
            "(kind = 'slack_update' AND provider = 'slack') OR "
            "(kind = 'github_issue' AND provider = 'github')",
            name="ck_collaboration_outputs_kind_provider",
        ),
        CheckConstraint(
            "status IN ('pending_approval', 'rejected', 'queued', 'delivering', "
            "'retry_scheduled', 'delivered', 'dead_lettered')",
            name="ck_collaboration_outputs_status",
        ),
        CheckConstraint("version > 0", name="ck_collaboration_outputs_version_positive"),
        CheckConstraint(
            "connector_version > 0 AND credential_version > 0",
            name="ck_collaboration_outputs_connector_versions_positive",
        ),
        CheckConstraint(
            "length(content_sha256) = 64",
            name="ck_collaboration_outputs_content_hash_length",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    incident_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    proposal_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    connector_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    workflow_run_id: Mapped[UUID | None] = mapped_column(Uuid, unique=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    destination: Mapped[str] = mapped_column(String(500), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(json_document, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    connector_version: Mapped[int] = mapped_column(Integer, nullable=False)
    credential_version: Mapped[int] = mapped_column(Integer, nullable=False)
    requested_by: Mapped[str] = mapped_column(String(100), nullable=False)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_reason: Mapped[str | None] = mapped_column(String(500))

    incident: Mapped[IncidentRecord] = relationship(
        back_populates="collaboration_outputs",
        foreign_keys=[incident_id],
    )
    proposal: Mapped[MitigationProposalRecord] = relationship(
        back_populates="collaboration_outputs",
        foreign_keys=[proposal_id],
    )
    decisions: Mapped[list["CollaborationDecisionRecord"]] = relationship(
        back_populates="output",
        cascade="all, delete-orphan",
        order_by="CollaborationDecisionRecord.created_at",
        passive_deletes=True,
    )
    delivery: Mapped["CollaborationDeliveryRecord | None"] = relationship(
        back_populates="output",
        cascade="all, delete-orphan",
        uselist=False,
        passive_deletes=True,
    )


class CollaborationDecisionRecord(Base):
    __tablename__ = "collaboration_decisions"
    __table_args__ = (
        Index(
            "ix_collaboration_decisions_output_created",
            "output_id",
            "created_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    output_id: Mapped[UUID] = mapped_column(
        ForeignKey("collaboration_outputs.id", ondelete="CASCADE"), nullable=False
    )
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    actor: Mapped[str] = mapped_column(String(100), nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    output: Mapped[CollaborationOutputRecord] = relationship(back_populates="decisions")


class CollaborationDeliveryRecord(Base):
    __tablename__ = "collaboration_deliveries"
    __table_args__ = (
        CheckConstraint(
            "status IN ('prepared', 'delivering', 'retry_scheduled', 'delivered', "
            "'dead_lettered')",
            name="ck_collaboration_deliveries_status",
        ),
        CheckConstraint(
            "attempt_count >= 0",
            name="ck_collaboration_deliveries_attempt_count_nonnegative",
        ),
    )

    output_id: Mapped[UUID] = mapped_column(
        ForeignKey("collaboration_outputs.id", ondelete="CASCADE"), primary_key=True
    )
    idempotency_key: Mapped[str] = mapped_column(
        String(100), nullable=False, unique=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    provider_receipt: Mapped[dict[str, Any]] = mapped_column(
        json_document, nullable=False, default=dict
    )
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    output: Mapped[CollaborationOutputRecord] = relationship(back_populates="delivery")


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
        UniqueConstraint("postmortem_id", "version", name="uq_postmortem_revisions_version"),
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


class WorkflowRunRecord(Base):
    __tablename__ = "workflow_runs"
    __table_args__ = (
        Index("ix_workflow_runs_incident_created", "incident_id", "created_at"),
        Index("ix_workflow_runs_status_updated", "status", "updated_at"),
        UniqueConstraint(
            "incident_id",
            "id",
            name="uq_workflow_runs_incident_id",
        ),
        UniqueConstraint("dedupe_key", name="uq_workflow_runs_dedupe_key"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    incident_id: Mapped[UUID] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False
    )
    workflow_type: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    current_step: Mapped[str | None] = mapped_column(String(64))
    dedupe_key: Mapped[str] = mapped_column(String(200), nullable=False)
    trace_id: Mapped[str | None] = mapped_column(String(128))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    failure_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    incident: Mapped[IncidentRecord] = relationship(back_populates="workflows")
    jobs: Mapped[list["WorkflowJobRecord"]] = relationship(
        back_populates="workflow_run",
        cascade="all, delete-orphan",
        order_by="WorkflowJobRecord.created_at",
        passive_deletes=True,
    )
    events: Mapped[list["WorkflowEventRecord"]] = relationship(
        back_populates="workflow_run",
        cascade="all, delete-orphan",
        order_by="WorkflowEventRecord.sequence",
        passive_deletes=True,
    )


class WorkflowJobRecord(Base):
    __tablename__ = "workflow_jobs"
    __table_args__ = (
        Index("ix_workflow_jobs_due", "status", "available_at"),
        Index("ix_workflow_jobs_lease", "status", "lease_expires_at"),
        UniqueConstraint("idempotency_key", name="uq_workflow_jobs_idempotency_key"),
        UniqueConstraint("workflow_run_id", "step_type", name="uq_workflow_jobs_run_step"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    workflow_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("workflow_runs.id", ondelete="CASCADE"), nullable=False
    )
    step_type: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(json_document, nullable=False, default=dict)
    result: Mapped[dict[str, Any]] = mapped_column(json_document, nullable=False, default=dict)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    lease_owner: Mapped[str | None] = mapped_column(String(100))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    workflow_run: Mapped[WorkflowRunRecord] = relationship(back_populates="jobs")
    events: Mapped[list["WorkflowEventRecord"]] = relationship(
        back_populates="job",
        passive_deletes=True,
    )
    outbox_messages: Mapped[list["OutboxMessageRecord"]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
        order_by="OutboxMessageRecord.dispatch_attempt",
        passive_deletes=True,
    )


class WorkflowEventRecord(Base):
    __tablename__ = "workflow_events"
    __table_args__ = (
        Index("ix_workflow_events_run_sequence", "workflow_run_id", "sequence"),
        UniqueConstraint("workflow_run_id", "sequence", name="uq_workflow_events_run_sequence"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    workflow_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("workflow_runs.id", ondelete="CASCADE"), nullable=False
    )
    workflow_job_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("workflow_jobs.id", ondelete="SET NULL")
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(json_document, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    workflow_run: Mapped[WorkflowRunRecord] = relationship(back_populates="events")
    job: Mapped[WorkflowJobRecord | None] = relationship(back_populates="events")


class OutboxMessageRecord(Base):
    __tablename__ = "outbox_messages"
    __table_args__ = (
        Index("ix_outbox_messages_due", "published_at", "available_at"),
        UniqueConstraint(
            "workflow_job_id",
            "dispatch_attempt",
            name="uq_outbox_messages_job_dispatch",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    workflow_job_id: Mapped[UUID] = mapped_column(
        ForeignKey("workflow_jobs.id", ondelete="CASCADE"), nullable=False
    )
    topic: Mapped[str] = mapped_column(String(100), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(json_document, nullable=False)
    dispatch_attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    publish_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    stream_message_id: Mapped[str | None] = mapped_column(String(128))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    job: Mapped[WorkflowJobRecord] = relationship(back_populates="outbox_messages")
