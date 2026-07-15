# PagerAgent

PagerAgent is an evidence-grounded incident-response copilot. It helps an on-call engineer connect an alert to telemetry, deploy history, relevant runbooks, customer impact, and a human-approved mitigation.

## Project status

**Phase 8A — identity and tenant boundaries.** PagerAgent now authenticates signed
browser and CLI sessions, resolves active organization membership and role from PostgreSQL on every
request, enforces explicit permissions, isolates incident aggregates and workflow SSE by
organization, derives human audit actors on the server, and authenticates monitoring through a
separate tenant-bound machine credential.

## The interview story

PagerAgent is designed around a simple principle: the model synthesizes evidence; deterministic tools collect and score it. The complete flow is:

1. A signed user session selects one active organization and receives an exact permission receipt.
2. A versioned simulator scenario introduces a code, configuration, or dependency failure.
3. Tenant-authenticated telemetry crosses an alert threshold and atomically creates an incident, durable workflow, first job, workflow events, and outbox message in PostgreSQL.
4. A separate relay publishes the job to Redis Streams; a database-leased worker gathers evidence, ranks causal signals, and retrieves the matching runbook.
5. PagerAgent proposes a cited typed action—or blocks automation when the evidence points outside its authority—without losing work if a process restarts.
6. An authorized human approves or rejects the proposal. Approval atomically queues a separate mitigation workflow instead of performing the external write in the request.
7. The worker executes the allow-listed action with a proposal-scoped idempotency key and records recovery verification before the incident is mitigated.
8. Resolution queues a durable postmortem workflow; the resulting cited report can be revised, finalized, and exported.
9. The tenant-filtered workflow recorder follows every queue, lease, retry, completion, and dead-letter event through a replayable server-sent event stream.

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
not claimed by this phase; that hosted identity-provider adapter is part of Phase 8B.

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
8B. Production integrations (next): provider-specific authorization-code/PKCE login, encrypted connector credentials, signed third-party webhooks, production evidence/action adapters, membership administration, and managed observability/export infrastructure.

See [the architecture guide](docs/architecture.md), the [Phase 8A walkthrough](docs/milestones/08a-production-identity.md), and [ADR 0009](docs/decisions/0009-organization-scoped-identity-and-access.md) for the identity and tenant-isolation rationale. The [Phase 7 walkthrough](docs/milestones/07-durable-orchestration.md) and [ADR 0008](docs/decisions/0008-postgres-is-the-workflow-source-of-truth.md) cover durable execution.
