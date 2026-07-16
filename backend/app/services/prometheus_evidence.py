"""Tenant-scoped selection and revision fencing for Prometheus evidence."""

from collections.abc import Callable
from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connectors.prometheus import (
    PrometheusClientLimits,
    PrometheusHttpApiProvider,
    PrometheusProviderError,
    PrometheusUnsupportedMetricError,
)
from app.connectors.runtime import (
    PrometheusConnectorUnavailableError,
    load_prometheus_connector_runtime,
)
from app.core.config import Settings, settings
from app.db.models import ConnectorCredentialRecord, ConnectorRecord
from app.domain.connectors import (
    ConnectorProvider,
    ConnectorStatus,
    PrometheusConfiguration,
    PrometheusCredentials,
)
from app.domain.prometheus import PrometheusEvidenceBundle, PrometheusQueryResult

PrometheusEvidenceMode = Literal["off", "auto", "connector"]


class ClosablePrometheusProvider(Protocol):
    def collect_metric(
        self,
        *,
        metric_name: str,
        service: str,
        observed_at: datetime,
        window_seconds: int,
    ) -> PrometheusQueryResult: ...

    def close(self) -> None: ...


PrometheusProviderFactory = Callable[
    [PrometheusConfiguration, PrometheusCredentials],
    ClosablePrometheusProvider,
]


class InvestigationPrometheusProvider(Protocol):
    version: str

    def collect_evidence(
        self,
        *,
        metric_name: str,
        service: str,
        observed_at: datetime,
        window_seconds: int,
    ) -> PrometheusEvidenceBundle | None: ...

    def lock_current_revision(self, evidence: PrometheusEvidenceBundle | None) -> None: ...


class PrometheusEvidenceSelectionError(PrometheusProviderError):
    """A sanitized failure to resolve one tenant-owned metrics source."""


def prometheus_client_limits(
    runtime_settings: Settings = settings,
) -> PrometheusClientLimits:
    """Translate validated runtime configuration into the adapter's hard limits."""

    per_series_samples = min(
        runtime_settings.prometheus_max_samples,
        (
            runtime_settings.prometheus_max_window_seconds
            // runtime_settings.prometheus_query_step_seconds
        )
        + 1,
    )
    return PrometheusClientLimits(
        request_budget=1,
        timeout_seconds=runtime_settings.prometheus_http_timeout_seconds,
        max_response_bytes=runtime_settings.prometheus_max_response_bytes,
        max_window_seconds=runtime_settings.prometheus_max_window_seconds,
        step_seconds=runtime_settings.prometheus_query_step_seconds,
        max_series=runtime_settings.prometheus_max_series,
        max_samples_per_series=per_series_samples,
        max_total_samples=runtime_settings.prometheus_max_samples,
        max_labels_per_series=runtime_settings.prometheus_max_labels_per_series,
        max_label_name_length=100,
        max_label_value_length=256,
    )


def build_prometheus_evidence_provider(
    configuration: PrometheusConfiguration,
    credentials: PrometheusCredentials,
    *,
    runtime_settings: Settings = settings,
) -> PrometheusHttpApiProvider:
    """Construct the live adapter only after its credential envelope is opened."""

    return PrometheusHttpApiProvider(
        configuration,
        credentials,
        limits=prometheus_client_limits(runtime_settings),
    )


class TenantPrometheusEvidenceProvider:
    """Select one service binding, collect unlocked, then fence its revision."""

    version = "tenant-prometheus-selector-v1"

    def __init__(
        self,
        session: Session,
        organization_id: UUID,
        *,
        mode: PrometheusEvidenceMode,
        provider_factory: PrometheusProviderFactory = build_prometheus_evidence_provider,
    ) -> None:
        self.session = session
        self.organization_id = organization_id
        self.mode = mode
        self.provider_factory = provider_factory

    def collect_evidence(
        self,
        *,
        metric_name: str,
        service: str,
        observed_at: datetime,
        window_seconds: int,
    ) -> PrometheusEvidenceBundle | None:
        if self.mode == "off":
            return None

        connector_ids = list(
            self.session.scalars(
                select(ConnectorRecord.id)
                .where(
                    ConnectorRecord.organization_id == self.organization_id,
                    ConnectorRecord.provider == ConnectorProvider.PROMETHEUS.value,
                    ConnectorRecord.enabled.is_(True),
                    ConnectorRecord.status == ConnectorStatus.CONFIGURED.value,
                    ConnectorRecord.configuration["service"].as_string() == service,
                )
                .order_by(ConnectorRecord.id)
                .limit(2)
            ).all()
        )
        if not connector_ids:
            self.session.rollback()
            if self.mode == "auto":
                return None
            raise PrometheusEvidenceSelectionError(
                "No enabled Prometheus connector is configured for this incident service"
            )
        if len(connector_ids) != 1:
            self.session.rollback()
            raise PrometheusEvidenceSelectionError(
                "Multiple enabled Prometheus connectors match this incident service"
            )

        try:
            runtime = load_prometheus_connector_runtime(self.session, connector_ids[0])
        except PrometheusConnectorUnavailableError as error:
            self.session.rollback()
            raise PrometheusEvidenceSelectionError(
                "The Prometheus connector became unavailable during evidence collection"
            ) from error
        if (
            runtime.organization_id != self.organization_id
            or runtime.configuration.service != service
        ):
            self.session.rollback()
            raise PrometheusEvidenceSelectionError(
                "The Prometheus connector changed during evidence collection"
            )

        connector_id = runtime.connector_id
        connector_version = runtime.connector_version
        credential_version = runtime.credential_version
        configuration = runtime.configuration
        credentials = runtime.credentials

        # Network I/O occurs without a database transaction or connector lock.
        # InvestigationService later fences this exact revision after all other
        # provider reads have completed and before any evidence is persisted.
        self.session.rollback()
        try:
            provider = self.provider_factory(configuration, credentials)
            try:
                try:
                    result = provider.collect_metric(
                        metric_name=metric_name,
                        service=service,
                        observed_at=observed_at,
                        window_seconds=window_seconds,
                    )
                except PrometheusUnsupportedMetricError:
                    if self.mode == "auto":
                        return None
                    raise
            finally:
                provider.close()
        except PrometheusProviderError:
            raise
        except Exception as error:
            raise PrometheusEvidenceSelectionError(
                "Prometheus evidence collection failed at the provider boundary"
            ) from error

        try:
            evidence = PrometheusEvidenceBundle.model_validate(
                {
                    **result.model_dump(),
                    "source_uri": f"prometheus://connector/{connector_id}/{service}",
                    "connector_id": connector_id,
                    "connector_version": connector_version,
                    "credential_version": credential_version,
                }
            )
        except (AttributeError, TypeError, ValueError) as error:
            raise PrometheusEvidenceSelectionError(
                "Prometheus returned evidence outside the selected connector binding"
            ) from error
        return evidence

    def lock_current_revision(self, evidence: PrometheusEvidenceBundle | None) -> None:
        """Hold the selected connector row lock through the evidence commit."""

        if evidence is None:
            return
        current = self.session.execute(
            select(ConnectorRecord, ConnectorCredentialRecord)
            .join(
                ConnectorCredentialRecord,
                ConnectorCredentialRecord.connector_id == ConnectorRecord.id,
            )
            .where(
                ConnectorRecord.id == evidence.connector_id,
                ConnectorRecord.organization_id == self.organization_id,
                ConnectorRecord.provider == ConnectorProvider.PROMETHEUS.value,
                ConnectorRecord.enabled.is_(True),
                ConnectorRecord.status == ConnectorStatus.CONFIGURED.value,
            )
            .with_for_update()
        ).one_or_none()
        try:
            current_configuration = (
                PrometheusConfiguration.model_validate(current[0].configuration)
                if current is not None
                else None
            )
        except (TypeError, ValueError):
            current_configuration = None
        if (
            current is None
            or current[0].version != evidence.connector_version
            or current[1].credential_version != evidence.credential_version
            or current_configuration is None
            or current_configuration.service != evidence.service
        ):
            self.session.rollback()
            raise PrometheusEvidenceSelectionError(
                "The Prometheus connector changed during evidence collection"
            )
