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
        default="pageragent_session", validation_alias="PAGERAGENT_SESSION_COOKIE_NAME"
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
    ingest_api_key: SecretStr = Field(
        default=SecretStr("pageragent-local-ingest-key"),
        validation_alias="PAGERAGENT_INGEST_API_KEY",
    )
    ingest_organization_slug: str = Field(
        default="pageragent-labs",
        validation_alias="PAGERAGENT_INGEST_ORGANIZATION_SLUG",
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
    investigation_telemetry_allowed_origins: str = Field(
        default="",
        validation_alias="PAGERAGENT_TELEMETRY_ALLOWED_ORIGINS",
    )
    database_url: str = "postgresql+psycopg://pageragent:pageragent@localhost:5432/pageragent"
    redis_url: str = "redis://localhost:6379/0"
    workflow_stream_name: str = "pageragent.workflows"
    workflow_consumer_group: str = "pageragent-workers"
    workflow_dead_letter_stream: str = "pageragent.workflows.dlq"
    workflow_max_attempts: int = 5
    workflow_lease_seconds: int = 30
    workflow_reclaim_idle_seconds: int = 35
    workflow_poll_interval_seconds: float = 0.5
    workflow_retry_base_seconds: int = 2
    workflow_delivery_repair_seconds: int = 60
    durable_mitigation_enabled: bool = False
    otel_console_exporter: bool = False
    backend_cors_origins: str = "http://localhost:5173"
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

        if self.environment.lower() in {"local", "test"}:
            return self

        errors: list[str] = []
        if self.auth_mode != "oidc":
            errors.append("PAGERAGENT_AUTH_MODE must be 'oidc'")

        oidc_fields = {
            "PAGERAGENT_OIDC_ISSUER": self.oidc_issuer,
            "PAGERAGENT_OIDC_AUDIENCE": self.oidc_audience,
            "PAGERAGENT_OIDC_JWKS_URL": self.oidc_jwks_url,
        }
        missing_oidc = [name for name, value in oidc_fields.items() if not value]
        if missing_oidc:
            errors.append(f"missing required OIDC settings: {', '.join(missing_oidc)}")
        for name in ("PAGERAGENT_OIDC_ISSUER", "PAGERAGENT_OIDC_JWKS_URL"):
            value = oidc_fields[name]
            if value and not value.startswith("https://"):
                errors.append(f"{name} must use HTTPS")

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

        if encoded_connector_key == "fJIrHdOnYNQ6If5g9sz8nNVTsN2I6uav5FRHX24GQMs=":
            errors.append(
                "PAGERAGENT_CONNECTOR_MASTER_KEY must be a non-development 32-byte key"
            )
        if self.connector_key_version == "local-v1":
            errors.append(
                "PAGERAGENT_CONNECTOR_KEY_VERSION must identify the production key"
            )
        if "fJIrHdOnYNQ6If5g9sz8nNVTsN2I6uav5FRHX24GQMs=" in decryption_keys.values():
            errors.append(
                "PAGERAGENT_CONNECTOR_DECRYPTION_KEYS must not contain the development key"
            )
        if any(not origin.startswith("https://") for origin in connector_origins):
            errors.append(
                "PAGERAGENT_CONNECTOR_ALLOWED_ORIGINS must contain only HTTPS origins"
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

        if not self.session_cookie_secure:
            errors.append("PAGERAGENT_SESSION_COOKIE_SECURE must be true")

        origins = [origin.strip() for origin in self.backend_cors_origins.split(",")]
        if "*" in origins:
            errors.append("BACKEND_CORS_ORIGINS cannot contain '*' when credentials are enabled")
        insecure_origins = [origin for origin in origins if not origin.startswith("https://")]
        if insecure_origins:
            errors.append("BACKEND_CORS_ORIGINS must contain only HTTPS origins")

        if errors:
            raise ValueError("Unsafe production configuration: " + "; ".join(errors))
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
