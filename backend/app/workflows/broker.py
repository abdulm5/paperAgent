from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from threading import Lock
from typing import Protocol

from redis import Redis
from redis.exceptions import ResponseError


@dataclass(frozen=True)
class StreamMessage:
    message_id: str
    values: dict[str, str]


class WorkflowBroker(Protocol):
    def ensure_group(self) -> None: ...

    def publish(self, values: dict[str, str]) -> str: ...

    def message_exists(self, message_id: str) -> bool: ...

    def read(self, consumer: str, *, block_ms: int = 1_000) -> list[StreamMessage]: ...

    def reclaim(self, consumer: str, *, min_idle_ms: int) -> list[StreamMessage]: ...

    def acknowledge(self, message_id: str) -> None: ...

    def dead_letter(self, values: dict[str, str]) -> str: ...


class RedisStreamBroker:
    """At-least-once Redis Streams transport.

    PostgreSQL owns workflow state. Redis only wakes workers, so duplicate stream
    messages and consumer failover are expected and handled by database leases.
    """

    def __init__(
        self,
        redis_url: str,
        *,
        stream_name: str,
        consumer_group: str,
        dead_letter_stream: str,
    ) -> None:
        self.client = Redis.from_url(redis_url, decode_responses=True)
        self.stream_name = stream_name
        self.consumer_group = consumer_group
        self.dead_letter_stream = dead_letter_stream

    def ensure_group(self) -> None:
        try:
            self.client.xgroup_create(
                self.stream_name,
                self.consumer_group,
                id="0-0",
                mkstream=True,
            )
        except ResponseError as error:
            if "BUSYGROUP" not in str(error):
                raise

    def publish(self, values: dict[str, str]) -> str:
        return str(self.client.xadd(self.stream_name, values))

    def message_exists(self, message_id: str) -> bool:
        entries = self.client.xrange(
            self.stream_name,
            min=message_id,
            max=message_id,
            count=1,
        )
        return bool(entries and str(entries[0][0]) == message_id)

    def read(self, consumer: str, *, block_ms: int = 1_000) -> list[StreamMessage]:
        try:
            response = self.client.xreadgroup(
                self.consumer_group,
                consumer,
                {self.stream_name: ">"},
                count=10,
                block=block_ms,
            )
        except ResponseError as error:
            if "NOGROUP" not in str(error):
                raise
            self.ensure_group()
            return []
        return self._messages(response)

    def reclaim(self, consumer: str, *, min_idle_ms: int) -> list[StreamMessage]:
        try:
            response = self.client.xautoclaim(
                self.stream_name,
                self.consumer_group,
                consumer,
                min_idle_ms,
                start_id="0-0",
                count=10,
            )
        except ResponseError as error:
            if "NOGROUP" not in str(error):
                raise
            self.ensure_group()
            return []
        raw_messages = response[1] if len(response) > 1 else []
        return [
            StreamMessage(message_id=str(message_id), values=dict(values))
            for message_id, values in raw_messages
        ]

    def acknowledge(self, message_id: str) -> None:
        self.client.xack(self.stream_name, self.consumer_group, message_id)

    def dead_letter(self, values: dict[str, str]) -> str:
        return str(self.client.xadd(self.dead_letter_stream, values))

    @staticmethod
    def _messages(response: list[object]) -> list[StreamMessage]:
        messages: list[StreamMessage] = []
        for _stream, entries in response:
            messages.extend(
                StreamMessage(message_id=str(message_id), values=dict(values))
                for message_id, values in entries
            )
        return messages


class InMemoryWorkflowBroker:
    """Deterministic broker used by unit and chaos tests."""

    def __init__(self) -> None:
        self._ready: deque[StreamMessage] = deque()
        self._pending: dict[str, StreamMessage] = {}
        self._stream_entries: dict[str, StreamMessage] = {}
        self._dead_letters: list[StreamMessage] = []
        self._next_id = 1
        self._lock = Lock()

    def ensure_group(self) -> None:
        return None

    def publish(self, values: dict[str, str]) -> str:
        with self._lock:
            message_id = f"{self._next_id}-0"
            self._next_id += 1
            message = StreamMessage(message_id, dict(values))
            self._stream_entries[message_id] = message
            self._ready.append(message)
            return message_id

    def message_exists(self, message_id: str) -> bool:
        with self._lock:
            return message_id in self._stream_entries

    def read(self, consumer: str, *, block_ms: int = 1_000) -> list[StreamMessage]:
        del consumer, block_ms
        with self._lock:
            if not self._ready:
                return []
            message = self._ready.popleft()
            self._pending[message.message_id] = message
            return [message]

    def reclaim(self, consumer: str, *, min_idle_ms: int) -> list[StreamMessage]:
        del consumer, min_idle_ms
        with self._lock:
            return list(self._pending.values())

    def acknowledge(self, message_id: str) -> None:
        with self._lock:
            self._pending.pop(message_id, None)

    def dead_letter(self, values: dict[str, str]) -> str:
        with self._lock:
            message_id = f"dlq-{self._next_id}-0"
            self._next_id += 1
            self._dead_letters.append(StreamMessage(message_id, dict(values)))
            return message_id

    def simulate_stream_loss(self) -> None:
        """Delete the in-memory transport stream for durability tests."""

        with self._lock:
            self._ready.clear()
            self._pending.clear()
            self._stream_entries.clear()

    @property
    def dead_letters(self) -> list[StreamMessage]:
        return list(self._dead_letters)
