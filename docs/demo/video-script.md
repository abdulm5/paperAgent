# PagerAgent demo-video script

Target length: 11–13 minutes for the core recording, plus an optional 2-minute reliability clip.
The goal is to explain the engineering decisions while the implemented system proves them.

## Before recording

1. Copy `.env.example` to `.env` only if no local `.env` exists.
2. Run `./scripts/seed-interview-demo.sh --check`.
3. Start the screen recording, then run `./scripts/seed-interview-demo.sh`. The build can be
   time-compressed, but keep the traffic, alert, workflow, investigation, and proposal output at
   normal speed.
4. Open <http://localhost:5173> in a clean browser and continue as the local incident commander.
5. Keep `docs/demo/architecture-walkthrough.md` open in another tab for the architecture shot.

The helper deliberately selects the deterministic synthesizer, fixture Git evidence, and no live
Prometheus connector. That makes every core take reproducible. Record `run-connector-demo.sh` as a
separate integration clip if live Prometheus custody is part of the final edit.

## Core recording

### 0:00–0:35 — Hook

**Screen:** Project title, then the dashboard identity checkpoint.

**Say:**

> PagerAgent is an evidence-grounded incident-response copilot. It takes an alert, reconstructs
> what changed, ranks causal signals, retrieves the relevant runbook, and drafts a cited response.
> The important part is what it cannot do: generated language never becomes operational authority.
> A deterministic policy creates a narrow action, a human approves it, and recovery telemetry—not
> an HTTP success—decides whether mitigation worked.

### 0:35–1:25 — Architecture in one pass

**Screen:** The “Runtime flow” diagram in `architecture-walkthrough.md`.

**Say:**

> The simulator emits telemetry and a machine-authenticated alert. One PostgreSQL transaction
> creates the incident, workflow, first job, workflow events, and outbox intent. A relay publishes
> the identifier to Redis Streams, and a database-leased worker gathers evidence. Deterministic
> rankers establish the facts; synthesis turns only those facts into readable claims. Approval
> queues a separate durable mitigation workflow. The same engine also handles collaboration
> delivery and postmortem generation.

Point at PostgreSQL and Redis while adding:

> PostgreSQL is the source of truth. Redis is replaceable at-least-once transport. That distinction
> is how the system survives a broker outage without losing the incident or its work.

### 1:25–2:00 — Identity and tenant authority

**Screen:** Identity checkpoint, then continue as the incident commander. Pause on the signed
authority receipt and organization selector.

**Say:**

> These personas exist only in local development, but they exercise the real permission model.
> Every request resolves a server-signed, database-backed session, active membership, one
> organization, and an exact permission set. The hosted path replaces this checkpoint with OIDC
> Authorization Code plus PKCE, while keeping the same revocable application session. Switching
> organizations revokes the old session instead of trusting stale token claims.

Optional proof: switch to `Sandbox Operations`, show the empty incident queue, then switch back to
`PagerAgent Labs`.

### 2:00–2:45 — Reproduce the outage

**Screen:** Terminal output from `seed-interview-demo.sh`.

**Say:**

> This is a versioned scenario, not a hand-edited database row. The runner resets prior incident
> state, sends 20 healthy requests, activates the bad release, and sends 40 more. Every fifth
> outage request in the digital-wallet cohort fails, producing eight failures out of 60 total
> requests. The local evaluator sees a 13.3 percent error rate against a 5 percent threshold.

Pause when the terminal prints the incident ID and workflow progress.

### 2:45–3:45 — Durable dispatch and incident receipt

**Screen:** Incident masthead, the five-stage proof rail, threshold breach, then the durable
dispatch recorder.

**Say:**

> Alert ingestion records the incident and investigation intent atomically. The dispatch recorder
> shows the outbox publication, Redis delivery, database lease, attempt number, and ordered workflow
> events. The API did not perform this analysis in an in-process background task, so an API restart
> cannot erase it. Duplicate deliveries reuse the same workflow and job identities.

Point across **Detect → Ground → Decide → Recover → Learn**:

> This rail is the product version of the architecture. A stage advances only when PagerAgent can
> render its stored receipt: the threshold, immutable evidence, authority decision, recovery
> telemetry, or versioned case file. It is not a decorative progress bar.

Point out the trace prefix if visible:

> The trace identifier is persisted with the workflow so work in a later process can still be
> correlated with the request that created it.

### 3:45–5:05 — Evidence before generation

**Screen:** Ranked investigation. Show the cluster, causal stack, commit dossier, runbook, and
provenance drawer.

**Say:**

> Before any model call, deterministic tools snapshot telemetry, cluster the eight
> `ValidationRuleMissing` failures, rank the deploy evidence, and retrieve the runbook. Commit
> `8fa23c1`, which changed wallet validation, ranks first. The rollback runbook also ranks first.
> Each stored artifact has a source, content hash, and immutable evidence ID.

Point to **Signal coverage**:

> This view is intentionally honest. Fixture Git and structured telemetry are present in this
> deterministic take; Prometheus is available through a separately validated connector; logs and
> traces are still marked “not collected.” I do not turn missing signals into fabricated facts.

### 5:05–6:15 — The AI boundary

**Screen:** Grounded decision packet, its four claim types and citations, then the typed action
envelope and dark write boundary.

**Say:**

> Synthesis receives the evidence packet and returns a strict brief: root cause, impact,
> recommendation, and risk, all with known evidence IDs. Unknown citations reject the whole draft.
> More importantly, the model does not choose executable parameters. Deterministic policy derives
> this `rollback_service` envelope for exactly `checkout-api`, expected commit `8fa23c1`, and target
> `stable-v1`. A prompt edit cannot add a new action type or target.

> The local take uses the deterministic provider so the result is reproducible. The OpenAI adapter
> implements the same typed interface and passes through the same citation and policy checks.

### 6:15–7:35 — Human approval and verified recovery

**Screen:** Use **Begin investigation**, add a concise timeline note, check “I reviewed the cited
evidence and rollback target,” add a decision note, and approve.

Suggested notes:

- Timeline: `Confirmed the failing cohort and ranked deploy evidence.`
- Decision: `Verified commit 8fa23c1, rollback target stable-v1, and recovery plan.`

**Say:**

> Approval is a persisted domain event, not just a button state. In the same transaction PagerAgent
> records the decision and queues a new mitigation workflow. The worker uses a proposal-scoped
> idempotency key, invokes only the allow-listed simulator action, and sends 15 canary requests that
> include the previously failing cohort.

Wait for the recovery receipt, then say:

> The incident becomes mitigated only after all 15 canaries return with zero failures. A deployment
> response alone would not be enough.

### 7:35–8:20 — Collaboration is a different permission

**Screen:** Collaboration outputs panel. If no Slack or GitHub issue connector is configured, leave
the disabled state visible rather than faking a receipt.

**Say:**

> Mitigation approval does not authorize external communication. A responder can prepare a
> server-built Slack update or GitHub issue, but an incident commander separately approves the
> exact content hash, destination, connector revision, and credential revision. Delivery runs as a
> durable workflow. Because PagerAgent cannot transact with Slack or GitHub, it puts a stable UUID
> marker in the remote artifact and reconciles that marker before retrying a write. That is
> effectively-once behavior for one marked output—not an exactly-once claim.

If a real provider connector is available, show the normalized timestamp or issue receipt. Never
display the credential or raw provider response.

### 8:20–9:35 — Resolution and the learning loop

**Screen:** Resolve the mitigated incident with note `Recovery remained healthy after the canary
cohort.` Wait for the postmortem workflow and open the case file.

**Say:**

> Resolution queues postmortem generation through the same durable path. Narrative sections are
> generated under the evidence allow-list, while the incident timeline is copied from database
> events rather than invented by the model. The report keeps immutable revisions and optimistic
> versions.

Edit one harmless sentence, assign or refine one prevention owner, and save with:
`Clarified customer impact and confirmed prevention ownership.`

Then check the review box, finalize, and export Markdown.

> Human edits remain attributed to the human; PagerAgent does not relabel changed prose as model
> verified. Finalization is explicit and irreversible through the API, and the exported report
> preserves the evidence index and prevention work.

### 9:35–10:25 — Evaluation, not vibes

**Screen:** Evaluation panel and its three-scenario matrix.

**Say:**

> The core pipeline is evaluated against three schema-versioned causal classes: a code regression,
> an upstream timeout with a nearby red-herring deploy, and a feature-flag regression. The suite
> checks top cause, runbook rank, impact, affected cohort, citation coverage, action safety,
> automation authority, and adversarial behavior. Missing or low-confidence evidence must degrade
> to `escalate_only`. These are deterministic regression gates, not a production accuracy claim.

### 10:25–11:05 — Release path

**Screen:** The “Release flow” diagram in `architecture-walkthrough.md`, then briefly show the CI,
release, and deployment workflow files.

**Say:**

> A release reuses the same receipt-first idea. Pull requests run the unit, frontend, simulator,
> PostgreSQL/Redis contention, manifest, scan, and image-contract gates. A semantic tag builds
> non-root multi-architecture images, publishes SBOM and provenance attestations, and creates a
> tagged release manifest pinned to both image digests. Deployment is a separate protected action:
> it verifies the release assets and both image attestations, checks the namespace-scoped runtime
> Secrets, runs the compatibility-aware migration command from the exact backend digest, and updates
> API, worker, relay, and frontend replicas only after that Job completes.

> Liveness checks only the process. Readiness requires PostgreSQL and one application-compatible
> Alembic head. Migration 0013 records the minimum supported application generation as 12. A future
> head is compatible only when that explicit marker still permits generation 12; a missing or higher
> marker fails closed. That is what supports migration-first overlap—it is not guessed from revision
> ordering. Redis deliberately does not evict the API because the transactional outbox is designed
> to absorb that outage.

### 11:05–11:55 — Production trust boundaries and honest scope

**Screen:** Connector custody or organization-access panel, then return to the architecture.

**Say:**

> Hosted identity uses a single-use OIDC transaction with state, nonce, browser binding, and PKCE,
> then issues a revocable PagerAgent session. Membership changes are versioned and audited. Local
> connector credentials use AES-GCM envelopes; hosted mode uses AWS KMS data keys with tenant and
> revision encryption context and is designed for provider workload identity. Provider calls happen
> outside database locks, then authority and revisions are checked again before a result commits.

> The current boundaries I would not overstate are equally important: the stock recording uses
> local personas and fixture Git evidence; Slack, GitHub, and hosted OIDC/KMS require real external
> configuration; and backend-specific log and trace evidence adapters are deferred.

### 11:55–12:15 — Close

**Screen:** The completed proof rail above the final postmortem.

**Say:**

> The project’s main idea is that AI can help an on-call engineer move faster without collapsing
> evidence, authority, execution, and audit into one opaque agent. Every important transition leaves
> a typed receipt, and every write stays narrower than the generated language around it. The proof
> rail is the compact summary: detect, ground, decide, recover, and learn.

## Optional reliability insert

Run this as a separate recording:

```bash
./scripts/run-durability-demo.sh
```

Use a short voice-over:

> Here Redis and the workers are intentionally offline. PostgreSQL still commits one incident and
> its unpublished work. After Redis returns, the relay repairs a missing stream delivery from the
> saved outbox intent. A duplicate completed-job message then reaches the worker and produces no
> second investigation or proposal. The automated tests additionally cover exponential backoff,
> lease takeover, stale-worker fencing, dead letters, and commit-ordered SSE replay.

With `--approve`, the walkthrough also includes the idempotent mutation and recovery canaries.

## Retake safety

- Rerun `./scripts/seed-interview-demo.sh`; it clears local incidents and resets the simulator.
- Do not run two seed helpers concurrently because they intentionally share the Compose project and
  reset endpoint.
- If demonstrating live Prometheus, start from a clean connector set. The evidence selector fails
  closed when multiple enabled connectors match the same organization and service.
- If the UI is already signed in as a lower-privilege role, sign out and choose incident commander
  before the approval segment.
