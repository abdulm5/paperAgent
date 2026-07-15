from __future__ import annotations

import argparse
import logging
import os
import signal
import socket
from threading import Event
from time import monotonic
from uuid import UUID

from redis.exceptions import RedisError

from app.core.config import settings
from app.core.telemetry import configure_telemetry
from app.db.session import SessionLocal
from app.workflows.broker import RedisStreamBroker, StreamMessage, WorkflowBroker
from app.workflows.dispatcher import OutboxDispatcher
from app.workflows.engine import ExecutionDisposition, WorkflowEngine

logger = logging.getLogger(__name__)


def build_broker() -> RedisStreamBroker:
    return RedisStreamBroker(
        settings.redis_url,
        stream_name=settings.workflow_stream_name,
        consumer_group=settings.workflow_consumer_group,
        dead_letter_stream=settings.workflow_dead_letter_stream,
    )


def process_message(
    message: StreamMessage,
    *,
    broker: WorkflowBroker,
    engine: WorkflowEngine,
) -> None:
    raw_job_id = message.values.get("workflow_job_id")
    if raw_job_id is None:
        logger.warning("acknowledging malformed workflow message id=%s", message.message_id)
        broker.acknowledge(message.message_id)
        return
    try:
        job_id = UUID(raw_job_id)
    except ValueError:
        logger.warning("acknowledging invalid workflow job id=%s", raw_job_id)
        broker.acknowledge(message.message_id)
        return

    result = engine.execute(job_id)
    if result.disposition is ExecutionDisposition.SKIPPED:
        # A reclaimed delivery can collide with a healthy worker's database lease.
        # Keep the stream entry pending so it can wake a replacement if that worker
        # subsequently dies. Missing jobs are the only skipped state safe to discard.
        if result.error == "job no longer exists":
            broker.acknowledge(message.message_id)
        return
    if result.disposition is ExecutionDisposition.DEAD_LETTERED:
        broker.dead_letter(
            {
                **message.values,
                "source_message_id": message.message_id,
                "error": result.error or "attempt limit exhausted",
            }
        )
    broker.acknowledge(message.message_id)


def run_relay(*, once: bool = False, stop: Event | None = None) -> None:
    broker = build_broker()
    broker.ensure_group()
    dispatcher = OutboxDispatcher(SessionLocal, broker)
    stopping = stop or Event()
    while not stopping.is_set():
        published = dispatcher.dispatch_once()
        if once:
            return
        if published == 0:
            stopping.wait(settings.workflow_poll_interval_seconds)


def run_worker(*, once: bool = False, stop: Event | None = None) -> None:
    broker = build_broker()
    broker.ensure_group()
    worker_id = f"{socket.gethostname()}-{os.getpid()}"
    engine = WorkflowEngine(SessionLocal, worker_id=worker_id)
    stopping = stop or Event()
    last_reclaim = 0.0
    while not stopping.is_set():
        try:
            messages: list[StreamMessage] = []
            if monotonic() - last_reclaim >= settings.workflow_reclaim_idle_seconds:
                messages.extend(
                    broker.reclaim(
                        worker_id,
                        min_idle_ms=settings.workflow_reclaim_idle_seconds * 1_000,
                    )
                )
                last_reclaim = monotonic()
            if not messages:
                messages = broker.read(worker_id, block_ms=1_000)
            for message in messages:
                process_message(message, broker=broker, engine=engine)
        except RedisError as error:
            logger.warning("workflow transport unavailable error=%s", error)
            stopping.wait(settings.workflow_poll_interval_seconds)
            try:
                broker.ensure_group()
            except RedisError:
                pass
        if once:
            return


def main() -> None:
    parser = argparse.ArgumentParser(description="PagerAgent durable workflow runtime")
    parser.add_argument("mode", choices=("relay", "work"))
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    configure_telemetry()
    stopping = Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stopping.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    if args.mode == "relay":
        run_relay(once=args.once, stop=stopping)
    else:
        run_worker(once=args.once, stop=stopping)


if __name__ == "__main__":
    main()
