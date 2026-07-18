from app.core.config import Settings
from app.db.session import build_engine_options


def test_postgres_engine_options_bound_connection_pool_and_statements() -> None:
    configuration = Settings(
        _env_file=None,
        DATABASE_URL="postgresql+psycopg://user:secret@localhost/pageragent",
        DATABASE_CONNECT_TIMEOUT_SECONDS=7,
        DATABASE_POOL_TIMEOUT_SECONDS=4,
        DATABASE_STATEMENT_TIMEOUT_MS=2_500,
    )

    assert build_engine_options(configuration) == {
        "pool_pre_ping": True,
        "pool_timeout": 4.0,
        "connect_args": {
            "connect_timeout": 7,
            "options": "-c statement_timeout=2500",
        },
    }


def test_sqlite_engine_options_do_not_receive_postgres_driver_arguments() -> None:
    configuration = Settings(_env_file=None, DATABASE_URL="sqlite+pysqlite:///test.db")

    assert build_engine_options(configuration) == {
        "pool_pre_ping": True,
        "pool_timeout": 5.0,
    }
