from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, Table, create_engine, inspect

from app.auth.constants import DEFAULT_ORGANIZATION_ID
from app.core.config import settings


def _migration_config() -> Config:
    backend_root = Path(__file__).parents[1]
    config = Config(str(backend_root / "alembic.ini"))
    config.set_main_option("script_location", str(backend_root / "migrations"))
    return config


def _revision(engine) -> str:
    with engine.connect() as connection:
        return connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one()


def _insert_local_credential(engine) -> tuple[Table, str]:
    metadata = MetaData()
    connectors = Table("connectors", metadata, autoload_with=engine)
    credentials = Table("connector_credentials", metadata, autoload_with=engine)
    connector_id = uuid4().hex
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            connectors.insert().values(
                id=connector_id,
                organization_id=DEFAULT_ORGANIZATION_ID.hex,
                name=f"Migration connector {connector_id[:8]}",
                provider="slack",
                configuration={
                    "service": "checkout-api",
                    "channel": "C0123456789",
                    "api_url": "https://slack.com",
                },
                enabled=False,
                status="disabled",
                version=1,
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            credentials.insert().values(
                connector_id=connector_id,
                credential_version=1,
                ciphertext=b"c" * 32,
                ciphertext_nonce=b"n" * 12,
                wrapped_data_key=b"w" * 48,
                wrapped_key_nonce=b"k" * 12,
                key_version="local-v1",
                credential_field_names=["bot_token"],
                created_at=now,
                updated_at=now,
            )
        )
    return credentials, connector_id


def test_hosted_identity_and_kms_migration_round_trips_empty_sqlite(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'phase9e-empty.db'}"
    previous_url = settings.database_url
    engine = None
    try:
        settings.database_url = database_url
        config = _migration_config()
        command.upgrade(config, "head")
        engine = create_engine(database_url)
        inspector = inspect(engine)
        assert {
            "auth_sessions",
            "oidc_login_transactions",
            "identity_audit_events",
        } <= set(inspector.get_table_names())
        assert "ix_oidc_login_transactions_org_pending" in {
            index["name"]
            for index in inspector.get_indexes("oidc_login_transactions")
        }
        assert "ix_auth_sessions_expires" in {
            index["name"] for index in inspector.get_indexes("auth_sessions")
        }
        assert "version" in {
            column["name"]
            for column in inspector.get_columns("organization_memberships")
        }
        credential_columns = {
            column["name"]: column
            for column in inspector.get_columns("connector_credentials")
        }
        assert credential_columns["cipher_scheme"]["nullable"] is False
        assert credential_columns["wrapped_key_nonce"]["nullable"] is True
        assert _revision(engine) == "20260718_0013"
        contract = Table(
            "pageragent_schema_contract",
            MetaData(),
            autoload_with=engine,
        )
        with engine.connect() as connection:
            assert connection.execute(contract.select()).one().minimum_application_generation == 12

        command.downgrade(config, "20260717_0011")
        inspector = inspect(engine)
        assert "auth_sessions" not in inspector.get_table_names()
        assert "cipher_scheme" not in {
            column["name"]
            for column in inspector.get_columns("connector_credentials")
        }
        assert "version" not in {
            column["name"]
            for column in inspector.get_columns("organization_memberships")
        }
        command.upgrade(config, "head")
        assert _revision(engine) == "20260718_0013"
    finally:
        settings.database_url = previous_url
        if engine is not None:
            engine.dispose()


def test_phase9e_backfills_local_cipher_scheme_without_touching_envelopes(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'phase9e-local.db'}"
    previous_url = settings.database_url
    engine = None
    try:
        settings.database_url = database_url
        config = _migration_config()
        command.upgrade(config, "20260717_0011")
        engine = create_engine(database_url)
        _credentials, connector_id = _insert_local_credential(engine)

        command.upgrade(config, "head")
        credentials = Table(
            "connector_credentials",
            MetaData(),
            autoload_with=engine,
        )
        with engine.connect() as connection:
            row = connection.execute(
                credentials.select().where(credentials.c.connector_id == connector_id)
            ).one()
        assert row.cipher_scheme == "local-aesgcm-v1"
        assert row.wrapped_data_key == b"w" * 48
        assert row.wrapped_key_nonce == b"k" * 12
        assert row.key_version == "local-v1"

        command.downgrade(config, "20260717_0011")
        assert _revision(engine) == "20260717_0011"
    finally:
        settings.database_url = previous_url
        if engine is not None:
            engine.dispose()


@pytest.mark.parametrize("populated_boundary", ["oidc", "kms"])
def test_phase9e_downgrade_refuses_to_discard_security_receipts(
    tmp_path: Path,
    populated_boundary: str,
) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / f'phase9e-{populated_boundary}.db'}"
    previous_url = settings.database_url
    engine = None
    try:
        settings.database_url = database_url
        config = _migration_config()
        command.upgrade(config, "20260717_0011")
        engine = create_engine(database_url)
        _credentials, connector_id = _insert_local_credential(engine)
        command.upgrade(config, "head")

        metadata = MetaData()
        if populated_boundary == "oidc":
            transactions = Table(
                "oidc_login_transactions",
                metadata,
                autoload_with=engine,
            )
            now = datetime.now(UTC)
            with engine.begin() as connection:
                connection.execute(
                    transactions.insert().values(
                        id=uuid4().hex,
                        state_hash="a" * 64,
                        browser_binding_hash="b" * 64,
                        nonce_hash="c" * 64,
                        verifier_ciphertext=b"v" * 59,
                        verifier_nonce=b"n" * 12,
                        organization_slug="pageragent-labs",
                        created_at=now,
                        expires_at=now + timedelta(minutes=5),
                    )
                )
            match = "hosted identity or administration receipts"
        else:
            credentials = Table(
                "connector_credentials",
                metadata,
                autoload_with=engine,
            )
            with engine.begin() as connection:
                connection.execute(
                    credentials.update()
                    .where(credentials.c.connector_id == connector_id)
                    .values(
                        cipher_scheme="aws-kms-v1",
                        wrapped_data_key=b"k" * 96,
                        wrapped_key_nonce=None,
                        key_version=(
                            "arn:aws:kms:us-east-1:123456789012:"
                            "key/12345678-1234-1234-1234-123456789012"
                        ),
                    )
                )
            match = "AWS KMS credential envelopes"

        with pytest.raises(RuntimeError, match=match):
            command.downgrade(config, "20260717_0011")
        assert _revision(engine) == "20260718_0012"
    finally:
        settings.database_url = previous_url
        if engine is not None:
            engine.dispose()
