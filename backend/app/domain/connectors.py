from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator


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
    repository: str = Field(min_length=3, max_length=300, pattern=r"^[^/\s]+/[^/\s]+$")
    app_id: int = Field(gt=0)
    installation_id: int = Field(gt=0)
    api_url: str | None = Field(default=None, min_length=1, max_length=500)


class PrometheusConfiguration(PublicMutationModel):
    base_url: str = Field(min_length=1, max_length=500)


class SlackConfiguration(PublicMutationModel):
    channel: str = Field(min_length=1, max_length=200)
    api_url: str | None = Field(default=None, min_length=1, max_length=500)


class GithubCredentials(PublicMutationModel):
    private_key: SecretStr = Field(min_length=1, max_length=65_536)


class PrometheusCredentials(PublicMutationModel):
    bearer_token: SecretStr = Field(min_length=1, max_length=8_192)


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
