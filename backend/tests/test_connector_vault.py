import base64
from dataclasses import replace
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.connectors.contracts import ConnectorContractError, validate_configuration
from app.connectors.vault import (
    AesGcmEnvelopeCipher,
    CredentialContext,
    CredentialIntegrityError,
    UnknownCredentialKeyError,
)
from app.core.config import Settings, settings
from app.domain.connectors import ConnectorProvider

ORGANIZATION_ID = UUID("00000000-0000-0000-0000-000000000001")
CONNECTOR_ID = UUID("00000000-0000-0000-0000-000000000901")
V1_KEY = b"1" * 32
V2_KEY = b"2" * 32


def context(**overrides: object) -> CredentialContext:
    values: dict[str, object] = {
        "organization_id": ORGANIZATION_ID,
        "connector_id": CONNECTOR_ID,
        "provider": ConnectorProvider.GITHUB,
        "credential_version": 1,
    }
    values.update(overrides)
    return CredentialContext(**values)  # type: ignore[arg-type]


def test_envelope_cipher_round_trips_with_random_data_key_and_independent_nonces() -> None:
    cipher = AesGcmEnvelopeCipher(active_key_version="v1", active_key=V1_KEY)
    credentials = {"private_key": "private-secret"}

    first = cipher.seal(credentials, context())
    second = cipher.seal(credentials, context())

    assert first.ciphertext != second.ciphertext
    assert first.wrapped_data_key != second.wrapped_data_key
    assert first.ciphertext_nonce != first.wrapped_key_nonce
    assert second.ciphertext_nonce != second.wrapped_key_nonce
    assert cipher.open(first, context()) == credentials
    assert b"private-secret" not in first.ciphertext
    assert b"private-secret" not in first.wrapped_data_key


@pytest.mark.parametrize(
    "wrong_context",
    [
        context(organization_id=UUID("00000000-0000-0000-0000-000000000002")),
        context(connector_id=UUID("00000000-0000-0000-0000-000000000902")),
        context(provider=ConnectorProvider.SLACK),
        context(credential_version=2),
    ],
)
def test_envelope_cipher_authenticates_full_tenant_connector_context(
    wrong_context: CredentialContext,
) -> None:
    cipher = AesGcmEnvelopeCipher(active_key_version="v1", active_key=V1_KEY)
    sealed = cipher.seal({"private_key": "secret"}, context())

    with pytest.raises(CredentialIntegrityError):
        cipher.open(sealed, wrong_context)


def test_key_rotation_decrypts_by_exact_stored_key_id_and_missing_key_fails_closed() -> None:
    v1_cipher = AesGcmEnvelopeCipher(active_key_version="v1", active_key=V1_KEY)
    sealed_v1 = v1_cipher.seal({"private_key": "old-secret"}, context())
    v2_cipher = AesGcmEnvelopeCipher(
        active_key_version="v2",
        active_key=V2_KEY,
        decryption_keys={"v1": V1_KEY},
    )

    assert v2_cipher.open(sealed_v1, context()) == {"private_key": "old-secret"}
    with pytest.raises(UnknownCredentialKeyError):
        AesGcmEnvelopeCipher(active_key_version="v2", active_key=V2_KEY).open(
            sealed_v1,
            context(),
        )


def test_tampered_key_id_is_authenticated_and_does_not_trigger_trial_decryption() -> None:
    cipher = AesGcmEnvelopeCipher(
        active_key_version="v1",
        active_key=V1_KEY,
        decryption_keys={"v2": V2_KEY},
    )
    sealed = cipher.seal({"private_key": "secret"}, context())

    with pytest.raises(CredentialIntegrityError):
        cipher.open(replace(sealed, key_version="v2"), context())


@pytest.mark.parametrize(
    "credentials",
    [
        {},
        {"private_key": ""},
        {"x" * 101: "secret"},
        {"private_key": "é" * 40_000},
    ],
)
def test_cipher_defensively_rejects_invalid_or_oversized_plaintext(
    credentials: dict[str, str],
) -> None:
    cipher = AesGcmEnvelopeCipher(active_key_version="v1", active_key=V1_KEY)

    with pytest.raises(ValueError, match="custody|vault input bounds|Credential values"):
        cipher.seal(credentials, context())


@pytest.mark.parametrize(
    ("key_id", "key"),
    [
        ("", V1_KEY),
        ("bad|delimiter", V1_KEY),
        ("space key", V1_KEY),
        ("v1", b"short"),
    ],
)
def test_cipher_rejects_invalid_key_identifiers_and_sizes(key_id: str, key: bytes) -> None:
    with pytest.raises(ValueError):
        AesGcmEnvelopeCipher(active_key_version=key_id, active_key=key)


def test_test_key_material_is_canonical_base64() -> None:
    assert base64.b64decode(base64.b64encode(V1_KEY), validate=True) == V1_KEY


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("PAGERAGENT_CONNECTOR_MASTER_KEY", "not-base64"),
        ("PAGERAGENT_CONNECTOR_MASTER_KEY", base64.b64encode(b"short").decode()),
        ("PAGERAGENT_CONNECTOR_KEY_VERSION", "bad|key"),
        ("PAGERAGENT_CONNECTOR_KEY_VERSION", "space key"),
        ("PAGERAGENT_CONNECTOR_ALLOWED_ORIGINS", "https://example.com:99999"),
        ("PAGERAGENT_CONNECTOR_ALLOWED_ORIGINS", "https://example.com/path"),
    ],
)
def test_settings_reject_invalid_connector_key_and_origin_configuration(
    field: str,
    value: str,
) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})


@pytest.mark.parametrize(
    "keyring",
    [
        '{"old-v1":"MTExMTExMTExMTExMTExMTExMTExMTExMTExMTExMTE=",'
        '"old-v1":"MjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjI="}',
        '{"bad|id":"MTExMTExMTExMTExMTExMTExMTExMTExMTExMTExMTE="}',
        '{"old-v1":"not-base64"}',
        "[]",
    ],
)
def test_settings_reject_invalid_or_duplicate_decryption_keyring(keyring: str) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, PAGERAGENT_CONNECTOR_DECRYPTION_KEYS=keyring)


def test_provider_contract_rejects_http_outside_local_even_when_origin_is_listed() -> None:
    production_settings = settings.model_copy(
        update={
            "environment": "production",
            "connector_allowed_origins": "http://prometheus:9090",
        }
    )

    with pytest.raises(ConnectorContractError, match="HTTPS"):
        validate_configuration(
            ConnectorProvider.PROMETHEUS,
            {"service": "checkout-api", "base_url": "http://prometheus:9090"},
            production_settings,
        )
