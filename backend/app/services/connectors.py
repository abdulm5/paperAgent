from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.connectors.contracts import (
    ConnectorContractError,
    validate_configuration,
    validate_credentials,
)
from app.connectors.github import GitHubAppEvidenceProvider, GitHubProviderError
from app.connectors.prometheus import PrometheusHttpApiProvider, PrometheusProviderError
from app.connectors.vault import (
    CredentialCipher,
    CredentialContext,
    CredentialVaultError,
    SealedCredentials,
    build_credential_cipher,
)
from app.db.models import (
    ConnectorAuditEventRecord,
    ConnectorCredentialRecord,
    ConnectorRecord,
    OrganizationRecord,
)
from app.domain.connectors import (
    ConnectorAuditEvent,
    ConnectorCreateInput,
    ConnectorCredentialsInput,
    ConnectorPatchInput,
    ConnectorProvider,
    ConnectorStatus,
    ConnectorSummary,
    GithubConfiguration,
    GithubCredentials,
    PrometheusConfiguration,
    PrometheusCredentials,
)
from app.services.github_evidence import build_github_evidence_provider
from app.services.prometheus_evidence import build_prometheus_evidence_provider

GithubProviderFactory = Callable[
    [GithubConfiguration, GithubCredentials], GitHubAppEvidenceProvider
]
PrometheusProviderFactory = Callable[
    [PrometheusConfiguration, PrometheusCredentials], PrometheusHttpApiProvider
]


def build_github_provider(
    configuration: GithubConfiguration,
    credentials: GithubCredentials,
) -> GitHubAppEvidenceProvider:
    """Build the live adapter only at the final decrypted credential boundary."""

    return build_github_evidence_provider(configuration, credentials)


def build_prometheus_provider(
    configuration: PrometheusConfiguration,
    credentials: PrometheusCredentials,
) -> PrometheusHttpApiProvider:
    """Build the metrics adapter only at the decrypted credential boundary."""

    return build_prometheus_evidence_provider(configuration, credentials)


class ConnectorNotFoundError(Exception):
    pass


class ConnectorVersionConflictError(Exception):
    def __init__(self, current_version: int) -> None:
        self.current_version = current_version
        super().__init__(f"Connector changed; current version is {current_version}")


class ConnectorNameConflictError(Exception):
    pass


class ConnectorEnablementError(Exception):
    pass


class ConnectorAuditIntegrityError(Exception):
    pass


_AUDIT_PAYLOAD_FIELDS: dict[str, frozenset[str]] = {
    "connector.created": frozenset(
        {
            "provider",
            "enabled",
            "status",
            "configuration_fields",
            "credential_fields",
            "credential_version",
        }
    ),
    "connector.updated": frozenset(
        {"changed_fields", "enabled", "status", "configuration_fields"}
    ),
    "connector.credentials_updated": frozenset(
        {"credential_fields", "credential_version", "enabled", "status"}
    ),
    "connector.validation_completed": frozenset({"valid", "enabled", "status", "message"}),
}


class ConnectorService:
    def __init__(
        self,
        session: Session,
        organization_id: UUID,
        *,
        cipher: CredentialCipher | None = None,
        github_provider_factory: GithubProviderFactory | None = None,
        prometheus_provider_factory: PrometheusProviderFactory | None = None,
    ) -> None:
        self.session = session
        self.organization_id = organization_id
        self.cipher = cipher or build_credential_cipher()
        self.github_provider_factory = github_provider_factory or build_github_provider
        self.prometheus_provider_factory = (
            prometheus_provider_factory or build_prometheus_provider
        )

    def list_connectors(self) -> list[ConnectorSummary]:
        records = self.session.scalars(
            select(ConnectorRecord)
            .where(ConnectorRecord.organization_id == self.organization_id)
            .options(selectinload(ConnectorRecord.credential))
            .order_by(ConnectorRecord.created_at, ConnectorRecord.id)
        ).all()
        return [self._to_summary(record) for record in records]

    def get_connector(self, connector_id: UUID) -> ConnectorSummary:
        return self._to_summary(self._get_record(connector_id))

    def list_events(self, connector_id: UUID) -> list[ConnectorAuditEvent]:
        self._get_record(connector_id)
        records = self.session.scalars(
            select(ConnectorAuditEventRecord)
            .where(
                ConnectorAuditEventRecord.connector_id == connector_id,
                ConnectorAuditEventRecord.organization_id == self.organization_id,
            )
            .order_by(
                ConnectorAuditEventRecord.connector_version,
                ConnectorAuditEventRecord.id,
            )
        ).all()
        return [self._to_event(record) for record in records]

    def create_connector(
        self,
        request: ConnectorCreateInput,
        *,
        actor: str,
    ) -> ConnectorSummary:
        if not request.name.strip():
            raise ConnectorContractError("Connector name must not be empty")
        configuration = validate_configuration(request.provider, request.configuration)
        credentials = validate_credentials(request.provider, request.credentials)
        connector_id = uuid4()
        credential_version = 1
        try:
            sealed = self.cipher.seal(
                credentials,
                self._credential_context(
                    connector_id,
                    request.provider,
                    credential_version,
                ),
            )
        except ValueError as error:
            raise ConnectorContractError("Connector credentials exceed custody limits") from error
        record = ConnectorRecord(
            id=connector_id,
            organization_id=self.organization_id,
            name=request.name.strip(),
            provider=request.provider.value,
            configuration=configuration,
            enabled=False,
            status=ConnectorStatus.DISABLED.value,
            version=1,
        )
        record.credential = self._credential_record(connector_id, credential_version, sealed)
        self.session.add(record)
        self._append_event(
            record,
            "connector.created",
            actor,
            {
                "provider": request.provider.value,
                "enabled": False,
                "status": ConnectorStatus.DISABLED.value,
                "configuration_fields": sorted(configuration),
                "credential_fields": list(sealed.credential_field_names),
                "credential_version": credential_version,
            },
        )
        self._commit_with_name_conflict()
        return self._to_summary(record)

    def patch_connector(
        self,
        connector_id: UUID,
        request: ConnectorPatchInput,
        *,
        actor: str,
    ) -> ConnectorSummary:
        if request.enabled is True:
            # All enable operations for a tenant take the same lock before any
            # connector row. This prevents two different rows from concurrently
            # observing that a service binding is unowned and both enabling.
            organization_exists = self.session.scalar(
                select(OrganizationRecord.id)
                .where(OrganizationRecord.id == self.organization_id)
                .with_for_update()
            )
            if organization_exists is None:
                raise ConnectorNotFoundError
        record = self._get_record(connector_id, for_update=True)
        self._check_version(record, request.expected_version)
        changed_fields: set[str] = set()

        if request.name is not None and not request.name.strip():
            raise ConnectorContractError("Connector name must not be empty")
        if request.name is not None and request.name.strip() != record.name:
            record.name = request.name.strip()
            changed_fields.add("name")
        if request.configuration is not None:
            record.configuration = validate_configuration(
                ConnectorProvider(record.provider), request.configuration
            )
            record.last_validated_at = None
            record.last_validation_ok = None
            record.last_validation_message = None
            record.enabled = False
            record.status = ConnectorStatus.DISABLED.value
            changed_fields.update({"configuration", "enabled"})
        if request.enabled is not None:
            if request.enabled and record.last_validation_ok is not True:
                raise ConnectorEnablementError(
                    "A connector must pass its current validation before it can be enabled"
                )
            if request.enabled and record.provider == ConnectorProvider.GITHUB.value:
                self._assert_github_enablement(record)
            if request.enabled and record.provider == ConnectorProvider.PROMETHEUS.value:
                self._assert_prometheus_enablement(record)
            record.enabled = request.enabled
            record.status = (
                ConnectorStatus.CONFIGURED.value
                if request.enabled
                else ConnectorStatus.DISABLED.value
            )
            changed_fields.add("enabled")

        if not changed_fields:
            raise ConnectorContractError("The connector patch does not change any values")
        record.version += 1
        record.updated_at = datetime.now(UTC)
        self._append_event(
            record,
            "connector.updated",
            actor,
            {
                "changed_fields": sorted(changed_fields),
                "enabled": record.enabled,
                "status": record.status,
                "configuration_fields": sorted(record.configuration),
            },
        )
        self._commit_with_name_conflict()
        return self._to_summary(record)

    def put_credentials(
        self,
        connector_id: UUID,
        request: ConnectorCredentialsInput,
        *,
        actor: str,
    ) -> ConnectorSummary:
        record = self._get_record(connector_id, for_update=True)
        self._check_version(record, request.expected_version)
        provider = ConnectorProvider(record.provider)
        credentials = validate_credentials(provider, request.credentials)
        current_credential_version = record.credential.credential_version
        credential_version = current_credential_version + 1
        try:
            sealed = self.cipher.seal(
                credentials,
                self._credential_context(connector_id, provider, credential_version),
            )
        except ValueError as error:
            raise ConnectorContractError("Connector credentials exceed custody limits") from error
        credential = record.credential
        credential.credential_version = credential_version
        credential.ciphertext = sealed.ciphertext
        credential.ciphertext_nonce = sealed.ciphertext_nonce
        credential.wrapped_data_key = sealed.wrapped_data_key
        credential.wrapped_key_nonce = sealed.wrapped_key_nonce
        credential.key_version = sealed.key_version
        credential.credential_field_names = list(sealed.credential_field_names)
        credential.updated_at = datetime.now(UTC)

        record.enabled = False
        record.status = ConnectorStatus.DISABLED.value
        record.last_validated_at = None
        record.last_validation_ok = None
        record.last_validation_message = None
        record.version += 1
        record.updated_at = datetime.now(UTC)
        self._append_event(
            record,
            "connector.credentials_updated",
            actor,
            {
                "credential_fields": list(sealed.credential_field_names),
                "credential_version": credential_version,
                "enabled": False,
                "status": ConnectorStatus.DISABLED.value,
            },
        )
        self.session.commit()
        return self._to_summary(record)

    def validate_connector(
        self,
        connector_id: UUID,
        expected_version: int,
        *,
        actor: str,
    ) -> ConnectorSummary:
        # Provider handshakes deliberately run outside a database transaction. A
        # version/credential compare-and-swap below prevents a slow result from
        # validating configuration that an administrator changed concurrently.
        record = self._get_record(connector_id)
        self._check_version(record, expected_version)
        provider = ConnectorProvider(record.provider)
        snapshot_version = record.version
        snapshot_credential_version = record.credential.credential_version
        configuration: dict[str, Any] | None = None
        credentials: dict[str, str] | None = None
        valid = True
        try:
            configuration = validate_configuration(provider, record.configuration)
            sealed = self._sealed_credentials(record.credential)
            credentials = self.cipher.open(
                sealed,
                self._credential_context(
                    connector_id,
                    provider,
                    record.credential.credential_version,
                ),
            )
            credentials = validate_credentials(provider, credentials)
        except (ConnectorContractError, CredentialVaultError):
            valid = False

        # End the read transaction before any external I/O. Snapshot values are
        # plain copies and no ORM object is used again until the locked reload.
        self.session.rollback()

        provider_handshake: ConnectorProvider | None = None
        if (
            valid
            and provider is ConnectorProvider.GITHUB
            and configuration is not None
            and credentials is not None
        ):
            try:
                github_configuration = GithubConfiguration.model_validate(configuration)
                github_credentials = GithubCredentials.model_validate(credentials)
                github_provider = self.github_provider_factory(
                    github_configuration,
                    github_credentials,
                )
                try:
                    github_provider.validate()
                finally:
                    close = getattr(github_provider, "close", None)
                    if callable(close):
                        close()
                provider_handshake = ConnectorProvider.GITHUB
            except GitHubProviderError:
                valid = False
            except Exception:
                # Private-key parser and injected transport details are never
                # reflected through connector receipts or audit payloads.
                valid = False
        elif (
            valid
            and provider is ConnectorProvider.PROMETHEUS
            and configuration is not None
            and credentials is not None
        ):
            try:
                prometheus_configuration = PrometheusConfiguration.model_validate(
                    configuration
                )
                prometheus_credentials = PrometheusCredentials.model_validate(credentials)
                prometheus_provider = self.prometheus_provider_factory(
                    prometheus_configuration,
                    prometheus_credentials,
                )
                try:
                    prometheus_provider.validate()
                finally:
                    close = getattr(prometheus_provider, "close", None)
                    if callable(close):
                        close()
                provider_handshake = ConnectorProvider.PROMETHEUS
            except PrometheusProviderError:
                valid = False
            except Exception:
                # Transport and injected-provider details stay outside the
                # validation receipt and append-only connector audit stream.
                valid = False

        record = self._get_record(connector_id, for_update=True)
        if (
            record.version != snapshot_version
            or record.credential.credential_version != snapshot_credential_version
        ):
            current_version = record.version
            self.session.rollback()
            raise ConnectorVersionConflictError(current_version)

        if valid:
            if provider_handshake is ConnectorProvider.GITHUB:
                message = (
                    "GitHub App installation and repository read handshake passed; "
                    "the connector may now be enabled."
                )
            elif provider_handshake is ConnectorProvider.PROMETHEUS:
                message = (
                    "Prometheus read-only query handshake passed; "
                    "the connector may now be enabled."
                )
            else:
                message = (
                    "Local connector contract and credential vault integrity passed; "
                    "this provider adapter is not active yet."
                )
            record.status = (
                ConnectorStatus.CONFIGURED.value
                if record.enabled
                else ConnectorStatus.DISABLED.value
            )
        else:
            if provider in {ConnectorProvider.GITHUB, ConnectorProvider.PROMETHEUS}:
                message = (
                    f"{provider.value.title()} provider handshake or credential validation "
                    "failed; the connector remains disabled."
                )
            else:
                message = "Local connector contract or credential validation failed."
            record.enabled = False
            record.status = ConnectorStatus.INVALID.value
        record.last_validated_at = datetime.now(UTC)
        record.last_validation_ok = valid
        record.last_validation_message = message
        record.version += 1
        record.updated_at = datetime.now(UTC)
        self._append_event(
            record,
            "connector.validation_completed",
            actor,
            {
                "valid": valid,
                "enabled": record.enabled,
                "status": record.status,
                "message": message,
            },
        )
        self.session.commit()
        return self._to_summary(record)

    def _get_record(self, connector_id: UUID, *, for_update: bool = False) -> ConnectorRecord:
        query = (
            select(ConnectorRecord)
            .where(
                ConnectorRecord.id == connector_id,
                ConnectorRecord.organization_id == self.organization_id,
            )
            .options(selectinload(ConnectorRecord.credential))
        )
        if for_update:
            query = query.with_for_update()
        record = self.session.scalar(query)
        if record is None or record.credential is None:
            raise ConnectorNotFoundError
        return record

    @staticmethod
    def _check_version(record: ConnectorRecord, expected_version: int) -> None:
        if record.version != expected_version:
            raise ConnectorVersionConflictError(record.version)

    def _assert_github_enablement(self, record: ConnectorRecord) -> None:
        configuration = validate_configuration(
            ConnectorProvider.GITHUB,
            record.configuration,
        )
        if set(record.credential.credential_field_names) != {
            "private_key",
            "webhook_secret",
        }:
            raise ConnectorEnablementError(
                "GitHub credentials must be rotated to the Phase 9B contract and revalidated"
            )
        self._assert_unique_service_binding(
            record,
            ConnectorProvider.GITHUB,
            str(configuration["service"]),
        )

    def _assert_prometheus_enablement(self, record: ConnectorRecord) -> None:
        configuration = validate_configuration(
            ConnectorProvider.PROMETHEUS,
            record.configuration,
        )
        if set(record.credential.credential_field_names) != {"bearer_token"}:
            raise ConnectorEnablementError(
                "Prometheus credentials must match the Phase 9C contract and revalidate"
            )
        self._assert_unique_service_binding(
            record,
            ConnectorProvider.PROMETHEUS,
            str(configuration["service"]),
        )

    def _assert_unique_service_binding(
        self,
        record: ConnectorRecord,
        provider: ConnectorProvider,
        service: str,
    ) -> None:
        other_records = self.session.scalars(
            select(ConnectorRecord).where(
                ConnectorRecord.organization_id == self.organization_id,
                ConnectorRecord.provider == provider.value,
                ConnectorRecord.enabled.is_(True),
                ConnectorRecord.id != record.id,
            )
        ).all()
        for other in other_records:
            try:
                other_configuration = validate_configuration(
                    provider,
                    other.configuration,
                )
            except ConnectorContractError:
                continue
            if other_configuration["service"] == service:
                provider_label = (
                    "GitHub" if provider is ConnectorProvider.GITHUB else provider.value.title()
                )
                raise ConnectorEnablementError(
                    f"Another enabled {provider_label} connector already owns "
                    "this service binding"
                )

    def _credential_context(
        self,
        connector_id: UUID,
        provider: ConnectorProvider,
        credential_version: int,
    ) -> CredentialContext:
        return CredentialContext(
            organization_id=self.organization_id,
            connector_id=connector_id,
            provider=provider,
            credential_version=credential_version,
        )

    @staticmethod
    def _credential_record(
        connector_id: UUID,
        credential_version: int,
        sealed: SealedCredentials,
    ) -> ConnectorCredentialRecord:
        return ConnectorCredentialRecord(
            connector_id=connector_id,
            credential_version=credential_version,
            ciphertext=sealed.ciphertext,
            ciphertext_nonce=sealed.ciphertext_nonce,
            wrapped_data_key=sealed.wrapped_data_key,
            wrapped_key_nonce=sealed.wrapped_key_nonce,
            key_version=sealed.key_version,
            credential_field_names=list(sealed.credential_field_names),
        )

    @staticmethod
    def _sealed_credentials(credential: ConnectorCredentialRecord) -> SealedCredentials:
        return SealedCredentials(
            ciphertext=credential.ciphertext,
            ciphertext_nonce=credential.ciphertext_nonce,
            wrapped_data_key=credential.wrapped_data_key,
            wrapped_key_nonce=credential.wrapped_key_nonce,
            key_version=credential.key_version,
            credential_field_names=tuple(credential.credential_field_names),
        )

    def _append_event(
        self,
        record: ConnectorRecord,
        event_type: str,
        actor: str,
        payload: dict[str, Any],
    ) -> None:
        allowed_fields = _AUDIT_PAYLOAD_FIELDS[event_type]
        if set(payload) != set(allowed_fields):
            raise ValueError("Connector audit payload does not match its allowlist")
        if not all(self._is_safe_audit_value(value) for value in payload.values()):
            raise ValueError("Connector audit payload contains an unsafe value")
        self.session.add(
            ConnectorAuditEventRecord(
                organization_id=self.organization_id,
                connector_id=record.id,
                event_type=event_type,
                actor=actor,
                connector_version=record.version,
                payload=payload,
            )
        )

    @classmethod
    def _is_safe_audit_value(cls, value: object) -> bool:
        if value is None or isinstance(value, bool | int):
            return True
        if isinstance(value, str):
            return len(value) <= 500
        if isinstance(value, list):
            return len(value) <= 32 and all(
                isinstance(item, str) and len(item) <= 100 for item in value
            )
        return False

    def _commit_with_name_conflict(self) -> None:
        try:
            self.session.commit()
        except IntegrityError as error:
            self.session.rollback()
            raise ConnectorNameConflictError(
                "A connector with this name already exists in the organization"
            ) from error

    @staticmethod
    def _to_summary(record: ConnectorRecord) -> ConnectorSummary:
        credential = record.credential
        if credential is None:
            raise ConnectorNotFoundError
        return ConnectorSummary(
            id=record.id,
            organization_id=record.organization_id,
            name=record.name,
            provider=ConnectorProvider(record.provider),
            configuration=record.configuration,
            enabled=record.enabled,
            status=ConnectorStatus(record.status),
            version=record.version,
            credentials_configured=True,
            credential_version=credential.credential_version,
            credential_fields=list(credential.credential_field_names),
            last_validated_at=record.last_validated_at,
            last_validation_ok=record.last_validation_ok,
            last_validation_message=record.last_validation_message,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )

    @staticmethod
    def _to_event(record: ConnectorAuditEventRecord) -> ConnectorAuditEvent:
        allowed_fields = _AUDIT_PAYLOAD_FIELDS.get(record.event_type)
        if allowed_fields is None or set(record.payload) != set(allowed_fields):
            raise ConnectorAuditIntegrityError("Connector audit payload schema is invalid")
        if not all(
            ConnectorService._is_safe_audit_value(value)
            for value in record.payload.values()
        ):
            raise ConnectorAuditIntegrityError("Connector audit payload value is invalid")
        return ConnectorAuditEvent(
            id=record.id,
            connector_id=record.connector_id,
            event_type=record.event_type,
            actor=record.actor,
            connector_version=record.connector_version,
            payload=record.payload,
            created_at=record.created_at,
        )
