from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session

from app.auth.constants import DEFAULT_ORGANIZATION_ID
from app.connectors.vault import CredentialContext, build_credential_cipher
from app.db.models import (
    ConnectorCredentialRecord,
    ConnectorRecord,
    OrganizationRecord,
)
from app.domain.connectors import (
    ConnectorProvider,
    PrometheusConfiguration,
    PrometheusCredentials,
)
from app.domain.prometheus import (
    PrometheusQueryResult,
    PrometheusSample,
    PrometheusSeries,
)
from app.services.prometheus_evidence import (
    PrometheusEvidenceSelectionError,
    TenantPrometheusEvidenceProvider,
)
from tests.conftest import TestingSessionLocal

OBSERVED_AT = datetime(2026, 7, 16, 16, 30, tzinfo=UTC)
TOKEN = "prometheus-read-token-SENTINEL"


def add_connector(
    session: Session,
    *,
    organization_id: UUID = DEFAULT_ORGANIZATION_ID,
    service: str = "checkout-api",
    enabled: bool = True,
    with_credentials: bool = True,
) -> ConnectorRecord:
    connector_id = uuid4()
    connector = ConnectorRecord(
        id=connector_id,
        organization_id=organization_id,
        name=f"Prometheus {connector_id}",
        provider=ConnectorProvider.PROMETHEUS.value,
        configuration={
            "service": service,
            "base_url": "http://prometheus:9090",
        },
        enabled=enabled,
        status="configured" if enabled else "disabled",
        version=5,
        last_validation_ok=enabled,
    )
    if with_credentials:
        sealed = build_credential_cipher().seal(
            {"bearer_token": TOKEN},
            CredentialContext(
                organization_id=organization_id,
                connector_id=connector_id,
                provider=ConnectorProvider.PROMETHEUS,
                credential_version=3,
            ),
        )
        connector.credential = ConnectorCredentialRecord(
            connector_id=connector_id,
            credential_version=3,
            ciphertext=sealed.ciphertext,
            ciphertext_nonce=sealed.ciphertext_nonce,
            wrapped_data_key=sealed.wrapped_data_key,
            wrapped_key_nonce=sealed.wrapped_key_nonce,
            key_version=sealed.key_version,
            credential_field_names=list(sealed.credential_field_names),
        )
    session.add(connector)
    session.commit()
    return connector


class RecordingProvider:
    def __init__(
        self,
        session: Session,
        configuration: PrometheusConfiguration,
        credentials: PrometheusCredentials,
    ) -> None:
        self.session = session
        self.configuration = configuration
        self.credentials = credentials
        self.closed = False
        self.called_outside_transaction = False

    def collect_metric(
        self,
        *,
        metric_name: str,
        service: str,
        observed_at: datetime,
        window_seconds: int,
    ) -> PrometheusQueryResult:
        self.called_outside_transaction = not self.session.in_transaction()
        return PrometheusQueryResult(
            provider_version="recording-prometheus-v1",
            catalog_version="prometheus-query-catalog-v1",
            query_id="alert.http-server-error-rate.v1",
            metric_name=metric_name,
            service=service,
            window_started_at=observed_at - timedelta(seconds=window_seconds),
            window_ended_at=observed_at,
            step_seconds=15,
            series_count=1,
            sample_count=1,
            series=[
                PrometheusSeries(
                    labels={"service": service},
                    samples=[PrometheusSample(observed_at=observed_at, value=0.13)],
                )
            ],
        )

    def close(self) -> None:
        self.closed = True


def build_selector(
    session: Session,
    *,
    mode: str,
    factory=None,
) -> TenantPrometheusEvidenceProvider:
    return TenantPrometheusEvidenceProvider(
        session,
        DEFAULT_ORGANIZATION_ID,
        mode=mode,  # type: ignore[arg-type]
        **({"provider_factory": factory} if factory is not None else {}),
    )


def collect(selector: TenantPrometheusEvidenceProvider):
    return selector.collect_evidence(
        metric_name="http_server_error_rate",
        service="checkout-api",
        observed_at=OBSERVED_AT,
        window_seconds=300,
    )


def test_selects_exact_tenant_service_decrypts_briefly_and_enriches_provenance(
    db_session: Session,
) -> None:
    other_organization_id = uuid4()
    db_session.add(
        OrganizationRecord(
            id=other_organization_id,
            slug="other-observability",
            name="Other Observability",
        )
    )
    db_session.commit()
    add_connector(db_session, organization_id=other_organization_id)
    add_connector(db_session, service="catalog-api")
    connector = add_connector(db_session)
    providers: list[RecordingProvider] = []

    def factory(
        configuration: PrometheusConfiguration,
        credentials: PrometheusCredentials,
    ) -> RecordingProvider:
        provider = RecordingProvider(db_session, configuration, credentials)
        providers.append(provider)
        return provider

    selector = build_selector(db_session, mode="connector", factory=factory)
    bundle = collect(selector)

    assert bundle is not None
    assert len(providers) == 1
    assert providers[0].configuration.service == "checkout-api"
    assert providers[0].credentials.bearer_token.get_secret_value() == TOKEN
    assert providers[0].called_outside_transaction is True
    assert providers[0].closed is True
    assert bundle.connector_id == connector.id
    assert bundle.connector_version == 5
    assert bundle.credential_version == 3
    assert bundle.source_uri == f"prometheus://connector/{connector.id}/checkout-api"
    assert TOKEN not in bundle.model_dump_json()

    selector.lock_current_revision(bundle)
    assert db_session.in_transaction() is True


def test_modes_are_explicit_when_no_matching_connector(db_session: Session) -> None:
    assert collect(build_selector(db_session, mode="off")) is None
    assert collect(build_selector(db_session, mode="auto")) is None
    with pytest.raises(PrometheusEvidenceSelectionError, match="No enabled Prometheus"):
        collect(build_selector(db_session, mode="connector"))


def test_auto_mode_fails_closed_when_matching_connector_cannot_open(
    db_session: Session,
) -> None:
    add_connector(db_session, with_credentials=False)

    with pytest.raises(PrometheusEvidenceSelectionError, match="became unavailable"):
        collect(build_selector(db_session, mode="auto"))


def test_duplicate_enabled_service_binding_fails_closed(db_session: Session) -> None:
    add_connector(db_session)
    add_connector(db_session)

    with pytest.raises(PrometheusEvidenceSelectionError, match="Multiple enabled"):
        collect(build_selector(db_session, mode="connector"))


def test_unexpected_provider_failure_is_sanitized(db_session: Session) -> None:
    add_connector(db_session)

    class FailingProvider(RecordingProvider):
        def collect_metric(
            self,
            *,
            metric_name: str,
            service: str,
            observed_at: datetime,
            window_seconds: int,
        ) -> PrometheusQueryResult:
            raise RuntimeError(f"transport echoed {TOKEN}")

    def factory(
        configuration: PrometheusConfiguration,
        credentials: PrometheusCredentials,
    ) -> RecordingProvider:
        return FailingProvider(db_session, configuration, credentials)

    with pytest.raises(PrometheusEvidenceSelectionError) as raised:
        collect(build_selector(db_session, mode="connector", factory=factory))

    assert TOKEN not in str(raised.value)
    assert "transport echoed" not in str(raised.value)


def test_revision_fence_discards_evidence_after_connector_revocation(
    db_session: Session,
) -> None:
    connector = add_connector(db_session)

    def factory(
        configuration: PrometheusConfiguration,
        credentials: PrometheusCredentials,
    ) -> RecordingProvider:
        return RecordingProvider(db_session, configuration, credentials)

    selector = build_selector(db_session, mode="connector", factory=factory)
    bundle = collect(selector)
    assert bundle is not None

    with TestingSessionLocal() as racing_session:
        current = racing_session.get(ConnectorRecord, connector.id)
        assert current is not None
        current.enabled = False
        current.status = "disabled"
        current.version += 1
        racing_session.commit()

    with pytest.raises(PrometheusEvidenceSelectionError, match="changed during"):
        selector.lock_current_revision(bundle)
