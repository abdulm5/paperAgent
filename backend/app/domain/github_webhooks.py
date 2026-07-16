from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

GithubEventType = Literal[
    "push",
    "pull_request",
    "deployment",
    "deployment_status",
    "release",
]


class GithubWebhookReceipt(BaseModel):
    delivery_id: str = Field(min_length=36, max_length=36)
    event_type: GithubEventType
    duplicate: bool


class GithubWebhookDelivery(BaseModel):
    connector_id: UUID
    delivery_id: str = Field(min_length=36, max_length=36)
    event_type: GithubEventType
    action: str | None
    repository: str
    installation_id: int
    connector_version: int
    credential_version: int
    body_sha256: str = Field(min_length=64, max_length=64)
    normalized_payload: dict[str, Any]
    received_at: datetime
