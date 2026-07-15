# ADR 0008: PostgreSQL is the workflow source of truth

## Status

Accepted.

## Context

Incident investigation, proposal generation, approved mitigation, and postmortem generation outlive the HTTP requests that trigger them. Scheduling those operations with process-local background callbacks loses work when the API exits. Writing domain state to PostgreSQL and then publishing a Redis message creates a different failure: the process can die after either side of the dual write, leaving durable state without work or work without its durable cause.

Redis Streams provides consumer groups, pending-message reclaim, and efficient worker wakeups, but using the stream as canonical workflow state would split attempts, leases, errors, and audit history from the incident record. It would also make stream retention or Redis loss a correctness event rather than a transport outage.

## Decision

PostgreSQL is authoritative for workflow existence and progress. The transaction that performs a triggering domain change also inserts the workflow run, first job, initial workflow events, and outbox message. Workflow jobs store their status, attempt and maximum-attempt counts, availability time, lease owner and expiry, last error, result, and unique idempotency key. Workflow events use a global integer ID for replay plus a unique per-workflow sequence for local ordering.

A dedicated outbox-relay process selects due unpublished rows and uses `XADD` to publish job identifiers to Redis Streams. For a stale latest receipt on a due nonterminal job, it checks the exact saved stream ID and republishes only when that entry is absent. A dedicated workflow-worker process consumes through a group, uses `XAUTOCLAIM` for abandoned messages, and consults PostgreSQL before every handler invocation. The worker commits a database lease before executing; an unexpired lease blocks another worker, while an expired lease permits recorded takeover only while the attempt budget remains. A reclaimed message that collides with an active or superseding lease remains pending instead of being discarded.

The relay marks publication only after Redis returns a stream message ID. A crash after `XADD` and before that commit can publish a duplicate; a missing-entry repair can intentionally do the same. The design therefore guarantees at-least-once transport. Duplicate or reclaimed delivery of a completed job is a no-op. Failures schedule exponential backoff through another durable outbox row. Reaching the attempt limit—whether through returned failures or workers repeatedly crashing after claim—records `dead_lettered` in PostgreSQL before another invocation and copies the delivery to a Redis dead-letter stream for operational inspection.

The lease is also a commit fence, not merely a scheduling hint. Core handlers carry the claimed worker ID and attempt number into their service transaction. Immediately before each domain commit, the service locks the workflow job and verifies that the same unexpired attempt still owns it; a superseded worker rolls back instead of overwriting a newer result. A transaction-scoped PostgreSQL advisory lock serializes workflow-event allocation after the run lock, ensuring the global SSE cursor follows commit order across concurrent runs.

Side effects use layered idempotency rather than an exactly-once claim:

- workflow runs deduplicate the business trigger;
- jobs identify one logical step;
- investigations and proposals link uniquely to their workflow job;
- postmortems and mitigation executions retain domain uniqueness constraints;
- mitigation retries reuse a proposal-scoped key at the action provider;
- recovery verification checks observed behavior after the mutation.

The API serves live status from PostgreSQL workflow events. Server-sent events resume after the global event ID, and the dashboard periodically reconciles complete REST snapshots. Redis is not the SSE audit log.

The originating OpenTelemetry trace ID is persisted on the workflow run. Worker consumer spans reuse that trace ID and attach the job ID, step, attempt, and worker identity, allowing asynchronous work in separate processes to be correlated with its HTTP trigger.

## Consequences

The API can commit new work while Redis, the relay, or all workers are unavailable. Recovery requires restarting processes, not reconstructing missing intent. Operators can distinguish queued, running, retrying, completed, and dead-lettered work from durable records and can replay UI events after a disconnect.

Delivery remains at least once. Commit fencing protects PostgreSQL effects but cannot prevent a worker from dying immediately after an external provider accepts a mutation. External action adapters must therefore remain replay-safe. The current simulator caches mutation keys only for its process lifetime; production adapters must supply durable provider idempotency or state reconciliation.

The design adds PostgreSQL writes and polling, Redis operational dependencies, exact stream-entry checks, explicit lease and repair-interval tuning, retained workflow history, a globally serialized event-publication gate, and a relay process. Those costs are accepted because they make the correctness boundary inspectable and testable. Redis append-only persistence improves the local demo but is not required for correctness because unpublished, missing-delivery, and retryable work remain represented in PostgreSQL.

SSE currently polls the workflow event table and the OpenTelemetry implementation optionally exports to the console. High-fan-out event distribution, event retention policies, an OTLP collector, production action/evidence adapters, authentication, and RBAC remain future production work.

## Alternatives considered

- **FastAPI background tasks:** simple, but tied to one web process and unable to recover a lost callback.
- **Publish directly to Redis after commit:** leaves a committed-domain-state/unpublished-job gap.
- **Publish to Redis before commit:** lets a worker observe work whose domain transaction may roll back.
- **Redis as the workflow database:** makes transport retention and availability part of business-state correctness.
- **Assume exactly-once messaging:** hides the unavoidable crash window around external effects instead of requiring idempotency and reconciliation.
