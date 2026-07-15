from datetime import datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator


class CausalKind(StrEnum):
    CODE_CHANGE = "code_change"
    CONFIGURATION_CHANGE = "configuration_change"
    UPSTREAM_DEPENDENCY = "upstream_dependency"
    UNKNOWN = "unknown"


class MitigationActionType(StrEnum):
    ROLLBACK_SERVICE = "rollback_service"
    DISABLE_FEATURE_FLAG = "disable_feature_flag"
    ESCALATE_ONLY = "escalate_only"


class ScenarioCausalTruth(BaseModel):
    kind: CausalKind
    reference: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=300)


class ScenarioActionTruth(BaseModel):
    action_type: MitigationActionType
    automation_allowed: bool
    parameters: dict[str, str | bool] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_authority(self) -> "ScenarioActionTruth":
        if self.action_type is MitigationActionType.ESCALATE_ONLY and self.automation_allowed:
            raise ValueError("escalate_only cannot permit automation")
        return self


class ScenarioGroundTruth(BaseModel):
    causal_signal: ScenarioCausalTruth
    expected_runbook: str = Field(min_length=1)
    expected_impacted_requests: int = Field(ge=0)
    affected_attributes: dict[str, list[str]]
    expected_action: ScenarioActionTruth
    red_herring_references: list[str] = Field(default_factory=list)


class ScenarioSimulation(BaseModel):
    mode: Literal[
        "release_regression", "upstream_dependency", "feature_flag_regression"
    ]
    healthy_requests: int = Field(ge=0)
    outage_requests: int = Field(gt=0)
    failure_every: int = Field(gt=0)
    payment_method: str
    error_type: str
    failure_status: int = Field(ge=400, le=599)
    active_release: str
    active_commit: str = Field(min_length=7, max_length=40)
    upstream_dependency: str | None = None
    feature_flag: str | None = None


class ScenarioThresholds(BaseModel):
    cause_top_1: float = Field(default=1.0, ge=0, le=1)
    runbook_mrr: float = Field(default=1.0, ge=0, le=1)
    citation_coverage: float = Field(default=1.0, ge=0, le=1)
    action_safety: float = Field(default=1.0, ge=0, le=1)
    resilience: float = Field(default=1.0, ge=0, le=1)


class ScenarioContract(BaseModel):
    schema_version: Literal["1.0"]
    id: str = Field(pattern=r"^[a-z0-9-]+$")
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=1_000)
    service: str = Field(min_length=1, max_length=100)
    severity: Literal["critical", "high", "medium", "low"]
    simulation: ScenarioSimulation
    ground_truth: ScenarioGroundTruth
    adversarial_cases: list[
        Literal[
            "red_herring_deploy",
            "hallucinated_citation",
            "missing_evidence",
            "low_confidence_action",
        ]
    ] = Field(min_length=1)
    thresholds: ScenarioThresholds = Field(default_factory=ScenarioThresholds)


class CauseCandidate(BaseModel):
    id: UUID | None = None
    kind: CausalKind
    reference: str
    title: str
    rank: int = Field(gt=0)
    score: float = Field(ge=0, le=1)
    explanation: list[str] = Field(min_length=1)
    evidence_ids: list[str] = Field(default_factory=list)


class AdversarialProbeResult(BaseModel):
    case: str
    passed: bool
    observation: str


class ScenarioMetrics(BaseModel):
    cause_top_1: float = Field(ge=0, le=1)
    runbook_mrr: float = Field(ge=0, le=1)
    impact_accuracy: float = Field(ge=0, le=1)
    affected_attribute_accuracy: float = Field(ge=0, le=1)
    citation_coverage: float = Field(ge=0, le=1)
    action_safety: float = Field(ge=0, le=1)
    automation_decision: float = Field(ge=0, le=1)
    resilience: float = Field(ge=0, le=1)


class ScenarioEvaluationResult(BaseModel):
    scenario_id: str
    title: str
    passed: bool
    predicted_cause: CauseCandidate
    predicted_runbook: str | None
    predicted_action: dict[str, Any]
    metrics: ScenarioMetrics
    adversarial_probes: list[AdversarialProbeResult]
    duration_ms: float = Field(ge=0)


class EvaluationGate(BaseModel):
    metric: str
    value: float
    threshold: float
    passed: bool


class EvaluationScorecard(BaseModel):
    schema_version: Literal["1.0"] = "1.0"
    suite_version: str
    generated_at: datetime
    passed: bool
    scenario_count: int
    aggregate_metrics: dict[str, float]
    gates: list[EvaluationGate]
    scenarios: list[ScenarioEvaluationResult]
