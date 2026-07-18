"""Real-infrastructure release gates for contention and recovery invariants.

These tests are intentionally opt-in: they require migrated PostgreSQL and Redis
instances and never substitute SQLite for the locking behavior under test.
"""

import os
from collections import Counter
from datetime import UTC, datetime, timedelta
from queue import Queue
from threading import Barrier, Event, Lock, Thread
from uuid import UUID, uuid4

import pytest
from redis import Redis
from sqlalchemy import delete, func, select

from app.auth.constants import DEFAULT_ORGANIZATION_ID
from app.core.config import settings
from app.db.models import (
    IdentityAuditEventRecord,
    IncidentRecord,
    OrganizationMembershipRecord,
    OrganizationRecord,
    OutboxMessageRecord,
    UserRecord,
    WorkflowEventRecord,
    WorkflowJobRecord,
)
from app.db.session import SessionLocal, engine
from app.domain.auth import Role
from app.domain.memberships import MembershipUpdateInput
from app.domain.workflows import WorkflowType
from app.memberships.service import ActorAuthorityChangedError, MembershipService
from app.workflows.broker import RedisStreamBroker
from app.workflows.dispatcher import OutboxDispatcher
from app.workflows.engine import ExecutionDisposition, WorkflowEngine
from app.workflows.store import WorkflowStore

pytestmark = pytest.mark.skipif(
    os.getenv("PAGERAGENT_INTEGRATION_TESTS") != "1",
    reason="Set PAGERAGENT_INTEGRATION_TESTS=1 to use local PostgreSQL and Redis.",
)


@pytest.fixture(autouse=True)
def require_postgresql() -> None:
    assert engine.dialect.name == "postgresql", (
        "PAGERAGENT_INTEGRATION_TESTS=1 requires DATABASE_URL to target PostgreSQL"
    )
    assert (engine.url.database or "").endswith("_release_test"), (
        "Release contention tests require a dedicated database whose name ends in _release_test"
    )


def _create_queued_jobs(
    count: int, *, step_type: str = "generate_postmortem"
) -> tuple[list[UUID], list[UUID]]:
    incident_ids: list[UUID] = []
    job_ids: list[UUID] = []
    now = datetime.now(UTC)
    with SessionLocal() as session:
        for index in range(count):
            incident_id = uuid4()
            incident_ids.append(incident_id)
            session.add(
                IncidentRecord(
                    id=incident_id,
                    organization_id=DEFAULT_ORGANIZATION_ID,
                    fingerprint=f"release-contention:{incident_id}",
                    active_fingerprint=f"release-contention:{incident_id}",
                    status="detected",
                    service="checkout-api",
                    severity="critical",
                    summary=f"Release contention fixture {index}",
                    started_at=now,
                    detected_at=now,
                    version=1,
                )
            )
            session.flush()
            run = WorkflowStore(session).enqueue(
                incident_id,
                WorkflowType.INCIDENT_RESPONSE,
                step_type,
                f"release-contention:{incident_id}",
                trace_id=uuid4().hex,
            )
            job_id = session.scalar(
                select(WorkflowJobRecord.id).where(WorkflowJobRecord.workflow_run_id == run.id)
            )
            assert job_id is not None
            job_ids.append(job_id)
        session.commit()
    return incident_ids, job_ids


def _delete_incidents(incident_ids: list[UUID]) -> None:
    with SessionLocal() as session:
        session.execute(delete(IncidentRecord).where(IncidentRecord.id.in_(incident_ids)))
        session.commit()


def test_postgres_contenders_execute_one_workflow_attempt_once() -> None:
    incident_ids, job_ids = _create_queued_jobs(1)
    job_id = job_ids[0]
    handler_entered = Event()
    release_handler = Event()
    contenders_finished = Event()
    contenders_remaining = 8
    contenders_remaining_lock = Lock()
    handler_calls: list[UUID] = []
    handler_lock = Lock()
    outcomes: Queue[ExecutionDisposition] = Queue()
    errors: Queue[BaseException] = Queue()

    def handler(_session, job: WorkflowJobRecord, _fence) -> dict[str, str]:
        with handler_lock:
            handler_calls.append(job.id)
        handler_entered.set()
        if not release_handler.wait(timeout=10):
            raise TimeoutError("Timed out waiting to release the claimed workflow")
        return {"postmortem_id": str(uuid4())}

    def execute(worker_id: str, *, finished: Event | None = None) -> None:
        try:
            result = WorkflowEngine(
                SessionLocal,
                worker_id=worker_id,
                handlers={"generate_postmortem": handler},
                lease_seconds=30,
            ).execute(job_id)
            outcomes.put(result.disposition)
        except BaseException as error:
            errors.put(error)
            handler_entered.set()
        finally:
            if finished is not None:
                finished.set()

    owner = Thread(target=execute, args=("release-owner",), daemon=True)
    contender_count = contenders_remaining
    contender_barrier = Barrier(contender_count)

    def contend(index: int) -> None:
        nonlocal contenders_remaining
        try:
            contender_barrier.wait(timeout=5)
            execute(f"release-contender-{index}")
        except BaseException as error:
            errors.put(error)
        finally:
            with contenders_remaining_lock:
                contenders_remaining -= 1
                if contenders_remaining == 0:
                    contenders_finished.set()

    contenders = [
        Thread(target=contend, args=(index,), daemon=True) for index in range(contender_count)
    ]

    try:
        owner.start()
        assert handler_entered.wait(timeout=5), "Owning worker did not enter the handler"
        for contender in contenders:
            contender.start()
        assert contenders_finished.wait(timeout=10), (
            "A competing worker blocked behind an already committed active lease"
        )
        release_handler.set()
        owner.join(timeout=10)

        assert not owner.is_alive()
        assert all(not contender.is_alive() for contender in contenders)
        assert errors.empty(), [repr(error) for error in list(errors.queue)]
        assert handler_calls == [job_id]
        assert Counter(outcomes.queue) == {
            ExecutionDisposition.COMPLETED: 1,
            ExecutionDisposition.SKIPPED: contender_count,
        }

        with SessionLocal() as session:
            job = session.get(WorkflowJobRecord, job_id)
            assert job is not None
            assert job.status == "completed"
            assert job.attempt_count == 1
            event_counts = dict(
                session.execute(
                    select(WorkflowEventRecord.event_type, func.count())
                    .where(WorkflowEventRecord.workflow_run_id == job.workflow_run_id)
                    .group_by(WorkflowEventRecord.event_type)
                )
            )
            assert event_counts["workflow.step_started"] == 1
            assert event_counts["workflow.step_completed"] == 1
    finally:
        release_handler.set()
        owner.join(timeout=2)
        for contender in contenders:
            contender.join(timeout=2)
        _delete_incidents(incident_ids)


def test_postgres_skip_locked_drains_batch_without_duplicate_publish() -> None:
    batch_size = 24
    incident_ids, job_ids = _create_queued_jobs(batch_size)
    start = Barrier(4)
    outcomes: Queue[int] = Queue()
    errors: Queue[BaseException] = Queue()
    published_job_ids: list[UUID] = []
    publish_lock = Lock()
    now = datetime.now(UTC) + timedelta(seconds=1)

    class RecordingBroker:
        def publish(self, values: dict[str, str]) -> str:
            job_id = UUID(values["workflow_job_id"])
            with publish_lock:
                published_job_ids.append(job_id)
                sequence = len(published_job_ids)
            return f"release-batch-{sequence}"

        def message_exists(self, _message_id: str) -> bool:
            return True

    broker = RecordingBroker()

    def dispatch() -> None:
        try:
            start.wait(timeout=5)
            outcomes.put(
                OutboxDispatcher(SessionLocal, broker).dispatch_once(
                    now=now,
                    limit=batch_size,
                )
            )
        except BaseException as error:
            errors.put(error)

    dispatchers = [Thread(target=dispatch, daemon=True) for _ in range(4)]
    try:
        for dispatcher in dispatchers:
            dispatcher.start()
        for dispatcher in dispatchers:
            dispatcher.join(timeout=20)

        assert all(not dispatcher.is_alive() for dispatcher in dispatchers)
        assert errors.empty(), [repr(error) for error in list(errors.queue)]
        assert sum(outcomes.queue) == batch_size
        assert Counter(published_job_ids) == Counter({job_id: 1 for job_id in job_ids})

        with SessionLocal() as session:
            receipts = session.scalars(
                select(OutboxMessageRecord).where(OutboxMessageRecord.workflow_job_id.in_(job_ids))
            ).all()
            assert len(receipts) == batch_size
            assert all(receipt.publish_attempts == 1 for receipt in receipts)
            assert all(receipt.stream_message_id is not None for receipt in receipts)
    finally:
        for dispatcher in dispatchers:
            dispatcher.join(timeout=2)
        _delete_incidents(incident_ids)


def test_postgres_serializes_competing_admin_authority_changes() -> None:
    organization_id = uuid4()
    user_ids = [uuid4(), uuid4()]
    issuer = "https://release-gate.identity.example"
    with SessionLocal() as session:
        organization = OrganizationRecord(
            id=organization_id,
            slug=f"release-gate-{organization_id.hex}",
            name="Release Gate Organization",
        )
        session.add(organization)
        for index, user_id in enumerate(user_ids):
            user = UserRecord(
                id=user_id,
                issuer=issuer,
                subject=f"release-admin-{index}-{organization_id.hex}",
                email=f"release-admin-{index}-{organization_id.hex}@example.com",
                display_name=f"Release Admin {index}",
                is_active=True,
            )
            session.add(user)
            session.add(
                OrganizationMembershipRecord(
                    organization_id=organization_id,
                    user_id=user_id,
                    role=Role.ADMIN.value,
                    is_active=True,
                    version=1,
                )
            )
        session.commit()

    start = Barrier(2)
    outcomes: Queue[str] = Queue()
    errors: Queue[BaseException] = Queue()

    def demote(actor_id: UUID, target_id: UUID) -> None:
        try:
            start.wait(timeout=5)
            with SessionLocal() as session:
                try:
                    MembershipService(
                        session,
                        organization_id,
                        configured_issuer=issuer,
                    ).update(
                        target_id,
                        MembershipUpdateInput(
                            expected_version=1,
                            role=Role.RESPONDER,
                        ),
                        actor_user_id=actor_id,
                    )
                except ActorAuthorityChangedError:
                    outcomes.put("authority_changed")
                else:
                    outcomes.put("updated")
        except BaseException as error:
            errors.put(error)

    contenders = [
        Thread(target=demote, args=(user_ids[0], user_ids[1]), daemon=True),
        Thread(target=demote, args=(user_ids[1], user_ids[0]), daemon=True),
    ]
    try:
        for contender in contenders:
            contender.start()
        for contender in contenders:
            contender.join(timeout=10)

        assert all(not contender.is_alive() for contender in contenders)
        assert errors.empty(), [repr(error) for error in list(errors.queue)]
        assert sorted(outcomes.queue) == ["authority_changed", "updated"]
        with SessionLocal() as session:
            memberships = session.scalars(
                select(OrganizationMembershipRecord).where(
                    OrganizationMembershipRecord.organization_id == organization_id
                )
            ).all()
            active_admins = [
                membership
                for membership in memberships
                if membership.is_active and membership.role == Role.ADMIN.value
            ]
            assert len(active_admins) == 1
            assert sorted(membership.version for membership in memberships) == [1, 2]
            audit_count = session.scalar(
                select(func.count())
                .select_from(IdentityAuditEventRecord)
                .where(IdentityAuditEventRecord.organization_id == organization_id)
            )
            assert audit_count == 1
    finally:
        for contender in contenders:
            contender.join(timeout=2)
        with SessionLocal() as session:
            session.execute(
                delete(IdentityAuditEventRecord).where(
                    IdentityAuditEventRecord.organization_id == organization_id
                )
            )
            session.execute(
                delete(OrganizationMembershipRecord).where(
                    OrganizationMembershipRecord.organization_id == organization_id
                )
            )
            session.execute(
                delete(OrganizationRecord).where(OrganizationRecord.id == organization_id)
            )
            session.execute(delete(UserRecord).where(UserRecord.id.in_(user_ids)))
            session.commit()


def test_redis_dead_letter_survives_source_stream_recreation() -> None:
    namespace = f"pageragent.release.{uuid4().hex}"
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
    job_id = uuid4()
    try:
        assert client.ping()
        broker.ensure_group()
        source_id = broker.publish(
            {
                "workflow_job_id": str(job_id),
                "step_type": "deliver_collaboration_output",
            }
        )
        delivered = broker.read("release-crashed-worker", block_ms=100)
        assert len(delivered) == 1
        assert delivered[0].message_id == source_id

        dead_letter_id = broker.dead_letter(
            {
                **delivered[0].values,
                "source_message_id": source_id,
                "failure_code": "release_gate_permanent_failure",
            }
        )
        broker.acknowledge(source_id)
        client.delete(stream_name)
        broker.ensure_group()

        dead_letters = client.xrange(
            dead_letter_stream,
            min=dead_letter_id,
            max=dead_letter_id,
            count=1,
        )
        assert len(dead_letters) == 1
        assert dead_letters[0][1]["workflow_job_id"] == str(job_id)
        assert dead_letters[0][1]["source_message_id"] == source_id
        assert client.xpending_range(stream_name, consumer_group, "-", "+", 10) == []
    finally:
        client.delete(stream_name, dead_letter_stream)
        broker.client.close()
        client.close()
