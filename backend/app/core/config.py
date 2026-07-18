import json
import re
from base64 import b64decode, b64encode
from binascii import Error as Base64DecodeError
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.runtime_urls import (
    is_reserved_public_host,
    validate_production_database_url,
    validate_production_redis_url,
)


class Settings(BaseSettings):
    """Runtime configuration loaded from environment variables."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    service_name: str = "pageragent-api"
    environment: str = Field(default="local", validation_alias="PAGERAGENT_ENV")
    auth_mode: Literal["local", "oidc"] = Field(
        default="local", validation_alias="PAGERAGENT_AUTH_MODE"
    )
    session_secret: SecretStr = Field(
        default=SecretStr("pageragent-local-session-secret-change-me"),
        validation_alias="PAGERAGENT_SESSION_SECRET",
    )
    session_cookie_name: str = Field(
        default="pageragent_session",
        min_length=1,
        max_length=100,
        pattern=r"^[A-Za-z0-9_-]+$",
        validation_alias="PAGERAGENT_SESSION_COOKIE_NAME",
    )
    session_cookie_secure: bool = Field(
        default=False, validation_alias="PAGERAGENT_SESSION_COOKIE_SECURE"
    )
    session_ttl_seconds: int = Field(
        default=28_800, gt=0, validation_alias="PAGERAGENT_SESSION_TTL_SECONDS"
    )
    oidc_issuer: str | None = Field(default=None, validation_alias="PAGERAGENT_OIDC_ISSUER")
    oidc_audience: str | None = Field(default=None, validation_alias="PAGERAGENT_OIDC_AUDIENCE")
    oidc_jwks_url: str | None = Field(default=None, validation_alias="PAGERAGENT_OIDC_JWKS_URL")
    oidc_client_id: str | None = Field(
        default=None, validation_alias="PAGERAGENT_OIDC_CLIENT_ID"
    )
    oidc_client_secret: SecretStr | None = Field(
        default=None, validation_alias="PAGERAGENT_OIDC_CLIENT_SECRET"
    )
    oidc_authorization_url: str | None = Field(
        default=None, validation_alias="PAGERAGENT_OIDC_AUTHORIZATION_URL"
    )
    oidc_token_url: str | None = Field(
        default=None, validation_alias="PAGERAGENT_OIDC_TOKEN_URL"
    )
    oidc_redirect_uri: str | None = Field(
        default=None, validation_alias="PAGERAGENT_OIDC_REDIRECT_URI"
    )
    oidc_frontend_url: str | None = Field(
        default=None, validation_alias="PAGERAGENT_OIDC_FRONTEND_URL"
    )
    oidc_default_organization_slug: str | None = Field(
        default=None,
        validation_alias="PAGERAGENT_OIDC_DEFAULT_ORGANIZATION_SLUG",
    )
    oidc_transaction_key: SecretStr = Field(
        default=SecretStr("MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="),
        validation_alias="PAGERAGENT_OIDC_TRANSACTION_KEY",
    )
    oidc_login_cookie_name: str = Field(
        default="pageragent_oidc_login",
        min_length=1,
        max_length=100,
        pattern=r"^[A-Za-z0-9_-]+$",
        validation_alias="PAGERAGENT_OIDC_LOGIN_COOKIE_NAME",
    )
    oidc_login_ttl_seconds: int = Field(
        default=300,
        ge=60,
        le=900,
        validation_alias="PAGERAGENT_OIDC_LOGIN_TTL_SECONDS",
    )
    oidc_http_timeout_seconds: float = Field(
        default=5.0,
        gt=0,
        le=30,
        validation_alias="PAGERAGENT_OIDC_HTTP_TIMEOUT_SECONDS",
    )
    oidc_max_response_bytes: int = Field(
        default=262_144,
        ge=1_024,
        le=1_048_576,
        validation_alias="PAGERAGENT_OIDC_MAX_RESPONSE_BYTES",
    )
    ingest_api_key: SecretStr = Field(
        default=SecretStr("pageragent-local-ingest-key"),
        validation_alias="PAGERAGENT_INGEST_API_KEY",
    )
    ingest_organization_slug: str = Field(
        default="pageragent-labs",
        validation_alias="PAGERAGENT_INGEST_ORGANIZATION_SLUG",
    )
    connector_cipher_provider: Literal["local", "aws_kms"] = Field(
        default="local",
        validation_alias="PAGERAGENT_CONNECTOR_CIPHER_PROVIDER",
    )
    connector_kms_key_arn: str | None = Field(
        default=None,
        min_length=20,
        max_length=2048,
        validation_alias="PAGERAGENT_CONNECTOR_KMS_KEY_ARN",
    )
    connector_kms_region: str | None = Field(
        default=None,
        min_length=3,
        max_length=64,
        pattern=r"^[a-z0-9-]+$",
        validation_alias="PAGERAGENT_CONNECTOR_KMS_REGION",
    )
    connector_kms_endpoint_url: str | None = Field(
        default=None,
        max_length=500,
        validation_alias="PAGERAGENT_CONNECTOR_KMS_ENDPOINT_URL",
    )
    connector_kms_application_id: str = Field(
        default="pageragent",
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$",
        validation_alias="PAGERAGENT_CONNECTOR_KMS_APPLICATION_ID",
    )
    connector_kms_connect_timeout_seconds: float = Field(
        default=3.0,
        gt=0,
        le=30,
        validation_alias="PAGERAGENT_CONNECTOR_KMS_CONNECT_TIMEOUT_SECONDS",
    )
    connector_kms_read_timeout_seconds: float = Field(
        default=5.0,
        gt=0,
        le=30,
        validation_alias="PAGERAGENT_CONNECTOR_KMS_READ_TIMEOUT_SECONDS",
    )
    connector_kms_max_attempts: int = Field(
        default=3,
        ge=1,
        le=10,
        validation_alias="PAGERAGENT_CONNECTOR_KMS_MAX_ATTEMPTS",
    )
    connector_master_key: SecretStr = Field(
        default=SecretStr("fJIrHdOnYNQ6If5g9sz8nNVTsN2I6uav5FRHX24GQMs="),
        validation_alias="PAGERAGENT_CONNECTOR_MASTER_KEY",
    )
    connector_key_version: str = Field(
        default="local-v1",
        validation_alias="PAGERAGENT_CONNECTOR_KEY_VERSION",
        min_length=1,
        max_length=100,
        pattern=r"^[A-Za-z0-9._-]+$",
    )
    connector_decryption_keys: SecretStr = Field(
        default=SecretStr("{}"),
        validation_alias="PAGERAGENT_CONNECTOR_DECRYPTION_KEYS",
    )
    connector_allowed_origins: str = Field(
        default=(
            "https://api.github.com,https://slack.com,http://prometheus:9090,"
            "http://localhost:9090"
        ),
        validation_alias="PAGERAGENT_CONNECTOR_ALLOWED_ORIGINS",
    )
    github_evidence_mode: Literal["auto", "connector", "fixture"] = Field(
        default="auto",
        validation_alias="GITHUB_EVIDENCE_MODE",
    )
    github_api_version: str = Field(
        default="2026-03-10",
        validation_alias="GITHUB_API_VERSION",
        pattern=r"^\d{4}-\d{2}-\d{2}$",
    )
    github_http_timeout_seconds: float = Field(
        default=5.0,
        gt=0,
        le=30,
        validation_alias="GITHUB_HTTP_TIMEOUT_SECONDS",
    )
    github_max_response_bytes: int = Field(
        default=1_048_576,
        ge=1_024,
        le=4_194_304,
        validation_alias="GITHUB_MAX_RESPONSE_BYTES",
    )
    github_max_api_requests: int = Field(
        default=24,
        ge=6,
        le=50,
        validation_alias="GITHUB_MAX_API_REQUESTS",
    )
    github_max_commits: int = Field(
        default=8,
        ge=1,
        le=20,
        validation_alias="GITHUB_MAX_COMMITS",
    )
    github_max_related_items: int = Field(
        default=10,
        ge=1,
        le=25,
        validation_alias="GITHUB_MAX_RELATED_ITEMS",
    )
    github_evidence_lookback_hours: int = Field(
        default=24,
        ge=1,
        le=168,
        validation_alias="GITHUB_EVIDENCE_LOOKBACK_HOURS",
    )
    prometheus_evidence_mode: Literal["off", "auto", "connector"] = Field(
        default="auto",
        validation_alias="PROMETHEUS_EVIDENCE_MODE",
    )
    prometheus_http_timeout_seconds: float = Field(
        default=6.0,
        gt=0,
        le=30,
        validation_alias="PROMETHEUS_HTTP_TIMEOUT_SECONDS",
    )
    prometheus_max_response_bytes: int = Field(
        default=1_048_576,
        ge=1_024,
        le=4_194_304,
        validation_alias="PROMETHEUS_MAX_RESPONSE_BYTES",
    )
    prometheus_max_series: int = Field(
        default=50,
        ge=1,
        le=100,
        validation_alias="PROMETHEUS_MAX_SERIES",
    )
    prometheus_max_samples: int = Field(
        default=2_000,
        ge=1,
        le=10_000,
        validation_alias="PROMETHEUS_MAX_SAMPLES",
    )
    prometheus_max_labels_per_series: int = Field(
        default=16,
        ge=1,
        le=32,
        validation_alias="PROMETHEUS_MAX_LABELS_PER_SERIES",
    )
    prometheus_max_window_seconds: int = Field(
        default=1_800,
        ge=60,
        le=21_600,
        validation_alias="PROMETHEUS_MAX_WINDOW_SECONDS",
    )
    prometheus_query_step_seconds: int = Field(
        default=15,
        ge=15,
        le=300,
        validation_alias="PROMETHEUS_QUERY_STEP_SECONDS",
    )
    investigation_telemetry_allowed_origins: str = Field(
        default="",
        validation_alias="PAGERAGENT_TELEMETRY_ALLOWED_ORIGINS",
    )
    database_url: str = Field(
        default="postgresql+psycopg://pageragent:pageragent@localhost:5432/pageragent",
        validation_alias="DATABASE_URL",
    )
    database_connect_timeout_seconds: int = Field(
        default=5,
        ge=1,
        le=30,
        validation_alias="DATABASE_CONNECT_TIMEOUT_SECONDS",
    )
    database_pool_timeout_seconds: float = Field(
        default=5.0,
        gt=0,
        le=30,
        validation_alias="DATABASE_POOL_TIMEOUT_SECONDS",
    )
    database_statement_timeout_ms: int = Field(
        default=5_000,
        ge=500,
        le=30_000,
        validation_alias="DATABASE_STATEMENT_TIMEOUT_MS",
    )
    redis_url: str = Field(
        default="redis://localhost:6379/0",
        validation_alias="REDIS_URL",
    )
    workflow_stream_name: str = "pageragent.workflows"
    workflow_consumer_group: str = "pageragent-workers"
    workflow_dead_letter_stream: str = "pageragent.workflows.dlq"
    workflow_max_attempts: int = 5
    workflow_lease_seconds: int = 30
    workflow_reclaim_idle_seconds: int = 35
    workflow_poll_interval_seconds: float = 0.5
    workflow_retry_base_seconds: int = 2
    workflow_delivery_repair_seconds: int = 60
    durable_mitigation_enabled: bool = Field(
        default=False,
        validation_alias="DURABLE_MITIGATION_ENABLED",
    )
    otel_console_exporter: bool = False
    backend_cors_origins: str = "http://localhost:5173"
    backend_trusted_hosts: str = Field(
        default="localhost,127.0.0.1,testserver,backend,pageragent.test",
        validation_alias="PAGERAGENT_TRUSTED_HOSTS",
    )
    runbook_directory: Path = Path("../runbooks")
    commit_fixture_path: Path = Path("../scenarios/checkout-commits.json")
    scenario_directory: Path = Path("../scenarios")
    auto_investigate_incidents: bool = False
    investigation_http_timeout_seconds: float = 5.0
    auto_generate_proposals: bool = True
    synthesis_provider: Literal["auto", "openai", "deterministic"] = "auto"
    openai_api_key: SecretStr | None = None
    openai_model: str = "gpt-5.6-luna"
    openai_base_url: str = "https://api.openai.com/v1"
    synthesis_http_timeout_seconds: float = 30.0
    checkout_control_url: str = "http://localhost:8100"
    recovery_canary_requests: int = 15
    auto_generate_postmortems: bool = False

    @field_validator("environment", mode="before")
    @classmethod
    def normalize_environment(cls, value: object) -> object:
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("backend_trusted_hosts", mode="before")
    @classmethod
    def normalize_trusted_hosts(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        return ",".join(host.strip().lower() for host in value.split(",") if host.strip())

    @model_validator(mode="after")
    def validate_production_identity_boundary(self) -> "Settings":
        """Reject development credentials and incomplete identity config outside dev/test."""
        encoded_connector_key = self.connector_master_key.get_secret_value()
        try:
            decoded_connector_key = b64decode(encoded_connector_key, validate=True)
        except (Base64DecodeError, ValueError) as error:
            raise ValueError(
                "PAGERAGENT_CONNECTOR_MASTER_KEY must be valid base64"
            ) from error
        if len(decoded_connector_key) != 32:
            raise ValueError(
                "PAGERAGENT_CONNECTOR_MASTER_KEY must decode to exactly 32 bytes"
            )
        # Reject alternate padded representations so key identifiers and
        # configuration checks remain deterministic across runtimes.
        if b64encode(decoded_connector_key).decode("ascii") != encoded_connector_key:
            raise ValueError(
                "PAGERAGENT_CONNECTOR_MASTER_KEY must use canonical base64 encoding"
            )
        if not self.connector_key_version.strip():
            raise ValueError("PAGERAGENT_CONNECTOR_KEY_VERSION must not be empty")

        encoded_transaction_key = self.oidc_transaction_key.get_secret_value()
        try:
            decoded_transaction_key = b64decode(encoded_transaction_key, validate=True)
        except (Base64DecodeError, ValueError) as error:
            raise ValueError(
                "PAGERAGENT_OIDC_TRANSACTION_KEY must be valid base64"
            ) from error
        if (
            len(decoded_transaction_key) != 32
            or b64encode(decoded_transaction_key).decode("ascii")
            != encoded_transaction_key
        ):
            raise ValueError(
                "PAGERAGENT_OIDC_TRANSACTION_KEY must be a canonical base64-encoded "
                "32-byte key"
            )

        def reject_duplicate_key_ids(pairs: list[tuple[str, object]]) -> dict[str, object]:
            result: dict[str, object] = {}
            for key_id, value in pairs:
                if key_id in result:
                    raise ValueError(
                        "PAGERAGENT_CONNECTOR_DECRYPTION_KEYS contains a duplicate key ID"
                    )
                result[key_id] = value
            return result

        try:
            decryption_keys = json.loads(
                self.connector_decryption_keys.get_secret_value(),
                object_pairs_hook=reject_duplicate_key_ids,
            )
        except (json.JSONDecodeError, ValueError) as error:
            raise ValueError(
                "PAGERAGENT_CONNECTOR_DECRYPTION_KEYS must be a JSON object"
            ) from error
        if not isinstance(decryption_keys, dict):
            raise ValueError(
                "PAGERAGENT_CONNECTOR_DECRYPTION_KEYS must be a JSON object"
            )
        if self.connector_key_version in decryption_keys:
            raise ValueError(
                "PAGERAGENT_CONNECTOR_DECRYPTION_KEYS must not repeat the active key ID"
            )
        for key_id, encoded_key in decryption_keys.items():
            if not isinstance(key_id, str) or not re.fullmatch(
                r"[A-Za-z0-9._-]{1,100}", key_id
            ):
                raise ValueError("Connector decryption key IDs are invalid")
            if not isinstance(encoded_key, str):
                raise ValueError("Connector decryption keys must be base64 strings")
            try:
                decoded_key = b64decode(encoded_key, validate=True)
            except (Base64DecodeError, ValueError) as error:
                raise ValueError("Connector decryption keys must use valid base64") from error
            if (
                len(decoded_key) != 32
                or b64encode(decoded_key).decode("ascii") != encoded_key
            ):
                raise ValueError(
                    "Connector decryption keys must be canonical base64-encoded 32-byte keys"
                )

        connector_origins = [
            origin.strip().rstrip("/")
            for origin in self.connector_allowed_origins.split(",")
            if origin.strip()
        ]
        if not connector_origins:
            raise ValueError("PAGERAGENT_CONNECTOR_ALLOWED_ORIGINS must not be empty")
        for origin in connector_origins:
            parsed = urlsplit(origin)
            try:
                parsed.port
            except ValueError as error:
                raise ValueError(
                    "PAGERAGENT_CONNECTOR_ALLOWED_ORIGINS contains an invalid port"
                ) from error
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.path not in {"", "/"}
                or parsed.query
                or parsed.fragment
                or origin != f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
            ):
                raise ValueError(
                    "PAGERAGENT_CONNECTOR_ALLOWED_ORIGINS must contain exact HTTP(S) origins"
                )

        minimum_github_budget = (
            9 + self.github_max_commits + min(self.github_max_related_items, 6)
        )
        if self.github_max_api_requests < minimum_github_budget:
            raise ValueError(
                "GITHUB_MAX_API_REQUESTS is too small for the configured bounded "
                "GitHub evidence pages and one authentication refresh"
            )
        if self.prometheus_query_step_seconds > self.prometheus_max_window_seconds:
            raise ValueError(
                "PROMETHEUS_QUERY_STEP_SECONDS must fit inside the maximum query window"
            )
        minimum_prometheus_samples = (
            self.prometheus_max_window_seconds // self.prometheus_query_step_seconds
        ) + 1
        if self.prometheus_max_samples < minimum_prometheus_samples:
            raise ValueError(
                "PROMETHEUS_MAX_SAMPLES must hold at least one complete bounded series"
            )

        kms_key_match = (
            re.fullmatch(
                r"arn:(aws|aws-us-gov|aws-cn):kms:([a-z0-9-]+):"
                r"([0-9]{12}):key/([A-Za-z0-9-]{1,128})",
                self.connector_kms_key_arn or "",
            )
            if self.connector_cipher_provider == "aws_kms"
            else None
        )
        if self.connector_cipher_provider == "aws_kms" and (
            kms_key_match is None
            or not self.connector_kms_region
            or kms_key_match.group(2) != self.connector_kms_region
        ):
            raise ValueError(
                "AWS KMS connector custody requires an exact key ARN and matching region"
            )
        if self.connector_cipher_provider == "aws_kms" and (
            re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}",
                self.connector_kms_application_id,
            )
            is None
            or re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}",
                self.environment,
            )
            is None
        ):
            raise ValueError(
                "AWS KMS encryption context identifiers use unsupported characters"
            )
        if self.connector_kms_endpoint_url is not None:
            parsed_kms_endpoint = urlsplit(self.connector_kms_endpoint_url)
            try:
                parsed_kms_endpoint.port
            except ValueError as error:
                raise ValueError(
                    "PAGERAGENT_CONNECTOR_KMS_ENDPOINT_URL contains an invalid port"
                ) from error
            if (
                parsed_kms_endpoint.scheme not in {"http", "https"}
                or not parsed_kms_endpoint.hostname
                or parsed_kms_endpoint.username is not None
                or parsed_kms_endpoint.password is not None
                or parsed_kms_endpoint.path not in {"", "/"}
                or parsed_kms_endpoint.query
                or parsed_kms_endpoint.fragment
                or self.connector_kms_endpoint_url
                != f"{parsed_kms_endpoint.scheme}://{parsed_kms_endpoint.netloc}".rstrip("/")
            ):
                raise ValueError(
                    "PAGERAGENT_CONNECTOR_KMS_ENDPOINT_URL must be an exact HTTP(S) origin"
                )

        if self.environment.lower() in {"local", "test"}:
            return self

        errors: list[str] = []
        service_role = self.service_name.strip().lower()
        supported_service_roles = {
            "pageragent-api",
            "pageragent-workflow-worker",
            "pageragent-outbox-relay",
            "pageragent-migration",
        }
        if service_role not in supported_service_roles:
            errors.append(
                "SERVICE_NAME must identify a supported production workload role"
            )

        try:
            validate_production_database_url(self.database_url)
        except ValueError as error:
            errors.append(str(error))

        if service_role != "pageragent-migration":
            try:
                validate_production_redis_url(self.redis_url)
            except ValueError as error:
                errors.append(str(error))

        if self.durable_mitigation_enabled:
            errors.append(
                "DURABLE_MITIGATION_ENABLED must remain false until a production action "
                "adapter has durable idempotency or target-state reconciliation"
            )

        # Background roles intentionally do not receive hosted browser/session
        # credentials. Their startup contract ends after validating only the
        # capabilities and stores they actually use.
        if service_role in {"pageragent-migration", "pageragent-outbox-relay"}:
            if self.connector_cipher_provider != "local":
                errors.append(
                    "Non-connector production workloads must explicitly disable KMS "
                    "connector custody"
                )
            if errors:
                raise ValueError("Unsafe production configuration: " + "; ".join(errors))
            return self

        if service_role == "pageragent-workflow-worker":
            if self.connector_cipher_provider != "aws_kms":
                errors.append(
                    "PAGERAGENT_CONNECTOR_CIPHER_PROVIDER must be 'aws_kms'"
                )
            if self.connector_kms_endpoint_url is not None:
                errors.append(
                    "PAGERAGENT_CONNECTOR_KMS_ENDPOINT_URL is forbidden outside local/test"
                )
            if decryption_keys:
                errors.append(
                    "PAGERAGENT_CONNECTOR_DECRYPTION_KEYS must be empty with production "
                    "KMS custody"
                )
            if any(not origin.startswith("https://") for origin in connector_origins):
                errors.append(
                    "PAGERAGENT_CONNECTOR_ALLOWED_ORIGINS must contain only HTTPS origins"
                )
            if any(
                is_reserved_public_host(urlsplit(origin).hostname or "")
                for origin in connector_origins
            ):
                errors.append(
                    "PAGERAGENT_CONNECTOR_ALLOWED_ORIGINS must use non-reserved public hosts"
                )
            if self.github_evidence_mode != "connector":
                errors.append(
                    "GITHUB_EVIDENCE_MODE must be 'connector' outside local/test "
                    "environments"
                )
            elif "https://api.github.com" not in connector_origins:
                errors.append(
                    "PAGERAGENT_CONNECTOR_ALLOWED_ORIGINS must include "
                    "https://api.github.com when GitHub connector evidence is enabled"
                )
            if self.prometheus_evidence_mode != "connector":
                errors.append(
                    "PROMETHEUS_EVIDENCE_MODE must be 'connector' outside local/test "
                    "environments"
                )
            telemetry_origins = [
                origin.strip()
                for origin in self.investigation_telemetry_allowed_origins.split(",")
                if origin.strip()
            ]
            if not telemetry_origins:
                errors.append("PAGERAGENT_TELEMETRY_ALLOWED_ORIGINS is required")
            elif any(not origin.startswith("https://") for origin in telemetry_origins):
                errors.append(
                    "PAGERAGENT_TELEMETRY_ALLOWED_ORIGINS must contain only HTTPS origins"
                )
            elif any(
                is_reserved_public_host(urlsplit(origin).hostname or "")
                for origin in telemetry_origins
            ):
                errors.append(
                    "PAGERAGENT_TELEMETRY_ALLOWED_ORIGINS must use non-reserved public hosts"
                )
            if errors:
                raise ValueError("Unsafe production configuration: " + "; ".join(errors))
            return self

        if self.auth_mode != "oidc":
            errors.append("PAGERAGENT_AUTH_MODE must be 'oidc'")

        oidc_fields = {
            "PAGERAGENT_OIDC_ISSUER": self.oidc_issuer,
            "PAGERAGENT_OIDC_AUDIENCE": self.oidc_audience,
            "PAGERAGENT_OIDC_JWKS_URL": self.oidc_jwks_url,
            "PAGERAGENT_OIDC_CLIENT_ID": self.oidc_client_id,
            "PAGERAGENT_OIDC_CLIENT_SECRET": (
                self.oidc_client_secret.get_secret_value()
                if self.oidc_client_secret is not None
                else None
            ),
            "PAGERAGENT_OIDC_AUTHORIZATION_URL": self.oidc_authorization_url,
            "PAGERAGENT_OIDC_TOKEN_URL": self.oidc_token_url,
            "PAGERAGENT_OIDC_REDIRECT_URI": self.oidc_redirect_uri,
            "PAGERAGENT_OIDC_FRONTEND_URL": self.oidc_frontend_url,
            "PAGERAGENT_OIDC_DEFAULT_ORGANIZATION_SLUG": (
                self.oidc_default_organization_slug
            ),
        }
        missing_oidc = [name for name, value in oidc_fields.items() if not value]
        if missing_oidc:
            errors.append(f"missing required OIDC settings: {', '.join(missing_oidc)}")
        for name in (
            "PAGERAGENT_OIDC_ISSUER",
            "PAGERAGENT_OIDC_JWKS_URL",
            "PAGERAGENT_OIDC_AUTHORIZATION_URL",
            "PAGERAGENT_OIDC_TOKEN_URL",
            "PAGERAGENT_OIDC_REDIRECT_URI",
            "PAGERAGENT_OIDC_FRONTEND_URL",
        ):
            value = oidc_fields[name]
            parsed = urlsplit(value) if value else None
            if value and (
                parsed is None
                or parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
            ):
                errors.append(f"{name} must use HTTPS")
            elif value and parsed is not None and is_reserved_public_host(
                parsed.hostname or ""
            ):
                errors.append(f"{name} must use a non-reserved public host")
        if self.oidc_audience and self.oidc_client_id:
            if self.oidc_audience != self.oidc_client_id:
                errors.append(
                    "PAGERAGENT_OIDC_AUDIENCE must equal PAGERAGENT_OIDC_CLIENT_ID"
                )
        if (
            encoded_transaction_key
            == "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="
        ):
            errors.append(
                "PAGERAGENT_OIDC_TRANSACTION_KEY must be a non-development 32-byte key"
            )

        session_secret = self.session_secret.get_secret_value()
        if (
            len(session_secret) < 32
            or session_secret == "pageragent-local-session-secret-change-me"
            or session_secret == "replace-this-before-running-outside-local-development"
        ):
            errors.append(
                "PAGERAGENT_SESSION_SECRET must be a non-development secret of 32+ characters"
            )

        ingest_key = self.ingest_api_key.get_secret_value()
        if len(ingest_key) < 32 or ingest_key == "pageragent-local-ingest-key":
            errors.append(
                "PAGERAGENT_INGEST_API_KEY must be a non-development key of 32+ characters"
            )
        if ingest_key == session_secret:
            errors.append(
                "PAGERAGENT_SESSION_SECRET and PAGERAGENT_INGEST_API_KEY must be distinct"
            )
        if (
            "ingest_organization_slug" not in self.model_fields_set
            or not self.ingest_organization_slug.strip()
        ):
            errors.append(
                "PAGERAGENT_INGEST_ORGANIZATION_SLUG must be explicitly configured"
            )

        if self.connector_cipher_provider != "aws_kms":
            errors.append(
                "PAGERAGENT_CONNECTOR_CIPHER_PROVIDER must be 'aws_kms'"
            )
        if self.connector_kms_endpoint_url is not None:
            errors.append(
                "PAGERAGENT_CONNECTOR_KMS_ENDPOINT_URL is forbidden outside local/test"
            )
        if decryption_keys:
            errors.append(
                "PAGERAGENT_CONNECTOR_DECRYPTION_KEYS must be empty with production KMS custody"
            )
        if any(not origin.startswith("https://") for origin in connector_origins):
            errors.append(
                "PAGERAGENT_CONNECTOR_ALLOWED_ORIGINS must contain only HTTPS origins"
            )
        if any(
            is_reserved_public_host(urlsplit(origin).hostname or "")
            for origin in connector_origins
        ):
            errors.append(
                "PAGERAGENT_CONNECTOR_ALLOWED_ORIGINS must use non-reserved public hosts"
            )
        if self.github_evidence_mode != "connector":
            errors.append(
                "GITHUB_EVIDENCE_MODE must be 'connector' outside local/test environments"
            )
        elif "https://api.github.com" not in connector_origins:
            errors.append(
                "PAGERAGENT_CONNECTOR_ALLOWED_ORIGINS must include https://api.github.com "
                "when GitHub connector evidence is enabled"
            )
        if self.prometheus_evidence_mode != "connector":
            errors.append(
                "PROMETHEUS_EVIDENCE_MODE must be 'connector' outside local/test environments"
            )

        telemetry_origins = [
            origin.strip()
            for origin in self.investigation_telemetry_allowed_origins.split(",")
            if origin.strip()
        ]
        if not telemetry_origins:
            errors.append("PAGERAGENT_TELEMETRY_ALLOWED_ORIGINS is required")
        elif any(not origin.startswith("https://") for origin in telemetry_origins):
            errors.append(
                "PAGERAGENT_TELEMETRY_ALLOWED_ORIGINS must contain only HTTPS origins"
            )
        elif any(
            is_reserved_public_host(urlsplit(origin).hostname or "")
            for origin in telemetry_origins
        ):
            errors.append(
                "PAGERAGENT_TELEMETRY_ALLOWED_ORIGINS must use non-reserved public hosts"
            )

        if not self.session_cookie_secure:
            errors.append("PAGERAGENT_SESSION_COOKIE_SECURE must be true")
        if not self.session_cookie_name.startswith("__Host-"):
            errors.append(
                "PAGERAGENT_SESSION_COOKIE_NAME must use the __Host- prefix"
            )
        if not self.oidc_login_cookie_name.startswith("__Host-"):
            errors.append(
                "PAGERAGENT_OIDC_LOGIN_COOKIE_NAME must use the __Host- prefix"
            )
        if self.session_cookie_name == self.oidc_login_cookie_name:
            errors.append("Session and OIDC login cookie names must be distinct")
        redirect = urlsplit(self.oidc_redirect_uri or "")
        if redirect.path != "/api/v1/auth/oidc/callback":
            errors.append(
                "PAGERAGENT_OIDC_REDIRECT_URI must use /api/v1/auth/oidc/callback"
            )
        frontend = urlsplit(self.oidc_frontend_url or "")
        redirect_origin = (
            redirect.scheme,
            redirect.hostname,
            redirect.port,
        )
        frontend_origin = (
            frontend.scheme,
            frontend.hostname,
            frontend.port,
        )
        if redirect_origin != frontend_origin:
            errors.append(
                "PAGERAGENT_OIDC_REDIRECT_URI and PAGERAGENT_OIDC_FRONTEND_URL "
                "must share one browser origin"
            )

        trusted_hosts = [
            host.strip() for host in self.backend_trusted_hosts.split(",") if host.strip()
        ]
        if not trusted_hosts or any(
            host == "*"
            or re.fullmatch(
                r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,62})\.)*"
                r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,62})",
                host,
            )
            is None
            for host in trusted_hosts
        ):
            errors.append(
                "PAGERAGENT_TRUSTED_HOSTS must contain exact DNS names or IPv4 addresses"
            )
        elif any(is_reserved_public_host(host) for host in trusted_hosts):
            errors.append(
                "PAGERAGENT_TRUSTED_HOSTS must use non-reserved public hosts"
            )
        elif trusted_hosts != [(frontend.hostname or "").lower()]:
            errors.append(
                "PAGERAGENT_TRUSTED_HOSTS must contain only the exact OIDC frontend hostname"
            )

        origins = [origin.strip() for origin in self.backend_cors_origins.split(",")]
        if "*" in origins:
            errors.append("BACKEND_CORS_ORIGINS cannot contain '*' when credentials are enabled")
        insecure_origins = [origin for origin in origins if not origin.startswith("https://")]
        if insecure_origins:
            errors.append("BACKEND_CORS_ORIGINS must contain only HTTPS origins")
        if any(
            is_reserved_public_host(urlsplit(origin).hostname or "") for origin in origins
        ):
            errors.append("BACKEND_CORS_ORIGINS must use non-reserved public hosts")

        if errors:
            raise ValueError("Unsafe production configuration: " + "; ".join(errors))
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
