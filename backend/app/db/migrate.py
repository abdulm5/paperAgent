"""Run an idempotent, rollback-aware database migration gate."""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import settings
from app.db.schema import classify_schema_revisions
from app.db.session import build_engine_options


@dataclass(frozen=True)
class SchemaSnapshot:
    revisions: set[str]
    minimum_application_generation: int | None

    @property
    def state(self) -> str:
        return classify_schema_revisions(
            self.revisions,
            self.minimum_application_generation,
        )


def inspect_schema(engine: Engine) -> SchemaSnapshot:
    """Read Alembic state and the explicit compatibility marker independently."""

    try:
        with engine.connect() as connection:
            revisions = set(
                connection.scalars(
                    text("SELECT version_num FROM alembic_version")
                ).all()
            )
    except SQLAlchemyError:
        # A reachable empty database has no Alembic table yet; the upgrade path
        # below owns creating it. Connection failures will also fail that gate.
        revisions = set()

    try:
        with engine.connect() as connection:
            minimum_application_generation = connection.scalar(
                text(
                    "SELECT minimum_application_generation "
                    "FROM pageragent_schema_contract WHERE singleton_id = 1"
                )
            )
    except SQLAlchemyError:
        minimum_application_generation = None

    return SchemaSnapshot(revisions, minimum_application_generation)


def upgrade_to_head() -> None:
    backend_root = Path(__file__).resolve().parents[2]
    configuration = Config(str(backend_root / "alembic.ini"))
    configuration.set_main_option("script_location", str(backend_root / "migrations"))
    command.upgrade(configuration, "head")


def migrate_database(
    engine: Engine,
    upgrade: Callable[[], None] | None = None,
) -> str:
    """Upgrade old schemas, no-op on compatible newer schemas, and fail closed."""

    before = inspect_schema(engine)
    if before.state in {"current", "forward_compatible"}:
        return before.state
    if before.state == "application_incompatible":
        raise RuntimeError(
            "Database schema requires a newer PagerAgent application generation"
        )

    (upgrade or upgrade_to_head)()
    after = inspect_schema(engine)
    if after.state not in {"current", "forward_compatible"}:
        raise RuntimeError("Database did not reach an application-compatible schema")
    return after.state


def main() -> int:
    engine = create_engine(settings.database_url, **build_engine_options(settings))
    try:
        state = migrate_database(engine)
    except Exception as error:  # noqa: BLE001 - CLI boundary must sanitize failures
        print(f"PagerAgent migration gate failed ({type(error).__name__})")
        return 1
    finally:
        engine.dispose()
    print(f"PagerAgent migration gate passed: {state}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
