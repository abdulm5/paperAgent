from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration loaded from environment variables."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    service_name: str = "pageragent-api"
    environment: str = Field(default="local", validation_alias="PAGERAGENT_ENV")
    database_url: str = "postgresql+psycopg://pageragent:pageragent@localhost:5432/pageragent"
    redis_url: str = "redis://localhost:6379/0"
    backend_cors_origins: str = "http://localhost:5173"
    runbook_directory: Path = Path("../runbooks")
    commit_fixture_path: Path = Path("../scenarios/checkout-commits.json")
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


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
