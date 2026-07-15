# PagerAgent

PagerAgent is an evidence-grounded incident-response copilot. It helps an on-call engineer connect an alert to telemetry, deploy history, relevant runbooks, customer impact, and a human-approved mitigation.

## Project status

**Phase 9A — secure connector control plane.** PagerAgent now gives each organization a typed,
audited custody boundary for future GitHub, Prometheus, and Slack integrations. Administrators can
register disabled connectors, rotate write-only credentials protected by per-revision envelope
encryption, validate local custody, and explicitly enable them. Incident commanders receive
read-only operational visibility; lower roles and other organizations receive no connector data.

## The interview story

PagerAgent is designed around a simple principle: the model synthesizes evidence; deterministic tools collect and score it. The complete flow is:

1. A signed user session selects one active organization and receives an exact permission receipt.
2. An administrator provisions a disabled provider connector through a typed contract; PagerAgent
   seals its write-only credential with a per-revision data key and records a sanitized custody
   event before the connector can be enabled.
3. A versioned simulator scenario introduces a code, configuration, or dependency failure.
4. Tenant-authenticated telemetry crosses an alert threshold and atomically creates an incident, durable workflow, first job, workflow events, and outbox message in PostgreSQL.
5. A separate relay publishes the job to Redis Streams; a database-leased worker gathers evidence, ranks causal signals, and retrieves the matching runbook.
6. PagerAgent proposes a cited typed action—or blocks automation when the evidence points outside its authority—without losing work if a process restarts.
7. An authorized human approves or rejects the proposal. Approval atomically queues a separate mitigation workflow instead of performing the external write in the request.
8. The worker executes the allow-listed action with a proposal-scoped idempotency key and records recovery verification before the incident is mitigated.
9. Resolution queues a durable postmortem workflow; the resulting cited report can be revised, finalized, and exported.
10. The tenant-filtered workflow recorder follows every queue, lease, retry, completion, and dead-letter event through a replayable server-sent event stream.

## Repository layout

| Path | Purpose |
| --- | --- |
| `backend/` | FastAPI APIs, PostgreSQL workflow state, outbox relay, Redis Streams worker, and incident services |
| `frontend/` | React operator dashboard |
| `simulator/` | Deterministic services, traffic, deploys, and alerts |
| `scenarios/` | Ground-truth incident definitions used by the simulator and evaluations |
| `runbooks/` | Versioned Markdown operational procedures |
| `evals/` | Benchmark definitions and quality gates |
| `docs/` | Architecture notes and explicit engineering decisions |
| `infra/` | Local observability and infrastructure configuration |

## Run locally

Copy the example environment file if needed, then start the development stack:

```bash
cp .env.example .env
docker compose up --build
```

Once running:

- API health: <http://localhost:8000/api/v1/health>
- API documentation: <http://localhost:8000/docs>
- Dashboard: <http://localhost:5173>
- Simulated checkout API: <http://localhost:8100/docs>

The included dashboard opens at a local-only identity checkpoint. Start as the viewer to inspect
read-only behavior, then use responder, incident commander, and admin personas to demonstrate the
exact RBAC boundary. Every persona can switch to an empty sandbox organization to demonstrate state
and SSE isolation. Outside local/test, PagerAgent disables personas, fails startup on development
secrets, and exposes a fixed-issuer OIDC token exchange for the same HttpOnly PagerAgent session.
The provider-specific authorization redirect/callback and PKCE browser bootstrap are intentionally
not claimed by this phase; that hosted identity-provider adapter is planned for Phase 9E.

To demonstrate the connector custody boundary independently from an incident:

```bash
./scripts/run-connector-demo.sh
```

The script creates a disabled Prometheus connector, proves the submitted token is absent from every
API and audit response, validates the authenticated envelope, enables the connector with an
optimistic version check, rotates the credential back into a disabled state, and prints the
sanitized custody history. It validates storage and authorization only—Phase 9A makes no provider
network request. The incident demo below still uses deterministic simulator telemetry, Git
fixtures, and local runbooks; Phase 9B and 9C will connect these custody records to the evidence
pipeline as separate vertical slices.

To replay the first incident automatically:

```bash
./scripts/run-demo.sh
```

The script signs in as the local admin through a short-lived Bearer session, sends 20 healthy
requests, activates the versioned `checkout-validation-bug` scenario, sends 40 additional requests,
and waits for the alert, durable investigation workflow, and grounded decision packet. The alert
evaluator uses its separate ingest key. The dashboard's dispatch recorder shows the PostgreSQL
outbox-to-stream-to-worker path while the evidence view fills in. The expected result is one
`ValidationRuleMissing` cluster with 8 failed digital-wallet requests, code change `8fa23c1` ranked
as the top causal signal, `checkout-api-rollback` retrieved first, and a cited rollback proposal
waiting for human approval.

Replay the other causal classes with the same end-to-end path:

```bash
./scripts/run-demo.sh --scenario payment-provider-timeout
./scripts/run-demo.sh --scenario checkout-feature-flag-regression --approve
```

The provider-timeout case deliberately includes commit `9c4e2d1` as a nearby red-herring deploy. PagerAgent ranks `payment-gateway` first and stops at an advisory-only escalation. The feature-flag case ranks `wallet_validation_v2`, proposes only that typed flag disable, verifies recovery canaries, and produces a configuration-aware postmortem.

To exercise the explicit command-line approval path as well:

```bash
./scripts/run-demo.sh --approve
```

That flag represents the operator decision. PagerAgent records it, rolls back only to the allow-listed `stable-v1` release, sends 15 recovery canaries including the failing cohort, and marks the incident mitigated only if every canary succeeds. The script then resolves the incident, waits for the automatic grounded postmortem, verifies its Markdown export, and prints the temporary export path. The normal dashboard path keeps approval, report editing, and finalization interactive.

To make the durability boundary visible during a demo, run the dedicated failure-and-recovery walkthrough:

```bash
./scripts/run-durability-demo.sh
```

The script deliberately takes Redis and the workers offline, proves the incident and unpublished work still exist in PostgreSQL, restarts the API while transport is unavailable, and restores Redis. It then erases an already-published stream entry and proves the relay detects the missing saved stream ID and reconstructs the latest nonterminal delivery from PostgreSQL before the worker resumes. Finally it injects a duplicate completed-job delivery and verifies that recovery creates no second incident, investigation, or proposal. Add `--approve` to include one idempotent mitigation and simulator mutation. The automated workflow tests additionally force exponential retries, crash-driven attempt exhaustion, commit fencing of a stale worker, commit-ordered SSE publication, healthy-receipt suppression, and takeover of an expired lease.

The demo works without an API key using a deterministic grounded synthesizer. To exercise real model synthesis, set `OPENAI_API_KEY` and leave `SYNTHESIS_PROVIDER=auto`; PagerAgent uses the OpenAI Responses API with a strict JSON schema. Set `SYNTHESIS_PROVIDER=deterministic` for reproducible offline demos.

Run the complete regression suite and print its causal/action verdicts:

```bash
./scripts/run-benchmark.sh
```

For local development outside Docker:

```bash
cd backend && python -m venv .venv && source .venv/bin/activate && pip install -r requirements-dev.txt && alembic upgrade head && uvicorn app.main:app --reload
cd frontend && npm install && npm run dev
```

## Development milestones

0. Foundation (complete): project structure, local stack, and architectural contracts.
1. Simulator (complete): a checkout service, deterministic bad deploy, synthetic traffic, and alert ingestion.
2. Incident core (complete): PostgreSQL persistence, lifecycle rules, and an operator dashboard.
3. Evidence (complete): immutable collection, error clustering, explainable commit ranking, hybrid runbook retrieval, and deterministic quality gates.
4. Copilot (complete): structured grounded briefs, citation guardrails, human decisions, allow-listed rollback execution, and recovery verification.
5. Postmortem (complete): grounded generation after resolution, exact timelines, immutable revisions, explicit finalization, prevention ownership, and Markdown export.
6. Evaluation expansion (complete): versioned multi-cause scenarios, cross-signal causal ranking, authority-aware actions, adversarial probes, and a visible reliability scorecard.
7. Durable orchestration (complete): transactional outbox, verified Redis Streams repair, leased attempts with commit fencing, retries and dead letters, replay-safe side effects, commit-ordered SSE workflow receipts, and trace correlation.
8A. Identity boundary (complete): fixed-issuer OIDC token verification and session exchange, database-backed membership and RBAC, server-derived actors, tenant-isolated incident/workflow access, CSRF protection, machine-authenticated alert ingestion, and server-controlled telemetry destinations.
9A. Connector control plane (complete): tenant-owned provider contracts, write-only credential APIs, per-revision AES-GCM envelope encryption, exact-key rotation, optimistic updates, safe disabled defaults, RBAC, and append-only custody events.
9B. GitHub evidence (next): GitHub App installation flow, signed webhook verification, replay protection, and real commit/deployment evidence.
9C. Observability evidence (planned): bounded Prometheus and OpenTelemetry queries persisted as immutable evidence snapshots.
9D. Collaboration outputs (planned): durable, idempotent Slack updates and GitHub issue creation.
9E. Hosted identity and administration (planned): provider-specific OIDC authorization-code/PKCE login and membership administration.

See [the Phase 9A walkthrough](docs/milestones/09a-connector-control-plane.md) and
[ADR 0010](docs/decisions/0010-connector-secrets-use-envelope-encryption.md) for the credential
custody contract. The [architecture guide](docs/architecture.md),
[Phase 8A walkthrough](docs/milestones/08a-production-identity.md), and
[ADR 0009](docs/decisions/0009-organization-scoped-identity-and-access.md) cover identity and tenant
isolation; the [Phase 7 walkthrough](docs/milestones/07-durable-orchestration.md) and
[ADR 0008](docs/decisions/0008-postgres-is-the-workflow-source-of-truth.md) cover durable execution.
