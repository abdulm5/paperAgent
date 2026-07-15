# Milestone 7: durable workflow orchestration

## Outcome

PagerAgent's long-running work now survives API responses, process restarts, duplicate delivery, and transient provider failures. Alert ingestion atomically stores an incident-response workflow with the new incident. Investigation completion durably queues proposal generation. Human approval stores a separate mitigation workflow before any external write, and resolution stores a postmortem workflow. None of those paths depends on FastAPI's in-process background-task queue.

PostgreSQL owns each workflow run, idempotent step job, attempt count, retry clock, worker lease, result, globally ordered event, and outbox delivery receipt. A dedicated relay publishes due outbox records to a Redis Stream, periodically checks the exact stream ID saved on a stale receipt, and republishes only when that entry is actually missing. A separate worker consumes through a group, commits a database lease before invoking a handler, and acknowledges the stream message only after the workflow engine reaches a persisted terminal or retry disposition.

The operator dashboard renders this as a flight recorder rather than a spinner. It shows queue, transport, and worker stages; current step; attempts; lease owner and expiry; trace prefix; failures; job results; and the ordered event history. Fresh browsers initialize from a REST snapshot, a global integer event cursor lets the server-sent event endpoint replay updates after reconnect, and periodic REST reconciliation protects against a stale browser snapshot. PostgreSQL event publishers share a transaction-scoped advisory gate, so event IDs become visible in commit order instead of letting a late lower ID fall behind the SSE cursor.

## Process topology

```text
backend API
  └─ PostgreSQL transaction: domain change + workflow + job + events + outbox
         └─ outbox-relay process
                └─ Redis Stream: pageragent.workflows / pageragent-workers
                       └─ workflow-worker process
                              ├─ investigate → generate_proposal
                              ├─ execute_mitigation
                              └─ generate_postmortem

backend SSE endpoint ← PostgreSQL workflow_events ← dashboard EventSource
```

The relay and worker use the same backend image but run different commands: `python -m app.workflows.worker relay` and `python -m app.workflows.worker work`. This keeps web-request capacity, transport publication, and expensive incident analysis independently restartable. Redis has append-only persistence enabled locally, but PostgreSQL remains sufficient to reconstruct whether work is queued, leased, retrying, completed, or dead-lettered.

## Delivery and safety semantics

1. Enqueueing is transactional. If the incident transaction rolls back, the workflow, job, events, and outbox roll back with it.
2. Redis Streams is at-least-once transport. Duplicate messages are valid behavior, not an exceptional state.
3. A deterministic workflow dedupe key prevents the same business trigger from creating another run.
4. Each logical step has a unique idempotency key. Completed jobs return a terminal no-op when redelivered.
5. A worker row-locks the job, increments its attempt, and commits an expiring database lease before calling the handler.
6. Redis `XAUTOCLAIM` recovers abandoned pending messages; an expired database lease permits a replacement worker to take ownership and records `workflow.lease_recovered`.
7. Every core handler verifies its worker/attempt fencing token under the job row lock in the same transaction as each domain commit. A superseded worker rolls back its stale writes, and its stream delivery stays pending.
8. A stale receipt on the latest due nonterminal dispatch is checked with an exact Redis `XRANGE`; a healthy entry is left alone, while a missing entry is republished from PostgreSQL.
9. Failed handlers schedule exponential backoff in PostgreSQL and create another outbox dispatch. Exhausting `max_attempts`, including through process crashes rather than caught failures, marks the job and run `dead_lettered` before another handler invocation and copies the stream delivery to the Redis dead-letter stream.
10. Investigation and proposal results link uniquely to their workflow job; postmortem and execution domain constraints make a replay return the existing result.
11. Approved mutations reuse `proposal-{proposal_id}`. The simulator returns the cached response for the same key and rejects the key if it names a different mutation.
12. Recovery canaries remain mandatory. Idempotent delivery does not weaken causal policy, human approval, executor allow-listing, or verification gates.

This is effectively-once behavior at the domain boundary, built on at-least-once execution. It is not a claim of exactly-once distributed delivery. The simulator's mutation-key cache is process-local and intentionally scoped to the demo; a production action adapter must use a durable provider idempotency key or reconcile observed target state before replay.

## Failure windows

| Crash location | What happens next |
| --- | --- |
| API dies before commit | No partial workflow survives. |
| API commits while relay is down | The outbox row remains unpublished until the relay returns. |
| Relay publishes and dies before saving the receipt | The row may be published again; duplicate delivery reaches the same job ID. |
| Redis loses a delivery after its receipt was saved | The relay verifies that the saved stream ID is absent, then republishes the latest dispatch while its PostgreSQL job remains due and nonterminal. |
| Worker dies before claim commit | The stream message remains reclaimable without a running database lease. |
| Worker dies after claim commit | The job remains running until its lease expires; another worker records takeover only if an attempt remains, otherwise it dead-letters without rerunning the handler. |
| A reclaim reaches a job with an active lease | The message remains pending so a later reclaim can recover it if the lease owner dies. |
| A slow worker is superseded before its domain commit | The commit-time worker/attempt fence rejects and rolls back the stale transaction. |
| Investigation/proposal/postmortem commits before job completion | Replay loads the existing domain result and then completes the job. |
| Mutation succeeds before execution receipt commits | Replay uses the same proposal-scoped mutation key, then reruns recovery verification. |
| Browser disconnects | `Last-Event-ID` resumes from a commit-ordered global PostgreSQL event cursor; REST reconciliation refreshes full workflow snapshots. |

## Trace correlation

The API wraps each request in an OpenTelemetry server span, returns `X-Trace-ID`, and persists that trace ID on any workflow created by the request. The worker reconstructs the persisted trace context and creates a consumer span named `workflow.<step>` with workflow job, step, attempt, and worker attributes. This connects a short HTTP request to work performed later in another process.

The local implementation can export spans to stdout with `OTEL_CONSOLE_EXPORTER=true`. It does not yet configure an OTLP collector or tracing backend; that belongs with the next production-integration phase.

## Verification

The orchestration suite exercises the reliability contracts directly:

- enqueue commit and rollback prove the domain/outbox atomicity boundary;
- duplicate workflow keys prove trigger deduplication;
- a missing stream entry is republished from its PostgreSQL receipt, while a healthy stale receipt produces no duplicate publish or event;
- duplicate delivery of a completed job invokes the handler once;
- investigation completion creates the proposal job and outbox record durably;
- fake-clock failures prove `2s`, `4s`, and subsequent exponential retry scheduling;
- attempt exhaustion records one database dead-letter event and one Redis DLQ message;
- an expired lease owned by a simulated crashed worker is recovered by a replacement worker, while an expired final attempt dead-letters without rerunning;
- a reclaim collision with an active lease remains pending instead of being discarded;
- a superseded worker cannot commit even a domain mutation made before workflow completion;
- opt-in integration tests use real PostgreSQL sessions for competing relay `SKIP LOCKED` claims, commit-ordered event publication, and the publish/worker receipt race, plus real Redis for exact receipt checks, group recreation, `XAUTOCLAIM`, and `XACK` behavior;
- global event cursor reads are strictly ordered for SSE replay and stale or equal frontend versions cannot replace a newer snapshot;
- simulator tests prove same-key mutation replay and conflicting-key rejection;
- frontend tests prove REST fallback, live SSE application, and EventSource cleanup.

## Interview explanation

Lead with the dual-write problem: “Creating an incident in PostgreSQL and then separately publishing a Redis job leaves a crash window.” PagerAgent closes that window with a transactional outbox. The API writes business state and the intent to perform work in one commit; publication can happen later and can safely happen more than once.

Then separate transport guarantees from effect guarantees. Redis provides at-least-once delivery and consumer failover. PostgreSQL leases determine who may run a job, commit-time fencing prevents a superseded attempt from saving domain state, unique job/domain identities absorb crash-window replays, and the external adapter uses an idempotency key plus state verification. That combination gives effectively-once incident effects without making an impossible exactly-once claim.

Finally show why the operator can trust the mechanism. The workflow event ledger is replayable, every retry exposes its error and next time, dead letters are visible instead of silently dropped, and the trace ID follows the request into the worker. This phase adds a distributed-systems story—transactions, delivery semantics, leases, replay, backoff, and observability—without moving operational authority away from the human approver.

## Demo narration

1. Start the full Compose stack and open the dashboard's durable dispatch recorder.
2. Run `./scripts/run-durability-demo.sh` in another terminal. It stops the relay, worker, and Redis before triggering the incident.
3. Show that the API creates one incident and a queued workflow while transport and execution are unavailable, then proves the unpublished intent exists in PostgreSQL.
4. Follow the script as it restarts the API and restores Redis. It saves a publish receipt, deletes that stream, proves the saved stream ID is absent, and runs the PostgreSQL repair scan; point out `publish_attempts = 2` before any worker resumes.
5. Show the lease owner and attempt appear after the repaired delivery, then point out the injected terminal duplicate and the assertion that only one investigation and proposal exist. Run with `--approve` to include the separate mitigation workflow, one idempotent simulator mutation, and recovery canaries.
6. Show the same trace prefix across the workflow receipt and console spans with `OTEL_CONSOLE_EXPORTER=true`.
7. Explain the forced-failure tests: duplicate delivery is a no-op, delays grow exponentially, an expired lease is recovered, and the final failure moves to the DLQ.
8. Resolve the incident and show postmortem generation travel through the same durable route.
9. End with the honest boundary: PostgreSQL can preserve intent and state, but a production action provider must also honor durable idempotency or support reconciliation.

Production evidence/action adapters, authentication and RBAC, team scoping, secret management, and managed trace export remain the next phase.
