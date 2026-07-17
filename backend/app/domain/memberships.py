import re
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.domain.auth import Role

_EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def _reject_ambiguous_text(value: str, field_name: str) -> str:
    if value != value.strip():
        raise ValueError(f"{field_name} must not contain surrounding whitespace")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{field_name} must not contain control characters")
    return value


class PublicMembershipMutation(BaseModel):
    """Reject server-owned tenant, actor, version, and audit fields."""

    model_config = ConfigDict(extra="forbid")


class MembershipProvisionInput(PublicMembershipMutation):
    issuer: str = Field(min_length=1, max_length=500)
    subject: str = Field(min_length=1, max_length=500)
    email: str = Field(min_length=3, max_length=320)
    display_name: str = Field(min_length=1, max_length=200)
    role: Role

    @field_validator("issuer", "subject", "display_name")
    @classmethod
    def validate_text(cls, value: str, info: Any) -> str:
        return _reject_ambiguous_text(value, info.field_name.replace("_", " ").title())

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        value = _reject_ambiguous_text(value, "Email")
        if _EMAIL_PATTERN.fullmatch(value) is None:
            raise ValueError("Email must be a valid address")
        return value.casefold()


class MembershipUpdateInput(PublicMembershipMutation):
    expected_version: int = Field(gt=0)
    role: Role | None = None
    is_active: bool | None = None

    @model_validator(mode="after")
    def require_change(self) -> "MembershipUpdateInput":
        if self.role is None and self.is_active is None:
            raise ValueError("At least one membership field must be changed")
        return self


class MembershipIdentity(BaseModel):
    id: UUID
    issuer: str
    subject: str
    email: str
    display_name: str
    is_active: bool


class MembershipDetail(BaseModel):
    organization_id: UUID
    user: MembershipIdentity
    role: Role
    is_active: bool
    version: int
    created_at: datetime
    updated_at: datetime


class IdentityAuditEvent(BaseModel):
    id: UUID
    organization_id: UUID
    target_user_id: UUID
    event_type: str
    actor: str
    membership_version: int
    payload: dict[str, Any]
    created_at: datetime
