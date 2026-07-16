import re
from datetime import datetime, timedelta
from math import isfinite
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_LABEL_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PERSISTED_LABEL_ALLOWLIST = frozenset(
    {"__name__", "service", "job", "instance", "cluster", "namespace"}
)


class PrometheusEvidenceModel(BaseModel):
    """Base contract for bounded, persistence-safe Prometheus evidence."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class PrometheusSample(PrometheusEvidenceModel):
    observed_at: datetime
    value: float

    @field_validator("observed_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Prometheus sample timestamps must include a timezone")
        return value

    @field_validator("value")
    @classmethod
    def require_finite_value(cls, value: float) -> float:
        if not isfinite(value):
            raise ValueError("Prometheus sample values must be finite")
        return value


class PrometheusSeries(PrometheusEvidenceModel):
    labels: dict[str, str] = Field(max_length=64)
    samples: list[PrometheusSample] = Field(max_length=10_000)

    @model_validator(mode="after")
    def validate_persistence_boundary(self) -> "PrometheusSeries":
        for name, value in self.labels.items():
            if (
                len(name) > 100
                or _LABEL_NAME_PATTERN.fullmatch(name) is None
                or name not in _PERSISTED_LABEL_ALLOWLIST
            ):
                raise ValueError("Prometheus evidence contains an invalid label name")
            if len(value) > 256 or any(
                ord(character) < 32 or ord(character) == 127 for character in value
            ):
                raise ValueError("Prometheus evidence contains an invalid label value")
        return self


class PrometheusQueryResult(PrometheusEvidenceModel):
    """Normalized client result before tenant connector provenance is attached."""

    provider: Literal["prometheus"] = "prometheus"
    provider_version: str = Field(min_length=1, max_length=100)
    catalog_version: str = Field(min_length=1, max_length=100)
    query_id: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[a-z0-9][a-z0-9._-]*$",
    )
    metric_name: str = Field(
        min_length=1,
        max_length=200,
        pattern=r"^[A-Za-z_:][A-Za-z0-9_:]*$",
    )
    service: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,98}[A-Za-z0-9])?$",
    )
    window_started_at: datetime
    window_ended_at: datetime
    step_seconds: int = Field(gt=0, le=86_400)
    series_count: int = Field(ge=0, le=10_000)
    sample_count: int = Field(ge=0, le=1_000_000)
    truncated: Literal[False] = False
    series: list[PrometheusSeries] = Field(max_length=10_000)

    @model_validator(mode="after")
    def validate_counts_and_window(self) -> "PrometheusQueryResult":
        if (
            self.window_started_at.tzinfo is None
            or self.window_started_at.utcoffset() is None
            or self.window_ended_at.tzinfo is None
            or self.window_ended_at.utcoffset() is None
        ):
            raise ValueError("Prometheus evidence windows must include timezones")
        if self.window_started_at > self.window_ended_at:
            raise ValueError("Prometheus evidence window is reversed")
        if self.window_ended_at - self.window_started_at > timedelta(days=1):
            raise ValueError("Prometheus evidence window exceeds its schema boundary")
        if self.series_count != len(self.series):
            raise ValueError("Prometheus series count does not match its payload")
        if self.sample_count != sum(len(item.samples) for item in self.series):
            raise ValueError("Prometheus sample count does not match its payload")
        identities = [tuple(sorted(item.labels.items())) for item in self.series]
        if identities != sorted(identities) or len(set(identities)) != len(identities):
            raise ValueError("Prometheus series must be uniquely and canonically ordered")
        for item in self.series:
            if item.labels.get("service") != self.service:
                raise ValueError("Prometheus series does not match its service binding")
            previous: datetime | None = None
            for sample in item.samples:
                if not self.window_started_at <= sample.observed_at <= self.window_ended_at:
                    raise ValueError("Prometheus sample falls outside its evidence window")
                if previous is not None and sample.observed_at <= previous:
                    raise ValueError("Prometheus samples must be strictly ordered")
                previous = sample.observed_at
        return self


class PrometheusEvidenceBundle(PrometheusQueryResult):
    """Tenant-enriched evidence safe to persist in an investigation ledger."""

    source_uri: str = Field(min_length=1, max_length=500)
    connector_id: UUID
    connector_version: int = Field(gt=0)
    credential_version: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_sanitized_source(self) -> "PrometheusEvidenceBundle":
        expected = f"prometheus://connector/{self.connector_id}/{self.service}"
        if self.source_uri != expected:
            raise ValueError("Prometheus evidence source must identify its connector and service")
        return self
