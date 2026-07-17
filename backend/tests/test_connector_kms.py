import os
import sys
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.connectors.kms import (
    AwsKmsEnvelopeCipher,
    CredentialKmsError,
    create_kms_client,
)
from app.connectors.runtime import (
    GithubConnectorCustodyUnavailableError,
    PrometheusConnectorCustodyUnavailableError,
    SlackConnectorCustodyUnavailableError,
    load_github_connector_runtime,
    load_prometheus_connector_runtime,
    load_slack_connector_runtime,
)
from app.connectors.vault import (
    AWS_KMS_CIPHER_SCHEME,
    AesGcmEnvelopeCipher,
    CredentialContext,
    CredentialCustodyUnavailableError,
    CredentialIntegrityError,
    UnknownCredentialKeyError,
    build_credential_cipher,
)
from app.core.config import Settings
from app.domain.connectors import ConnectorProvider

ORGANIZATION_ID = UUID("00000000-0000-0000-0000-000000000001")
CONNECTOR_ID = UUID("00000000-0000-0000-0000-000000000901")
KEY_ARN = "arn:aws:kms:us-east-1:123456789012:key/11111111-2222-3333-4444-555555555555"
OTHER_KEY_ARN = (
    "arn:aws:kms:us-east-1:123456789012:key/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
)


class FakeKmsError(Exception):
    def __init__(self, code: str, message: str = "sensitive-provider-detail") -> None:
        super().__init__(message)
        self.response = {"Error": {"Code": code, "Message": message}}


class FakeKmsClient:
    def __init__(self) -> None:
        self.master_keys = {KEY_ARN: b"k" * 32}
        self.generate_calls: list[dict[str, object]] = []
        self.decrypt_calls: list[dict[str, object]] = []
        self.generate_overrides: dict[str, object] = {}
        self.decrypt_overrides: dict[str, object] = {}
        self.generate_error: Exception | None = None
        self.decrypt_error: Exception | None = None

    def generate_data_key(self, **kwargs: object) -> dict[str, object]:
        self.generate_calls.append(dict(kwargs))
        if self.generate_error is not None:
            raise self.generate_error
        key_id = cast(str, kwargs["KeyId"])
        encryption_context = cast(dict[str, str], kwargs["EncryptionContext"])
        master_key = self.master_keys.get(key_id)
        if master_key is None:
            raise FakeKmsError("NotFoundException")
        data_key = os.urandom(32)
        nonce = os.urandom(12)
        blob = nonce + AESGCM(master_key).encrypt(
            nonce,
            data_key,
            self._aad(key_id, encryption_context),
        )
        response: dict[str, object] = {
            "Plaintext": data_key,
            "CiphertextBlob": blob,
            "KeyId": key_id,
        }
        response.update(self.generate_overrides)
        return response

    def decrypt(self, **kwargs: object) -> dict[str, object]:
        self.decrypt_calls.append(dict(kwargs))
        if self.decrypt_error is not None:
            raise self.decrypt_error
        key_id = cast(str, kwargs["KeyId"])
        encryption_context = cast(dict[str, str], kwargs["EncryptionContext"])
        blob = cast(bytes, kwargs["CiphertextBlob"])
        master_key = self.master_keys.get(key_id)
        if master_key is None:
            raise FakeKmsError("NotFoundException")
        try:
            plaintext = AESGCM(master_key).decrypt(
                blob[:12],
                blob[12:],
                self._aad(key_id, encryption_context),
            )
        except (InvalidTag, ValueError):
            raise FakeKmsError("InvalidCiphertextException") from None
        response: dict[str, object] = {"Plaintext": plaintext, "KeyId": key_id}
        response.update(self.decrypt_overrides)
        return response

    @staticmethod
    def _aad(key_id: str, encryption_context: dict[str, str]) -> bytes:
        values = "|".join(
            f"{key}={value}" for key, value in sorted(encryption_context.items())
        )
        return f"fake-kms|{key_id}|{values}".encode("ascii")


def context(**overrides: object) -> CredentialContext:
    values: dict[str, object] = {
        "organization_id": ORGANIZATION_ID,
        "connector_id": CONNECTOR_ID,
        "provider": ConnectorProvider.GITHUB,
        "credential_version": 1,
    }
    values.update(overrides)
    return CredentialContext(**values)  # type: ignore[arg-type]


def cipher(client: FakeKmsClient) -> AwsKmsEnvelopeCipher:
    return AwsKmsEnvelopeCipher(
        key_arn=KEY_ARN,
        region="us-east-1",
        application="pageragent-api",
        environment="production",
        client=client,
    )


def test_kms_cipher_round_trip_uses_exact_key_context_and_envelope_metadata() -> None:
    client = FakeKmsClient()
    vault = cipher(client)
    credentials = {"private_key": "private-secret", "webhook_secret": "hook-secret"}

    sealed = vault.seal(credentials, context())

    assert sealed.cipher_scheme == AWS_KMS_CIPHER_SCHEME
    assert sealed.key_version == KEY_ARN
    assert sealed.wrapped_key_nonce is None
    assert sealed.credential_field_names == ("private_key", "webhook_secret")
    assert b"private-secret" not in sealed.ciphertext
    assert b"private-secret" not in sealed.wrapped_data_key
    assert client.generate_calls == [
        {
            "KeyId": KEY_ARN,
            "KeySpec": "AES_256",
            "EncryptionContext": {
                "pageragent:application": "pageragent-api",
                "pageragent:environment": "production",
                "pageragent:organization-id": str(ORGANIZATION_ID),
                "pageragent:connector-id": str(CONNECTOR_ID),
                "pageragent:provider": "github",
                "pageragent:credential-version": "1",
            },
        }
    ]

    assert vault.open(sealed, context()) == credentials
    assert client.decrypt_calls[0] == {
        "KeyId": KEY_ARN,
        "CiphertextBlob": sealed.wrapped_data_key,
        "EncryptionContext": client.generate_calls[0]["EncryptionContext"],
        "EncryptionAlgorithm": "SYMMETRIC_DEFAULT",
    }


@pytest.mark.parametrize(
    "wrong_context",
    [
        context(organization_id=UUID("00000000-0000-0000-0000-000000000002")),
        context(connector_id=UUID("00000000-0000-0000-0000-000000000902")),
        context(provider=ConnectorProvider.SLACK),
        context(credential_version=2),
    ],
)
def test_kms_envelope_cannot_be_copied_across_credential_contexts(
    wrong_context: CredentialContext,
) -> None:
    client = FakeKmsClient()
    vault = cipher(client)
    sealed = vault.seal({"private_key": "secret"}, context())

    with pytest.raises(CredentialIntegrityError, match="authentication"):
        vault.open(sealed, wrong_context)


def test_kms_cipher_rejects_tampered_payload_and_wrapped_key() -> None:
    client = FakeKmsClient()
    vault = cipher(client)
    sealed = vault.seal({"private_key": "secret"}, context())

    tampered_payload = sealed.ciphertext[:-1] + bytes([sealed.ciphertext[-1] ^ 1])
    with pytest.raises(CredentialIntegrityError, match="integrity"):
        vault.open(replace(sealed, ciphertext=tampered_payload), context())

    tampered_key = sealed.wrapped_data_key[:-1] + bytes(
        [sealed.wrapped_data_key[-1] ^ 1]
    )
    with pytest.raises(CredentialIntegrityError, match="authentication"):
        vault.open(replace(sealed, wrapped_data_key=tampered_key), context())


def test_kms_cipher_requires_exact_stored_and_returned_key_arns() -> None:
    client = FakeKmsClient()
    vault = cipher(client)
    sealed = vault.seal({"private_key": "secret"}, context())

    with pytest.raises(UnknownCredentialKeyError, match="unavailable"):
        vault.open(replace(sealed, key_version=OTHER_KEY_ARN), context())
    assert client.decrypt_calls == []

    client.decrypt_overrides["KeyId"] = OTHER_KEY_ARN
    with pytest.raises(UnknownCredentialKeyError, match="unexpected key ID"):
        vault.open(sealed, context())


@pytest.mark.parametrize(
    ("overrides", "error_type"),
    [
        ({"Plaintext": b"x" * 31}, CredentialKmsError),
        ({"Plaintext": b"x" * 33}, CredentialKmsError),
        ({"CiphertextBlob": b""}, CredentialKmsError),
        ({"CiphertextBlob": b"x" * 6_145}, CredentialKmsError),
        ({"KeyId": OTHER_KEY_ARN}, UnknownCredentialKeyError),
    ],
)
def test_kms_cipher_strictly_validates_generate_data_key_responses(
    overrides: dict[str, object],
    error_type: type[Exception],
) -> None:
    client = FakeKmsClient()
    client.generate_overrides.update(overrides)

    with pytest.raises(error_type):
        cipher(client).seal({"private_key": "secret"}, context())


@pytest.mark.parametrize(
    ("overrides", "error_type"),
    [
        ({"Plaintext": b"x" * 31}, CredentialKmsError),
        ({"Plaintext": b"x" * 33}, CredentialKmsError),
        ({"KeyId": OTHER_KEY_ARN}, UnknownCredentialKeyError),
        ({"EncryptionAlgorithm": "RSAES_OAEP_SHA_256"}, CredentialKmsError),
    ],
)
def test_kms_cipher_strictly_validates_decrypt_responses(
    overrides: dict[str, object],
    error_type: type[Exception],
) -> None:
    client = FakeKmsClient()
    vault = cipher(client)
    sealed = vault.seal({"private_key": "secret"}, context())
    client.decrypt_overrides.update(overrides)

    with pytest.raises(error_type):
        vault.open(sealed, context())


@pytest.mark.parametrize(
    "changes",
    [
        {"ciphertext_nonce": b"short"},
        {"wrapped_key_nonce": b"unexpected-nonce"},
        {"wrapped_data_key": b""},
        {"wrapped_data_key": b"x" * 6_145},
        {"ciphertext": b"short"},
        {"credential_field_names": ()},
    ],
)
def test_kms_cipher_rejects_invalid_envelope_bounds_before_network_io(
    changes: dict[str, object],
) -> None:
    client = FakeKmsClient()
    vault = cipher(client)
    sealed = vault.seal({"private_key": "secret"}, context())

    with pytest.raises(CredentialIntegrityError, match="metadata"):
        vault.open(replace(sealed, **changes), context())
    assert client.decrypt_calls == []


def test_kms_cipher_sanitizes_provider_errors_and_suppresses_causes() -> None:
    client = FakeKmsClient()
    client.generate_error = RuntimeError("aws_access_key_id=DO_NOT_LEAK")

    with pytest.raises(CredentialKmsError) as generated:
        cipher(client).seal({"private_key": "secret"}, context())
    assert isinstance(generated.value, CredentialCustodyUnavailableError)
    assert "DO_NOT_LEAK" not in str(generated.value)
    assert generated.value.__cause__ is None

    client = FakeKmsClient()
    vault = cipher(client)
    sealed = vault.seal({"private_key": "secret"}, context())
    client.decrypt_error = RuntimeError("provider_response=DO_NOT_LEAK")
    with pytest.raises(CredentialKmsError) as decrypted:
        vault.open(sealed, context())
    assert isinstance(decrypted.value, CredentialCustodyUnavailableError)
    assert "DO_NOT_LEAK" not in str(decrypted.value)
    assert decrypted.value.__cause__ is None


def test_kms_unknown_keys_are_permanent_while_transient_states_are_retryable() -> None:
    missing_client = FakeKmsClient()
    missing_client.generate_error = FakeKmsError("NotFoundException")
    with pytest.raises(UnknownCredentialKeyError):
        cipher(missing_client).seal({"private_key": "secret"}, context())

    client = FakeKmsClient()
    vault = cipher(client)
    sealed = vault.seal({"private_key": "secret"}, context())
    client.decrypt_error = FakeKmsError("DisabledException")
    with pytest.raises(CredentialCustodyUnavailableError):
        vault.open(sealed, context())


@pytest.mark.parametrize(
    ("loader", "provider", "configuration", "expected_error"),
    [
        (
            load_github_connector_runtime,
            ConnectorProvider.GITHUB,
            {
                "service": "checkout-api",
                "repository": "pageragent/checkout",
                "app_id": 1001,
                "installation_id": 2002,
                "api_url": "https://api.github.com",
            },
            GithubConnectorCustodyUnavailableError,
        ),
        (
            load_prometheus_connector_runtime,
            ConnectorProvider.PROMETHEUS,
            {
                "service": "checkout-api",
                "base_url": "http://prometheus:9090",
            },
            PrometheusConnectorCustodyUnavailableError,
        ),
        (
            load_slack_connector_runtime,
            ConnectorProvider.SLACK,
            {
                "service": "checkout-api",
                "channel": "C0123456789",
                "api_url": "https://slack.com",
            },
            SlackConnectorCustodyUnavailableError,
        ),
    ],
)
def test_runtime_loaders_preserve_retryable_custody_failures(
    monkeypatch: pytest.MonkeyPatch,
    loader: Any,
    provider: ConnectorProvider,
    configuration: dict[str, object],
    expected_error: type[Exception],
) -> None:
    snapshot = SimpleNamespace(
        connector_id=CONNECTOR_ID,
        organization_id=ORGANIZATION_ID,
        connector_version=1,
        credential_version=1,
        provider=provider,
        configuration=configuration,
        sealed=object(),
    )

    class UnavailableCipher:
        def open(self, *_args: object, **_kwargs: object) -> dict[str, str]:
            raise CredentialKmsError("sanitized transient outage")

    monkeypatch.setattr(
        "app.connectors.runtime._load_enabled_snapshot",
        lambda *_args, **_kwargs: snapshot,
    )

    with pytest.raises(expected_error) as captured:
        loader(cast(Any, object()), CONNECTOR_ID, cipher=UnavailableCipher())

    assert isinstance(captured.value.__cause__, CredentialKmsError)


def test_build_cipher_retains_local_compatibility() -> None:
    built = build_credential_cipher(Settings(_env_file=None))

    assert isinstance(built, AesGcmEnvelopeCipher)
    sealed = built.seal({"private_key": "secret"}, context())
    assert sealed.cipher_scheme == "local-aesgcm-v1"
    assert built.open(sealed, context()) == {"private_key": "secret"}


def test_kms_client_factory_uses_workload_chain_without_static_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    factory_calls = 0

    class FakeConfig:
        def __init__(
            self,
            *,
            connect_timeout: float,
            read_timeout: float,
            ignore_configured_endpoint_urls: bool,
            retries: dict[str, object],
        ) -> None:
            self.connect_timeout = connect_timeout
            self.read_timeout = read_timeout
            self.ignore_configured_endpoint_urls = ignore_configured_endpoint_urls
            self.retries = retries

    def client_factory(service: str, **kwargs: object) -> object:
        nonlocal factory_calls
        factory_calls += 1
        captured.update({"service": service, **kwargs})
        return object()

    monkeypatch.setitem(sys.modules, "boto3", SimpleNamespace(client=client_factory))
    monkeypatch.setitem(sys.modules, "botocore", SimpleNamespace())
    monkeypatch.setitem(
        sys.modules,
        "botocore.config",
        SimpleNamespace(Config=FakeConfig),
    )

    create_kms_client.cache_clear()
    try:
        created = create_kms_client(
            region="us-east-1",
            endpoint_url="http://localhost:4566",
            connect_timeout_seconds=1.5,
            read_timeout_seconds=4.5,
            max_attempts=4,
        )
        cached = create_kms_client(
            region="us-east-1",
            endpoint_url="http://localhost:4566",
            connect_timeout_seconds=1.5,
            read_timeout_seconds=4.5,
            max_attempts=4,
        )
    finally:
        create_kms_client.cache_clear()

    assert created is not None
    assert cached is created
    assert factory_calls == 1
    client_config = captured.pop("config")
    assert captured == {
        "service": "kms",
        "region_name": "us-east-1",
        "endpoint_url": "http://localhost:4566",
    }
    assert not {"aws_access_key_id", "aws_secret_access_key"} & captured.keys()
    assert client_config.connect_timeout == 1.5
    assert client_config.read_timeout == 4.5
    assert client_config.ignore_configured_endpoint_urls is True
    assert client_config.retries == {
        "mode": "standard",
        "total_max_attempts": 4,
    }


def test_kms_client_factory_sanitizes_workload_credential_initialization_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeConfig:
        def __init__(self, **_options: object) -> None:
            return None

    def failing_client_factory(_service: str, **_options: object) -> object:
        raise RuntimeError("sensitive workload profile path")

    monkeypatch.setitem(
        sys.modules,
        "boto3",
        SimpleNamespace(client=failing_client_factory),
    )
    monkeypatch.setitem(sys.modules, "botocore", SimpleNamespace())
    monkeypatch.setitem(
        sys.modules,
        "botocore.config",
        SimpleNamespace(Config=FakeConfig),
    )
    create_kms_client.cache_clear()
    try:
        with pytest.raises(CredentialKmsError) as failure:
            create_kms_client(region="us-east-1")
    finally:
        create_kms_client.cache_clear()

    assert str(failure.value) == "AWS KMS client initialization failed"
    assert failure.value.__cause__ is None


def test_build_cipher_selects_kms_with_settings_and_injected_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeKmsClient()
    calls: list[dict[str, object]] = []

    def factory(
        *,
        region: str,
        endpoint_url: str | None = None,
        connect_timeout_seconds: float,
        read_timeout_seconds: float,
        max_attempts: int,
    ) -> FakeKmsClient:
        calls.append(
            {
                "region": region,
                "endpoint_url": endpoint_url,
                "connect_timeout_seconds": connect_timeout_seconds,
                "read_timeout_seconds": read_timeout_seconds,
                "max_attempts": max_attempts,
            }
        )
        return client

    monkeypatch.setattr("app.connectors.kms.create_kms_client", factory)
    configuration = Settings(
        _env_file=None,
        PAGERAGENT_CONNECTOR_CIPHER_PROVIDER="aws_kms",
        PAGERAGENT_CONNECTOR_KMS_KEY_ARN=KEY_ARN,
        PAGERAGENT_CONNECTOR_KMS_REGION="us-east-1",
        PAGERAGENT_CONNECTOR_KMS_CONNECT_TIMEOUT_SECONDS=2.5,
        PAGERAGENT_CONNECTOR_KMS_READ_TIMEOUT_SECONDS=6.5,
        PAGERAGENT_CONNECTOR_KMS_MAX_ATTEMPTS=5,
    )

    built = build_credential_cipher(configuration)

    assert isinstance(built, AwsKmsEnvelopeCipher)
    assert calls == [
        {
            "region": "us-east-1",
            "endpoint_url": None,
            "connect_timeout_seconds": 2.5,
            "read_timeout_seconds": 6.5,
            "max_attempts": 5,
        }
    ]
    sealed = built.seal({"private_key": "secret"}, context())
    assert built.open(sealed, context()) == {"private_key": "secret"}


def test_kms_application_context_is_stable_across_api_and_worker_service_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeKmsClient()

    def factory(**_options: object) -> FakeKmsClient:
        return client

    monkeypatch.setattr("app.connectors.kms.create_kms_client", factory)
    common = {
        "PAGERAGENT_CONNECTOR_CIPHER_PROVIDER": "aws_kms",
        "PAGERAGENT_CONNECTOR_KMS_KEY_ARN": KEY_ARN,
        "PAGERAGENT_CONNECTOR_KMS_REGION": "us-east-1",
        "PAGERAGENT_CONNECTOR_KMS_APPLICATION_ID": "pageragent-hosted",
        "PAGERAGENT_ENV": "test",
    }
    api_cipher = build_credential_cipher(
        Settings(_env_file=None, service_name="pageragent-api", **common)
    )
    worker_cipher = build_credential_cipher(
        Settings(_env_file=None, service_name="pageragent-workflow-worker", **common)
    )

    sealed = api_cipher.seal({"private_key": "shared-envelope"}, context())

    assert worker_cipher.open(sealed, context()) == {"private_key": "shared-envelope"}
    assert client.generate_calls[0]["EncryptionContext"] == {
        "pageragent:application": "pageragent-hosted",
        "pageragent:environment": "test",
        "pageragent:organization-id": str(ORGANIZATION_ID),
        "pageragent:connector-id": str(CONNECTOR_ID),
        "pageragent:provider": "github",
        "pageragent:credential-version": "1",
    }


def test_kms_cipher_rejects_aliases_and_region_mismatches() -> None:
    client = FakeKmsClient()
    with pytest.raises(ValueError, match="exact key ARN"):
        AwsKmsEnvelopeCipher(
            key_arn="alias/pageragent",
            region="us-east-1",
            application="pageragent-api",
            environment="production",
            client=client,
        )
    with pytest.raises(ValueError, match="region"):
        AwsKmsEnvelopeCipher(
            key_arn=KEY_ARN,
            region="us-west-2",
            application="pageragent-api",
            environment="production",
            client=client,
        )
    with pytest.raises(ValueError, match="application identifier"):
        AwsKmsEnvelopeCipher(
            key_arn=KEY_ARN,
            region="us-east-1",
            application="pageragent api",
            environment="production",
            client=client,
        )


def test_build_cipher_rejects_custom_kms_endpoint_outside_local_test() -> None:
    configuration = SimpleNamespace(
        connector_cipher_provider="aws_kms",
        connector_kms_key_arn=KEY_ARN,
        connector_kms_region="us-east-1",
        connector_kms_endpoint_url="https://kms.example.test",
        service_name="pageragent-api",
        environment="production",
    )

    with pytest.raises(ValueError, match="local/test"):
        build_credential_cipher(cast(Any, configuration))


@pytest.mark.parametrize(
    "endpoint",
    [
        "file:///tmp/kms",
        "http://user:secret@localhost:4566",
        "http://localhost:4566/kms",
        "http://localhost:4566?key=value",
        "http://localhost:invalid",
    ],
)
def test_settings_rejects_non_origin_kms_endpoints(endpoint: str) -> None:
    with pytest.raises(ValueError, match="KMS_ENDPOINT_URL"):
        Settings(
            _env_file=None,
            PAGERAGENT_CONNECTOR_CIPHER_PROVIDER="aws_kms",
            PAGERAGENT_CONNECTOR_KMS_KEY_ARN=KEY_ARN,
            PAGERAGENT_CONNECTOR_KMS_REGION="us-east-1",
            PAGERAGENT_CONNECTOR_KMS_ENDPOINT_URL=endpoint,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("PAGERAGENT_CONNECTOR_KMS_CONNECT_TIMEOUT_SECONDS", 0),
        ("PAGERAGENT_CONNECTOR_KMS_READ_TIMEOUT_SECONDS", 31),
        ("PAGERAGENT_CONNECTOR_KMS_MAX_ATTEMPTS", 0),
        ("PAGERAGENT_CONNECTOR_KMS_MAX_ATTEMPTS", 11),
    ],
)
def test_settings_rejects_unbounded_kms_client_behavior(
    field: str,
    value: int,
) -> None:
    with pytest.raises(ValueError):
        Settings(_env_file=None, **{field: value})


def test_settings_rejects_invalid_kms_context_identifiers_at_startup() -> None:
    with pytest.raises(ValueError, match="KMS_APPLICATION_ID"):
        Settings(
            _env_file=None,
            PAGERAGENT_CONNECTOR_KMS_APPLICATION_ID="pageragent api",
            PAGERAGENT_CONNECTOR_CIPHER_PROVIDER="aws_kms",
            PAGERAGENT_CONNECTOR_KMS_KEY_ARN=KEY_ARN,
            PAGERAGENT_CONNECTOR_KMS_REGION="us-east-1",
        )
