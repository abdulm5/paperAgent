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
    CredentialCustodyUnavailableError,
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


class GithubConnectorCustodyUnavailableError(GithubConnectorUnavailableError):
    """GitHub runtime loading failed because credential custody is unavailable."""


class PrometheusConnectorUnavailableError(Exception):
    """The public connector ID cannot resolve to a safe Prometheus runtime."""


class PrometheusConnectorCustodyUnavailableError(PrometheusConnectorUnavailableError):
    """Prometheus runtime loading failed because credential custody is unavailable."""


class SlackConnectorUnavailableError(Exception):
    """The public connector ID cannot resolve to a safe Slack runtime."""


class SlackConnectorCustodyUnavailableError(SlackConnectorUnavailableError):
    """Slack runtime loading failed because credential custody is unavailable."""


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


@dataclass(frozen=True)
class _ConnectorSnapshot:
    connector_id: UUID
    organization_id: UUID
    connector_version: int
    credential_version: int
    provider: ConnectorProvider
    configuration: dict[str, object]
    sealed: SealedCredentials


def load_github_connector_runtime(
    session: Session,
    connector_id: UUID,
    *,
    cipher: CredentialCipher | None = None,
) -> GithubConnectorRuntime:
    """Load one enabled GitHub connector without accepting tenant input from the caller."""

    snapshot = _load_enabled_snapshot(
        session,
        connector_id,
        ConnectorProvider.GITHUB,
    )
    if snapshot is None:
        raise GithubConnectorUnavailableError

    try:
        normalized_configuration = validate_configuration(
            snapshot.provider,
            snapshot.configuration,
        )
        opened_credentials = (cipher or build_credential_cipher()).open(
            snapshot.sealed,
            CredentialContext(
                organization_id=snapshot.organization_id,
                connector_id=snapshot.connector_id,
                provider=snapshot.provider,
                credential_version=snapshot.credential_version,
            ),
        )
        normalized_credentials = validate_credentials(
            snapshot.provider,
            opened_credentials,
        )
        configuration = GithubConfiguration.model_validate(normalized_configuration)
        credentials = GithubCredentials.model_validate(normalized_credentials)
    except CredentialCustodyUnavailableError as error:
        raise GithubConnectorCustodyUnavailableError from error
    except (
        ConnectorContractError,
        CredentialVaultError,
        TypeError,
        ValueError,
        ValidationError,
    ) as error:
        raise GithubConnectorUnavailableError from error
    if not _snapshot_remains_current(session, snapshot):
        raise GithubConnectorUnavailableError

    return GithubConnectorRuntime(
        connector_id=snapshot.connector_id,
        organization_id=snapshot.organization_id,
        connector_version=snapshot.connector_version,
        credential_version=snapshot.credential_version,
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

    snapshot = _load_enabled_snapshot(
        session,
        connector_id,
        ConnectorProvider.PROMETHEUS,
    )
    if snapshot is None:
        raise PrometheusConnectorUnavailableError

    try:
        normalized_configuration = validate_configuration(
            snapshot.provider,
            snapshot.configuration,
        )
        opened_credentials = (cipher or build_credential_cipher()).open(
            snapshot.sealed,
            CredentialContext(
                organization_id=snapshot.organization_id,
                connector_id=snapshot.connector_id,
                provider=snapshot.provider,
                credential_version=snapshot.credential_version,
            ),
        )
        normalized_credentials = validate_credentials(
            snapshot.provider,
            opened_credentials,
        )
        configuration = PrometheusConfiguration.model_validate(normalized_configuration)
        credentials = PrometheusCredentials.model_validate(normalized_credentials)
    except CredentialCustodyUnavailableError as error:
        raise PrometheusConnectorCustodyUnavailableError from error
    except (
        ConnectorContractError,
        CredentialVaultError,
        TypeError,
        ValueError,
        ValidationError,
    ) as error:
        raise PrometheusConnectorUnavailableError from error
    if not _snapshot_remains_current(session, snapshot):
        raise PrometheusConnectorUnavailableError

    return PrometheusConnectorRuntime(
        connector_id=snapshot.connector_id,
        organization_id=snapshot.organization_id,
        connector_version=snapshot.connector_version,
        credential_version=snapshot.credential_version,
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

    snapshot = _load_enabled_snapshot(
        session,
        connector_id,
        ConnectorProvider.SLACK,
    )
    if snapshot is None:
        raise SlackConnectorUnavailableError

    try:
        normalized_configuration = validate_configuration(
            snapshot.provider,
            snapshot.configuration,
        )
        opened_credentials = (cipher or build_credential_cipher()).open(
            snapshot.sealed,
            CredentialContext(
                organization_id=snapshot.organization_id,
                connector_id=snapshot.connector_id,
                provider=snapshot.provider,
                credential_version=snapshot.credential_version,
            ),
        )
        normalized_credentials = validate_credentials(
            snapshot.provider,
            opened_credentials,
        )
        configuration = SlackConfiguration.model_validate(normalized_configuration)
        credentials = SlackCredentials.model_validate(normalized_credentials)
    except CredentialCustodyUnavailableError as error:
        raise SlackConnectorCustodyUnavailableError from error
    except (
        ConnectorContractError,
        CredentialVaultError,
        TypeError,
        ValueError,
        ValidationError,
    ) as error:
        raise SlackConnectorUnavailableError from error
    if not _snapshot_remains_current(session, snapshot):
        raise SlackConnectorUnavailableError

    return SlackConnectorRuntime(
        connector_id=snapshot.connector_id,
        organization_id=snapshot.organization_id,
        connector_version=snapshot.connector_version,
        credential_version=snapshot.credential_version,
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
        cipher_scheme=credential.cipher_scheme,
        credential_field_names=tuple(credential.credential_field_names),
    )


def _load_enabled_snapshot(
    session: Session,
    connector_id: UUID,
    provider: ConnectorProvider,
) -> _ConnectorSnapshot | None:
    record = session.scalar(
        select(ConnectorRecord)
        .where(
            ConnectorRecord.id == connector_id,
            ConnectorRecord.provider == provider.value,
            ConnectorRecord.enabled.is_(True),
            ConnectorRecord.status == ConnectorStatus.CONFIGURED.value,
        )
        .options(selectinload(ConnectorRecord.credential))
    )
    if record is None or record.credential is None:
        session.rollback()
        return None
    credential = record.credential
    snapshot = _ConnectorSnapshot(
        connector_id=record.id,
        organization_id=record.organization_id,
        connector_version=record.version,
        credential_version=credential.credential_version,
        provider=provider,
        configuration=dict(record.configuration),
        sealed=_sealed_credentials(credential),
    )
    # AWS KMS Decrypt must never inherit a database read transaction.
    session.rollback()
    return snapshot


def _snapshot_remains_current(
    session: Session,
    snapshot: _ConnectorSnapshot,
) -> bool:
    record = session.scalar(
        select(ConnectorRecord)
        .where(
            ConnectorRecord.id == snapshot.connector_id,
            ConnectorRecord.organization_id == snapshot.organization_id,
            ConnectorRecord.provider == snapshot.provider.value,
            ConnectorRecord.enabled.is_(True),
            ConnectorRecord.status == ConnectorStatus.CONFIGURED.value,
            ConnectorRecord.version == snapshot.connector_version,
        )
        .options(selectinload(ConnectorRecord.credential))
    )
    current = bool(
        record is not None
        and record.credential is not None
        and record.credential.credential_version == snapshot.credential_version
    )
    # Do not leak the final consistency read into caller-side provider I/O.
    session.rollback()
    return current
