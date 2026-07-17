import json
import os
import re
from collections.abc import Mapping
from functools import lru_cache
from typing import Protocol, cast

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.connectors.vault import (
    AWS_KMS_CIPHER_SCHEME,
    MAX_CREDENTIAL_CIPHERTEXT_BYTES,
    CredentialContext,
    CredentialCustodyUnavailableError,
    CredentialIntegrityError,
    SealedCredentials,
    UnknownCredentialKeyError,
    deserialize_credentials,
    serialize_credentials,
    valid_credential_field_names,
)

_KMS_KEY_ARN = re.compile(
    r"^arn:(?P<partition>aws|aws-us-gov|aws-cn):kms:"
    r"(?P<region>[a-z0-9-]{3,64}):(?P<account>[0-9]{12}):"
    r"key/(?P<key_id>[A-Za-z0-9-]{1,128})$"
)
_SAFE_CONTEXT_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
_AUTHENTICATION_ERROR_CODES = frozenset(
    {
        "IncorrectKeyException",
        "InvalidCiphertextException",
    }
)
_UNKNOWN_KEY_ERROR_CODES = frozenset({"NotFoundException"})


class CredentialKmsError(CredentialCustodyUnavailableError):
    """A sanitized, retryable failure at the external KMS custody boundary."""


class KmsClient(Protocol):
    def generate_data_key(self, **kwargs: object) -> Mapping[str, object]: ...

    def decrypt(self, **kwargs: object) -> Mapping[str, object]: ...


class AwsKmsEnvelopeCipher:
    """AWS KMS data-key envelope encryption for connector credentials.

    The AWS SDK client is injected to make the network boundary explicit and
    testable. Production construction uses the SDK's workload credential chain;
    PagerAgent never accepts static AWS credentials as application settings.
    """

    def __init__(
        self,
        *,
        key_arn: str,
        region: str,
        application: str,
        environment: str,
        client: KmsClient,
    ) -> None:
        match = _KMS_KEY_ARN.fullmatch(key_arn)
        if match is None:
            raise ValueError("Credential KMS key must be an exact key ARN")
        if match.group("region") != region:
            raise ValueError("Credential KMS key ARN and client region must match")
        if _SAFE_CONTEXT_VALUE.fullmatch(application) is None:
            raise ValueError("Credential KMS application identifier is invalid")
        if _SAFE_CONTEXT_VALUE.fullmatch(environment) is None:
            raise ValueError("Credential KMS environment identifier is invalid")
        self._active_key_arn = key_arn
        self._region = region
        self._application = application
        self._environment = environment
        self._client = client

    def seal(
        self,
        credentials: Mapping[str, str],
        context: CredentialContext,
    ) -> SealedCredentials:
        fields, plaintext = serialize_credentials(credentials)
        encryption_context = self._encryption_context(context)
        try:
            response = self._client.generate_data_key(
                KeyId=self._active_key_arn,
                KeySpec="AES_256",
                EncryptionContext=encryption_context,
            )
        except Exception as error:
            if _kms_error_code(error) in _UNKNOWN_KEY_ERROR_CODES:
                raise UnknownCredentialKeyError(
                    "Configured credential KMS key is unavailable"
                ) from None
            raise CredentialKmsError("Credential KMS data-key generation failed") from None

        data_key = response.get("Plaintext") if isinstance(response, Mapping) else None
        wrapped_data_key = (
            response.get("CiphertextBlob") if isinstance(response, Mapping) else None
        )
        returned_key_id = response.get("KeyId") if isinstance(response, Mapping) else None
        if returned_key_id != self._active_key_arn:
            raise UnknownCredentialKeyError("Credential KMS returned an unexpected key ID")
        if not isinstance(data_key, bytes) or len(data_key) != 32:
            raise CredentialKmsError("Credential KMS returned an invalid plaintext data key")
        if (
            not isinstance(wrapped_data_key, bytes)
            or not 1 <= len(wrapped_data_key) <= 6_144
        ):
            raise CredentialKmsError("Credential KMS returned an invalid wrapped data key")

        ciphertext_nonce = os.urandom(12)
        ciphertext = AESGCM(data_key).encrypt(
            ciphertext_nonce,
            plaintext,
            self._payload_aad(encryption_context),
        )
        return SealedCredentials(
            ciphertext=ciphertext,
            ciphertext_nonce=ciphertext_nonce,
            wrapped_data_key=wrapped_data_key,
            wrapped_key_nonce=None,
            key_version=self._active_key_arn,
            credential_field_names=fields,
            cipher_scheme=AWS_KMS_CIPHER_SCHEME,
        )

    def open(
        self,
        sealed: SealedCredentials,
        context: CredentialContext,
    ) -> dict[str, str]:
        if sealed.cipher_scheme != AWS_KMS_CIPHER_SCHEME:
            raise CredentialIntegrityError("Credential cipher scheme is incompatible")
        if sealed.key_version != self._active_key_arn:
            raise UnknownCredentialKeyError("Stored credential KMS key is unavailable")
        if (
            not isinstance(sealed.ciphertext_nonce, bytes)
            or len(sealed.ciphertext_nonce) != 12
            or sealed.wrapped_key_nonce is not None
            or not isinstance(sealed.wrapped_data_key, bytes)
            or not 1 <= len(sealed.wrapped_data_key) <= 6_144
            or not isinstance(sealed.ciphertext, bytes)
            or not 16 <= len(sealed.ciphertext) <= MAX_CREDENTIAL_CIPHERTEXT_BYTES
            or not valid_credential_field_names(sealed.credential_field_names)
        ):
            raise CredentialIntegrityError("Credential KMS envelope metadata is invalid")

        encryption_context = self._encryption_context(context)
        try:
            response = self._client.decrypt(
                KeyId=sealed.key_version,
                CiphertextBlob=sealed.wrapped_data_key,
                EncryptionContext=encryption_context,
                EncryptionAlgorithm="SYMMETRIC_DEFAULT",
            )
        except Exception as error:
            error_code = _kms_error_code(error)
            if error_code in _AUTHENTICATION_ERROR_CODES:
                raise CredentialIntegrityError(
                    "Credential KMS envelope authentication failed"
                ) from None
            if error_code in _UNKNOWN_KEY_ERROR_CODES:
                raise UnknownCredentialKeyError(
                    "Stored credential KMS key is unavailable"
                ) from None
            raise CredentialKmsError("Credential KMS decryption failed") from None

        data_key = response.get("Plaintext") if isinstance(response, Mapping) else None
        returned_key_id = response.get("KeyId") if isinstance(response, Mapping) else None
        returned_algorithm = (
            response.get("EncryptionAlgorithm") if isinstance(response, Mapping) else None
        )
        if returned_key_id != sealed.key_version:
            raise UnknownCredentialKeyError("Credential KMS returned an unexpected key ID")
        if returned_algorithm not in {None, "SYMMETRIC_DEFAULT"}:
            raise CredentialKmsError("Credential KMS returned an invalid algorithm")
        if not isinstance(data_key, bytes) or len(data_key) != 32:
            raise CredentialKmsError("Credential KMS returned an invalid plaintext data key")

        try:
            plaintext = AESGCM(data_key).decrypt(
                sealed.ciphertext_nonce,
                sealed.ciphertext,
                self._payload_aad(encryption_context),
            )
        except (InvalidTag, ValueError):
            raise CredentialIntegrityError(
                "Credential envelope integrity check failed"
            ) from None
        return deserialize_credentials(plaintext, sealed.credential_field_names)

    def _encryption_context(self, context: CredentialContext) -> dict[str, str]:
        return {
            "pageragent:application": self._application,
            "pageragent:environment": self._environment,
            "pageragent:organization-id": str(context.organization_id),
            "pageragent:connector-id": str(context.connector_id),
            "pageragent:provider": context.provider.value,
            "pageragent:credential-version": str(context.credential_version),
        }

    @staticmethod
    def _payload_aad(encryption_context: Mapping[str, str]) -> bytes:
        # This AAD binds the payload to its immutable tenancy context, but is
        # deliberately independent of a KMS key ID so the wrapped key can be
        # re-encrypted during key rotation without opening the credential body.
        encoded_context = json.dumps(
            dict(encryption_context),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        return b"pageragent:connector-credential:aws-kms:v1|" + encoded_context


@lru_cache(maxsize=8)
def create_kms_client(
    *,
    region: str,
    endpoint_url: str | None = None,
    connect_timeout_seconds: float = 3.0,
    read_timeout_seconds: float = 5.0,
    max_attempts: int = 3,
) -> KmsClient:
    """Create a KMS client through the standard AWS workload credential chain."""

    try:
        import boto3
        from botocore.config import Config
    except ImportError:
        raise CredentialKmsError("AWS KMS client dependency is unavailable") from None
    try:
        options: dict[str, object] = {
            "region_name": region,
            "config": Config(
                connect_timeout=connect_timeout_seconds,
                read_timeout=read_timeout_seconds,
                ignore_configured_endpoint_urls=True,
                retries={
                    "mode": "standard",
                    "total_max_attempts": max_attempts,
                },
            ),
        }
        if endpoint_url is not None:
            options["endpoint_url"] = endpoint_url
        return cast(KmsClient, boto3.client("kms", **options))
    except Exception:
        # Credential/profile/region discovery can fail before the first KMS API
        # method call. Keep that boundary typed and provider-detail free too.
        raise CredentialKmsError("AWS KMS client initialization failed") from None


def _kms_error_code(error: Exception) -> str | None:
    response = getattr(error, "response", None)
    if not isinstance(response, Mapping):
        return None
    details = response.get("Error")
    if not isinstance(details, Mapping):
        return None
    code = details.get("Code")
    return code if isinstance(code, str) else None
