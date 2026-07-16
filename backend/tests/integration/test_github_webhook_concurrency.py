import hashlib
import hmac
import json
import os
from queue import Queue
from threading import Barrier, Thread
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import delete, func, select

from app.auth.constants import DEFAULT_ORGANIZATION_ID
from app.connectors.runtime import GithubConnectorRuntime
from app.connectors.vault import CredentialContext, build_credential_cipher
from app.db.models import (
    ConnectorAuditEventRecord,
    ConnectorCredentialRecord,
    ConnectorRecord,
    GithubWebhookDeliveryRecord,
)
from app.db.session import SessionLocal, engine
from app.domain.connectors import (
    ConnectorPatchInput,
    ConnectorProvider,
    GithubConfiguration,
    GithubCredentials,
)
from app.services.connectors import ConnectorEnablementError, ConnectorService
from app.services.github_webhooks import GithubWebhookService

pytestmark = pytest.mark.skipif(
    os.getenv("PAGERAGENT_INTEGRATION_TESTS") != "1",
    reason="Set PAGERAGENT_INTEGRATION_TESTS=1 to use local PostgreSQL.",
)


def test_postgres_concurrent_exact_delivery_retry_creates_one_inbox_row() -> None:
    assert engine.dialect.name == "postgresql", (
        "PAGERAGENT_INTEGRATION_TESTS=1 requires DATABASE_URL to target PostgreSQL"
    )
    connector_id = uuid4()
    delivery_id = str(uuid4())
    repository = "pageragent/concurrency-proof"
    webhook_secret = "postgres-concurrency-webhook-secret-32-bytes"
    runtime = GithubConnectorRuntime(
        connector_id=connector_id,
        organization_id=DEFAULT_ORGANIZATION_ID,
        connector_version=1,
        credential_version=1,
        configuration=GithubConfiguration(
            service="checkout-api",
            repository=repository,
            app_id=1001,
            installation_id=2002,
            api_url="https://api.github.com",
        ),
        credentials=GithubCredentials(
            private_key=SecretStr("unused-by-webhook-ingestion"),
            webhook_secret=SecretStr(webhook_secret),
        ),
    )
    body = json.dumps(
        {
            "repository": {"full_name": repository},
            "installation": {"id": 2002},
            "ref": "refs/heads/main",
            "before": "1" * 40,
            "after": "2" * 40,
            "created": False,
            "deleted": False,
            "forced": False,
            "commits": [],
            "head_commit": None,
        },
        separators=(",", ":"),
    ).encode()
    signature = "sha256=" + hmac.new(
        webhook_secret.encode(),
        body,
        hashlib.sha256,
    ).hexdigest()
    start = Barrier(2)
    outcomes: Queue[bool] = Queue()
    errors: Queue[BaseException] = Queue()

    with SessionLocal() as session:
        sealed = build_credential_cipher().seal(
            {
                "private_key": runtime.credentials.private_key.get_secret_value(),
                "webhook_secret": webhook_secret,
            },
            CredentialContext(
                organization_id=DEFAULT_ORGANIZATION_ID,
                connector_id=connector_id,
                provider=ConnectorProvider.GITHUB,
                credential_version=1,
            ),
        )
        connector = ConnectorRecord(
            id=connector_id,
            organization_id=DEFAULT_ORGANIZATION_ID,
            name=f"GitHub concurrency {connector_id}",
            provider="github",
            configuration=runtime.configuration.model_dump(),
            enabled=True,
            status="configured",
            version=1,
        )
        connector.credential = ConnectorCredentialRecord(
            connector_id=connector_id,
            credential_version=1,
            ciphertext=sealed.ciphertext,
            ciphertext_nonce=sealed.ciphertext_nonce,
            wrapped_data_key=sealed.wrapped_data_key,
            wrapped_key_nonce=sealed.wrapped_key_nonce,
            key_version=sealed.key_version,
            credential_field_names=list(sealed.credential_field_names),
        )
        session.add(connector)
        session.commit()

    def ingest() -> None:
        try:
            start.wait(timeout=5)
            with SessionLocal() as session:
                receipt = GithubWebhookService(session, runtime).ingest(
                    delivery_id=delivery_id,
                    event_type="push",
                    signature=signature,
                    raw_body=body,
                )
                outcomes.put(receipt.duplicate)
        except BaseException as error:
            errors.put(error)

    first = Thread(target=ingest, daemon=True)
    second = Thread(target=ingest, daemon=True)
    try:
        first.start()
        second.start()
        first.join(timeout=10)
        second.join(timeout=10)

        assert not first.is_alive() and not second.is_alive()
        assert errors.empty(), [repr(error) for error in list(errors.queue)]
        assert sorted(outcomes.queue) == [False, True]
        with SessionLocal() as session:
            count = session.scalar(
                select(func.count())
                .select_from(GithubWebhookDeliveryRecord)
                .where(
                    GithubWebhookDeliveryRecord.connector_id == connector_id,
                    GithubWebhookDeliveryRecord.delivery_id == delivery_id,
                )
            )
        assert count == 1
    finally:
        first.join(timeout=1)
        second.join(timeout=1)
        with SessionLocal() as session:
            session.execute(
                delete(GithubWebhookDeliveryRecord).where(
                    GithubWebhookDeliveryRecord.connector_id == connector_id
                )
            )
            session.execute(delete(ConnectorRecord).where(ConnectorRecord.id == connector_id))
            session.commit()


def test_postgres_serializes_competing_github_service_enables() -> None:
    assert engine.dialect.name == "postgresql", (
        "PAGERAGENT_INTEGRATION_TESTS=1 requires DATABASE_URL to target PostgreSQL"
    )
    connector_ids = [uuid4(), uuid4()]
    with SessionLocal() as session:
        for index, connector_id in enumerate(connector_ids):
            sealed = build_credential_cipher().seal(
                {
                    "private_key": "integration-private-key",
                    "webhook_secret": "integration-webhook-secret-with-32-bytes",
                },
                CredentialContext(
                    organization_id=DEFAULT_ORGANIZATION_ID,
                    connector_id=connector_id,
                    provider=ConnectorProvider.GITHUB,
                    credential_version=1,
                ),
            )
            connector = ConnectorRecord(
                id=connector_id,
                organization_id=DEFAULT_ORGANIZATION_ID,
                name=f"Competing GitHub connector {connector_id}",
                provider="github",
                configuration={
                    "service": "checkout-api",
                    "repository": f"pageragent/concurrency-{index}",
                    "app_id": 1001,
                    "installation_id": 2002 + index,
                    "api_url": "https://api.github.com",
                },
                enabled=False,
                status="disabled",
                version=1,
                last_validation_ok=True,
            )
            connector.credential = ConnectorCredentialRecord(
                connector_id=connector_id,
                credential_version=1,
                ciphertext=sealed.ciphertext,
                ciphertext_nonce=sealed.ciphertext_nonce,
                wrapped_data_key=sealed.wrapped_data_key,
                wrapped_key_nonce=sealed.wrapped_key_nonce,
                key_version=sealed.key_version,
                credential_field_names=list(sealed.credential_field_names),
            )
            session.add(connector)
        session.commit()

    start = Barrier(2)
    outcomes: Queue[str] = Queue()
    errors: Queue[BaseException] = Queue()

    def enable(connector_id: UUID) -> None:
        try:
            start.wait(timeout=5)
            with SessionLocal() as session:
                try:
                    ConnectorService(session, DEFAULT_ORGANIZATION_ID).patch_connector(
                        connector_id,
                        ConnectorPatchInput(expected_version=1, enabled=True),
                        actor="integration-test",
                    )
                except ConnectorEnablementError:
                    outcomes.put("conflict")
                else:
                    outcomes.put("enabled")
        except BaseException as error:
            errors.put(error)

    first = Thread(target=enable, args=(connector_ids[0],), daemon=True)
    second = Thread(target=enable, args=(connector_ids[1],), daemon=True)
    try:
        first.start()
        second.start()
        first.join(timeout=10)
        second.join(timeout=10)

        assert not first.is_alive() and not second.is_alive()
        assert errors.empty(), [repr(error) for error in list(errors.queue)]
        assert sorted(outcomes.queue) == ["conflict", "enabled"]
        with SessionLocal() as session:
            enabled = session.scalars(
                select(ConnectorRecord.id).where(
                    ConnectorRecord.id.in_(connector_ids),
                    ConnectorRecord.enabled.is_(True),
                )
            ).all()
        assert len(enabled) == 1
    finally:
        first.join(timeout=1)
        second.join(timeout=1)
        with SessionLocal() as session:
            session.execute(
                delete(ConnectorAuditEventRecord).where(
                    ConnectorAuditEventRecord.connector_id.in_(connector_ids)
                )
            )
            session.execute(delete(ConnectorRecord).where(ConnectorRecord.id.in_(connector_ids)))
            session.commit()
