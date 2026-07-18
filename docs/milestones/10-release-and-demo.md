# Phase 10 — final release and interview demo

## Outcome

PagerAgent can now be released as an immutable, migration-gated application and demonstrated as one
coherent incident-response system. This phase does not pretend that a checked-in manifest is a live
production deployment. It makes the deployment contract, failure gates, deterministic recording,
and remaining provider responsibilities explicit and executable.

## What changed

### Managed release contract

- The backend and frontend have non-root production stages with read-only-root-filesystem-compatible
  paths. The backend image embeds the exact migrations, runbooks, and versioned scenarios used by
  the release.
- Kubernetes separates foundation resources, a bounded compatibility-aware migration Job, and
  long-running workloads. The API, worker, relay, and migration all reference one backend image.
  Promotion accepts a tagged immutable release, verifies its two assets, and derives only the
  digest-pinned image references bound to that tag and source revision.
- API liveness proves only that the process can answer. Readiness checks PostgreSQL and requires the
  single Alembic head to be application-compatible. Migration `20260718_0013` creates the singleton
  `pageragent_schema_contract` marker with `minimum_application_generation=12`. This release's own
  head reports `current`; a single future head reports `forward_compatible` only when that explicit
  marker is present and no greater than application generation 12. A missing, invalid, or higher
  marker fails closed. Redis is deliberately excluded because PostgreSQL owns queued intent and the
  outbox is designed to absorb a broker interruption.
- Schema compatibility follows expand/contract: additive migrations ship before code depends on
  them and preserve the minimum-generation marker; an incompatible migration raises it. Destructive
  cleanup is a separately reviewed maintenance release after old images are retired. A database
  downgrade is never an automatic rollback strategy, and compatibility-aware rollback begins at
  this marker-introducing release boundary rather than being inferred for older releases.
- Hosted responses disable interactive API documentation, reject untrusted Host headers, and add
  anti-sniffing, framing, referrer, permissions, content-security, and transport-security headers.
- The hosted dashboard is served by unprivileged Nginx with a same-origin content-security policy,
  HSTS, frame denial, MIME-sniffing protection, and a dedicated static health endpoint.
- A one-time cluster-admin bootstrap owns the Namespace. Environment deployment credentials remain
  namespace-scoped, while API, worker, relay, migration, and frontend use separate service accounts
  with service-account-token automount disabled. Runtime values are split across database,
  transport, API identity, and connector-custody Secrets so each process receives only what it
  needs; provider workload-identity overlays are limited to the API and worker.
- GitHub Actions runs the unit and real-infrastructure suites, validates manifests, scans and builds
  release images, emits multi-architecture GHCR artifacts with SBOM/provenance metadata, and
  publishes the manifest plus metadata as a tagged GitHub Release. A protected manual workflow
  verifies the release assets and both image attestations, then promotes those exact digests after
  its migration gate succeeds. Release and runtime-secret revisions are pod-template annotations,
  so code promotion or secret rotation creates an observable rollout.

### Resilience and security gates

- PostgreSQL integration tests exercise simultaneous job claims, `SKIP LOCKED` outbox draining, and
  competing organization-admin mutations. A Redis integration test proves the dead-letter receipt
  is independent of source-stream recreation.
- `run-load-gate.py` creates a bounded, uniquely namespaced alert-ingestion profile and records
  throughput, error rate, latency percentiles, deduplication, and incident cardinality without
  writing the ingest key into its JSON receipt. It requires the complete expected fingerprint set,
  exactly one stable incident per fingerprint, and a distinct incident across fingerprints.
- `run-chaos-drill.py` requires explicit confirmation, interrupts only the local Redis/worker path,
  proves PostgreSQL still accepts work, then correlates the exact created incident, workflow, jobs,
  stream IDs, acknowledgements, and dead-letter absence before declaring recovery. It restores
  the success path and makes a best-effort restart of stopped services in a `finally` path after a
  failed assertion; operators still inspect Compose health after a failed drill.
- `run-security-gate.py` validates a materialized hosted environment with the same application
  settings model and probes live authentication, validation redaction, CORS, Host, health, schema,
  documentation, and response-header contracts.

### Explainable demo surface

- The dashboard now presents a five-stage receipt rail: **Detect → Ground → Decide → Recover →
  Learn**. It derives every state and detail from the selected incident, investigation, proposal,
  execution, or postmortem instead of showing decorative counters.
- Selection, resource, action, request-scope, and authority generations fence delayed reads,
  mutations, authentication callbacks, and CSRF commits across incident or organization switches.
- `seed-interview-demo.sh` checks prerequisites, fixes the evidence/model modes to deterministic
  values, resets the local scenario, and stops at the human approval boundary by default. The same
  entry point supports the code, dependency, and configuration scenarios and can complete the
  terminal-only path with `--approve`.
- `docs/demo/` contains the timed video script, architecture walkthrough, interview Q&A, and honest
  role-specific résumé bullets. Claims distinguish checked-in fixture behavior from measured load,
  a configured provider, or a live managed deployment.

## How to explain the release design

Start with one sentence:

> The release preserves the same invariant as the incident pipeline: establish a durable fact,
> validate it, then cross an explicit authority boundary.

Map that sentence to three concrete examples:

1. A verified tagged release identifies attested image digests; `python -m app.db.migrate` upgrades
   an older database, no-ops on an explicitly compatible current/future database, and rejects a
   future incompatible marker; only then does the deployment update the application replicas.
2. PostgreSQL records incident intent; Redis can fail and recover without deciding whether the
   incident exists.
3. The proof rail does not mark recovery complete when a deployment call returns—it waits for the
   stored canary receipt.

That framing connects CI/CD, distributed systems, AI safety, and the dashboard without presenting
them as unrelated features.

## Verification

Fast local checks:

```bash
(cd backend && .venv/bin/ruff check . && .venv/bin/pytest -q)
(cd simulator && .venv/bin/ruff check app tests && .venv/bin/pytest -q)
(cd frontend && npm test -- --run && npm run build)
docker compose config --quiet
backend/.venv/bin/python deploy/validate_manifests.py deploy/kubernetes
./scripts/seed-interview-demo.sh --check
```

The reusable CI quality gate enforces lint, unit/frontend/simulator tests, migration reversibility,
dedicated release-database PostgreSQL/Redis tests, manifest validation, image hardening, and image
scans. GitHub Release publication additionally requires those checks, digest builds, release
assets, and image attestations. The manually dispatched `release-evidence` workflow runs load,
local live-HTTP security, and exact-workflow chaos checks in an isolated Compose project and uploads
their JSON receipts. Materialized-hosted-configuration security and real cluster rollout remain explicit
target-environment evidence gates: they are not implied by a green pull request and must be run and
retained for the target release. Their commands are intentionally separated in:

- `docs/release/resilience-and-security-gates.md`; and
- `docs/deployment/managed-release.md`.

Save their JSON receipts, image digests, migration head, Git revision/dirty state, environment, and
timestamps together. Only a clean receipt tied to the candidate revision supports
environment-specific latency/throughput or deployment claims on a résumé.

## Remaining production responsibilities

- Run the PostgreSQL/Redis suite only against a disposable database ending in `_release_test`, and
  run load/chaos against an approved test stack rather than a shared environment.
- Build and scan both images in the hosted workflow; the local contract alone does not prove a
  registry push or a cluster rollout.
- Supply managed PostgreSQL/Redis, TLS, external secrets, workload identity/KMS policy, OIDC client,
  network policy, backup/restore drills, and provider credentials for the target environment.
- Configure the explicit ingest organization and connect an external alerting system that sends the
  typed alert contract with a dedicated ingest key; the hosted deployment does not run the local
  simulator's threshold evaluator.
- Configure a managed OTLP backend and implement bounded backend-specific log/trace evidence
  adapters before claiming full logs/traces support.
- Keep destructive mitigation disabled until a production action adapter has provider-grade
  idempotency or target-state reconciliation and an environment-specific approval policy.
