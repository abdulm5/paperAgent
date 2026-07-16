"""Tenant-scoped selection and enrichment for live GitHub incident evidence."""

from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal, Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connectors.github import (
    GitHubAppEvidenceProvider,
    GitHubClientLimits,
    GitHubProviderError,
)
from app.connectors.runtime import (
    GithubConnectorUnavailableError,
    load_github_connector_runtime,
)
from app.core.config import Settings, settings
from app.db.models import (
    ConnectorCredentialRecord,
    ConnectorRecord,
    GithubWebhookDeliveryRecord,
)
from app.domain.connectors import (
    ConnectorProvider,
    ConnectorStatus,
    GithubConfiguration,
    GithubCredentials,
)
from app.domain.github import GitEvidenceBundle, GitWebhookReceipt
from app.investigation.commits import FixtureGitProvider

GitHubEvidenceMode = Literal["auto", "connector", "fixture"]


class ClosableGitHubProvider(Protocol):
    def collect_evidence(
        self,
        deployed_at: datetime,
        service: str,
        active_commit_sha: str,
    ) -> GitEvidenceBundle: ...

    def close(self) -> None: ...


GitHubProviderFactory = Callable[
    [GithubConfiguration, GithubCredentials],
    ClosableGitHubProvider,
]


class GitHubEvidenceSelectionError(GitHubProviderError):
    """A sanitized failure to resolve one tenant-owned evidence source."""


def github_client_limits(runtime_settings: Settings = settings) -> GitHubClientLimits:
    """Translate validated runtime settings into the adapter's safety envelope."""

    related_items = runtime_settings.github_max_related_items
    return GitHubClientLimits(
        request_budget=runtime_settings.github_max_api_requests,
        max_response_bytes=runtime_settings.github_max_response_bytes,
        timeout_seconds=runtime_settings.github_http_timeout_seconds,
        lookback_hours=runtime_settings.github_evidence_lookback_hours,
        max_commits=runtime_settings.github_max_commits,
        max_files_per_commit=50,
        max_pull_requests=related_items,
        # Each deployment needs a second status request. Keeping this list at
        # six leaves room for one token refresh within the global request cap.
        max_deployments=min(related_items, 6),
        max_releases=related_items,
    )


def build_github_evidence_provider(
    configuration: GithubConfiguration,
    credentials: GithubCredentials,
    *,
    runtime_settings: Settings = settings,
) -> GitHubAppEvidenceProvider:
    """Construct the live adapter only after credentials have been decrypted."""

    return GitHubAppEvidenceProvider(
        configuration,
        credentials,
        api_version=runtime_settings.github_api_version,
        limits=github_client_limits(runtime_settings),
    )


class TenantGitEvidenceProvider:
    """Select exactly one enabled connector for an incident's tenant and service."""

    version = "tenant-github-selector-v1"

    def __init__(
        self,
        session: Session,
        organization_id: UUID,
        *,
        mode: GitHubEvidenceMode,
        fixture_path: Path,
        lookback_hours: int,
        max_webhook_receipts: int,
        provider_factory: GitHubProviderFactory = build_github_evidence_provider,
    ) -> None:
        self.session = session
        self.organization_id = organization_id
        self.mode = mode
        self.fixture_provider = FixtureGitProvider(fixture_path)
        self.lookback_hours = lookback_hours
        self.max_webhook_receipts = max_webhook_receipts
        self.provider_factory = provider_factory

    def collect_evidence(
        self,
        deployed_at: datetime,
        service: str,
        active_commit_sha: str,
    ) -> GitEvidenceBundle:
        if self.mode == "fixture":
            return self.fixture_provider.collect_evidence(
                deployed_at,
                service,
                active_commit_sha,
            )

        connector_ids = list(
            self.session.scalars(
                select(ConnectorRecord.id)
                .where(
                    ConnectorRecord.organization_id == self.organization_id,
                    ConnectorRecord.provider == ConnectorProvider.GITHUB.value,
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
                return self.fixture_provider.collect_evidence(
                    deployed_at,
                    service,
                    active_commit_sha,
                )
            raise GitHubEvidenceSelectionError(
                "No enabled GitHub connector is configured for this incident service"
            )
        if len(connector_ids) != 1:
            self.session.rollback()
            raise GitHubEvidenceSelectionError(
                "Multiple enabled GitHub connectors match this incident service"
            )

        try:
            runtime = load_github_connector_runtime(self.session, connector_ids[0])
        except GithubConnectorUnavailableError as error:
            self.session.rollback()
            raise GitHubEvidenceSelectionError(
                "The GitHub connector became unavailable during evidence collection"
            ) from error
        if (
            runtime.organization_id != self.organization_id
            or runtime.configuration.service != service
        ):
            self.session.rollback()
            raise GitHubEvidenceSelectionError(
                "The GitHub connector changed during evidence collection"
            )

        receipts = self._load_webhook_receipts(
            connector_id=runtime.connector_id,
            repository=runtime.configuration.repository,
            installation_id=runtime.configuration.installation_id,
            service=service,
            deployed_at=deployed_at,
        )
        connector_id = runtime.connector_id
        connector_version = runtime.connector_version
        credential_version = runtime.credential_version
        configuration = runtime.configuration
        credentials = runtime.credentials

        # End all database reads before the provider performs network I/O.
        self.session.rollback()
        provider = self.provider_factory(configuration, credentials)
        try:
            bundle = provider.collect_evidence(deployed_at, service, active_commit_sha)
        finally:
            provider.close()
        if (
            bundle.provider != "github_app"
            or bundle.repository != configuration.repository
            or bundle.service != service
        ):
            raise GitHubEvidenceSelectionError(
                "GitHub returned evidence outside the selected connector binding"
            )
        enriched_bundle = GitEvidenceBundle.model_validate(
            {
                **bundle.model_dump(),
                "connector_id": connector_id,
                "connector_version": connector_version,
                "credential_version": credential_version,
                "webhook_receipts": [receipt.model_dump() for receipt in receipts],
            }
        )

        # Reacquire the connector after network I/O and keep this row lock through
        # the investigation's evidence commit. An administrator can revoke or
        # rotate a connector while GitHub is responding; evidence from that stale
        # snapshot must never enter a later connector revision's investigation.
        current = self.session.execute(
            select(ConnectorRecord, ConnectorCredentialRecord)
            .join(
                ConnectorCredentialRecord,
                ConnectorCredentialRecord.connector_id == ConnectorRecord.id,
            )
            .where(
                ConnectorRecord.id == connector_id,
                ConnectorRecord.organization_id == self.organization_id,
                ConnectorRecord.provider == ConnectorProvider.GITHUB.value,
                ConnectorRecord.enabled.is_(True),
                ConnectorRecord.status == ConnectorStatus.CONFIGURED.value,
            )
            .with_for_update()
        ).one_or_none()
        try:
            current_configuration = (
                GithubConfiguration.model_validate(current[0].configuration)
                if current is not None
                else None
            )
        except (TypeError, ValueError):
            current_configuration = None
        if (
            current is None
            or current[0].version != connector_version
            or current[1].credential_version != credential_version
            or current_configuration != configuration
        ):
            self.session.rollback()
            raise GitHubEvidenceSelectionError(
                "The GitHub connector changed during evidence collection"
            )

        return enriched_bundle

    def _load_webhook_receipts(
        self,
        *,
        connector_id: UUID,
        repository: str,
        installation_id: int,
        service: str,
        deployed_at: datetime,
    ) -> list[GitWebhookReceipt]:
        lower_bound = deployed_at - timedelta(hours=self.lookback_hours)
        upper_bound = deployed_at + timedelta(minutes=15)
        records = self.session.scalars(
            select(GithubWebhookDeliveryRecord)
            .where(
                GithubWebhookDeliveryRecord.organization_id == self.organization_id,
                GithubWebhookDeliveryRecord.connector_id == connector_id,
                GithubWebhookDeliveryRecord.repository == repository,
                GithubWebhookDeliveryRecord.installation_id == installation_id,
                GithubWebhookDeliveryRecord.received_at >= lower_bound,
                GithubWebhookDeliveryRecord.received_at <= upper_bound,
                GithubWebhookDeliveryRecord.normalized_payload["service"].as_string()
                == service,
            )
            .order_by(
                GithubWebhookDeliveryRecord.received_at.desc(),
                GithubWebhookDeliveryRecord.id.desc(),
            )
            .limit(self.max_webhook_receipts)
        ).all()
        return [
            GitWebhookReceipt(
                delivery_id=record.delivery_id,
                event_type=record.event_type,
                action=record.action,
                repository=record.repository,
                installation_id=record.installation_id,
                connector_version=record.connector_version,
                credential_version=record.credential_version,
                body_sha256=record.body_sha256,
                received_at=record.received_at,
            )
            for record in records
        ]
