from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration loaded from environment variables."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    service_name: str = "pageragent-api"
    environment: str = "local"
    database_url: str = "postgresql+psycopg://pageragent:pageragent@localhost:5432/pageragent"
    redis_url: str = "redis://localhost:6379/0"
    backend_cors_origins: str = "http://localhost:5173"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
