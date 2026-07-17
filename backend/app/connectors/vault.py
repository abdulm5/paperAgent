import base64
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import Settings, settings
from app.domain.connectors import ConnectorProvider

LOCAL_CIPHER_SCHEME = "local-aesgcm-v1"
AWS_KMS_CIPHER_SCHEME = "aws-kms-v1"
MAX_CREDENTIAL_CIPHERTEXT_BYTES = 262_144
MAX_CREDENTIAL_PLAINTEXT_BYTES = MAX_CREDENTIAL_CIPHERTEXT_BYTES - 16


class CredentialVaultError(Exception):
    """Base class for fail-closed credential custody errors."""


class CredentialCustodyUnavailableError(CredentialVaultError):
    """A retryable outage at an external credential-custody boundary."""


class UnknownCredentialKeyError(CredentialVaultError):
    pass


class CredentialIntegrityError(CredentialVaultError):
    pass


@dataclass(frozen=True)
class CredentialContext:
    organization_id: UUID
    connector_id: UUID
    provider: ConnectorProvider
    credential_version: int


@dataclass(frozen=True)
class SealedCredentials:
    ciphertext: bytes
    ciphertext_nonce: bytes
    wrapped_data_key: bytes
    wrapped_key_nonce: bytes | None
    key_version: str
    credential_field_names: tuple[str, ...]
    cipher_scheme: str = LOCAL_CIPHER_SCHEME


class CredentialCipher(Protocol):
    """Envelope cipher boundary suitable for replacement by a KMS-backed adapter."""

    def seal(
        self,
        credentials: Mapping[str, str],
        context: CredentialContext,
    ) -> SealedCredentials: ...

    def open(
        self,
        sealed: SealedCredentials,
        context: CredentialContext,
    ) -> dict[str, str]: ...


class AesGcmEnvelopeCipher:
    """AES-GCM envelope encryption with an independently random data key per write."""

    def __init__(
        self,
        *,
        active_key_version: str,
        active_key: bytes,
        decryption_keys: Mapping[str, bytes] | None = None,
    ) -> None:
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", active_key_version):
            raise ValueError("Active credential key version is invalid")
        if len(active_key) != 32:
            raise ValueError("Credential master keys must contain exactly 32 bytes")
        keyring = dict(decryption_keys or {})
        if active_key_version in keyring:
            raise ValueError("The active credential key ID must not be duplicated")
        if any(
            not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", key_id) or len(key) != 32
            for key_id, key in keyring.items()
        ):
            raise ValueError("Every credential decryption key must be a named 32-byte key")
        keyring[active_key_version] = active_key
        self._active_key_version = active_key_version
        self._keyring = keyring

    def seal(
        self,
        credentials: Mapping[str, str],
        context: CredentialContext,
    ) -> SealedCredentials:
        fields, plaintext = serialize_credentials(credentials)
        data_key = AESGCM.generate_key(bit_length=256)
        ciphertext_nonce = os.urandom(12)
        wrapped_key_nonce = os.urandom(12)
        while wrapped_key_nonce == ciphertext_nonce:
            wrapped_key_nonce = os.urandom(12)

        base_aad = self._associated_data(context, self._active_key_version)
        ciphertext = AESGCM(data_key).encrypt(
            ciphertext_nonce,
            plaintext,
            base_aad + b"|payload",
        )
        wrapped_data_key = AESGCM(self._keyring[self._active_key_version]).encrypt(
            wrapped_key_nonce,
            data_key,
            base_aad + b"|data-key",
        )
        return SealedCredentials(
            ciphertext=ciphertext,
            ciphertext_nonce=ciphertext_nonce,
            wrapped_data_key=wrapped_data_key,
            wrapped_key_nonce=wrapped_key_nonce,
            key_version=self._active_key_version,
            credential_field_names=fields,
            cipher_scheme=LOCAL_CIPHER_SCHEME,
        )

    def open(
        self,
        sealed: SealedCredentials,
        context: CredentialContext,
    ) -> dict[str, str]:
        if sealed.cipher_scheme != LOCAL_CIPHER_SCHEME:
            raise CredentialIntegrityError("Credential cipher scheme is incompatible")
        master_key = self._keyring.get(sealed.key_version)
        if master_key is None:
            raise UnknownCredentialKeyError("Credential key ID is unavailable")
        if (
            not isinstance(sealed.ciphertext_nonce, bytes)
            or len(sealed.ciphertext_nonce) != 12
            or not isinstance(sealed.wrapped_key_nonce, bytes)
            or len(sealed.wrapped_key_nonce) != 12
            or not isinstance(sealed.wrapped_data_key, bytes)
            or len(sealed.wrapped_data_key) != 48
            or not isinstance(sealed.ciphertext, bytes)
            or not 16 <= len(sealed.ciphertext) <= MAX_CREDENTIAL_CIPHERTEXT_BYTES
            or not valid_credential_field_names(sealed.credential_field_names)
        ):
            raise CredentialIntegrityError("Credential nonce metadata is invalid")

        base_aad = self._associated_data(context, sealed.key_version)
        try:
            data_key = AESGCM(master_key).decrypt(
                sealed.wrapped_key_nonce,
                sealed.wrapped_data_key,
                base_aad + b"|data-key",
            )
            plaintext = AESGCM(data_key).decrypt(
                sealed.ciphertext_nonce,
                sealed.ciphertext,
                base_aad + b"|payload",
            )
        except (InvalidTag, ValueError) as error:
            raise CredentialIntegrityError("Credential envelope integrity check failed") from error
        return deserialize_credentials(plaintext, sealed.credential_field_names)

    @staticmethod
    def _associated_data(context: CredentialContext, key_version: str) -> bytes:
        return (
            "pageragent:connector-credential:v1"
            f"|organization={context.organization_id}"
            f"|connector={context.connector_id}"
            f"|provider={context.provider.value}"
            f"|credential-version={context.credential_version}"
            f"|key-version={key_version}"
        ).encode("ascii")


def serialize_credentials(
    credentials: Mapping[str, str],
) -> tuple[tuple[str, ...], bytes]:
    """Validate and deterministically serialize one bounded credential document."""

    if (
        not credentials
        or len(credentials) > 16
        or any(
            not isinstance(key, str)
            or not key
            or len(key) > 100
            or not isinstance(value, str)
            or not value
            or len(value.encode("utf-8")) > 65_536
            for key, value in credentials.items()
        )
        or sum(len(value.encode("utf-8")) for value in credentials.values()) > 131_072
    ):
        raise ValueError("Credential values do not satisfy vault input bounds")
    fields = tuple(sorted(credentials))
    plaintext = json.dumps(
        dict(credentials),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(plaintext) > MAX_CREDENTIAL_PLAINTEXT_BYTES:
        raise ValueError("Serialized credentials exceed the vault ciphertext bound")
    return fields, plaintext


def valid_credential_field_names(fields: tuple[str, ...]) -> bool:
    return bool(fields) and len(fields) <= 16 and all(
        isinstance(field, str) and bool(field) and len(field) <= 100
        for field in fields
    ) and len(set(fields)) == len(fields)


def deserialize_credentials(
    plaintext: bytes,
    fields: tuple[str, ...],
) -> dict[str, str]:
    try:
        decoded = json.loads(plaintext)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise CredentialIntegrityError("Credential envelope contents are invalid") from error
    if (
        not isinstance(decoded, dict)
        or set(decoded) != set(fields)
        or any(
            not isinstance(key, str) or not isinstance(value, str) or not value
            for key, value in decoded.items()
        )
    ):
        raise CredentialIntegrityError("Credential envelope contents are invalid")
    return decoded


def build_credential_cipher(configuration: Settings = settings) -> CredentialCipher:
    provider = getattr(configuration, "connector_cipher_provider", "local")
    if provider == "aws_kms":
        from app.connectors.kms import AwsKmsEnvelopeCipher, create_kms_client

        key_arn = getattr(configuration, "connector_kms_key_arn", None)
        region = getattr(configuration, "connector_kms_region", None)
        endpoint_url = getattr(configuration, "connector_kms_endpoint_url", None)
        if not isinstance(key_arn, str) or not key_arn:
            raise ValueError("An AWS KMS key ARN is required for credential custody")
        if not isinstance(region, str) or not region:
            raise ValueError("An AWS KMS region is required for credential custody")
        if endpoint_url is not None:
            endpoint_url = str(endpoint_url)
            if configuration.environment not in {"local", "test"}:
                raise ValueError("Custom KMS endpoints are restricted to local/test")
        client = create_kms_client(
            region=region,
            endpoint_url=endpoint_url,
            connect_timeout_seconds=getattr(
                configuration,
                "connector_kms_connect_timeout_seconds",
                3.0,
            ),
            read_timeout_seconds=getattr(
                configuration,
                "connector_kms_read_timeout_seconds",
                5.0,
            ),
            max_attempts=getattr(configuration, "connector_kms_max_attempts", 3),
        )
        return AwsKmsEnvelopeCipher(
            key_arn=key_arn,
            region=region,
            # This identifier must remain identical across the API and every
            # worker that opens the same envelope. SERVICE_NAME is intentionally
            # process-specific telemetry metadata and cannot be used here.
            application=getattr(
                configuration,
                "connector_kms_application_id",
                "pageragent",
            ),
            environment=configuration.environment,
            client=client,
        )
    if provider != "local":
        raise ValueError("Credential cipher provider is unsupported")
    active_key = base64.b64decode(
        configuration.connector_master_key.get_secret_value(),
        validate=True,
    )
    encoded_old_keys = json.loads(
        configuration.connector_decryption_keys.get_secret_value()
    )
    old_keys = {
        key_id: base64.b64decode(encoded, validate=True)
        for key_id, encoded in encoded_old_keys.items()
    }
    return AesGcmEnvelopeCipher(
        active_key_version=configuration.connector_key_version,
        active_key=active_key,
        decryption_keys=old_keys,
    )
