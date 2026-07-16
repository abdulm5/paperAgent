from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import AnyHttpUrl, BaseModel, Field, model_validator


class AlertSeverity(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class IncidentStatus(StrEnum):
    DETECTED = "detected"
    INVESTIGATING = "investigating"
    MITIGATED = "mitigated"
    RESOLVED = "resolved"


class MetricEvidence(BaseModel):
    name: str = Field(
        min_length=1,
        max_length=200,
        pattern=r"^[A-Za-z_:][A-Za-z0-9_:]*$",
    )
    value: float = Field(ge=0)
    threshold: float = Field(ge=0)
    window_seconds: int = Field(gt=0, le=86_400)
    request_count: int = Field(ge=0)
    failed_request_count: int = Field(ge=0)


class ReleaseEvidence(BaseModel):
    name: str = Field(min_length=1)
    commit_sha: str = Field(min_length=7, max_length=40)
    deployed_at: datetime


class AlertPayload(BaseModel):
    fingerprint: str = Field(min_length=1, max_length=200)
    source: str = Field(min_length=1, max_length=100)
    service: str = Field(min_length=1, max_length=100)
    severity: AlertSeverity
    summary: str = Field(min_length=1, max_length=500)
    started_at: datetime
    detected_at: datetime
    metric: MetricEvidence
    release: ReleaseEvidence
    telemetry_url: AnyHttpUrl

    @model_validator(mode="after")
    def validate_timeline(self) -> "AlertPayload":
        if self.started_at > self.detected_at:
            raise ValueError("started_at must be at or before detected_at")
        return self


class IncidentSummary(BaseModel):
    id: UUID
    status: IncidentStatus
    service: str
    severity: AlertSeverity
    summary: str
    started_at: datetime
    detected_at: datetime
    received_at: datetime
    updated_at: datetime
    resolved_at: datetime | None
    version: int


class IncidentEvent(BaseModel):
    id: UUID
    event_type: str
    actor: str
    from_status: IncidentStatus | None
    to_status: IncidentStatus | None
    note: str | None
    payload: dict[str, object]
    created_at: datetime


class IncidentDetail(IncidentSummary):
    alert: AlertPayload
    alert_count: int
    events: list[IncidentEvent]


class AlertIngestResponse(BaseModel):
    incident: IncidentDetail
    deduplicated: bool


class IncidentTransitionInput(BaseModel):
    to_status: IncidentStatus
    note: str | None = Field(default=None, max_length=2_000)
    expected_version: int = Field(gt=0)


class IncidentTransitionRequest(IncidentTransitionInput):
    """Trusted service command with a server-derived audit actor."""

    actor: str = Field(min_length=1, max_length=100)


class ResetResponse(BaseModel):
    cleared_incidents: int
