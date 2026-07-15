import os
from datetime import UTC, datetime, timedelta
from queue import Queue
from threading import Event, Lock, Thread
from uuid import UUID, uuid4

import pytest
from redis import Redis
from sqlalchemy import delete, select

from app.auth.constants import DEFAULT_ORGANIZATION_ID
from app.core.config import settings
from app.db.models import (
    IncidentRecord,
    OutboxMessageRecord,
    WorkflowEventRecord,
    WorkflowJobRecord,
    WorkflowRunRecord,
)
from app.db.session import SessionLocal, engine
from app.domain.workflows import WorkflowType
from app.workflows.broker import RedisStreamBroker
from app.workflows.dispatcher import OutboxDispatcher
from app.workflows.engine import ExecutionDisposition, WorkflowEngine
from app.workflows.store import WorkflowStore

pytestmark = pytest.mark.skipif(
    os.getenv("PAGERAGENT_INTEGRATION_TESTS") != "1",
    reason="Set PAGERAGENT_INTEGRATION_TESTS=1 to use local PostgreSQL and Redis.",
)


def _create_queued_job() -> tuple[UUID, UUID, UUID]:
    incident_id = uuid4()
    now = datetime.now(UTC)
    with SessionLocal() as session:
        session.add(
            IncidentRecord(
                id=incident_id,
                organization_id=DEFAULT_ORGANIZATION_ID,
                fingerprint=f"workflow-integration:{incident_id}",
                active_fingerprint=f"workflow-integration:{incident_id}",
                status="detected",
                service="checkout-api",
                severity="critical",
                summary="Real-infrastructure workflow claim test",
                started_at=now,
                detected_at=now,
                version=1,
            )
        )
        session.flush()
        run = WorkflowStore(session).enqueue(
            incident_id,
            WorkflowType.INCIDENT_RESPONSE,
            "investigate",
            f"workflow-integration:{incident_id}",
            trace_id=uuid4().hex,
        )
        session.commit()
        job_id = session.scalar(
            select(WorkflowJobRecord.id).where(WorkflowJobRecord.workflow_run_id == run.id)
        )
        assert job_id is not None
        return incident_id, run.id, job_id


def _delete_incident(incident_id: UUID) -> None:
    with SessionLocal() as session:
        session.execute(delete(IncidentRecord).where(IncidentRecord.id == incident_id))
        session.commit()


def test_postgres_skip_locked_allows_exactly_one_competing_relay_publish() -> None:
    assert engine.dialect.name == "postgresql", (
        "PAGERAGENT_INTEGRATION_TESTS=1 requires DATABASE_URL to target PostgreSQL"
    )
    incident_id, _, job_id = _create_queued_job()
    first_publish_entered = Event()
    release_first_publish = Event()
    second_dispatch_finished = Event()
    outcomes: Queue[tuple[str, int]] = Queue()
    errors: Queue[BaseException] = Queue()
    publish_count = 0
    publish_count_lock = Lock()
    dispatch_time = datetime.now(UTC) + timedelta(seconds=1)

    class HoldingBroker:
        def publish(self, _values: dict[str, str]) -> str:
            nonlocal publish_count
            with publish_count_lock:
                publish_count += 1
                current_publish = publish_count
            if current_publish == 1:
                first_publish_entered.set()
                if not release_first_publish.wait(timeout=5):
                    raise TimeoutError("Timed out waiting to release the first relay")
            return f"integration-stream-{current_publish}"

    broker = HoldingBroker()

    def dispatch(name: str, *, finished: Event | None = None) -> None:
        try:
            published = OutboxDispatcher(SessionLocal, broker).dispatch_once(now=dispatch_time)
            outcomes.put((name, published))
        except BaseException as error:
            errors.put(error)
            first_publish_entered.set()
        finally:
            if finished is not None:
                finished.set()

    first_dispatch = Thread(target=dispatch, args=("first",), daemon=True)
    second_dispatch = Thread(
        target=dispatch,
        args=("second",),
        kwargs={"finished": second_dispatch_finished},
        daemon=True,
    )
    try:
        first_dispatch.start()
        assert first_publish_entered.wait(timeout=5), "First relay did not acquire the outbox row"
        second_dispatch.start()
        assert second_dispatch_finished.wait(timeout=5), (
            "SKIP LOCKED did not release the competing relay"
        )
        release_first_publish.set()
        first_dispatch.join(timeout=5)
        second_dispatch.join(timeout=5)

        assert errors.empty(), [repr(error) for error in list(errors.queue)]
        observed = sorted(published for _, published in list(outcomes.queue))
        assert observed == [0, 1]
        assert publish_count == 1

        with SessionLocal() as session:
            receipt = session.scalar(
                select(OutboxMessageRecord).where(OutboxMessageRecord.workflow_job_id == job_id)
            )
            assert receipt is not None
            assert receipt.publish_attempts == 1
            assert receipt.stream_message_id == "integration-stream-1"
    finally:
        release_first_publish.set()
        first_dispatch.join(timeout=5)
        second_dispatch.join(timeout=5)
        _delete_incident(incident_id)


def test_postgres_event_cursor_follows_commit_order() -> None:
    assert engine.dialect.name == "postgresql", (
        "PAGERAGENT_INTEGRATION_TESTS=1 requires DATABASE_URL to target PostgreSQL"
    )
    first_incident_id, first_run_id, _ = _create_queued_job()
    second_incident_id, second_run_id, _ = _create_queued_job()
    first_event_inserted = Event()
    release_first_commit = Event()
    second_finished = Event()
    outcomes: Queue[tuple[str, int]] = Queue()
    errors: Queue[BaseException] = Queue()

    def append_event(
        name: str,
        run_id: UUID,
        *,
        hold_commit: bool = False,
        finished: Event | None = None,
    ) -> None:
        try:
            with SessionLocal() as session:
                run = session.get(WorkflowRunRecord, run_id)
                assert run is not None
                event = WorkflowStore(session).append_event(
                    run,
                    f"integration.cursor_{name}",
                )
                outcomes.put((name, event.id))
                if hold_commit:
                    first_event_inserted.set()
                    if not release_first_commit.wait(timeout=5):
                        raise TimeoutError("Timed out waiting to release the first event commit")
                session.commit()
        except BaseException as error:
            errors.put(error)
            first_event_inserted.set()
        finally:
            if finished is not None:
                finished.set()

    first = Thread(
        target=append_event,
        args=("first", first_run_id),
        kwargs={"hold_commit": True},
        daemon=True,
    )
    second = Thread(
        target=append_event,
        args=("second", second_run_id),
        kwargs={"finished": second_finished},
        daemon=True,
    )
    try:
        first.start()
        assert first_event_inserted.wait(timeout=5), "First event was not allocated"
        second.start()
        assert not second_finished.wait(timeout=0.25), (
            "Second event committed before the earlier event transaction resolved"
        )
        release_first_commit.set()
        first.join(timeout=5)
        second.join(timeout=5)

        assert errors.empty(), [repr(error) for error in list(errors.queue)]
        ids = dict(outcomes.queue)
        assert ids["first"] < ids["second"]
        with SessionLocal() as session:
            replay = WorkflowStore(session).events_after(ids["first"] - 1)
            replay_ids = [event.id for event in replay]
            assert replay_ids.index(ids["first"]) < replay_ids.index(ids["second"])
    finally:
        release_first_commit.set()
        first.join(timeout=5)
        second.join(timeout=5)
        _delete_incident(first_incident_id)
        _delete_incident(second_incident_id)


def test_redis_stream_publish_read_reclaim_and_ack() -> None:
    namespace = f"pageragent.integration.{uuid4().hex}"
    stream_name = f"{namespace}.stream"
    consumer_group = f"{namespace}.group"
    dead_letter_stream = f"{namespace}.dlq"
    client = Redis.from_url(settings.redis_url, decode_responses=True)
    broker = RedisStreamBroker(
        settings.redis_url,
        stream_name=stream_name,
        consumer_group=consumer_group,
        dead_letter_stream=dead_letter_stream,
    )
    try:
        assert client.ping()
        broker.ensure_group()
        published_id = broker.publish(
            {
                "workflow_job_id": str(uuid4()),
                "step_type": "investigate",
                "source": "real-infrastructure-test",
            }
        )
        assert broker.message_exists(published_id)

        delivered = broker.read("integration-consumer-a", block_ms=100)
        assert len(delivered) == 1
        assert delivered[0].message_id == published_id

        reclaimed = broker.reclaim("integration-consumer-b", min_idle_ms=0)
        assert len(reclaimed) == 1
        assert reclaimed[0] == delivered[0]

        broker.acknowledge(reclaimed[0].message_id)
        assert client.xpending_range(stream_name, consumer_group, "-", "+", 10) == []

        client.delete(stream_name)
        assert not broker.message_exists(published_id)
        assert broker.read("integration-consumer-b", block_ms=1) == []
        restored_id = broker.publish(
            {
                "workflow_job_id": str(uuid4()),
                "step_type": "generate_postmortem",
                "source": "repaired-stream-test",
            }
        )
        assert broker.message_exists(restored_id)
        restored = broker.read("integration-consumer-b", block_ms=100)
        assert len(restored) == 1
        assert restored[0].message_id == restored_id
        broker.acknowledge(restored_id)
    finally:
        client.delete(stream_name, dead_letter_stream)
        broker.client.close()
        client.close()


def test_postgres_receipt_republishes_after_redis_stream_loss() -> None:
    incident_id, _, job_id = _create_queued_job()
    namespace = f"pageragent.integration.repair.{uuid4().hex}"
    stream_name = f"{namespace}.stream"
    consumer_group = f"{namespace}.group"
    dead_letter_stream = f"{namespace}.dlq"
    client = Redis.from_url(settings.redis_url, decode_responses=True)
    broker = RedisStreamBroker(
        settings.redis_url,
        stream_name=stream_name,
        consumer_group=consumer_group,
        dead_letter_stream=dead_letter_stream,
    )
    dispatcher = OutboxDispatcher(SessionLocal, broker, repair_after_seconds=1)
    first_publish_at = datetime.now(UTC) + timedelta(seconds=1)
    try:
        broker.ensure_group()
        assert dispatcher.dispatch_once(now=first_publish_at) == 1
        assert client.xlen(stream_name) == 1

        client.delete(stream_name)
        assert dispatcher.dispatch_once(now=first_publish_at + timedelta(seconds=1)) == 1
        broker.ensure_group()
        repaired = broker.read("integration-repair-consumer", block_ms=100)
        assert len(repaired) == 1
        assert repaired[0].values["workflow_job_id"] == str(job_id)

        with SessionLocal() as session:
            receipt = session.scalar(
                select(OutboxMessageRecord).where(OutboxMessageRecord.workflow_job_id == job_id)
            )
            assert receipt is not None
            assert receipt.publish_attempts == 2
    finally:
        client.delete(stream_name, dead_letter_stream)
        broker.client.close()
        client.close()
        _delete_incident(incident_id)


def test_dispatch_receipt_keeps_monotonic_version_if_worker_finishes_first() -> None:
    incident_id, run_id, job_id = _create_queued_job()

    def investigate(_session, _job, _fence) -> dict[str, str]:
        return {"investigation_id": str(uuid4())}

    engine = WorkflowEngine(
        SessionLocal,
        worker_id="integration-racing-worker",
        handlers={"investigate": investigate},
    )

    class RacingBroker:
        def publish(self, values: dict[str, str]) -> str:
            assert UUID(values["workflow_job_id"]) == job_id
            result = engine.execute(job_id)
            assert result.disposition is ExecutionDisposition.COMPLETED
            return "integration-race-1"

    try:
        assert OutboxDispatcher(SessionLocal, RacingBroker()).dispatch_once() == 1
        with SessionLocal() as session:
            run = session.get(WorkflowRunRecord, run_id)
            assert run is not None
            assert run.version == 4
            event_types = session.scalars(
                select(WorkflowEventRecord.event_type)
                .where(WorkflowEventRecord.workflow_run_id == run_id)
                .order_by(WorkflowEventRecord.sequence)
            ).all()
            assert event_types[-1] == "workflow.delivery_published"
    finally:
        _delete_incident(incident_id)
