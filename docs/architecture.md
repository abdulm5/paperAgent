# PagerAgent architecture

## Design goal

PagerAgent supports an on-call engineer; it does not autonomously operate production systems. A recommendation is only useful if the operator can see why it was made, inspect its source evidence, and approve or reject it.

## Target request path

```text
Simulator + tenant ingest key → alert ingestion → incident + workflow + outbox (one DB commit)
                                                ↓
                          outbox relay → Redis Stream → leased worker
                                                ↓
          logs / metrics / traces / deploys / runbooks
                             ↓
                    evidence normalization
                             ↓
            deterministic rankers and retrieval services
                 ↓              ↓
          cross-signal cause ranking + evaluation gates
                             ↓
       structured synthesis constrained by cited evidence
                             ↓
       policy check → human approval → typed executor
                             ↓
                 recovery canaries → resolution
                             ↓
        grounded postmortem → revision log → final record
```

The LLM is deliberately downstream of evidence gathering. It can summarize, compare hypotheses, and draft communication. It cannot establish facts without attached evidence, and it cannot perform production writes.

## Components and ownership

| Component | Current responsibility | Later responsibility |
| --- | --- | --- |
| `simulator/` | Reproduce code, configuration, and upstream-dependency failures | Add production-like distributed services |
| `backend/` | Authenticate identities, enforce organization membership and RBAC, persist incidents and workflows, relay a transactional outbox, execute database-leased jobs, rank causes, validate grounded briefs, enforce approval policy, verify recovery, and version postmortems | Add production evidence and action adapters |
| PostgreSQL | Own incident state, workflow runs and jobs, ordered workflow events, leases, retry clocks, results, and unpublished outbox messages | Move to a managed high-availability deployment |
| Redis Streams | Wake workers with at-least-once delivery, consumer groups, pending-message reclaim, and a dead-letter stream | Move to a managed transport and tune retention |
| `frontend/` | Present the signed organization scope, exact permission receipt, evidence, causal rankings, evaluation gates, workflow delivery receipts, recovery receipts, and postmortem document control | Add connector and membership administration |
| `runbooks/` | Supply versioned procedures to hybrid retrieval | Source grounded mitigation steps |
| `scenarios/` | Define versioned simulation, ground truth, adversarial cases, and thresholds | Grow a reviewed incident corpus |
| `evals/` | Score cause ranking, retrieval, impact, traceability, action safety, authority, and resilience | Add model-provider comparison and historical trends |

## First vertical slice

The first full incident is `checkout-validation-bug`. It has one service, one intentionally faulty deployment, a known alert threshold, one rollback runbook, and known impact. Narrow scope lets us verify each inference before we generalize it.

## Data boundaries

PostgreSQL separates current incident state from immutable alert deliveries and append-only lifecycle events. The API updates current state and appends its matching event in one transaction.

The organization is the human-access boundary. A signed session proves the PagerAgent user ID and
selected organization, but each request reloads the active user, membership, and role from
PostgreSQL. Role-to-permission mapping is explicit, and incident-owned reads join or filter through
`incidents.organization_id`. Cross-organization identifiers return `404`. The same active alert
fingerprint is unique only inside one organization, preventing monitors for two tenants from
deduplicating into the same incident. Workflow jobs remain global internal work, but workers derive
their incident and organization from PostgreSQL instead of trusting the Redis payload.

Local browser sessions use an HTTP-only `SameSite=Strict` cookie. Unsafe cookie-authenticated
requests also require a per-session CSRF header, while local CLI demos use the same signed session
as a Bearer credential. The production-facing OIDC exchange proves external identity through a
fixed issuer, audience, JWKS endpoint, and algorithm allow-list before PagerAgent checks locally
provisioned membership. Provider-specific browser redirect, callback, and PKCE wiring is a Phase 8B
integration rather than an implicit claim of the included local dashboard.
Human audit actors are derived from that principal. Monitoring uses a separate tenant-bound machine
ingest key; the alert body cannot select its organization. Telemetry collection accepts only
server-configured origins and applies URL, redirect, DNS, and IP-range policy before fetching.

PostgreSQL is also the workflow source of truth. A workflow enqueue writes the run, initial job, ordered workflow events, and outbox row in the same transaction as the business transition that caused it. Redis does not decide whether work exists; it transports a job identifier after the transaction commits. Losing Redis therefore delays work but does not erase it. In addition to unpublished rows, the relay periodically checks the exact stream ID on the latest stale receipt for a due nonterminal job; it republishes only when that entry is missing. Publication and repair can both create duplicates, so workers treat duplicate delivery as expected.

Each worker claim locks the job row, records a worker identity and expiring lease, increments the attempt count, and commits before running the handler. A second worker skips an active lease and may take over after expiry only when the attempt budget remains; otherwise it dead-letters without another handler invocation. Core services recheck that worker/attempt token under the job lock at domain commit time, so a superseded worker rolls back stale writes. Completed jobs are terminal no-ops on redelivery. Failed attempts use a persisted exponential-backoff clock and another outbox dispatch; the final allowed failure marks both job and workflow `dead_lettered` and copies the stream message to a Redis dead-letter stream.

The evidence layer stores collection snapshots as content-hashed artifacts and stores derived clusters, causal candidates, commit candidates, and runbook matches separately. Each investigation captures its collector, clusterer, ranker, and retriever versions plus an input hash. Each derived record carries evidence identifiers, so a score can be traced back to telemetry, dependency health, configuration history, deploy history, commit metadata, and the runbook corpus.

Provider interfaces isolate evidence collection from analysis. The demo uses HTTP telemetry, a fixture-backed Git provider, and local Markdown runbooks; production integrations can replace those providers without changing the deterministic ranking contracts.

The synthesis provider is also replaceable. With an API key, the OpenAI adapter uses the Responses API and a strict structured-output schema. Without a key, the deterministic provider creates the same typed contract for offline demos. Both outputs pass through the same citation validator. The model produces language only; a deterministic policy derives the action envelope from the top causal signal and matching safety runbook.

Approval and execution are separate durable records. An approval is committed before any external write. The checkout simulator executor accepts only typed rollback or feature-flag-disable envelopes for `checkout-api`, uses a proposal-scoped idempotency key, then sends a canary cohort that includes digital wallets. Upstream-dependency causes always produce `escalate_only`, and low-confidence or missing evidence cannot unlock a write. A successful HTTP call is insufficient: telemetry verification must pass before the incident moves from investigating to mitigated.

In durable mode, approval and mitigation-workflow enqueueing share one transaction and the HTTP request returns before execution. Investigation and proposal records carry a unique nullable workflow-job identity, postmortems retain their one-per-incident constraint, and mitigation executions retain a unique proposal and idempotency key. These guards turn at-least-once handler invocation into effectively-once persisted results. The simulator caches an idempotency key's mutation result and rejects reuse for another mutation, which covers the demo's external-write replay window. A production action adapter must provide a durable downstream idempotency contract or reconcile current target state; exactly-once delivery cannot be created by Redis or PostgreSQL alone.

Postmortem generation is gated twice: the incident must be resolved, and it must contain a verified mitigation execution. The narrative generator can draft prose and prevention work, but the service constructs the timeline directly from append-only incident events and rejects citations outside the incident's evidence graph. Each generation, operator edit, and finalization stores an immutable snapshot with an increasing version. Optimistic version checks prevent silent overwrites, and finalization permanently closes the editing path. Operator edits retain their original evidence bindings and exact timeline; the revision author and reason make the human-authored change explicit.

## Phase 3 investigation path

```text
threshold alert
    → HTTP telemetry snapshot + deployment history
    → failure signature clustering
    → versioned Git candidate provider
    → weighted, explainable commit ranker
    → metadata + lexical + hashed-vector runbook retrieval
    → persisted evidence ledger and dashboard citations
    → scenario ground-truth quality gate
```

## Phase 4 mitigation path

```text
completed investigation
    → structured grounded brief
    → claim-to-evidence validation
    → typed rollback envelope (still read-only)
    → append-only human approve/reject decision
    → allow-list policy check
    → idempotent simulator rollback
    → digital-wallet recovery canaries
    → verified telemetry → incident mitigated
```

## Phase 5 learning path

```text
verified mitigation + resolved lifecycle
    → structured blameless narrative
    → citation allow-list validation
    → exact timeline assembled from incident events
    → persisted draft + immutable v1 snapshot
    → optimistic, attributed operator revisions
    → explicit team-review acknowledgment
    → locked final record + Markdown export
```

## Phase 6 reliability path

```text
versioned scenario contract
    → deterministic telemetry fixture or live simulator activation
    → cluster + deploy + dependency + configuration evidence
    → cross-signal causal ranking
    → cause-specific runbook and action policy
    → adversarial probes: red herring / citation / missing evidence / low confidence
    → per-scenario metrics + aggregate gates
    → API scorecard + operator calibration matrix
```

## Phase 7 durable orchestration path

```text
FastAPI process
    business state + workflow run + job + workflow events + outbox
                              │ one PostgreSQL transaction
                              ▼
outbox-relay process ── XADD ──→ Redis Stream / consumer group
                                      │ XREADGROUP or XAUTOCLAIM
                                      ▼
workflow-worker process
    DB row lock → attempt + lease commit → handler → result/event commit → XACK
                     │ failure
                     ├─ retry clock + new outbox dispatch
                     └─ attempt limit → DB dead_lettered + Redis DLQ copy

FastAPI SSE endpoint ← globally ordered PostgreSQL workflow events
        dashboard ← EventSource replay + periodic REST reconciliation
```

The API, relay, and worker are separate processes in Docker Compose. The API can accept and commit work while the relay or worker is unavailable. The relay reads due unpublished outbox rows and stale receipts for the latest nonterminal job delivery with `FOR UPDATE SKIP LOCKED`; before repairing a stale receipt it uses the saved stream ID to distinguish an intact Redis entry from actual transport loss. The worker consumes the `pageragent-workers` group and periodically uses `XAUTOCLAIM` for messages abandoned by another consumer. A lease collision remains pending instead of being acknowledged, and core services verify the worker/attempt fence under the job lock in each domain commit. Redis append-only persistence is enabled for the local stack, but correctness still comes from PostgreSQL state, verified delivery repair, and replay-safe handlers.

### Failure windows

| Failure window | Durable behavior |
| --- | --- |
| Before the API transaction commits | Incident and workflow changes roll back together; there is no orphan job. |
| After commit, before Redis publish | The unpublished outbox row remains due and the relay publishes it after restart. |
| After `XADD`, before the relay records `published_at` | The relay may publish a duplicate; the job identity and terminal-state check absorb it. |
| After `published_at`, but the Redis stream is lost | A stale-receipt scan checks the exact saved stream ID, detects its absence, and republishes the latest due nonterminal job from PostgreSQL. |
| After stream delivery, before a database claim | The message remains pending in the consumer group and can be reclaimed. |
| A reclaim collides with another worker's active lease | The delivery remains pending; it is acknowledged only after a persisted terminal or retry disposition. |
| After claim commit, before handler completion | The job remains `running`; after its database lease expires, another worker records lease recovery only when an attempt remains. At the limit it dead-letters without invoking the handler again. |
| A superseded worker reaches a domain commit | Its worker/attempt token fails under the locked job row and the stale domain transaction rolls back. |
| After a handler's domain record commits, before the workflow job completes | The unique workflow-job link or domain uniqueness rule returns the existing result on replay. |
| After an external mutation, before its result commits | The same proposal-scoped key is retried; the simulator returns its cached mutation result. Production adapters must offer equivalent durable idempotency or reconciliation. |
| Browser disconnects during an update | A transaction-scoped PostgreSQL event gate keeps global IDs in commit order, so SSE resumes safely after `Last-Event-ID`; periodic REST reads still reconcile full snapshots. |

### Trace and operator correlation

The API creates a server span, returns its 32-character trace ID in `X-Trace-ID`, and stores that ID on the workflow run. A worker reconstructs the trace context and creates a consumer span named for the workflow step with job, attempt, and worker attributes. `OTEL_CONSOLE_EXPORTER=true` prints spans in the current local implementation; an OTLP collector/exporter remains production integration work. The dashboard shows the trace prefix alongside lease owner, attempt count, retry time, job result, and ordered events, connecting the UI receipt to logs and spans without treating Redis as an audit database.

## Phase 8A identity and tenant path

```text
local persona or externally obtained OIDC token
                  │ verify / exchange identity
                  ▼
       PagerAgent signed session
                  │ reload on every request
                  ▼
active user + organization membership + current role
                  │ explicit permission
                  ▼
organization-scoped incident aggregate
        │          │          │          │
 investigation  proposal  postmortem  workflow SSE

monitoring process ── tenant-bound ingest key ──► alert endpoint
```

The dashboard's authority receipt shows the current principal, organization, role, and exact grants.
That visibility is explanatory rather than authoritative: the backend independently checks every
permission and ownership predicate. Switching organizations closes the existing event stream and
clears every incident-derived React state before loading the next scope, so the old tenant cannot
remain visible under a new label. The workflow stream revalidates membership before each event poll
and terminates when the signed session expires.
