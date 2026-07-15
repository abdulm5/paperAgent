from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.api.routes.workflows import encode_workflow_event
from app.auth.constants import DEFAULT_ORGANIZATION_ID
from app.db.models import (
    IncidentRecord,
    OutboxMessageRecord,
    WorkflowEventRecord,
    WorkflowJobRecord,
    WorkflowRunRecord,
)
from app.domain.workflows import WorkflowStatus, WorkflowType
from app.workflows.broker import InMemoryWorkflowBroker
from app.workflows.dispatcher import OutboxDispatcher
from app.workflows.engine import ExecutionDisposition, WorkflowEngine
from app.workflows.store import WorkflowStore
from app.workflows.worker import process_message


class FakeClock:
    def __init__(self, current: datetime) -> None:
        self.current = current

    def __call__(self) -> datetime:
        return self.current


def session_factory(session: Session) -> Callable[[], Session]:
    factory = sessionmaker(bind=session.get_bind(), autoflush=False, expire_on_commit=False)
    return factory


def create_incident(session: Session) -> IncidentRecord:
    now = datetime.now(UTC)
    incident = IncidentRecord(
        organization_id=DEFAULT_ORGANIZATION_ID,
        fingerprint="workflow-test-incident",
        active_fingerprint="workflow-test-incident",
        status="detected",
        service="checkout-api",
        severity="critical",
        summary="Workflow reliability test incident",
        started_at=now,
        detected_at=now,
        version=1,
    )
    session.add(incident)
    session.flush()
    return incident


def enqueue_workflow(
    session: Session,
    *,
    step_type: str = "generate_postmortem",
    max_attempts: int = 5,
    dedupe_key: str = "workflow-test",
) -> tuple[UUID, UUID]:
    incident = create_incident(session)
    run = WorkflowStore(session).enqueue(
        incident.id,
        WorkflowType.POSTMORTEM,
        step_type,
        dedupe_key,
        max_attempts=max_attempts,
    )
    session.commit()
    job_id = session.scalar(
        select(WorkflowJobRecord.id).where(
            WorkflowJobRecord.workflow_run_id == run.id,
            WorkflowJobRecord.step_type == step_type,
        )
    )
    assert job_id is not None
    return run.id, job_id


def as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def test_enqueue_stages_run_job_events_and_outbox_in_one_transaction(
    db_session: Session,
) -> None:
    incident = create_incident(db_session)

    run = WorkflowStore(db_session).enqueue(
        incident.id,
        WorkflowType.INCIDENT_RESPONSE,
        "investigate",
        "incident-response:test-alert",
        payload={"source": "unit-test"},
        trace_id="trace-test-alert",
    )

    assert db_session.scalar(select(func.count()).select_from(WorkflowRunRecord)) == 1
    assert db_session.scalar(select(func.count()).select_from(WorkflowJobRecord)) == 1
    assert db_session.scalar(select(func.count()).select_from(WorkflowEventRecord)) == 2
    assert db_session.scalar(select(func.count()).select_from(OutboxMessageRecord)) == 1
    assert run.status == WorkflowStatus.QUEUED.value
    assert run.current_step == "investigate"
    db_session.commit()


def test_enqueue_rolls_back_run_job_events_and_outbox_together(db_session: Session) -> None:
    incident = create_incident(db_session)
    db_session.commit()

    WorkflowStore(db_session).enqueue(
        incident.id,
        WorkflowType.INCIDENT_RESPONSE,
        "investigate",
        "incident-response:rolled-back",
    )
    db_session.rollback()

    assert db_session.scalar(select(func.count()).select_from(IncidentRecord)) == 1
    assert db_session.scalar(select(func.count()).select_from(WorkflowRunRecord)) == 0
    assert db_session.scalar(select(func.count()).select_from(WorkflowJobRecord)) == 0
    assert db_session.scalar(select(func.count()).select_from(WorkflowEventRecord)) == 0
    assert db_session.scalar(select(func.count()).select_from(OutboxMessageRecord)) == 0


def test_duplicate_workflow_dedupe_key_reuses_the_existing_run(
    db_session: Session,
) -> None:
    incident = create_incident(db_session)
    store = WorkflowStore(db_session)

    first = store.enqueue(
        incident.id,
        WorkflowType.POSTMORTEM,
        "generate_postmortem",
        "postmortem:resolved-event",
    )
    duplicate = store.enqueue(
        incident.id,
        WorkflowType.POSTMORTEM,
        "generate_postmortem",
        "postmortem:resolved-event",
    )
    db_session.commit()

    assert duplicate.id == first.id
    assert db_session.scalar(select(func.count()).select_from(WorkflowRunRecord)) == 1
    assert db_session.scalar(select(func.count()).select_from(WorkflowJobRecord)) == 1
    assert db_session.scalar(select(func.count()).select_from(OutboxMessageRecord)) == 1


def test_outbox_dispatch_publishes_due_message_and_records_receipt(
    db_session: Session,
) -> None:
    run_id, job_id = enqueue_workflow(db_session)
    factory = session_factory(db_session)
    broker = InMemoryWorkflowBroker()
    dispatch_time = datetime.now(UTC) + timedelta(seconds=1)

    published = OutboxDispatcher(factory, broker).dispatch_once(now=dispatch_time)
    messages = broker.read("test-consumer", block_ms=0)

    assert published == 1
    assert len(messages) == 1
    assert messages[0].values["workflow_job_id"] == str(job_id)
    with factory() as session:
        outbox = session.scalar(
            select(OutboxMessageRecord).where(OutboxMessageRecord.workflow_job_id == job_id)
        )
        assert outbox is not None
        assert outbox.published_at is not None
        assert outbox.publish_attempts == 1
        assert outbox.stream_message_id == messages[0].message_id
        detail = WorkflowStore(session).get_detail(run_id)
        assert detail.version == 2
        assert detail.jobs[0].deliveries[0].stream_message_id == messages[0].message_id
        assert detail.events[-1].event_type == "workflow.delivery_published"


def test_outbox_publish_failure_records_backoff_and_live_delivery_event(
    db_session: Session,
) -> None:
    run_id, job_id = enqueue_workflow(db_session)
    factory = session_factory(db_session)
    attempt_at = datetime.now(UTC) + timedelta(seconds=1)

    class UnavailableBroker(InMemoryWorkflowBroker):
        def publish(self, values: dict[str, str]) -> str:
            del values
            raise ConnectionError("redis is offline")

    dispatcher = OutboxDispatcher(factory, UnavailableBroker())
    assert dispatcher.dispatch_once(now=attempt_at) == 0

    with factory() as session:
        outbox = session.scalar(
            select(OutboxMessageRecord).where(OutboxMessageRecord.workflow_job_id == job_id)
        )
        assert outbox is not None
        assert outbox.published_at is None
        assert outbox.publish_attempts == 1
        assert outbox.last_error == "redis is offline"
        assert as_utc(outbox.available_at) == attempt_at + timedelta(seconds=2)
        detail = WorkflowStore(session).get_detail(run_id)
        assert detail.version == 2
        assert detail.events[-1].event_type == "workflow.delivery_failed"
        assert detail.events[-1].payload["retry_at"] == as_utc(outbox.available_at).isoformat()


def test_outbox_republishes_latest_nonterminal_job_after_transport_loss(
    db_session: Session,
) -> None:
    run_id, job_id = enqueue_workflow(db_session)
    factory = session_factory(db_session)
    broker = InMemoryWorkflowBroker()
    first_publish_at = datetime.now(UTC) + timedelta(seconds=1)
    dispatcher = OutboxDispatcher(factory, broker, repair_after_seconds=30)

    assert dispatcher.dispatch_once(now=first_publish_at) == 1
    lost = broker.read("lost-transport", block_ms=0)[0]
    broker.acknowledge(lost.message_id)
    assert broker.reclaim("repair-worker", min_idle_ms=0) == []
    assert dispatcher.dispatch_once(now=first_publish_at + timedelta(seconds=29)) == 0
    broker.simulate_stream_loss()

    repaired_at = first_publish_at + timedelta(seconds=30)
    assert dispatcher.dispatch_once(now=repaired_at) == 1
    repaired = broker.read("repair-worker", block_ms=0)
    assert len(repaired) == 1
    assert repaired[0].values["workflow_job_id"] == str(job_id)
    with factory() as session:
        outbox = session.scalar(
            select(OutboxMessageRecord).where(OutboxMessageRecord.workflow_job_id == job_id)
        )
        assert outbox is not None
        assert outbox.publish_attempts == 2
        assert as_utc(outbox.published_at) == repaired_at
        detail = WorkflowStore(session).get_detail(run_id)
        assert detail.events[-1].event_type == "workflow.delivery_repaired"
        assert detail.events[-1].payload["publish_attempt"] == 2


def test_outbox_does_not_republish_a_healthy_stale_receipt(db_session: Session) -> None:
    run_id, job_id = enqueue_workflow(db_session)
    factory = session_factory(db_session)
    broker = InMemoryWorkflowBroker()
    first_publish_at = datetime.now(UTC) + timedelta(seconds=1)
    dispatcher = OutboxDispatcher(factory, broker, repair_after_seconds=30)

    assert dispatcher.dispatch_once(now=first_publish_at) == 1
    delivered = broker.read("healthy-worker", block_ms=0)[0]
    broker.acknowledge(delivered.message_id)
    assert dispatcher.dispatch_once(now=first_publish_at + timedelta(seconds=30)) == 0

    with factory() as session:
        receipt = session.scalar(
            select(OutboxMessageRecord).where(OutboxMessageRecord.workflow_job_id == job_id)
        )
        detail = WorkflowStore(session).get_detail(run_id)
        assert receipt is not None
        assert receipt.publish_attempts == 1
        assert receipt.stream_message_id == delivered.message_id
        assert detail.version == 2
        assert [event.event_type for event in detail.events].count(
            "workflow.delivery_published"
        ) == 1
        assert all(event.event_type != "workflow.delivery_repaired" for event in detail.events)


def test_outbox_receipt_check_failure_backs_off_without_duplicate_publish(
    db_session: Session,
) -> None:
    run_id, job_id = enqueue_workflow(db_session)
    factory = session_factory(db_session)
    first_publish_at = datetime.now(UTC) + timedelta(seconds=1)

    class CheckUnavailableBroker(InMemoryWorkflowBroker):
        def message_exists(self, message_id: str) -> bool:
            del message_id
            raise ConnectionError("redis receipt lookup failed")

    broker = CheckUnavailableBroker()
    dispatcher = OutboxDispatcher(factory, broker, repair_after_seconds=30)
    assert dispatcher.dispatch_once(now=first_publish_at) == 1
    assert dispatcher.dispatch_once(now=first_publish_at + timedelta(seconds=30)) == 0

    with factory() as session:
        receipt = session.scalar(
            select(OutboxMessageRecord).where(OutboxMessageRecord.workflow_job_id == job_id)
        )
        detail = WorkflowStore(session).get_detail(run_id)
        assert receipt is not None
        assert receipt.publish_attempts == 1
        assert as_utc(receipt.available_at) == first_publish_at + timedelta(seconds=60)
        assert detail.version == 2
        assert all(event.event_type != "workflow.delivery_repaired" for event in detail.events)


def test_duplicate_delivery_of_completed_job_is_a_terminal_noop(
    db_session: Session,
) -> None:
    run_id, _ = enqueue_workflow(db_session)
    factory = session_factory(db_session)
    broker = InMemoryWorkflowBroker()
    clock = FakeClock(datetime.now(UTC) + timedelta(seconds=1))
    handler_calls: list[UUID] = []

    def handler(_session: Session, job: WorkflowJobRecord, _fence) -> dict[str, object]:
        handler_calls.append(job.id)
        return {"postmortem_id": "postmortem-test"}

    engine = WorkflowEngine(
        factory,
        worker_id="worker-a",
        handlers={"generate_postmortem": handler},
        clock=clock,
    )
    dispatcher = OutboxDispatcher(factory, broker)
    assert dispatcher.dispatch_once(now=clock.current) == 1
    original = broker.read("worker-a", block_ms=0)[0]

    process_message(original, broker=broker, engine=engine)
    broker.publish(original.values)
    duplicate = broker.read("worker-b", block_ms=0)[0]
    process_message(duplicate, broker=broker, engine=engine)

    assert handler_calls == [handler_calls[0]]
    with factory() as session:
        run = session.get(WorkflowRunRecord, run_id)
        assert run is not None
        assert run.status == WorkflowStatus.COMPLETED.value
        completed_events = session.scalars(
            select(WorkflowEventRecord).where(
                WorkflowEventRecord.workflow_run_id == run_id,
                WorkflowEventRecord.event_type == "workflow.step_completed",
            )
        ).all()
        assert len(completed_events) == 1


def test_investigation_completion_durably_queues_proposal_step(
    db_session: Session,
) -> None:
    run_id, investigation_job_id = enqueue_workflow(
        db_session,
        step_type="investigate",
        dedupe_key="incident-response:chain",
    )
    factory = session_factory(db_session)
    clock = FakeClock(datetime.now(UTC) + timedelta(seconds=1))

    def handler(_session: Session, _job: WorkflowJobRecord, _fence) -> dict[str, object]:
        return {"investigation_id": "00000000-0000-0000-0000-000000000123"}

    result = WorkflowEngine(
        factory,
        worker_id="chain-worker",
        handlers={"investigate": handler},
        clock=clock,
    ).execute(investigation_job_id)

    assert result.disposition is ExecutionDisposition.COMPLETED
    with factory() as session:
        run = session.get(WorkflowRunRecord, run_id)
        jobs = session.scalars(
            select(WorkflowJobRecord)
            .where(WorkflowJobRecord.workflow_run_id == run_id)
            .order_by(WorkflowJobRecord.created_at)
        ).all()
        assert run is not None
        assert run.status == WorkflowStatus.QUEUED.value
        assert run.current_step == "generate_proposal"
        assert {job.step_type for job in jobs} == {"investigate", "generate_proposal"}
        proposal_job = next(job for job in jobs if job.step_type == "generate_proposal")
        assert proposal_job.payload["investigation_id"].endswith("0123")
        assert (
            session.scalar(
                select(func.count())
                .select_from(OutboxMessageRecord)
                .where(OutboxMessageRecord.workflow_job_id == proposal_job.id)
            )
            == 1
        )


def test_retry_delay_grows_exponentially(db_session: Session) -> None:
    _, job_id = enqueue_workflow(db_session, max_attempts=4)
    factory = session_factory(db_session)
    clock = FakeClock(datetime.now(UTC) + timedelta(seconds=1))

    def failing_handler(_session: Session, _job: WorkflowJobRecord, _fence) -> dict[str, object]:
        raise TimeoutError("temporary evidence timeout")

    engine = WorkflowEngine(
        factory,
        worker_id="retry-worker",
        handlers={"generate_postmortem": failing_handler},
        clock=clock,
        retry_base_seconds=2,
    )

    first_attempt_at = clock.current
    first = engine.execute(job_id)
    assert first.disposition is ExecutionDisposition.RETRY_SCHEDULED
    with factory() as session:
        job = session.get(WorkflowJobRecord, job_id)
        assert job is not None
        first_retry_at = as_utc(job.available_at)
        assert first_retry_at == first_attempt_at + timedelta(seconds=2)

    clock.current = first_retry_at
    second = engine.execute(job_id)
    assert second.disposition is ExecutionDisposition.RETRY_SCHEDULED
    with factory() as session:
        job = session.get(WorkflowJobRecord, job_id)
        assert job is not None
        assert job.attempt_count == 2
        assert as_utc(job.available_at) == first_retry_at + timedelta(seconds=4)
        attempts = session.scalars(
            select(OutboxMessageRecord.dispatch_attempt)
            .where(OutboxMessageRecord.workflow_job_id == job_id)
            .order_by(OutboxMessageRecord.dispatch_attempt)
        ).all()
        assert list(attempts) == [1, 2, 3]


def test_attempt_limit_moves_job_and_stream_message_to_dead_letter(
    db_session: Session,
) -> None:
    run_id, job_id = enqueue_workflow(db_session, max_attempts=2)
    factory = session_factory(db_session)
    broker = InMemoryWorkflowBroker()
    clock = FakeClock(datetime.now(UTC) + timedelta(seconds=1))

    def failing_handler(_session: Session, _job: WorkflowJobRecord, _fence) -> dict[str, object]:
        raise ConnectionError("provider unavailable")

    engine = WorkflowEngine(
        factory,
        worker_id="dead-letter-worker",
        handlers={"generate_postmortem": failing_handler},
        clock=clock,
        retry_base_seconds=2,
    )
    dispatcher = OutboxDispatcher(factory, broker)
    assert dispatcher.dispatch_once(now=clock.current) == 1
    process_message(
        broker.read("dead-letter-worker", block_ms=0)[0],
        broker=broker,
        engine=engine,
    )

    with factory() as session:
        job = session.get(WorkflowJobRecord, job_id)
        assert job is not None
        clock.current = as_utc(job.available_at)

    assert dispatcher.dispatch_once(now=clock.current) == 1
    process_message(
        broker.read("dead-letter-worker", block_ms=0)[0],
        broker=broker,
        engine=engine,
    )

    with factory() as session:
        job = session.get(WorkflowJobRecord, job_id)
        run = session.get(WorkflowRunRecord, run_id)
        assert job is not None
        assert run is not None
        assert job.status == WorkflowStatus.DEAD_LETTERED.value
        assert run.status == WorkflowStatus.DEAD_LETTERED.value
        assert job.attempt_count == 2
        assert (
            session.scalar(
                select(func.count())
                .select_from(WorkflowEventRecord)
                .where(
                    WorkflowEventRecord.workflow_run_id == run_id,
                    WorkflowEventRecord.event_type == "workflow.dead_lettered",
                )
            )
            == 1
        )
    assert len(broker.dead_letters) == 1
    assert broker.dead_letters[0].values["workflow_job_id"] == str(job_id)


def test_expired_lease_is_recovered_by_another_worker(db_session: Session) -> None:
    run_id, job_id = enqueue_workflow(db_session)
    factory = session_factory(db_session)
    now = datetime.now(UTC) + timedelta(seconds=10)
    clock = FakeClock(now)
    handler_calls: list[UUID] = []

    with factory() as session:
        job = session.get(WorkflowJobRecord, job_id)
        assert job is not None
        job.status = WorkflowStatus.RUNNING.value
        job.attempt_count = 1
        job.lease_owner = "crashed-worker"
        job.lease_expires_at = now - timedelta(seconds=1)
        job.workflow_run.status = WorkflowStatus.RUNNING.value
        session.commit()

    def handler(_session: Session, job: WorkflowJobRecord, _fence) -> dict[str, object]:
        handler_calls.append(job.id)
        return {"postmortem_id": "postmortem-recovered"}

    result = WorkflowEngine(
        factory,
        worker_id="replacement-worker",
        handlers={"generate_postmortem": handler},
        clock=clock,
        lease_seconds=30,
    ).execute(job_id)

    assert result.disposition is ExecutionDisposition.COMPLETED
    assert handler_calls == [job_id]
    with factory() as session:
        job = session.get(WorkflowJobRecord, job_id)
        assert job is not None
        assert job.attempt_count == 2
        assert job.status == WorkflowStatus.COMPLETED.value
        recovered = session.scalars(
            select(WorkflowEventRecord).where(
                WorkflowEventRecord.workflow_run_id == run_id,
                WorkflowEventRecord.event_type == "workflow.lease_recovered",
            )
        ).all()
        assert len(recovered) == 1
        assert recovered[0].payload["worker_id"] == "replacement-worker"


def test_expired_lease_at_attempt_limit_dead_letters_without_reexecution(
    db_session: Session,
) -> None:
    run_id, job_id = enqueue_workflow(db_session, max_attempts=1)
    factory = session_factory(db_session)
    now = datetime.now(UTC) + timedelta(seconds=10)
    handler_calls: list[UUID] = []

    with factory() as session:
        job = session.get(WorkflowJobRecord, job_id)
        assert job is not None
        job.status = WorkflowStatus.RUNNING.value
        job.attempt_count = 1
        job.lease_owner = "crashed-final-attempt"
        job.lease_expires_at = now - timedelta(seconds=1)
        job.workflow_run.status = WorkflowStatus.RUNNING.value
        session.commit()

    def handler(_session: Session, job: WorkflowJobRecord, _fence) -> dict[str, object]:
        handler_calls.append(job.id)
        return {"postmortem_id": "must-not-run"}

    result = WorkflowEngine(
        factory,
        worker_id="replacement-worker",
        handlers={"generate_postmortem": handler},
        clock=FakeClock(now),
    ).execute(job_id)

    assert result.disposition is ExecutionDisposition.DEAD_LETTERED
    assert handler_calls == []
    with factory() as session:
        job = session.get(WorkflowJobRecord, job_id)
        run = session.get(WorkflowRunRecord, run_id)
        assert job is not None
        assert run is not None
        assert job.status == WorkflowStatus.DEAD_LETTERED.value
        assert job.attempt_count == 1
        assert run.status == WorkflowStatus.DEAD_LETTERED.value
        assert "maximum of 1 attempts exhausted" in (job.last_error or "")


def test_delivery_stays_pending_when_reclaim_collides_with_an_active_lease(
    db_session: Session,
) -> None:
    _, job_id = enqueue_workflow(db_session)
    factory = session_factory(db_session)
    broker = InMemoryWorkflowBroker()
    now = datetime.now(UTC) + timedelta(seconds=1)
    clock = FakeClock(now)

    assert OutboxDispatcher(factory, broker).dispatch_once(now=now) == 1
    message = broker.read("replacement-worker", block_ms=0)[0]
    with factory() as session:
        job = session.get(WorkflowJobRecord, job_id)
        assert job is not None
        job.status = WorkflowStatus.RUNNING.value
        job.attempt_count = 1
        job.lease_owner = "healthy-worker"
        job.lease_expires_at = now + timedelta(seconds=30)
        job.workflow_run.status = WorkflowStatus.RUNNING.value
        session.commit()

    process_message(
        message,
        broker=broker,
        engine=WorkflowEngine(factory, worker_id="replacement-worker", clock=clock),
    )

    assert broker.reclaim("later-worker", min_idle_ms=0) == [message]


def test_superseded_worker_cannot_commit_with_a_stale_fencing_token(
    db_session: Session,
) -> None:
    run_id, job_id = enqueue_workflow(db_session)
    factory = session_factory(db_session)
    clock = FakeClock(datetime.now(UTC) + timedelta(seconds=1))

    def handler(session: Session, _job: WorkflowJobRecord, fence) -> dict[str, object]:
        with factory() as replacement_session:
            replacement_job = replacement_session.get(WorkflowJobRecord, job_id)
            assert replacement_job is not None
            replacement_job.lease_owner = "replacement-worker"
            replacement_job.attempt_count += 1
            replacement_session.commit()
        incident = session.scalar(select(IncidentRecord))
        assert incident is not None
        incident.summary = "stale worker must not commit this mutation"
        fence.commit(session)
        return {"postmortem_id": "stale-worker-result"}

    result = WorkflowEngine(
        factory,
        worker_id="stale-worker",
        handlers={"generate_postmortem": handler},
        clock=clock,
    ).execute(job_id)

    assert result.disposition is ExecutionDisposition.SKIPPED
    assert result.error is not None
    assert "is no longer active" in result.error
    with factory() as session:
        job = session.get(WorkflowJobRecord, job_id)
        run = session.get(WorkflowRunRecord, run_id)
        assert job is not None
        assert run is not None
        assert job.status == WorkflowStatus.RUNNING.value
        assert job.result == {}
        assert run.status == WorkflowStatus.RUNNING.value
        incident = session.scalar(select(IncidentRecord))
        assert incident is not None
        assert incident.summary == "Workflow reliability test incident"


def test_event_cursor_helper_returns_strict_global_order(db_session: Session) -> None:
    incident = create_incident(db_session)
    store = WorkflowStore(db_session)
    first = store.enqueue(
        incident.id,
        WorkflowType.INCIDENT_RESPONSE,
        "investigate",
        "event-order:first",
    )
    store.enqueue(
        incident.id,
        WorkflowType.POSTMORTEM,
        "generate_postmortem",
        "event-order:second",
    )
    db_session.commit()

    all_events = store.events_after()
    cursor = next(event.id for event in all_events if event.workflow_run_id == first.id)
    replay = store.events_after(last_id=cursor)

    assert [event.id for event in all_events] == sorted(event.id for event in all_events)
    assert all(event.id > cursor for event in replay)
    assert [event.id for event in replay] == sorted(event.id for event in replay)


def test_sse_envelope_is_reconnectable_and_contains_the_current_snapshot(
    db_session: Session,
) -> None:
    run_id, _ = enqueue_workflow(db_session)
    store = WorkflowStore(db_session)
    event = store.events_after()[0]
    encoded = encode_workflow_event(event, store.get_detail(run_id))

    assert encoded.startswith(f"id: {event.id}\nevent: workflow\ndata: ")
    assert f'"workflow_id":"{run_id}"' in encoded
    assert '"status":"queued"' in encoded
    assert encoded.endswith("\n\n")
