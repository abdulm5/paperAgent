from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator


class ProposalStatus(StrEnum):
    ADVISORY = "advisory"
    PENDING_APPROVAL = "pending_approval"
    REJECTED = "rejected"
    APPROVED = "approved"
    EXECUTING = "executing"
    VERIFICATION_PASSED = "verification_passed"
    EXECUTION_FAILED = "execution_failed"


class ProposalDecision(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"


class ClaimKind(StrEnum):
    ROOT_CAUSE = "root_cause"
    IMPACT = "impact"
    RECOMMENDATION = "recommendation"
    RISK = "risk"


class GroundedClaim(BaseModel):
    kind: ClaimKind
    text: str = Field(min_length=1, max_length=2_000)
    evidence_ids: list[str] = Field(min_length=1)


class GroundedBriefDraft(BaseModel):
    root_cause_summary: str = Field(min_length=1, max_length=2_000)
    confidence: float = Field(ge=0, le=1)
    impact_summary: str = Field(min_length=1, max_length=2_000)
    recommended_action: str = Field(min_length=1, max_length=2_000)
    risk_summary: str = Field(min_length=1, max_length=2_000)
    verification_steps: list[str] = Field(min_length=1, max_length=10)
    slack_update: str = Field(min_length=1, max_length=3_000)
    claims: list[GroundedClaim] = Field(min_length=4)


class ActionEnvelope(BaseModel):
    action_type: Literal[
        "rollback_service", "disable_feature_flag", "escalate_only"
    ] = "rollback_service"
    target_service: Literal["checkout-api"] = "checkout-api"
    target_release: Literal["stable-v1"] | None = "stable-v1"
    expected_faulty_commit: str | None = Field(default=None, min_length=7, max_length=40)
    feature_flag: str | None = Field(default=None, min_length=1, max_length=100)
    automation_allowed: bool = True

    @model_validator(mode="after")
    def validate_action_parameters(self) -> "ActionEnvelope":
        if self.action_type == "rollback_service":
            if self.target_release != "stable-v1" or not self.expected_faulty_commit:
                raise ValueError("Rollback requires a known release and faulty commit")
        elif self.action_type == "disable_feature_flag":
            if not self.feature_flag or self.target_release is not None:
                raise ValueError("Feature-flag action requires only a flag name")
        elif self.automation_allowed or self.target_release is not None:
            raise ValueError("Advisory actions cannot cross the write boundary")
        return self


class ProposalDecisionInput(BaseModel):
    decision: ProposalDecision
    note: str | None = Field(default=None, max_length=2_000)


class ProposalDecisionRequest(ProposalDecisionInput):
    """Trusted service command with a server-derived audit actor."""

    actor: str = Field(min_length=1, max_length=100)


class ProposalDecisionDetail(BaseModel):
    id: UUID
    decision: ProposalDecision
    actor: str
    note: str | None
    created_at: datetime


class MitigationExecutionDetail(BaseModel):
    id: UUID
    status: str
    executor_version: str
    idempotency_key: str
    request_payload: dict[str, object]
    response_payload: dict[str, object]
    before_telemetry: dict[str, object]
    after_telemetry: dict[str, object]
    recovery_verified: bool
    failure_reason: str | None
    started_at: datetime
    completed_at: datetime | None


class MitigationProposalDetail(BaseModel):
    id: UUID
    incident_id: UUID
    investigation_id: UUID
    status: ProposalStatus
    synthesizer_version: str
    model_name: str
    prompt_version: str
    input_hash: str
    root_cause_summary: str
    confidence: float
    impact_summary: str
    recommended_action: str
    risk_summary: str
    verification_steps: list[str]
    slack_update: str
    claims: list[GroundedClaim]
    action: ActionEnvelope
    failure_reason: str | None
    created_at: datetime
    decided_at: datetime | None
    decisions: list[ProposalDecisionDetail]
    execution: MitigationExecutionDetail | None
