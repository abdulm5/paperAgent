from collections.abc import Iterator
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings, settings


def build_engine_options(configuration: Settings) -> dict[str, Any]:
    """Build bounded database options without touching the network."""

    options: dict[str, Any] = {
        "pool_pre_ping": True,
        "pool_timeout": configuration.database_pool_timeout_seconds,
    }
    if configuration.database_url.startswith("postgresql+psycopg://"):
        options["connect_args"] = {
            "connect_timeout": configuration.database_connect_timeout_seconds,
            "options": (
                f"-c statement_timeout={configuration.database_statement_timeout_ms}"
            ),
        }
    return options


engine = create_engine(settings.database_url, **build_engine_options(settings))
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    with SessionLocal() as session:
        yield session
