from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class CollaborationOutputKind(StrEnum):
    SLACK_UPDATE = "slack_update"
    GITHUB_ISSUE = "github_issue"


class CollaborationProvider(StrEnum):
    SLACK = "slack"
    GITHUB = "github"


class CollaborationOutputStatus(StrEnum):
    PENDING_APPROVAL = "pending_approval"
    REJECTED = "rejected"
    QUEUED = "queued"
    DELIVERING = "delivering"
    RETRY_SCHEDULED = "retry_scheduled"
    DELIVERED = "delivered"
    DEAD_LETTERED = "dead_lettered"


class CollaborationDecision(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"


class PublicCollaborationMutation(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CollaborationOutputCreateInput(PublicCollaborationMutation):
    proposal_id: UUID
    expected_proposal_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    kinds: list[CollaborationOutputKind] = Field(min_length=1, max_length=2)

    @field_validator("kinds")
    @classmethod
    def reject_duplicate_kinds(
        cls, value: list[CollaborationOutputKind]
    ) -> list[CollaborationOutputKind]:
        if len(set(value)) != len(value):
            raise ValueError("Collaboration output kinds must be unique")
        return value


class CollaborationDecisionInput(PublicCollaborationMutation):
    decision: CollaborationDecision
    expected_version: int = Field(gt=0)
    expected_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    note: str | None = Field(default=None, max_length=1_000)


class CollaborationDecisionRequest(CollaborationDecisionInput):
    actor: str = Field(min_length=1, max_length=100)


class CollaborationDecisionDetail(BaseModel):
    id: UUID
    decision: CollaborationDecision
    actor: str
    note: str | None
    created_at: datetime


class CollaborationDeliveryReceipt(BaseModel):
    idempotency_key: str
    status: str
    attempt_count: int
    provider_receipt: dict[str, Any]
    last_error_code: str | None
    started_at: datetime | None
    updated_at: datetime
    delivered_at: datetime | None


class CollaborationOutputDetail(BaseModel):
    id: UUID
    incident_id: UUID
    proposal_id: UUID
    connector_id: UUID
    workflow_run_id: UUID | None
    kind: CollaborationOutputKind
    provider: CollaborationProvider
    status: CollaborationOutputStatus
    version: int
    destination: str
    payload: dict[str, Any]
    content_sha256: str
    connector_version: int
    credential_version: int
    requested_by: str
    requested_at: datetime
    decided_at: datetime | None
    delivered_at: datetime | None
    failure_reason: str | None
    decisions: list[CollaborationDecisionDetail]
    delivery: CollaborationDeliveryReceipt | None
