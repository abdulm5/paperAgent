"""Fail-closed loading of ephemeral connector runtimes."""

from dataclasses import dataclass
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.connectors.contracts import (
    ConnectorContractError,
    validate_configuration,
    validate_credentials,
)
from app.connectors.vault import (
    CredentialCipher,
    CredentialContext,
    CredentialVaultError,
    SealedCredentials,
    build_credential_cipher,
)
from app.db.models import ConnectorCredentialRecord, ConnectorRecord
from app.domain.connectors import (
    ConnectorProvider,
    ConnectorStatus,
    GithubConfiguration,
    GithubCredentials,
    PrometheusConfiguration,
    PrometheusCredentials,
    SlackConfiguration,
    SlackCredentials,
)


class GithubConnectorUnavailableError(Exception):
    """The public connector ID cannot resolve to a safe GitHub runtime."""


class PrometheusConnectorUnavailableError(Exception):
    """The public connector ID cannot resolve to a safe Prometheus runtime."""


class SlackConnectorUnavailableError(Exception):
    """The public connector ID cannot resolve to a safe Slack runtime."""


@dataclass(frozen=True)
class GithubConnectorRuntime:
    connector_id: UUID
    organization_id: UUID
    connector_version: int
    credential_version: int
    configuration: GithubConfiguration
    credentials: GithubCredentials


@dataclass(frozen=True)
class PrometheusConnectorRuntime:
    connector_id: UUID
    organization_id: UUID
    connector_version: int
    credential_version: int
    configuration: PrometheusConfiguration
    credentials: PrometheusCredentials


@dataclass(frozen=True)
class SlackConnectorRuntime:
    connector_id: UUID
    organization_id: UUID
    connector_version: int
    credential_version: int
    configuration: SlackConfiguration
    credentials: SlackCredentials


def load_github_connector_runtime(
    session: Session,
    connector_id: UUID,
    *,
    cipher: CredentialCipher | None = None,
) -> GithubConnectorRuntime:
    """Load one enabled GitHub connector without accepting tenant input from the caller."""

    record = session.scalar(
        select(ConnectorRecord)
        .where(
            ConnectorRecord.id == connector_id,
            ConnectorRecord.provider == ConnectorProvider.GITHUB.value,
            ConnectorRecord.enabled.is_(True),
            ConnectorRecord.status == ConnectorStatus.CONFIGURED.value,
        )
        .options(selectinload(ConnectorRecord.credential))
    )
    if record is None or record.credential is None:
        raise GithubConnectorUnavailableError

    credential = record.credential
    try:
        provider = ConnectorProvider(record.provider)
        normalized_configuration = validate_configuration(
            provider,
            record.configuration,
        )
        sealed = _sealed_credentials(credential)
        opened_credentials = (cipher or build_credential_cipher()).open(
            sealed,
            CredentialContext(
                organization_id=record.organization_id,
                connector_id=record.id,
                provider=provider,
                credential_version=credential.credential_version,
            ),
        )
        normalized_credentials = validate_credentials(provider, opened_credentials)
        configuration = GithubConfiguration.model_validate(normalized_configuration)
        credentials = GithubCredentials.model_validate(normalized_credentials)
    except (
        ConnectorContractError,
        CredentialVaultError,
        TypeError,
        ValueError,
        ValidationError,
    ) as error:
        raise GithubConnectorUnavailableError from error

    return GithubConnectorRuntime(
        connector_id=record.id,
        organization_id=record.organization_id,
        connector_version=record.version,
        credential_version=credential.credential_version,
        configuration=configuration,
        credentials=credentials,
    )


def load_prometheus_connector_runtime(
    session: Session,
    connector_id: UUID,
    *,
    cipher: CredentialCipher | None = None,
) -> PrometheusConnectorRuntime:
    """Load one enabled Prometheus connector at the final decryption boundary."""

    record = session.scalar(
        select(ConnectorRecord)
        .where(
            ConnectorRecord.id == connector_id,
            ConnectorRecord.provider == ConnectorProvider.PROMETHEUS.value,
            ConnectorRecord.enabled.is_(True),
            ConnectorRecord.status == ConnectorStatus.CONFIGURED.value,
        )
        .options(selectinload(ConnectorRecord.credential))
    )
    if record is None or record.credential is None:
        raise PrometheusConnectorUnavailableError

    credential = record.credential
    try:
        provider = ConnectorProvider(record.provider)
        normalized_configuration = validate_configuration(provider, record.configuration)
        opened_credentials = (cipher or build_credential_cipher()).open(
            _sealed_credentials(credential),
            CredentialContext(
                organization_id=record.organization_id,
                connector_id=record.id,
                provider=provider,
                credential_version=credential.credential_version,
            ),
        )
        normalized_credentials = validate_credentials(provider, opened_credentials)
        configuration = PrometheusConfiguration.model_validate(normalized_configuration)
        credentials = PrometheusCredentials.model_validate(normalized_credentials)
    except (
        ConnectorContractError,
        CredentialVaultError,
        TypeError,
        ValueError,
        ValidationError,
    ) as error:
        raise PrometheusConnectorUnavailableError from error

    return PrometheusConnectorRuntime(
        connector_id=record.id,
        organization_id=record.organization_id,
        connector_version=record.version,
        credential_version=credential.credential_version,
        configuration=configuration,
        credentials=credentials,
    )


def load_slack_connector_runtime(
    session: Session,
    connector_id: UUID,
    *,
    cipher: CredentialCipher | None = None,
) -> SlackConnectorRuntime:
    """Load one enabled Slack connector at the final decryption boundary."""

    record = session.scalar(
        select(ConnectorRecord)
        .where(
            ConnectorRecord.id == connector_id,
            ConnectorRecord.provider == ConnectorProvider.SLACK.value,
            ConnectorRecord.enabled.is_(True),
            ConnectorRecord.status == ConnectorStatus.CONFIGURED.value,
        )
        .options(selectinload(ConnectorRecord.credential))
    )
    if record is None or record.credential is None:
        raise SlackConnectorUnavailableError

    credential = record.credential
    try:
        provider = ConnectorProvider(record.provider)
        normalized_configuration = validate_configuration(provider, record.configuration)
        opened_credentials = (cipher or build_credential_cipher()).open(
            _sealed_credentials(credential),
            CredentialContext(
                organization_id=record.organization_id,
                connector_id=record.id,
                provider=provider,
                credential_version=credential.credential_version,
            ),
        )
        normalized_credentials = validate_credentials(provider, opened_credentials)
        configuration = SlackConfiguration.model_validate(normalized_configuration)
        credentials = SlackCredentials.model_validate(normalized_credentials)
    except (
        ConnectorContractError,
        CredentialVaultError,
        TypeError,
        ValueError,
        ValidationError,
    ) as error:
        raise SlackConnectorUnavailableError from error

    return SlackConnectorRuntime(
        connector_id=record.id,
        organization_id=record.organization_id,
        connector_version=record.version,
        credential_version=credential.credential_version,
        configuration=configuration,
        credentials=credentials,
    )


def _sealed_credentials(credential: ConnectorCredentialRecord) -> SealedCredentials:
    return SealedCredentials(
        ciphertext=credential.ciphertext,
        ciphertext_nonce=credential.ciphertext_nonce,
        wrapped_data_key=credential.wrapped_data_key,
        wrapped_key_nonce=credential.wrapped_key_nonce,
        key_version=credential.key_version,
        credential_field_names=tuple(credential.credential_field_names),
    )
