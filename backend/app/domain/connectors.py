import re
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)

GITHUB_OWNER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$"


class ConnectorProvider(StrEnum):
    GITHUB = "github"
    PROMETHEUS = "prometheus"
    SLACK = "slack"


class ConnectorStatus(StrEnum):
    CONFIGURED = "configured"
    DISABLED = "disabled"
    INVALID = "invalid"


class PublicMutationModel(BaseModel):
    """Reject server-owned fields such as organization and audit actor."""

    model_config = ConfigDict(extra="forbid")


class ConnectorCreateInput(PublicMutationModel):
    name: str = Field(min_length=1, max_length=200)
    provider: ConnectorProvider
    configuration: dict[str, Any] = Field(min_length=1, max_length=16)
    credentials: dict[str, SecretStr] = Field(min_length=1, max_length=8)


class ConnectorPatchInput(PublicMutationModel):
    expected_version: int = Field(gt=0)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    configuration: dict[str, Any] | None = Field(default=None, min_length=1, max_length=16)
    enabled: bool | None = None

    @model_validator(mode="after")
    def require_change(self) -> "ConnectorPatchInput":
        if self.name is None and self.configuration is None and self.enabled is None:
            raise ValueError("At least one connector field must be changed")
        return self


class ConnectorCredentialsInput(PublicMutationModel):
    expected_version: int = Field(gt=0)
    credentials: dict[str, SecretStr] = Field(min_length=1, max_length=8)


class ConnectorValidateInput(PublicMutationModel):
    expected_version: int = Field(gt=0)


class GithubConfiguration(PublicMutationModel):
    service: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,98}[A-Za-z0-9])?$",
    )
    repository: str = Field(
        min_length=3,
        max_length=201,
        pattern=r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$",
    )
    app_id: int = Field(gt=0)
    installation_id: int = Field(gt=0)
    issue_creation_enabled: bool = False
    api_url: Literal["https://api.github.com"] = "https://api.github.com"

    @field_validator("repository")
    @classmethod
    def reject_repository_dot_segments(cls, value: str) -> str:
        owner, repository = value.split("/", 1)
        if (
            owner in {".", ".."}
            or repository in {".", ".."}
            or re.fullmatch(GITHUB_OWNER_PATTERN, owner) is None
        ):
            raise ValueError("GitHub repository contains a forbidden path segment")
        # GitHub repository paths are case-insensitive. Persist one canonical
        # form so REST paths, webhook bindings, and evidence queries agree.
        return value.lower()


class PrometheusConfiguration(PublicMutationModel):
    service: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,98}[A-Za-z0-9])?$",
    )
    base_url: str = Field(min_length=1, max_length=500)


class SlackConfiguration(PublicMutationModel):
    service: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,98}[A-Za-z0-9])?$",
    )
    channel: str = Field(pattern=r"^[CG][A-Z0-9]{8,31}$")
    api_url: Literal["https://slack.com"] = "https://slack.com"


class GithubCredentials(PublicMutationModel):
    private_key: SecretStr = Field(min_length=1, max_length=65_536)
    webhook_secret: SecretStr = Field(min_length=32, max_length=8_192)


class PrometheusCredentials(PublicMutationModel):
    bearer_token: SecretStr = Field(min_length=1, max_length=8_192)

    @field_validator("bearer_token")
    @classmethod
    def reject_ambiguous_bearer_tokens(cls, value: SecretStr) -> SecretStr:
        token = value.get_secret_value()
        contains_control_character = any(
            ord(character) < 32 or ord(character) == 127 for character in token
        )
        if token != token.strip() or contains_control_character:
            raise ValueError("Prometheus bearer token contains forbidden whitespace or controls")
        return value


class SlackCredentials(PublicMutationModel):
    bot_token: SecretStr = Field(min_length=1, max_length=8_192)


class ConnectorSummary(BaseModel):
    id: UUID
    organization_id: UUID
    name: str
    provider: ConnectorProvider
    configuration: dict[str, Any]
    enabled: bool
    status: ConnectorStatus
    version: int
    credentials_configured: bool
    credential_version: int
    credential_fields: list[str]
    last_validated_at: datetime | None
    last_validation_ok: bool | None
    last_validation_message: str | None
    created_at: datetime
    updated_at: datetime


class ConnectorAuditEvent(BaseModel):
    id: UUID
    connector_id: UUID
    event_type: str
    actor: str
    connector_version: int
    payload: dict[str, Any]
    created_at: datetime
