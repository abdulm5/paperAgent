from datetime import UTC, datetime
from enum import StrEnum
from threading import RLock
from uuid import UUID, uuid4

from pydantic import AnyHttpUrl, BaseModel, Field, model_validator


class AlertSeverity(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class IncidentStatus(StrEnum):
    DETECTED = "detected"


class MetricEvidence(BaseModel):
    name: str = Field(min_length=1)
    value: float = Field(ge=0)
    threshold: float = Field(ge=0)
    window_seconds: int = Field(gt=0)
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


class Incident(BaseModel):
    id: UUID
    status: IncidentStatus
    service: str
    severity: AlertSeverity
    summary: str
    started_at: datetime
    detected_at: datetime
    received_at: datetime
    alert: AlertPayload


class AlertIngestResponse(BaseModel):
    incident: Incident
    deduplicated: bool


class IncidentStore:
    """Thread-safe milestone-one store; PostgreSQL replaces it in milestone two."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._incidents: dict[UUID, Incident] = {}
        self._active_fingerprints: dict[str, UUID] = {}

    def ingest(self, alert: AlertPayload) -> tuple[Incident, bool]:
        with self._lock:
            existing_id = self._active_fingerprints.get(alert.fingerprint)
            if existing_id is not None:
                return self._incidents[existing_id], True

            incident = Incident(
                id=uuid4(),
                status=IncidentStatus.DETECTED,
                service=alert.service,
                severity=alert.severity,
                summary=alert.summary,
                started_at=alert.started_at,
                detected_at=alert.detected_at,
                received_at=datetime.now(UTC),
                alert=alert,
            )
            self._incidents[incident.id] = incident
            self._active_fingerprints[alert.fingerprint] = incident.id
            return incident, False

    def list(self) -> list[Incident]:
        with self._lock:
            return sorted(
                self._incidents.values(),
                key=lambda incident: incident.received_at,
                reverse=True,
            )

    def get(self, incident_id: UUID) -> Incident | None:
        with self._lock:
            return self._incidents.get(incident_id)

    def clear(self) -> int:
        with self._lock:
            count = len(self._incidents)
            self._incidents.clear()
            self._active_fingerprints.clear()
            return count


incident_store = IncidentStore()
