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
| `backend/` | Complete hosted OIDC login, enforce revocable organization membership and RBAC, custody typed connector credentials, collect bounded GitHub and Prometheus evidence, persist incidents and workflows, relay a transactional outbox, execute database-leased jobs, rank causes, validate grounded briefs, enforce approval policy, deliver approved collaboration outputs, verify recovery, and version postmortems | Add backend-specific log/trace adapters |
| PostgreSQL | Own single-use login transactions, revocable sessions, versioned memberships and identity audits, connector metadata and encrypted envelopes, custody events, verified provider receipts, content-hashed evidence, incident state, workflows, leases, results, and unpublished outbox messages | Move to a managed high-availability deployment |
| Redis Streams | Wake workers with at-least-once delivery, consumer groups, pending-message reclaim, and a dead-letter stream | Move to a managed transport and tune retention |
| `frontend/` | Present the signed organization scope, exact permission receipt, audited membership administration, connector custody ledger, evidence, causal rankings, evaluation gates, workflow and collaboration delivery receipts, recovery receipts, and postmortem document control | Add hosted deployment administration |
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
as a Bearer credential. Hosted login is a backend-for-frontend OIDC Authorization Code flow with
PKCE `S256`: PagerAgent creates a single-use encrypted login transaction, binds it to a temporary
HttpOnly browser cookie, consumes it before code exchange, verifies issuer/audience/azp/nonce/time,
and redirects only to the configured frontend. Provider tokens never enter React or the application
session. Every signed PagerAgent JWT identifies an unexpired, unrevoked PostgreSQL session row;
current membership and role are still reloaded on every request.
Human audit actors are derived from that principal. Monitoring uses a separate tenant-bound machine
ingest key; the alert body cannot select its organization. Telemetry collection accepts only
server-configured origins and applies URL, redirect, DNS, and IP-range policy before fetching.

Connectors use the same organization boundary and add a narrower credential-custody boundary.
Incident commanders can read non-secret connector state; only administrators can write, validate,
or enable it. Provider schemas divide ordinary configuration from write-only credential fields.
Each credential revision is encrypted under a fresh random data-encryption key. Local development
wraps that key with an exact-version AES-GCM key; production obtains and wraps it through AWS KMS
`GenerateDataKey` under an exact key ARN and immutable tenant/connector/revision encryption context.
Authenticated associated data binds the payload to that same context, so copied or edited rows fail
closed. PagerAgent does not configure static AWS credentials; a production deployment is designed
to supply the SDK credential chain through provider workload identity. KMS calls run outside
database transactions and their results pass connector/credential revision checks before commit or
runtime use. API responses and allowlisted audit payloads reveal field presence and revision only.
The Phase 9A generic validation checked provider schema and vault integrity without making an
external request. GitHub, Prometheus, and Slack validators now snapshot the connector and credential
revision, end the database transaction, perform their bounded provider handshake, then lock and
record the result only if both revisions are still current. GitHub issue-write authorization is
tested only when that explicit connector capability is enabled.

Prometheus connector origins must exactly match the server allowlist; requests append only fixed
API paths, reject redirects, ignore environment proxies, and use ordinary TLS verification.
Unlike the older telemetry-source policy, this adapter does not pre-resolve or pin DNS answers.
Exact origin validation is not a complete SSRF boundary: production deployment must enforce egress
policy that permits the intended Prometheus destination and blocks metadata or unrelated internal
destinations.

GitHub webhooks use a separate public authentication boundary. The connector UUID routes the
delivery but does not authenticate it. PagerAgent caps the raw request, verifies
`X-Hub-Signature-256` with the encrypted webhook secret before parsing JSON, binds repository and
installation identity to the connector, rechecks the locked connector and credential revisions,
and stores only bounded normalized fields plus the body hash and authenticating revisions. A
composite tenant/connector foreign key enforces ownership at rest. PostgreSQL uniqueness on
connector and delivery ID makes an exact retry idempotent; reusing a delivery ID with different
content fails closed. The high-volume provider inbox is separate from the versioned, low-volume
connector custody ledger.

PostgreSQL is also the workflow source of truth. A workflow enqueue writes the run, initial job, ordered workflow events, and outbox row in the same transaction as the business transition that caused it. Redis does not decide whether work exists; it transports a job identifier after the transaction commits. Losing Redis therefore delays work but does not erase it. In addition to unpublished rows, the relay periodically checks the exact stream ID on the latest stale receipt for a due nonterminal job; it republishes only when that entry is missing. Publication and repair can both create duplicates, so workers treat duplicate delivery as expected.

Each worker claim locks the job row, records a worker identity and expiring lease, increments the attempt count, and commits before running the handler. A second worker skips an active lease and may take over after expiry only when the attempt budget remains; otherwise it dead-letters without another handler invocation. Core services recheck that worker/attempt token under the job lock at domain commit time, so a superseded worker rolls back stale writes. Completed jobs are terminal no-ops on redelivery. Failed attempts use a persisted exponential-backoff clock and another outbox dispatch; the final allowed failure marks both job and workflow `dead_lettered` and copies the stream message to a Redis dead-letter stream.

The evidence layer stores collection snapshots as content-hashed artifacts and stores derived clusters, causal candidates, commit candidates, and runbook matches separately. Each investigation captures its collector, clusterer, ranker, and retriever versions plus an input hash. Each derived record carries evidence identifiers, so a score can be traced back to telemetry, dependency health, configuration history, deploy history, commit metadata, and the runbook corpus.

Provider interfaces isolate evidence collection from analysis. The default local demo uses HTTP
telemetry, optional bounded Prometheus corroboration, a fixture-backed Git provider, and local
Markdown runbooks. Tenant-and-service selectors decrypt exactly one enabled provider envelope at
the adapter boundary. After network I/O they lock and compare-and-swap connector plus credential
revisions through evidence commit, discarding results collected across a completed revocation or
rotation. Non-local environments require connector modes and never silently substitute fixtures
for configured provider evidence.

The synthesis provider is also replaceable. With an API key, the OpenAI adapter uses the Responses API and a strict structured-output schema. Without a key, the deterministic provider creates the same typed contract for offline demos. Both outputs pass through the same citation validator. The model produces language only; a deterministic policy derives the action envelope from the top causal signal and matching safety runbook.

Approval and execution are separate durable records. An approval is committed before any external write. The checkout simulator executor accepts only typed rollback or feature-flag-disable envelopes for `checkout-api`, uses a proposal-scoped idempotency key, then sends a canary cohort that includes digital wallets. Upstream-dependency causes always produce `escalate_only`, and low-confidence or missing evidence cannot unlock a write. A successful HTTP call is insufficient: telemetry verification must pass before the incident moves from investigating to mitigated.

Collaboration approval is separate again: mitigation authority never implies authority to publish
to Slack or GitHub. PagerAgent constructs the draft from grounded proposal fields, freezes its
destination, hash, connector revision, and credential revision, and records an explicit human
decision. Approval atomically queues the existing durable workflow; rejection queues nothing. The
provider adapter checkpoints delivery before network I/O, reconciles a stable output UUID in a
bounded remote history, and then either returns the existing receipt or performs one marked write.
This closes the common retry window without claiming a cross-system exactly-once transaction.

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

## Phase 9A connector custody path

```text
admin create or credential rotation
                │ typed provider contract
                ├──► non-secret configuration
                └──► write-only credentials → fresh 256-bit data key
                                              → AES-GCM ciphertext
                                              → wrapped key + exact active key ID
                │
                ▼
connector + credential envelope + sanitized event (one PostgreSQL transaction)

admin metadata / enable / validate mutation + expected version
                │ tenant-scoped row lock
                ▼
configuration or status receipt + sanitized event (one PostgreSQL transaction)

both paths ──► metadata/detail API: field names only
           └─► validation: authenticated envelope, no provider network request
```

The final line describes the Phase 9A generic custody check. GitHub validation is superseded by the
networked Phase 9B path below.

## Phase 9B GitHub evidence path

```text
admin validation request
        │ snapshot connector version + credential revision, decrypt envelope
        ▼
end DB transaction → RS256 App JWT → exact installation lookup → repository-scoped token
        │ fixed GitHub origin, API version, time/byte/request/item limits
        ▼
repository read succeeds → tenant-scoped row lock → compare revisions → validation event

GitHub webhook → raw-byte limit → HMAC-SHA256 → repository/installation binding
        │
        └──► normalized delivery + body hash → unique PostgreSQL inbox receipt

incident workflow → organization + service binding → one enabled GitHub connector
        │ no open evidence transaction during network collection
        ▼
commits + pull requests + deployments/status + releases + webhook receipts
        │ re-lock exact revisions; discard patches, bodies, URLs, tokens
        ▼
content-hashed artifacts → deterministic commit/cause rankers → cited proposal
```

The App private key and webhook secret remain inside the Phase 9A envelope. App JWTs and
installation tokens are ephemeral and never enter a database record, workflow message, evidence
payload, trace attribute, or error string. GitHub responses are read serially from one bounded page
per evidence class; redirects and environment proxies are disabled, response bytes and total calls
are capped, and rate-limit/provider errors are sanitized for the existing durable retry policy.

Telemetry remains the operational observation of the active release. GitHub metadata corroborates
which changes, pull requests, deployments, and releases surround that observation; it does not
override recovery gates or create mitigation authority.

## Phase 9C.1 Prometheus evidence path

```text
incident organization + service + alert metric
        │ one enabled tenant/service Prometheus connector
        ▼
decrypt bearer envelope → snapshot connector + credential revisions → end DB transaction
        │ fixed server-owned query ID; fixed POST path; time/byte/series/sample/label bounds
        ▼
normalized matrix + sanitized internal source URI
        │ after all network reads, lock order is GitHub then Prometheus
        ▼
compare exact revisions → content-hashed prometheus_metric_snapshot → evidence commit
        │
        └──► at most +0.01 corroboration for an existing non-unknown cause
```

The current catalog supports only the alert's `http_server_error_rate` metric. The incident cannot
supply PromQL, and the response cannot introduce unapproved labels, native histograms, non-finite
values, duplicate series, or samples outside its server-derived window. The final connector lock
is held through artifact and ranking persistence, so a completed revocation invalidates stale
provider data without holding a database lock during network I/O.

This is a Prometheus metrics slice, not a generic OpenTelemetry query implementation. Logs and
traces require explicit backend APIs and their own authentication, pagination, redaction, and
resource-limit contracts.

## Phase 9D durable collaboration path

```text
grounded proposal + tenant/service
        │ server selects enabled connector and builds bounded content
        ▼
pending output + destination + content hash + exact connector revisions
        │ separate collaboration permission and optimistic decision
        ├── reject ──► decision only
        └── approve ─► decision + workflow + job + outbox (one DB transaction)
                                      │ identifier-only Redis message
                                      ▼
leased/fenced worker → revision check → persisted delivering checkpoint
                                      │
                    bounded provider-marker reconciliation
                         │                         │
                 existing receipt             no marker
                         │                         └──► one marked write
                         └──────────────┬──────────┘
                                        ▼
                         normalized receipt / retry / dead letter
```

Slack receives the output UUID as both `client_msg_id` and opaque PagerAgent metadata. GitHub
receives it as a hidden issue-body marker. Every attempt searches one bounded recent window before
writing; an incomplete scan or contradictory match fails closed. This is intentionally described
as reconciliation-backed, effectively-once domain behavior over at-least-once workflow delivery.
PostgreSQL cannot atomically commit with either provider, so the architecture makes that ambiguity
and its repair receipt visible instead of labeling it exactly once.

## Phase 9E hosted identity and managed-key path

```text
browser login
    │ state + nonce + browser binding + PKCE S256
    ▼
encrypted single-use OIDC transaction ──► fixed IdP authorization endpoint
    │ code callback: verify + consume before provider I/O
    ▼
fixed token endpoint → verified issuer/subject/nonce → active membership
    │
    └──► revocable PostgreSQL session → HttpOnly PagerAgent cookie

organization admin + expected membership version
    │ recheck active admin under organization lock
    ▼
self/last-admin policy → membership update + immutable identity audit

connector credential snapshot → end DB transaction → AWS KMS data-key operation
    │ exact key ARN + organization/connector/provider/revision context
    ▼
AES-GCM payload envelope → lock/read current revisions → commit or discard
```

State, nonce, browser binding, and verifier are independent random values. PostgreSQL retains only
their hashes and an AES-GCM-encrypted verifier; the browser cookie contains only the temporary
binding. Pending transaction counts and cleanup are serialized by organization so unauthenticated
login starts cannot grow durable state without bound. The callback cannot choose endpoints,
tenants, or return locations, and provider tokens never cross the browser boundary. Logout revokes
the session row, organization switching replaces it, membership deactivation revokes its active
rows, and SSE rechecks that authority before every event rather than only at connection time.
Hosted ingress exposes the frontend, relative API, and OIDC callback on one browser origin so
host-only `__Host-` cookies cannot be tossed by sibling subdomains or stranded on an API hostname.

Membership administration uses stable issuer plus subject identity, never email linking. Admins
can list, provision, deactivate, or change roles only inside the active organization. Optimistic
versions reject stale tabs, while self-demotion and last-active-admin checks preserve an
administration path. Every accepted mutation appends a sanitized version-correlated audit receipt.
The first admin comes from a one-shot offline exact-subject bootstrap with its own receipt; there is
no unauthenticated bootstrap route.

Production credential custody uses the AWS SDK workload credential chain; PagerAgent exposes no
static AWS credential settings. The application requires a full key ARN, matching region, and no
custom endpoint or local decryption ring outside development. IAM roles, KMS key policy, CloudTrail,
egress/VPC endpoints, key rotation, backup, and recovery remain explicit infrastructure duties,
not capabilities silently claimed by this repository.
KMS clients have bounded timeouts and retries, stored envelopes are pinned to the configured key
ARN, and transient custody outages are retried without relabeling integrity failures as transient.
One stable KMS application identifier is shared by API and worker processes; their distinct
telemetry service names never become part of credential cryptographic authority.

Connector credentials are decrypted only at the final adapter boundary. Redis sees only the
internal output ID, and workflow failures carry an allowlisted provider error code rather than raw
response text. Provider calls happen outside database locks, while connector revision checks and
workflow attempt fencing prevent a completed revocation or expired worker from committing stale
authority. Mitigation and collaboration decisions remain independent append-only events.

New connectors begin disabled. A successful custody validation records a receipt but does not
silently enable the connector; the administrator makes that state transition explicitly with the
latest version. Rotating credentials increments both revisions and returns the connector to the
disabled state, preventing an unverified credential from inheriting authority. There is no delete
API: disabling preserves the append-only event trail, and a migration downgrade refuses to discard
populated custody tables.
