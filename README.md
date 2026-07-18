# PagerAgent

PagerAgent is an evidence-grounded incident-response copilot. It helps an on-call engineer connect an alert to telemetry, deploy history, relevant runbooks, customer impact, and a human-approved mitigation.

## Project status

**Final release and interview-demo phase.** PagerAgent now has a production-shaped release path:
non-root immutable images, a migration-gated Kubernetes rollout, reusable CI, digest-pinned release
assets with verifiable image provenance, and explicit PostgreSQL/Redis contention, load, chaos, and
security gates. The dashboard's five-stage proof rail makes the core system invariant
visible—detect, ground, decide, recover, and learn—while a deterministic seed command and a complete
video/interview kit reproduce the canonical incident without external provider drift. Hosted OIDC,
audited administration, and AWS KMS data-key custody remain part of the same hosted trust
boundary.

## The interview story

PagerAgent is designed around a simple principle: the model synthesizes evidence; deterministic tools collect and score it. The complete flow is:

1. A local persona or hosted OIDC Authorization Code + PKCE transaction creates a revocable,
   database-backed PagerAgent session for one active organization and exact permission receipt.
2. An administrator provisions stable issuer/subject memberships and changes roles or active state
   through optimistic versions, last-admin safeguards, and immutable audit receipts.
3. An administrator provisions a disabled provider connector through a typed contract; PagerAgent
   seals its write-only credential with a per-revision data key and records a sanitized custody
   event before the connector can be validated and enabled. Hosted envelopes wrap that data key
   with an exact AWS KMS key and tenant/revision encryption context.
4. Provider validation happens outside database locks, then compare-and-swaps the connector and
   credential revisions. GitHub uses a repository-scoped installation token; Prometheus uses one
   fixed credential-bearing read query.
5. A versioned simulator scenario introduces a code, configuration, or dependency failure.
6. Tenant-authenticated telemetry crosses an alert threshold and atomically creates an incident, durable workflow, first job, workflow events, and outbox message in PostgreSQL.
7. A separate relay publishes the job to Redis Streams; a database-leased worker gathers telemetry,
   bounded Prometheus metrics, normalized GitHub evidence, and runbooks, then ranks causal signals.
8. PagerAgent proposes a cited typed action—or blocks automation when the evidence points outside its authority—without losing work if a process restarts.
9. An authorized human approves or rejects the proposal. Approval atomically queues a separate mitigation workflow instead of performing the external write in the request.
10. Slack updates and GitHub issues require another explicit collaboration decision. Approval freezes the grounded content, destination, and connector revisions before atomically entering the outbox.
11. The worker executes the allow-listed mitigation or reconciles the collaboration delivery marker before one remote write, then records recovery or a normalized provider receipt.
12. Resolution queues a durable postmortem workflow; the resulting cited report can be revised, finalized, and exported.
13. The tenant-filtered workflow recorder follows every queue, lease, retry, completion, and dead-letter event through a replayable server-sent event stream.
14. Protected promotion verifies the tagged release assets and both OCI image attestations, runs
    the digest-pinned backend's compatibility-aware migration gate, and only then rolls API, worker,
    relay, and frontend replicas. Readiness requires PostgreSQL, one Alembic head, and an explicit
    schema marker compatible with this application generation; Redis outages remain absorbable by
    the transactional outbox.

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
| `deploy/` | Provider-neutral Kubernetes release contract and static validators |
| `infra/` | Local observability and infrastructure configuration |

## Run locally

Copy the example environment file if needed, then start the development stack:

```bash
cp .env.example .env
docker compose up --build
```

Once running:

- API health: <http://localhost:8000/api/v1/health>
- API liveness: <http://localhost:8000/api/v1/health/live>
- API database/schema readiness: <http://localhost:8000/api/v1/health/ready>
- API documentation: <http://localhost:8000/docs>
- Dashboard: <http://localhost:5173>
- Simulated checkout API: <http://localhost:8100/docs>

The included dashboard opens at a local-only identity checkpoint. Start as the viewer to inspect
read-only behavior, then use responder, incident commander, and admin personas to demonstrate the
exact RBAC boundary. Every persona can switch to an empty sandbox organization to demonstrate state
and SSE isolation. The admin-only **Organization access** panel provisions stable OIDC subjects,
applies versioned role/status changes, and shows their audit receipts.
The stock local environment accepts only the demo issuer
`https://identity.pageragent.local`; hosted deployments replace it with their exact IdP issuer.

Outside local/test, PagerAgent disables personas and the direct token-exchange shortcut, fails
startup on incomplete production boundaries, and performs OIDC Authorization Code + PKCE through
server-owned login and callback routes. Provider tokens never enter React or the PagerAgent cookie;
the final browser credential is a revocable HttpOnly PagerAgent session. The local stack keeps the
deterministic AES-GCM connector cipher. Hosted deployments select AWS KMS, use the standard workload
credential chain, and must supply IAM/key policy, CloudTrail, egress, and rotation controls.

After hosted migrations and IdP registration, establish the first administrator offline from
`backend/` (there is intentionally no public bootstrap endpoint):

```bash
python -m app.memberships.bootstrap \
  --organization pageragent-production \
  --organization-name "PagerAgent Production" \
  --issuer https://identity.example.com \
  --subject 00u-stable-idp-subject \
  --email admin@example.com \
  --display-name "PagerAgent Admin"
```

The command requires the exact configured issuer, refuses when an active admin exists, and writes
an immutable `bootstrap:offline` receipt. Configure `PAGERAGENT_INGEST_ORGANIZATION_SLUG`
explicitly for the organization that should own machine-created incidents; it is independent from
the browser's OIDC default organization. A hosted alerting system—not the local simulator
evaluator—must send the typed alert payload to `/api/v1/alerts` with the dedicated ingest key and
an allow-listed telemetry URL. See the Phase 9E walkthrough for production `__Host-` cookie names,
ingress callback-log redaction/rate limits, and the complete KMS contract.

To demonstrate the connector custody boundary independently from an incident:

```bash
./scripts/run-connector-demo.sh
```

The script creates a disabled Prometheus connector, proves the submitted token is absent from every
API and audit response, performs the live fixed read handshake, enables the connector with an
optimistic version check, rotates the credential back into a disabled state, revalidates it, and
prints the sanitized custody history. Run `./scripts/run-demo.sh` afterward to persist the bounded
`prometheus_metric_snapshot` and show its query/window/count/revision receipt in the investigation.
The local Prometheus container is a reproducible adapter proof, not a claim of production identity
or egress isolation; hosted deployments require an HTTPS authorization boundary and an outbound
network policy restricted to the configured origin.

To run the live GitHub evidence proof, export a GitHub App installation and a separate webhook
secret, then run:

```bash
export GITHUB_APP_ID=...
export GITHUB_INSTALLATION_ID=...
export GITHUB_REPOSITORY=owner/repository
export GITHUB_SERVICE=checkout-api
export GITHUB_PRIVATE_KEY_FILE=/absolute/path/to/app-private-key.pem
export GITHUB_WEBHOOK_SECRET=...
./scripts/run-github-evidence-demo.sh
```

Install the App on the exact repository with **Contents: read**, **Pull requests: read**, and
**Deployments: read** permissions. For provider-originated deliveries, configure the App webhook
URL as `https://<your-pageragent-host>/api/v1/webhooks/github/<connector_id>`, use the same
independent webhook secret, and subscribe to push, pull request, deployment, deployment status, and
release events. The proof script creates the connector first and synthesizes a correctly signed
delivery locally so the authentication and replay behavior remain easy to reproduce without a
public tunnel.

The script never prints or places the PEM in a command-line argument. It creates or rotates the
disabled connector, performs the real repository handshake, enables it, accepts one correctly
signed delivery, proves an exact retry is idempotent, rejects a changed-body replay, and prints the
normalized tenant-scoped receipt. Add `--with-incident` to run the checkout scenario in explicit
connector mode and assert that its investigation contains GitHub App evidence artifacts. That
option requires at least one commit in the configured repository within PagerAgent's 24-hour
evidence window so the demo can prove live commit ranking, and it requires
`GITHUB_SERVICE=checkout-api` because that is the included incident scenario.

To replay the first incident automatically with deterministic Git fixtures:

```bash
./scripts/run-demo.sh
```

For the cleanest interview rehearsal or video take, use the release seed wrapper. It validates the
Docker daemon, Compose and scenario contracts, forces deterministic synthesis and fixture Git
evidence, disables optional live Prometheus evidence, resets prior demo state, and stops at the
dashboard approval boundary:

```bash
./scripts/seed-interview-demo.sh --check
./scripts/seed-interview-demo.sh
```

Use `--approve` for a terminal-only proof through mitigation, recovery canaries, resolution, and
postmortem export. The [demo kit](docs/demo/README.md) contains the timed recording script,
architecture walkthrough, interview Q&A, and role-specific résumé bullets.

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

The dashboard also exposes the independent Phase 9D communication boundary. Configure and
validate a Slack service/channel connector or opt an existing GitHub App connector into issue
creation, then prepare an output from the grounded proposal. A responder can prepare the exact
server-built preview; an incident commander separately approves or rejects it. The panel follows
queued, delivering, retry, delivered, and dead-letter states and displays only the normalized
Slack timestamp or GitHub issue receipt. To demonstrate the crash window, interrupt a worker after
the remote write: the next attempt searches for the stable output UUID and records a reconciled
receipt instead of creating another message or issue.

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

For local development outside Docker, initialize the backend environment once, then run the two
servers in separate terminals:

```bash
(cd backend && python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt)
(cd backend && .venv/bin/alembic upgrade head && .venv/bin/uvicorn app.main:app --reload)
(cd frontend && npm install && npm run dev)
```

Initialize the simulator's test environment once before running the Phase 10 verification commands:

```bash
(cd simulator && python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt)
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
8A. Identity boundary (complete): fixed-issuer OIDC token verification, database-backed membership and RBAC, server-derived actors, tenant-isolated incident/workflow access, CSRF protection, machine-authenticated alert ingestion, and server-controlled telemetry destinations.
9A. Connector control plane (complete): tenant-owned provider contracts, write-only credential APIs, per-revision AES-GCM envelope encryption, exact-key rotation, optimistic updates, safe disabled defaults, RBAC, and append-only custody events.
9B. GitHub evidence (complete): multiline App-key custody, repository-scoped installation authorization, two-phase provider validation, signed webhook verification, durable replay protection, bounded/rate-aware REST collection, and normalized commit/PR/deployment/release evidence.
9C.1. Prometheus evidence (complete): server-owned PromQL catalog, bounded range collection, revision-fenced tenant/service selection, immutable metric snapshots, and conservative causal corroboration.
9C.2. Logs and traces (planned): bounded, backend-specific APIs for OpenTelemetry-derived telemetry.
9D. Collaboration outputs (complete): separately approved server-grounded Slack updates and GitHub issues, atomic outbox enqueueing, revision-fenced workers, bounded provider-marker reconciliation, normalized delivery receipts, retries, and dead letters.
9E. Hosted identity and administration (complete): bounded server-owned OIDC authorization-code/PKCE
login, revocable database sessions and SSE authority, audited first-admin bootstrap and optimistic
membership administration, plus retry-aware AWS KMS connector data-key custody designed for provider
workload identity.
10. Final release and demo (complete): hardened production images, migration-gated Kubernetes
deployment, CI/CD with verified release assets and attested digest-pinned images, PostgreSQL/Redis
contention tests, bounded load and exact-workflow chaos drills with best-effort cleanup, hosted
security gates, a five-stage response proof rail, and a deterministic interview/video kit.

See [the final release walkthrough](docs/milestones/10-release-and-demo.md),
[the managed deployment runbook](docs/deployment/managed-release.md),
[the release-gate guide](docs/release/resilience-and-security-gates.md), and
[the interview demo kit](docs/demo/README.md) for the complete handoff.

See [the Phase 9E walkthrough](docs/milestones/09e-hosted-identity-and-kms.md),
[ADR 0014](docs/decisions/0014-hosted-login-is-a-bff-transaction.md), and
[ADR 0015](docs/decisions/0015-production-credentials-use-aws-kms-data-keys.md) for the hosted
browser, revocable-session, membership-administration, and managed-key boundaries.
[The Phase 9D walkthrough](docs/milestones/09d-durable-collaboration.md) and
[ADR 0013](docs/decisions/0013-collaboration-delivery-is-reconciled-not-exactly-once.md) cover the
separate communication approval, provider-marker reconciliation, and dead-letter boundary.
[The Phase 9C.1 walkthrough](docs/milestones/09c-observability-evidence.md) and
[ADR 0012](docs/decisions/0012-observability-evidence-is-bounded-before-it-is-causal.md) cover the
Prometheus query, network, and causal boundary. [The Phase 9B walkthrough](docs/milestones/09b-github-evidence.md)
and [ADR 0011](docs/decisions/0011-github-deliveries-are-authenticated-idempotent-inputs.md) cover
the GitHub trust boundary. [Phase 9A](docs/milestones/09a-connector-control-plane.md) and
[ADR 0010](docs/decisions/0010-connector-secrets-use-envelope-encryption.md) cover credential
custody. The [architecture guide](docs/architecture.md),
[Phase 8A walkthrough](docs/milestones/08a-production-identity.md), and
[ADR 0009](docs/decisions/0009-organization-scoped-identity-and-access.md) cover identity and tenant
isolation; the [Phase 7 walkthrough](docs/milestones/07-durable-orchestration.md) and
[ADR 0008](docs/decisions/0008-postgres-is-the-workflow-source-of-truth.md) cover durable execution.
