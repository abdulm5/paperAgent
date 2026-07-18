# PagerAgent interview guide

Use these answers as a framework, not a memorized script. Lead with the outcome, name the design
constraint, and then point to one concrete receipt or failure case.

## The pitch

### One sentence

PagerAgent is a multi-tenant incident-response copilot that turns alerts into cited evidence,
human-approved typed mitigations, verified recovery, durable collaboration outputs, and versioned
postmortems without granting the language model production authority.

### Thirty seconds

> I built PagerAgent to explore how an AI system can assist an on-call engineer without becoming an
> opaque autonomous operator. A transactional outbox turns an alert into durable work, workers
> collect and hash bounded telemetry, deploy, and runbook evidence, and deterministic rankers
> establish the causal packet before synthesis. The model produces only cited language. A separate
> policy creates a narrow action envelope, a human approves it, and canary telemetry verifies
> recovery. PostgreSQL records every authority and effect receipt; Redis Streams is only
> at-least-once transport.

### Ninety seconds

> The local demo reproduces three causal classes: a code regression, a feature-flag regression, and
> an upstream timeout with a nearby red-herring deploy. Alert ingestion atomically stores the
> incident, first workflow job, ordered events, and outbox intent in PostgreSQL. A relay publishes
> identifiers to Redis Streams, and a database-leased worker collects structured telemetry, Git
> evidence, optional bounded Prometheus snapshots, and versioned runbooks. The clusterer, causal
> ranker, and retriever run before any LLM.
>
> Both the deterministic and OpenAI synthesizers return the same typed cited brief. Unknown
> citations fail validation, and deterministic policy—not the model—derives rollback, exact
> feature-flag disable, or advisory-only escalation. Approval is an append-only domain event that
> queues a new workflow. Leases, commit-time fencing, unique domain identities, retries, and
> reconciliation make replay safe. The executor runs recovery canaries before mitigation is
> recorded. Hosted mode adds OIDC Authorization Code plus PKCE, revocable database sessions,
> audited memberships, and AWS KMS data-key envelopes.

## Architecture questions

### “Walk me through one incident.”

1. The simulator produces 20 healthy and 40 outage requests.
2. The alert evaluator crosses a 5% threshold and sends a typed, machine-authenticated alert.
3. PostgreSQL commits the incident, investigation workflow, job, events, and outbox together.
4. The relay publishes the job ID to Redis; a worker commits a lease before running it.
5. The worker snapshots telemetry, clusters eight cohort failures, ranks commit `8fa23c1`, and
   retrieves `checkout-api-rollback`.
6. Synthesis produces four cited claims; citation validation and deterministic policy produce a
   typed rollback envelope.
7. An incident commander begins investigation, reviews citations, and approves.
8. A separate workflow applies the allow-listed mutation with a stable idempotency key.
9. Fifteen canaries, including the failing cohort, must report zero failures before mitigation.
10. Resolution queues a cited, revisioned postmortem.

### “Why PostgreSQL and Redis?”

> They serve different guarantees. PostgreSQL owns business state, idempotency, leases, ordered
> events, and outbox intent. Redis Streams is a fast at-least-once wake-up mechanism with consumer
> groups and reclaim. If Redis disappears, intent still exists in PostgreSQL and the relay can
> republish it. If Redis delivers twice, the database recognizes the same job and domain result.

### “Why not Celery, Temporal, or Kafka?”

> A production team could choose any of them. I implemented the mechanics directly because the
> project’s learning goal was to make the guarantees visible: transactional enqueue, publish
> receipts, leases, fencing, retries, DLQ, and repair. Redis Streams keeps the local topology small;
> PostgreSQL remains the source of truth. At larger scale I would evaluate a managed workflow or log
> system, but I would preserve the same idempotency and authority contracts.

### “What is the transactional outbox solving?”

> Without it, the API could commit an incident and crash before publishing the investigation job,
> or publish a job and roll back the incident. PagerAgent writes business state and publish intent
> in one PostgreSQL transaction. Publication happens later and may repeat safely.

### “Is it exactly once?”

> No. Redis and provider calls are at least once, and PostgreSQL cannot atomically commit with Slack,
> GitHub, or the simulator. PagerAgent provides effectively-once domain effects using deterministic
> workflow/job identities, unique result relationships, leases, commit-time fencing, provider
> idempotency markers, and reconciliation. Calling that exactly once would hide the remote-write
> crash window.

### “What is fencing, and why is a lease not enough?”

> A lease decides who should be working now, but a paused old worker can wake up after its lease
> expires and try to commit. PagerAgent gives every claim an attempt token. The handler rechecks that
> token while holding the job lock in the same transaction as its domain write. A superseded worker
> rolls back even if it already performed expensive work.

### “How does SSE stay consistent?”

> Workflow events use a global integer cursor and a PostgreSQL transaction-scoped publication gate,
> so visible event IDs follow commit order. A reconnect uses `Last-Event-ID`; the UI also performs
> periodic REST reconciliation. The stream rechecks the revocable session and membership rather
> than trusting authority only when the connection opens.

## AI and evaluation questions

### “What part is actually AI?”

> The synthesizer turns a structured evidence packet into concise root-cause, impact,
> recommendation, risk, verification, and communication language. The OpenAI adapter uses strict
> structured output; the deterministic adapter implements the same interface for offline tests and
> demos. Clustering, ranking, citation validation, action derivation, authorization, execution, and
> recovery verification are deterministic.

### “Why not let the model call tools directly?”

> During an incident, plausible language is not enough to authorize a production change. Tool
> calling would still need an authority layer, schemas, allow-lists, approval, idempotency, and
> recovery checks. PagerAgent makes that layer explicit: the model cannot define action parameters,
> and adding an action requires code and tests rather than a prompt edit.

### “How do you prevent hallucinated citations?”

> Every evidence artifact receives an immutable ID. The synthesizer must return evidence IDs for
> four required claim types. The validator compares them with the incident’s allow-list and checks
> that each claim matches its rendered field. An unknown ID rejects the entire draft; it is not
> silently removed.

### “How do you distinguish correlation from causation?”

> Deploy proximity is one feature, not the answer. The causal layer combines error signatures,
> affected cohorts, release/configuration/dependency state, and deploy evidence. The upstream
> scenario intentionally places a nearby observability commit next to provider timeouts; the
> dependency signal must rank above it and policy must remain advisory-only.

### “How did you evaluate it?”

> Three strict scenario contracts contain simulation parameters and causal ground truth. The suite
> measures top-one cause, runbook reciprocal rank, impact count, affected attributes, citation
> coverage, action correctness, automation decision, and adversarial resilience. Probes include a
> red-herring deploy, invented citation, missing evidence, and low confidence. These are fixture
> regression gates, not a claim of real-world accuracy.

### “Why have a deterministic synthesizer?”

> It makes CI and the recorded demo independent of network access, cost, provider drift, and model
> variance while exercising the same typed contract and downstream guardrails. The real model
> adapter proves integration, but core correctness does not depend on getting identical prose twice.

### “What happens with missing evidence?”

> The UI shows the missing signal. The causal layer can return unknown, and deterministic policy
> maps missing or low-confidence evidence to `escalate_only`. Missing logs or traces do not become
> invented support for a rollback.

## Security and identity questions

### “How does hosted login work?”

> It is a backend-for-frontend OIDC Authorization Code + PKCE flow. State, nonce, browser binding,
> and verifier are independent. PagerAgent stores only hashes and an encrypted verifier, uses a
> temporary HttpOnly cookie, and atomically consumes the transaction before token exchange. It
> verifies RS256, issuer, audience/authorized party, nonce, times, and minimum RSA key size, maps an
> exact issuer/subject to a pre-provisioned membership, and issues its own revocable HttpOnly
> session. Provider tokens never reach React or browser storage.

### “Why have a database session if the app cookie is a JWT?”

> A self-contained token remains valid until expiry. PagerAgent maps every JWT to an unexpired,
> unrevoked PostgreSQL row and reloads membership on each request. Logout, organization switch, and
> membership deactivation therefore take effect immediately at the next authority check.

### “How is tenant isolation enforced?”

> The server derives the organization from the authenticated principal or ingest configuration;
> callers do not select arbitrary tenant IDs in domain commands. Queries filter by organization,
> cross-tenant IDs return not found, and composite foreign keys stop rows from combining an
> incident, proposal, workflow, or connector from different organizations. Tests use two
> organizations and every role boundary, and also delay an old authority refresh across a switch:
> request epochs suppress its auth callbacks, and only the current authority generation can commit
> the new session and CSRF token.

### “How do you protect connector credentials?”

> Credentials are typed and write-only. API and audit responses return revisions and sanitized
> metadata, never secret values. New or rotated credentials disable a connector until a live
> validation succeeds and an admin enables the exact revision. Local mode uses AES-GCM envelopes;
> hosted mode uses KMS data keys and tenant/connector/revision encryption context.

### “Why call KMS outside the database transaction?”

> Network latency or a KMS outage should not hold database locks. PagerAgent snapshots authority,
> ends the transaction, calls KMS, then locks and compare-and-swaps the exact organization,
> connector, and credential revisions. Stale results fail closed. The same snapshot/call/fence
> pattern is used for provider validation and evidence collection.

### “What happens if KMS is down?”

> Bounded SDK timeouts and retries turn availability failures into sanitized retryable workflow
> errors. Integrity failure, wrong encryption context, or an unknown key is permanent because
> retrying cannot make corrupted authority valid.

### “How do you prevent SSRF or unbounded telemetry queries?”

> Provider configuration is typed, production origins must use allowed HTTPS boundaries, clients
> use fixed paths with redirects and environment proxies disabled, and response bytes/time/request
> counts are capped. Prometheus callers select a server-owned query ID rather than supplying
> arbitrary PromQL; the window, step, series, sample, label, and body size are bounded.

## Collaboration and postmortem questions

### “Why is Slack/GitHub approval separate from mitigation approval?”

> Authority to change a service is not authority to speak in a broad channel or create a permanent
> repository issue. PagerAgent freezes server-built content, destination, content hash, and
> connector revisions, then records a separate actor decision. Rejection creates no workflow or
> provider call.

### “What if Slack accepts a message and the worker crashes before saving the receipt?”

> Every output has a provider-visible stable UUID. Before a retry writes, PagerAgent searches one
> bounded recent window for that marker and requires the exact approved content. One match becomes a
> reconciled receipt; conflicting or incomplete results fail closed; no match permits one write.

### “Why is the postmortem timeline deterministic?”

> A generated timeline could invent sequence or timestamps. PagerAgent builds the exact timeline
> from incident events and lets synthesis handle narrative sections. Draft edits create immutable
> attributed snapshots, and finalization requires explicit review.

## Release and operations questions

### “How do you know deployment uses what the release built?”

> The deploy workflow takes a release tag, not caller-supplied image paths. It verifies and
> downloads that tag's release manifest and metadata, checks their revision and expected GHCR
> repositories, and derives the full digest references from the bundle. It then verifies both OCI
> attestations against the repository's release workflow, tag ref, and source commit before any
> cluster change. The migration Job and long-running workloads therefore use the attested backend
> digest from the same release receipt.

### “Why migrate before rolling out, and how can you roll back?”

> The schema policy is expand/contract. Additive changes land first, the migration Job must finish,
> and only then do replicas roll. Migration 0013 adds an explicit minimum-application-generation
> marker set to 12. This image accepts its own head, or a future linear head only when that marker
> still permits application generation 12; a missing or higher marker fails closed. The migration
> command upgrades older schemas, no-ops on compatible current/future schemas, and rejects an
> incompatible future schema. Compatibility-aware rollback starts at this release boundary, so I do
> not infer that an older release is safe from revision ordering alone. Database downgrade remains a
> separate reviewed recovery operation.

### “Which release gates are real, and which are manual?”

> Pull requests and tags automatically enforce lint, unit/frontend/simulator tests, migration
> reversibility, a dedicated `_release_test` PostgreSQL/Redis suite, manifest checks, image builds,
> and scans. A tag additionally must publish digest metadata and provenance attestations; protected
> deployment verifies them and waits for migration and rollout readiness. Load, chaos, and live
> hosted-security results depend on a target environment, so I run them explicitly and retain their
> JSON receipts. I do not present a green unit-test run as load or managed-deployment evidence.

### “Why split the Namespace, service accounts, and Secrets?”

> A namespace-scoped deploy credential should not need cluster-wide permission to create its own
> Namespace, so a cluster administrator performs that one-time bootstrap. API, worker, relay,
> migration, and frontend have distinct service accounts with token automount disabled. Database,
> transport, API identity, and connector-custody values are separate Secrets, and each process
> projects only its required sets. Provider workload identity is then enabled only for API/worker
> identities that actually call KMS rather than giving every pod the same ambient authority.

### “How does a secret rotation restart pods?”

> External Secret contents can change without changing a Deployment spec. Promotion therefore
> requires a non-secret runtime-secret revision and writes it, together with the release revision,
> into each pod-template annotation. Changing either value creates a new ReplicaSet and leaves an
> auditable reason for the rollout without exposing secret material.

### “What exactly does the chaos receipt prove?”

> It does not infer recovery from aggregate stream length. The drill records each created incident,
> loads its exact workflow and job IDs, follows the saved Redis delivery IDs, and requires the
> workflow to complete with those entries acknowledged and the job IDs absent from the DLQ. It then
> repeats that correlation across a stopped worker. A `finally` path attempts to restart Redis,
> relay, and worker, but after a failed drill I still inspect Compose health because the workflow
> receipt does not prove cleanup. The receipt proves the two injected workflows recovered; it is
> not a claim that every unrelated message in the stream was healthy.

## Design tradeoffs

### “What was the hardest design problem?”

Strong answer:

> Separating delivery from effect. The outbox solves the database-to-Redis dual write, but it does
> not solve a provider accepting a request before the receipt commits. I ended up with different
> replay strategies for different boundaries: database uniqueness for internal results, a stable
> simulator idempotency key for mitigation, and provider-visible markers plus reconciliation for
> Slack and GitHub. That led me to document effectively-once semantics precisely instead of using
> one vague retry claim.

### “What would you change at larger scale?”

- Partition or archive high-volume workflow/evidence ledgers while preserving incident-level
  provenance.
- Scale beyond the checked-in two-replica relay/worker deployment from measured queue lag and
  database contention; `SKIP LOCKED`, consumer groups, leases, and fencing are designed for that
  concurrency model.
- Move to managed PostgreSQL/Redis with tested backup, failover, connection-pool, and migration
  procedures.
- Add deployment-level egress controls, workload identity, managed secret injection, and OTLP
  export.
- Add backend-specific bounded log and trace adapters and expand the scenario/evaluation corpus.
- Evaluate a managed workflow engine if operational cost outweighs the value of the custom runtime.

### “What are the honest limitations?”

> The included scenario corpus has three causal classes and one simulated service, so it proves
> deterministic regression behavior rather than broad incident accuracy. The stock demo uses local
> personas, fixture Git evidence, and a local encryption key. Real GitHub, Slack, hosted OIDC, and
> KMS paths require external configuration. Prometheus metrics are implemented, but backend-specific
> logs and traces are deferred. A production action adapter would need provider-grade durable
> idempotency or target-state reconciliation, not the simulator’s process-local cache.

## Behavioral and ownership framing

### “Tell me about a time you improved reliability.”

Use this STAR outline:

- **Situation:** An incident API could commit database state but lose background work during a
  crash, and retries could duplicate effects.
- **Task:** Preserve every response step and make replay safe without claiming impossible
  distributed exactly-once delivery.
- **Action:** Added a transactional outbox, Redis Streams relay, database leases, heartbeats,
  commit-time fencing, exponential retry/DLQ, exact receipt repair, unique domain results, and
  provider-specific idempotency/reconciliation.
- **Result:** The failure demo can stop Redis and workers, restart the API, repair a missing stream
  entry, inject a duplicate terminal delivery, and still show one incident, investigation, and
  proposal.

### “Tell me about a security decision.”

Use this outline:

- **Situation:** A signed OIDC token and encrypted connector secret could still leave stale
  authority, browser-token exposure, or lock-held network calls.
- **Task:** Make identity revocable and provider custody safe across API and worker processes.
- **Action:** Implemented a single-use PKCE BFF transaction, database-backed sessions, exact
  issuer/subject memberships, versioned admin changes, KMS data-key envelopes with stable context,
  and snapshot/call/compare-and-swap revision fencing.
- **Result:** Membership deactivation terminates subsequent API/SSE authority, secrets never enter
  the browser or read APIs, and stale KMS/provider results cannot overwrite a newer revision.

## Phrases to use

- “Evidence before generation.”
- “Model language is not operational authority.”
- “PostgreSQL owns intent; Redis transports identifiers.”
- “At-least-once execution with effectively-once domain effects.”
- “Snapshot, call outside the transaction, then compare-and-swap authority.”
- “Approval and effect are separate receipts.”
- “Fixture regression gates, not production accuracy.”

## Claims to avoid

- “Exactly once.”
- “Fully autonomous SRE.”
- “The AI found the root cause by itself.”
- “Production deployed” unless a real managed environment has actually been deployed and observed.
- “Supports logs and traces.” The current UI explicitly marks them not collected.
- “Secrets are safe because they are encrypted.” Explain access control, write-only APIs,
  encryption context, revision fencing, workload identity, and redaction too.
- “100% accurate.” The current perfect fixture gates describe three deterministic scenarios only.
