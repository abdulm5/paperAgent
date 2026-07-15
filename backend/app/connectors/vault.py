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


class CredentialVaultError(Exception):
    """Base class for fail-closed credential custody errors."""


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
    wrapped_key_nonce: bytes
    key_version: str
    credential_field_names: tuple[str, ...]


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
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
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
        )

    def open(
        self,
        sealed: SealedCredentials,
        context: CredentialContext,
    ) -> dict[str, str]:
        master_key = self._keyring.get(sealed.key_version)
        if master_key is None:
            raise UnknownCredentialKeyError("Credential key ID is unavailable")
        if (
            len(sealed.ciphertext_nonce) != 12
            or len(sealed.wrapped_key_nonce) != 12
            or len(sealed.wrapped_data_key) != 48
            or not 16 <= len(sealed.ciphertext) <= 262_144
            or not sealed.credential_field_names
            or len(sealed.credential_field_names) > 16
            or any(
                not isinstance(field, str) or not field or len(field) > 100
                for field in sealed.credential_field_names
            )
            or len(set(sealed.credential_field_names)) != len(sealed.credential_field_names)
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
            decoded = json.loads(plaintext)
        except (InvalidTag, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            raise CredentialIntegrityError("Credential envelope integrity check failed") from error

        if (
            not isinstance(decoded, dict)
            or set(decoded) != set(sealed.credential_field_names)
            or any(
                not isinstance(key, str) or not isinstance(value, str)
                for key, value in decoded.items()
            )
        ):
            raise CredentialIntegrityError("Credential envelope contents are invalid")
        return decoded

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


def build_credential_cipher(configuration: Settings = settings) -> CredentialCipher:
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
