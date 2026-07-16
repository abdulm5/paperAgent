from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session

from app.auth.constants import DEFAULT_ORGANIZATION_ID
from app.connectors.vault import CredentialContext, build_credential_cipher
from app.db.models import (
    ConnectorCredentialRecord,
    ConnectorRecord,
    GithubWebhookDeliveryRecord,
    OrganizationRecord,
)
from app.domain.connectors import ConnectorProvider, GithubConfiguration, GithubCredentials
from app.domain.github import GitEvidenceBundle
from app.investigation.commits import FixtureGitProvider
from app.services.github_evidence import (
    GitHubEvidenceSelectionError,
    TenantGitEvidenceProvider,
)
from tests.conftest import TestingSessionLocal
from tests.test_github_app import PRIVATE_KEY_PEM

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEPLOYED_AT = datetime(2026, 7, 16, 16, 30, tzinfo=UTC)
WEBHOOK_SECRET = "github-webhook-secret-with-at-least-32-bytes"


def add_connector(
    session: Session,
    *,
    organization_id: UUID = DEFAULT_ORGANIZATION_ID,
    service: str = "checkout-api",
    repository: str = "octo-org/pageragent",
    enabled: bool = True,
    with_credentials: bool = True,
) -> ConnectorRecord:
    connector_id = uuid4()
    connector = ConnectorRecord(
        id=connector_id,
        organization_id=organization_id,
        name=f"GitHub {connector_id}",
        provider=ConnectorProvider.GITHUB.value,
        configuration={
            "service": service,
            "repository": repository,
            "app_id": 12345,
            "installation_id": 67890,
            "api_url": "https://api.github.com",
        },
        enabled=enabled,
        status="configured" if enabled else "disabled",
        version=4,
        last_validation_ok=enabled,
    )
    if with_credentials:
        sealed = build_credential_cipher().seal(
            {
                "private_key": PRIVATE_KEY_PEM,
                "webhook_secret": WEBHOOK_SECRET,
            },
            CredentialContext(
                organization_id=organization_id,
                connector_id=connector_id,
                provider=ConnectorProvider.GITHUB,
                credential_version=2,
            ),
        )
        connector.credential = ConnectorCredentialRecord(
            connector_id=connector_id,
            credential_version=2,
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
        configuration: GithubConfiguration,
        credentials: GithubCredentials,
    ) -> None:
        self.session = session
        self.configuration = configuration
        self.credentials = credentials
        self.closed = False
        self.called_outside_transaction = False

    def collect_evidence(
        self,
        deployed_at: datetime,
        service: str,
        active_commit_sha: str,
    ) -> GitEvidenceBundle:
        self.called_outside_transaction = not self.session.in_transaction()
        fixture = FixtureGitProvider(
            REPOSITORY_ROOT / "scenarios/checkout-commits.json"
        ).collect_evidence(deployed_at, service, active_commit_sha)
        return GitEvidenceBundle(
            source_uri=f"github://{self.configuration.repository}",
            provider="github_app",
            repository=self.configuration.repository,
            provider_version="recording-github-v1",
            deployed_at=deployed_at,
            service=service,
            active_commit_sha=active_commit_sha,
            commits=fixture.commits,
        )

    def close(self) -> None:
        self.closed = True


def build_selector(
    session: Session,
    *,
    mode: str,
    factory=None,
    max_webhook_receipts: int = 10,
) -> TenantGitEvidenceProvider:
    return TenantGitEvidenceProvider(
        session,
        DEFAULT_ORGANIZATION_ID,
        mode=mode,  # type: ignore[arg-type]
        fixture_path=REPOSITORY_ROOT / "scenarios/checkout-commits.json",
        lookback_hours=24,
        max_webhook_receipts=max_webhook_receipts,
        **({"provider_factory": factory} if factory is not None else {}),
    )


def test_selects_exact_tenant_service_and_enriches_connector_and_webhook_provenance(
    db_session: Session,
) -> None:
    other_organization_id = uuid4()
    db_session.add(
        OrganizationRecord(
            id=other_organization_id,
            slug="other-operations",
            name="Other Operations",
        )
    )
    db_session.commit()
    add_connector(
        db_session,
        organization_id=other_organization_id,
        repository="other-org/wrong-tenant",
    )
    add_connector(
        db_session,
        service="catalog-api",
        repository="octo-org/wrong-service",
    )
    connector = add_connector(db_session)
    matching_delivery_id = "11111111-1111-4111-8111-111111111111"
    db_session.add_all(
        [
            GithubWebhookDeliveryRecord(
                organization_id=DEFAULT_ORGANIZATION_ID,
                connector_id=connector.id,
                delivery_id=matching_delivery_id,
                event_type="deployment_status",
                action="created",
                repository="octo-org/pageragent",
                installation_id=67890,
                connector_version=4,
                credential_version=2,
                body_sha256="a" * 64,
                normalized_payload={"service": "checkout-api"},
                received_at=DEPLOYED_AT,
            ),
            GithubWebhookDeliveryRecord(
                organization_id=DEFAULT_ORGANIZATION_ID,
                connector_id=connector.id,
                delivery_id="22222222-2222-4222-8222-222222222222",
                event_type="push",
                action=None,
                repository="octo-org/pageragent",
                installation_id=67890,
                connector_version=4,
                credential_version=2,
                body_sha256="b" * 64,
                normalized_payload={"service": "checkout-api"},
                received_at=DEPLOYED_AT - timedelta(days=2),
            ),
        ]
    )
    db_session.commit()

    providers: list[RecordingProvider] = []

    def factory(
        configuration: GithubConfiguration,
        credentials: GithubCredentials,
    ) -> RecordingProvider:
        provider = RecordingProvider(db_session, configuration, credentials)
        providers.append(provider)
        return provider

    bundle = build_selector(db_session, mode="connector", factory=factory).collect_evidence(
        DEPLOYED_AT,
        "checkout-api",
        "8fa23c1",
    )

    assert len(providers) == 1
    assert providers[0].configuration.repository == "octo-org/pageragent"
    assert providers[0].credentials.webhook_secret.get_secret_value() == WEBHOOK_SECRET
    assert providers[0].called_outside_transaction is True
    assert providers[0].closed is True
    assert bundle.provider == "github_app"
    assert bundle.connector_id == connector.id
    assert bundle.connector_version == 4
    assert bundle.credential_version == 2
    assert [receipt.delivery_id for receipt in bundle.webhook_receipts] == [
        matching_delivery_id
    ]
    assert bundle.webhook_receipts[0].body_sha256 == "a" * 64
    assert bundle.webhook_receipts[0].connector_version == 4
    assert bundle.webhook_receipts[0].credential_version == 2
    assert "normalized_payload" not in bundle.model_dump_json()


def test_auto_mode_falls_back_only_when_no_matching_connector(
    db_session: Session,
) -> None:
    auto_bundle = build_selector(db_session, mode="auto").collect_evidence(
        DEPLOYED_AT,
        "checkout-api",
        "8fa23c1",
    )

    assert auto_bundle.provider == "fixture"
    with pytest.raises(GitHubEvidenceSelectionError, match="No enabled GitHub connector"):
        build_selector(db_session, mode="connector").collect_evidence(
            DEPLOYED_AT,
            "checkout-api",
            "8fa23c1",
        )


def test_auto_mode_fails_closed_when_a_matching_connector_is_unavailable(
    db_session: Session,
) -> None:
    add_connector(db_session, with_credentials=False)

    with pytest.raises(GitHubEvidenceSelectionError, match="became unavailable"):
        build_selector(db_session, mode="auto").collect_evidence(
            DEPLOYED_AT,
            "checkout-api",
            "8fa23c1",
        )


def test_duplicate_enabled_service_binding_fails_closed(db_session: Session) -> None:
    add_connector(db_session, repository="octo-org/first")
    add_connector(db_session, repository="octo-org/second")

    with pytest.raises(GitHubEvidenceSelectionError, match="Multiple enabled"):
        build_selector(db_session, mode="connector").collect_evidence(
            DEPLOYED_AT,
            "checkout-api",
            "8fa23c1",
        )


def test_connector_revocation_during_provider_call_discards_stale_evidence(
    db_session: Session,
) -> None:
    connector = add_connector(db_session)
    providers: list[RecordingProvider] = []

    class RevokingProvider(RecordingProvider):
        def collect_evidence(
            self,
            deployed_at: datetime,
            service: str,
            active_commit_sha: str,
        ) -> GitEvidenceBundle:
            with TestingSessionLocal() as racing_session:
                current = racing_session.get(ConnectorRecord, connector.id)
                assert current is not None
                current.enabled = False
                current.status = "disabled"
                current.version += 1
                racing_session.commit()
            return super().collect_evidence(deployed_at, service, active_commit_sha)

    def factory(
        configuration: GithubConfiguration,
        credentials: GithubCredentials,
    ) -> RecordingProvider:
        provider = RevokingProvider(db_session, configuration, credentials)
        providers.append(provider)
        return provider

    with pytest.raises(GitHubEvidenceSelectionError, match="changed during"):
        build_selector(db_session, mode="connector", factory=factory).collect_evidence(
            DEPLOYED_AT,
            "checkout-api",
            "8fa23c1",
        )

    assert len(providers) == 1
    assert providers[0].closed is True
