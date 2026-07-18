# PagerAgent résumé bullets

Use a three-bullet set that matches the role. These options describe implemented behavior; do not
combine all of them into one oversized project entry.

## Project header

**PagerAgent — Evidence-Grounded AI Incident Response Copilot**
Python, FastAPI, PostgreSQL, Redis Streams, React/TypeScript, OpenTelemetry, Docker, OpenAI Responses
API, GitHub Apps, Prometheus, OIDC, AWS KMS

Repository/demo links can follow the title once they are public.

## Balanced SWE set

- Built a multi-tenant incident-response copilot that converts threshold alerts into immutable
  evidence, ranked causal signals, cited response briefs, human-approved typed mitigations, recovery
  receipts, and versioned postmortems.
- Designed an at-least-once workflow runtime with a PostgreSQL transactional outbox, Redis Streams,
  expiring leases, commit-time fencing, exponential retries, dead letters, and replay-safe domain
  identities; demonstrated recovery from broker/worker outages and duplicate deliveries.
- Enforced production-shaped trust boundaries with OIDC Authorization Code + PKCE, revocable
  database sessions, RBAC/tenant isolation, audited membership administration, write-only connector
  APIs, and AWS KMS data-key envelopes with revision-bound encryption context.

## AI/ML systems set

- Architected an evidence-before-generation pipeline that clusters failures, ranks deploy,
  configuration, and dependency causes, retrieves runbooks, and supplies only immutable evidence IDs
  to deterministic or OpenAI structured synthesis.
- Prevented generated text from becoming operational authority by validating four required cited
  claim types and deriving rollback, exact feature-flag disable, or advisory-only escalation through
  deterministic policy plus explicit human approval.
- Created a schema-versioned three-scenario evaluation suite covering cause ranking, runbook MRR,
  impact/cohort accuracy, citation coverage, action safety, automation decisions, and adversarial
  red-herring, missing-evidence, low-confidence, and hallucinated-citation probes.

## Distributed-systems set

- Implemented durable incident workflows using atomic domain/outbox commits, identifier-only Redis
  Stream messages, database leases and heartbeats, stale-worker fencing, exact stream-receipt repair,
  exponential backoff, DLQ state, and commit-ordered SSE replay.
- Achieved effectively-once domain effects over at-least-once execution using deterministic workflow
  and job keys, unique result relationships, proposal-scoped mutation keys, and provider-visible
  Slack/GitHub delivery markers with bounded reconciliation.
- Separated API, relay, and worker processes so incidents and queued work survive API/Redis outages;
  verified missing-delivery reconstruction, expired-lease takeover, and terminal duplicate no-ops in
  automated and scripted failure tests.

## Security/backend set

- Implemented a same-origin OIDC BFF flow with single-use state/nonce/browser-binding/PKCE
  transactions, strict token verification, HttpOnly sessions backed by revocable PostgreSQL rows,
  per-session CSRF protection, and authority rechecks for long-lived SSE streams.
- Built optimistic, audited organization administration with stable issuer/subject membership,
  self/last-admin safeguards, immediate session revocation, tenant-filtered queries, and composite
  foreign keys that prevent cross-organization authority joins.
- Protected GitHub, Slack, and Prometheus credentials through typed write-only APIs, disabled-by-
  default rotation, live validation, AES-GCM/KMS envelopes designed for provider workload identity,
  fixed or allow-listed provider origins, bounded responses, sanitized errors, and
  snapshot/call/compare-and-swap fencing.

## Full-stack/product set

- Developed a React operations dashboard that exposes the incident ledger, threshold evidence,
  causal rankings, immutable provenance, approval boundaries, workflow attempts/leases/retries,
  recovery receipts, collaboration delivery state, RBAC, connector custody, and postmortem revisions.
- Built the FastAPI/PostgreSQL backend and deterministic checkout simulator for an end-to-end flow
  from 20 healthy and 40 outage requests through 8 cohort failures, a cited rollback decision, and
  15 zero-failure recovery canaries.
- Added replayable server-sent workflow events with REST reconciliation and tenant/session rechecks,
  keeping the dashboard current across disconnects while preventing stale frontend snapshots from
  replacing newer workflow state.
- Fenced browser request and authority generations across organization switches so a delayed prior-
  tenant response cannot sign out, overwrite, or lend its CSRF token to the new tenant scope.

## Platform/reliability set

- Built a semantic-tag release pipeline that scans non-root multi-architecture images, publishes
  SBOMs and OCI provenance attestations, publishes a verifiable digest-pinned deployment bundle,
  and makes protected promotion re-check both release assets and image/source identity before
  touching a cluster.
- Designed a migration-first Kubernetes contract with one-time Namespace bootstrap,
  process-specific service accounts and Secret projections, runtime-secret rollout revisions, and
  fail-closed PostgreSQL readiness keyed to an explicit minimum-application-generation marker rather
  than revision ordering, without treating Redis as business state.
- Added dedicated PostgreSQL/Redis contention tests plus bounded load, exact-workflow chaos, and
  hosted-security gates that produce revisioned JSON evidence and distinguish CI-enforced checks
  from environment-specific release qualification.

## One-line compact versions

- Built an evidence-grounded incident copilot with deterministic causal ranking, cited LLM
  synthesis, approval-gated typed actions, and telemetry-verified recovery.
- Built a durable PostgreSQL-outbox/Redis-Streams workflow engine with leases, fencing, retries, DLQ,
  SSE replay, and effectively-once domain effects.
- Secured a multi-tenant FastAPI/React control plane with OIDC PKCE, revocable sessions, RBAC, audited
  memberships, GitHub/Prometheus/Slack connectors, and KMS envelope encryption.
- Built an attested, digest-pinned Kubernetes release path with compatibility-aware migration gates,
  marker-based expand/contract readiness, least-privilege runtime boundaries, and reproducible
  resilience/security evidence.

## Quantification guidance

Safe checked-in numbers:

- 3 causal scenario classes;
- 20 healthy + 40 outage requests in the canonical scenario;
- 8 deterministic failing-cohort requests;
- 15 recovery canaries;
- 4 required cited claim types; and
- 3 action-policy outcomes: rollback, feature-flag disable, and advisory escalation.

Add measured load, latency, throughput, coverage, or deployment numbers only after the final release
test produces a saved artifact and repeatable command. Require the receipt's `source_revision` to
match the candidate and `source_dirty=false`. Prefer wording such as “sustained X jobs/s at Y p95 in
the checked-in load profile” over an unqualified scale claim.

## Claims that weaken the project

Avoid:

- “autonomous incident remediation” — the human decision is a core feature;
- “exactly-once distributed workflows” — the implementation is honest at-least-once plus
  effectively-once domain behavior;
- “100% root-cause accuracy” — the suite has perfect deterministic fixture gates, not production
  accuracy;
- “full observability platform” — metrics are implemented, while backend-specific log/trace
  evidence adapters are deferred; and
- “production deployed” until a managed environment, provider configuration, and saved deployment
  verification actually exist.
