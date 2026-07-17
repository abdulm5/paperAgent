from collections.abc import Callable, Mapping

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.constants import DEFAULT_ORGANIZATION_ID
from app.connectors.runtime import (
    PrometheusConnectorUnavailableError,
    load_prometheus_connector_runtime,
)
from app.connectors.vault import (
    AWS_KMS_CIPHER_SCHEME,
    CredentialContext,
    CredentialCustodyUnavailableError,
    SealedCredentials,
)
from app.db.models import (
    ConnectorCredentialRecord,
    ConnectorRecord,
    OrganizationMembershipRecord,
    UserRecord,
)
from app.domain.connectors import (
    ConnectorCreateInput,
    ConnectorCredentialsInput,
    ConnectorPatchInput,
)
from app.services.connectors import (
    ConnectorAuthorityChangedError,
    ConnectorCustodyUnavailableError,
    ConnectorEnablementError,
    ConnectorService,
    ConnectorVersionConflictError,
)
from tests.conftest import TEST_USER_ID, TestingSessionLocal

KMS_KEY_ARN = (
    "arn:aws:kms:us-east-1:123456789012:"
    "key/11111111-2222-3333-4444-555555555555"
)


class TransactionObservingKmsCipher:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.documents: dict[int, dict[str, str]] = {}
        self.observations: list[tuple[str, bool]] = []
        self.on_seal: Callable[[], None] | None = None
        self.on_open: Callable[[], None] | None = None

    def seal(
        self,
        credentials: Mapping[str, str],
        context: CredentialContext,
    ) -> SealedCredentials:
        self.observations.append(("seal", self.session.in_transaction()))
        if self.on_seal is not None:
            self.on_seal()
        self.documents[context.credential_version] = dict(credentials)
        return SealedCredentials(
            ciphertext=b"ciphertext-payload" + bytes([context.credential_version]),
            ciphertext_nonce=b"n" * 12,
            wrapped_data_key=b"k" * 96,
            wrapped_key_nonce=None,
            key_version=KMS_KEY_ARN,
            credential_field_names=tuple(sorted(credentials)),
            cipher_scheme="aws-kms-v1",
        )

    def open(
        self,
        sealed: SealedCredentials,
        context: CredentialContext,
    ) -> dict[str, str]:
        self.observations.append(("open", self.session.in_transaction()))
        assert sealed.cipher_scheme == "aws-kms-v1"
        if self.on_open is not None:
            self.on_open()
        return dict(self.documents[context.credential_version])


class PassingPrometheusProvider:
    def validate(self) -> None:
        return None

    def close(self) -> None:
        return None


def connector_request(token: str = "initial-token") -> ConnectorCreateInput:
    return ConnectorCreateInput.model_validate(
        {
            "name": "KMS metrics",
            "provider": "prometheus",
            "configuration": {
                "service": "checkout-api",
                "base_url": "http://prometheus:9090",
            },
            "credentials": {"bearer_token": token},
        }
    )


def service(session: Session, cipher: TransactionObservingKmsCipher) -> ConnectorService:
    return ConnectorService(
        session,
        DEFAULT_ORGANIZATION_ID,
        cipher=cipher,
        prometheus_provider_factory=lambda _configuration, _credentials: (
            PassingPrometheusProvider()
        ),
        required_cipher_scheme=AWS_KMS_CIPHER_SCHEME,
        required_key_version=KMS_KEY_ARN,
    )


def provision_admin(session: Session) -> None:
    session.add(
        UserRecord(
            id=TEST_USER_ID,
            issuer="urn:pageragent:test",
            subject="kms-admin",
            email="kms-admin@example.test",
            display_name="KMS Admin",
            is_active=True,
        )
    )
    session.add(
        OrganizationMembershipRecord(
            organization_id=DEFAULT_ORGANIZATION_ID,
            user_id=TEST_USER_ID,
            role="admin",
            is_active=True,
        )
    )
    session.commit()


def test_connector_creation_ends_auth_read_transaction_before_kms_and_rechecks_admin(
    db_session: Session,
) -> None:
    provision_admin(db_session)
    assert db_session.scalar(
        select(UserRecord.id).where(UserRecord.id == TEST_USER_ID)
    ) == TEST_USER_ID
    assert db_session.in_transaction() is True
    cipher = TransactionObservingKmsCipher(db_session)

    created = service(db_session, cipher).create_connector(
        connector_request(),
        actor="user:admin",
        actor_user_id=TEST_USER_ID,
    )

    assert created.version == 1
    assert cipher.observations == [("seal", False)]


def test_kms_rotation_discards_envelope_when_admin_loses_authority_during_seal(
    db_session: Session,
) -> None:
    provision_admin(db_session)
    cipher = TransactionObservingKmsCipher(db_session)
    connector_service = service(db_session, cipher)
    created = connector_service.create_connector(
        connector_request(),
        actor="user:admin",
        actor_user_id=TEST_USER_ID,
    )

    def revoke_admin() -> None:
        with TestingSessionLocal() as racing_session:
            membership = racing_session.get(
                OrganizationMembershipRecord,
                (DEFAULT_ORGANIZATION_ID, TEST_USER_ID),
            )
            assert membership is not None
            membership.is_active = False
            membership.version += 1
            racing_session.commit()

    cipher.on_seal = revoke_admin
    with pytest.raises(ConnectorAuthorityChangedError):
        connector_service.put_credentials(
            created.id,
            ConnectorCredentialsInput.model_validate(
                {
                    "expected_version": created.version,
                    "credentials": {"bearer_token": "discarded-token"},
                }
            ),
            actor="user:admin",
            actor_user_id=TEST_USER_ID,
        )

    db_session.expire_all()
    persisted = db_session.get(ConnectorCredentialRecord, created.id)
    assert persisted is not None
    assert persisted.credential_version == 1


def test_kms_envelopes_are_persisted_and_opened_without_database_transactions(
    db_session: Session,
) -> None:
    cipher = TransactionObservingKmsCipher(db_session)
    connector_service = service(db_session, cipher)
    created = connector_service.create_connector(connector_request(), actor="user:admin")

    credential = db_session.get(ConnectorCredentialRecord, created.id)
    assert credential is not None
    assert credential.cipher_scheme == "aws-kms-v1"
    assert credential.wrapped_key_nonce is None
    assert credential.key_version == KMS_KEY_ARN

    rotated = connector_service.put_credentials(
        created.id,
        ConnectorCredentialsInput.model_validate(
            {
                "expected_version": created.version,
                "credentials": {"bearer_token": "rotated-token"},
            }
        ),
        actor="user:admin",
    )
    validated = connector_service.validate_connector(
        created.id,
        rotated.version,
        actor="user:admin",
    )
    enabled = connector_service.patch_connector(
        created.id,
        ConnectorPatchInput.model_validate(
            {"expected_version": validated.version, "enabled": True}
        ),
        actor="user:admin",
    )
    runtime = load_prometheus_connector_runtime(
        db_session,
        created.id,
        cipher=cipher,
    )

    assert enabled.enabled is True
    assert runtime.credentials.bearer_token.get_secret_value() == "rotated-token"
    assert cipher.observations == [
        ("seal", False),
        ("seal", False),
        ("open", False),
        ("open", False),
    ]


def test_kms_rotation_discards_envelope_when_connector_changes_during_seal(
    db_session: Session,
) -> None:
    cipher = TransactionObservingKmsCipher(db_session)
    connector_service = service(db_session, cipher)
    created = connector_service.create_connector(connector_request(), actor="user:admin")

    def race_connector() -> None:
        with TestingSessionLocal() as racing_session:
            record = racing_session.get(ConnectorRecord, created.id)
            assert record is not None
            record.name = "Changed during KMS seal"
            record.version += 1
            racing_session.commit()

    cipher.on_seal = race_connector
    with pytest.raises(ConnectorVersionConflictError) as conflict:
        connector_service.put_credentials(
            created.id,
            ConnectorCredentialsInput.model_validate(
                {
                    "expected_version": created.version,
                    "credentials": {"bearer_token": "discarded-token"},
                }
            ),
            actor="user:admin",
        )

    assert conflict.value.current_version == 2
    persisted = db_session.get(ConnectorCredentialRecord, created.id)
    assert persisted is not None
    assert persisted.credential_version == 1


def test_runtime_discards_decryption_when_connector_is_revoked_during_kms_open(
    db_session: Session,
) -> None:
    cipher = TransactionObservingKmsCipher(db_session)
    connector_service = service(db_session, cipher)
    created = connector_service.create_connector(connector_request(), actor="user:admin")
    validated = connector_service.validate_connector(
        created.id,
        created.version,
        actor="user:admin",
    )
    connector_service.patch_connector(
        created.id,
        ConnectorPatchInput.model_validate(
            {"expected_version": validated.version, "enabled": True}
        ),
        actor="user:admin",
    )

    def revoke_connector() -> None:
        with TestingSessionLocal() as racing_session:
            record = racing_session.scalar(
                select(ConnectorRecord).where(ConnectorRecord.id == created.id)
            )
            assert record is not None
            record.enabled = False
            record.status = "disabled"
            record.version += 1
            racing_session.commit()

    cipher.on_open = revoke_connector
    with pytest.raises(PrometheusConnectorUnavailableError):
        load_prometheus_connector_runtime(db_session, created.id, cipher=cipher)

    assert cipher.observations[-1] == ("open", False)


def test_transient_kms_validation_outage_does_not_invalidate_connector(
    db_session: Session,
) -> None:
    class UnavailableOnOpenCipher(TransactionObservingKmsCipher):
        def open(
            self,
            sealed: SealedCredentials,
            context: CredentialContext,
        ) -> dict[str, str]:
            self.observations.append(("open", self.session.in_transaction()))
            raise CredentialCustodyUnavailableError("temporary KMS outage")

    cipher = UnavailableOnOpenCipher(db_session)
    connector_service = service(db_session, cipher)
    created = connector_service.create_connector(connector_request(), actor="user:admin")

    with pytest.raises(ConnectorCustodyUnavailableError):
        connector_service.validate_connector(
            created.id,
            created.version,
            actor="user:admin",
        )

    db_session.expire_all()
    persisted = db_session.get(ConnectorRecord, created.id)
    assert persisted is not None
    assert persisted.version == created.version
    assert persisted.status == "disabled"
    assert persisted.enabled is False
    assert persisted.last_validation_ok is None
    assert cipher.observations[-1] == ("open", False)


def test_connector_cannot_enable_an_envelope_from_the_previous_custody_provider(
    db_session: Session,
) -> None:
    cipher = TransactionObservingKmsCipher(db_session)
    connector_service = service(db_session, cipher)
    created = connector_service.create_connector(connector_request(), actor="user:admin")
    validated = connector_service.validate_connector(
        created.id,
        created.version,
        actor="user:admin",
    )

    connector_service.required_cipher_scheme = "local-aesgcm-v1"
    with pytest.raises(ConnectorEnablementError, match="active custody provider"):
        connector_service.patch_connector(
            created.id,
            ConnectorPatchInput.model_validate(
                {"expected_version": validated.version, "enabled": True}
            ),
            actor="user:admin",
        )


def test_connector_cannot_reenable_an_envelope_from_a_previous_kms_key_arn(
    db_session: Session,
) -> None:
    cipher = TransactionObservingKmsCipher(db_session)
    connector_service = service(db_session, cipher)
    created = connector_service.create_connector(connector_request(), actor="user:admin")
    validated = connector_service.validate_connector(
        created.id,
        created.version,
        actor="user:admin",
    )

    connector_service.required_key_version = (
        "arn:aws:kms:us-east-1:123456789012:"
        "key/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    )
    with pytest.raises(ConnectorEnablementError, match="active custody provider"):
        connector_service.patch_connector(
            created.id,
            ConnectorPatchInput.model_validate(
                {"expected_version": validated.version, "enabled": True}
            ),
            actor="user:admin",
        )
