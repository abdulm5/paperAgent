from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class PaymentMethod(StrEnum):
    CARD = "card"
    BANK_TRANSFER = "bank_transfer"
    DIGITAL_WALLET = "digital_wallet"


class ReleaseName(StrEnum):
    STABLE = "stable-v1"
    FAULTY = "faulty-v2"
    OBSERVABILITY = "observability-v3"


class ScenarioName(StrEnum):
    VALIDATION_BUG = "checkout-validation-bug"
    PROVIDER_TIMEOUT = "payment-provider-timeout"
    FEATURE_FLAG_REGRESSION = "checkout-feature-flag-regression"


class CheckoutRequest(BaseModel):
    user_id: str = Field(min_length=1, max_length=100)
    cart_total_cents: int = Field(gt=0, le=10_000_000)
    payment_method: PaymentMethod


class CheckoutResponse(BaseModel):
    status: str = "accepted"
    order_id: str
    request_id: str
    trace_id: str
    release: ReleaseName


class CheckoutFailure(BaseModel):
    status: str = "failed"
    error_code: str
    message: str
    request_id: str
    trace_id: str
    release: ReleaseName


class ReleaseMetadata(BaseModel):
    name: ReleaseName
    commit_sha: str
    deployed_at: datetime


class DeploymentEvent(BaseModel):
    previous_release: ReleaseName | None
    release: ReleaseName
    commit_sha: str
    deployed_at: datetime


class TelemetryEvent(BaseModel):
    timestamp: datetime
    service: str = "checkout-api"
    endpoint: str = "/checkout"
    request_id: str
    trace_id: str
    user_id: str
    payment_method: PaymentMethod
    release: ReleaseName
    commit_sha: str
    http_status: int
    outcome: str
    latency_ms: float
    error_type: str | None = None
    scenario_id: str = "healthy"
    upstream_dependency: str | None = None
    feature_flag: str | None = None


class ConfigurationChange(BaseModel):
    name: str
    previous_value: bool
    value: bool
    changed_at: datetime
    actor: str


class TelemetrySnapshot(BaseModel):
    service: str = "checkout-api"
    observed_at: datetime
    window_started_at: datetime
    window_seconds: int
    current_release: ReleaseMetadata
    request_count: int
    successful_request_count: int
    failed_request_count: int
    error_rate: float
    p95_latency_ms: float
    first_failure_at: datetime | None
    deployments: list[DeploymentEvent]
    feature_flags: dict[str, bool]
    dependencies: dict[str, str]
    configuration_changes: list[ConfigurationChange]
    scenario_id: str
    recent_events: list[TelemetryEvent]


class ResetResponse(BaseModel):
    status: str = "reset"
    active_release: ReleaseName
    request_count: int = 0


class ScenarioStateResponse(BaseModel):
    scenario_id: ScenarioName
    active_release: ReleaseName
    feature_flags: dict[str, bool]
    dependencies: dict[str, str]


class FeatureFlagResponse(BaseModel):
    name: str
    value: bool
    changed_at: datetime
