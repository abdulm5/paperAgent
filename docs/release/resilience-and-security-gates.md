# Resilience and security release gates

PagerAgent treats a release as an evidence-producing exercise. A green unit-test run is
necessary, but it does not prove PostgreSQL row-lock behavior, Redis consumer recovery, or
the safety of a materialized hosted configuration. These gates make those claims executable
and leave JSON receipts that can be attached to a release or shown in the demo.

## What the gates prove

| Gate | Contract under test | Passing evidence |
| --- | --- | --- |
| PostgreSQL worker contention | A committed lease fences competing workers before a handler runs | Nine workers contend; the handler runs once, one result completes, and eight skip |
| PostgreSQL outbox contention | `FOR UPDATE SKIP LOCKED` lets relays drain independent rows without duplicate publication | Four relays drain 24 receipts; every job is published exactly once in the test broker |
| Hosted-admin contention | The organization lock serializes authority mutations | Two admins concurrently demote one another; one mutation commits, the newly unauthorized actor is rejected, and one admin remains |
| Redis dead-letter durability | The DLQ is independent of recreation of the transient source stream | A pending message is dead-lettered and acknowledged; its receipt remains after source-stream recreation |
| HTTP load | Alert validation, authentication, database writes, active-incident deduplication, and response serialization hold under bounded concurrency | Error rate, p95 latency, and throughput meet explicit thresholds; every expected fingerprint maps to exactly one stable incident and distinct fingerprints remain distinct |
| Redis/worker chaos | PostgreSQL accepts workflow intent while Redis is absent, the relay repairs delivery, and a restarted worker drains its backlog | The exact created incidents and workflows complete; their saved stream IDs are no longer pending and their job IDs are absent from the DLQ; cleanup attempts to restart stopped services |
| Security | Hosted settings pass the application's fail-closed validator and live endpoints preserve auth, non-reflection, sanitized validation, and CORS boundaries | Both the configuration and live HTTP sections pass without printing secret values |

The tests establish *effectively-once domain execution behind a database fence*. They do not
claim exactly-once delivery to an external provider. Slack and GitHub delivery remains an
at-least-once workflow reconciled by idempotency keys and durable receipts.

## Where the gates run

| Stage | Enforced before it can pass | Explicit evidence retained by the operator |
| --- | --- | --- |
| Pull request / reusable CI | lint, unit/frontend/simulator tests, migration up/down/up, the dedicated PostgreSQL/Redis integration suite, manifest validation, hardened image builds, and image scans | none beyond the workflow logs and artifacts |
| Semantic-tag release | the reusable CI gate, digest builds, image scans, release metadata/manifest publication, and OCI provenance attestations | tagged GitHub Release and attested image digests |
| Protected deployment | release-asset verification, image-attestation verification, static render checks, external-secret presence, Alembic Job completion, and workload readiness | deployment run, migration logs, and rolled-out revisions |
| Manually dispatched `release-evidence` workflow | isolated local Compose stack, bounded exact-cardinality load gate, local live-HTTP security gate, exact-workflow chaos recovery, and teardown | revision/run-attempt-named JSON artifact bundle retained for 30 days |
| Environment qualification | not inferred from CI; run deliberately against an approved disposable or staging environment | load, chaos, and hosted-security JSON receipts plus target/environment metadata |

A green pull request therefore does not claim a production load result or a real chaos recovery.
The dispatchable workflow makes the local evidence repeatable, but it still does not materialize a
hosted IdP/KMS/managed-database environment. Target-specific measurements remain explicit promotion
evidence.

## 1. Real PostgreSQL and Redis integration suite

Start only the infrastructure dependencies, then recreate a database whose explicit
`_release_test` suffix is enforced by the contention suite. The relay cases intentionally claim all
due outbox rows in that database, so never point this suite at the normal `pageragent` database,
staging, or production:

```bash
docker compose up --detach postgres redis
docker compose exec -T postgres dropdb --if-exists -U pageragent pageragent_release_test
docker compose exec -T postgres createdb -U pageragent pageragent_release_test
(
  cd backend
  DATABASE_URL=postgresql+psycopg://pageragent:pageragent@localhost:5432/pageragent_release_test \
    .venv/bin/alembic upgrade head
)
```

Run every real-infrastructure test, including the release contention gates:

```bash
(
  cd backend
  PAGERAGENT_INTEGRATION_TESTS=1 \
  DATABASE_URL=postgresql+psycopg://pageragent:pageragent@localhost:5432/pageragent_release_test \
  REDIS_URL=redis://localhost:6379/15 \
    .venv/bin/pytest -q tests/integration
)
```

The environment flag is deliberate. With it unset, the suite skips rather than silently
substituting SQLite for PostgreSQL locks or an in-memory broker for Redis Streams. The commands use
the backend-local `.venv` created by the root README's development setup.

## 2. Bounded HTTP load gate

Bring up the application stack, then run the local-only default profile:

```bash
docker compose up --detach --build
./scripts/run-load-gate.py \
  --requests 200 \
  --concurrency 20 \
  --unique-fingerprints 40 \
  --output /tmp/pageragent-release/load.json
```

Defaults fail the command when error rate exceeds 1%, p95 exceeds 1.5 seconds, throughput is
below 5 requests/second, no duplicate is observed, the expected fingerprint set is incomplete, a
fingerprint maps to more than one incident, or distinct fingerprints collapse to the same incident.
These are reproducible demo gates, not universal production SLOs. Establish environment-specific
thresholds from repeated warm runs and record the hardware, database size, and commit alongside the
JSON result.

The script refuses non-loopback targets unless `--allow-remote` is explicit because it creates
real incident records, and remote API and telemetry URLs must use HTTPS. It never writes the ingest
key to its report. A run uses a fresh UUID namespace, so its incidents cannot deduplicate against a
previous run. For an approved staging run, provide the real allow-listed telemetry URL explicitly:

Set `PAGERAGENT_STAGING_ORIGIN` and `PAGERAGENT_STAGING_TELEMETRY_URL` to the target's real HTTPS
values first; reserved documentation domains are rejected.

```bash
./scripts/run-load-gate.py \
  --base-url "$PAGERAGENT_STAGING_ORIGIN" \
  --telemetry-url "$PAGERAGENT_STAGING_TELEMETRY_URL" \
  --allow-remote \
  --output /tmp/pageragent-release/load-staging.json
```

## 3. Bounded chaos drill with best-effort cleanup

The drill requires an already healthy local Compose stack and explicit confirmation:

```bash
./scripts/run-chaos-drill.py \
  --confirm \
  --output /tmp/pageragent-release/chaos.json
```

It performs this sequence:

1. Verify that the API, checkout simulator, Redis, relay, and worker are already running and that
   database/schema readiness passes; authenticate through the local development identity boundary.
2. Stop Redis and ingest an alert. The API still commits the incident, workflow, and outbox
   receipt to PostgreSQL.
3. Start Redis and restart the relay and worker. The drill follows that exact incident's response
   workflow until every job completes, its saved delivery IDs are absent from `XPENDING`, and its
   job IDs are absent from the dead-letter stream.
4. Stop the workflow worker, ingest a second alert, and verify that exact workflow receives a saved
   stream delivery while the relay continues publishing.
5. Start the worker and follow the second workflow to completion, acknowledgement, and confirmed
   DLQ absence. Aggregate stream length is diagnostic only; it is never the recovery assertion.

A `finally` path makes a best-effort start of Redis, the relay, and the worker after a failed
assertion or timeout. Inspect `docker compose ps` after any failed or interrupted drill; the
workflow-recovery receipt does not prove cleanup succeeded. The drill does not delete volumes or
incidents. It is restricted to a loopback API origin and will not run without `--confirm` or
`PAGERAGENT_CHAOS_CONFIRM=1`.
If a stack overrides `WORKFLOW_STREAM_NAME`, `WORKFLOW_CONSUMER_GROUP`, or
`WORKFLOW_DEAD_LETTER_STREAM`, pass the same values through the environment or the corresponding
command-line options.

This drill intentionally leaves PostgreSQL running: that is the source of truth whose
durability absorbs a transport outage. Database loss belongs in the managed service's backup,
restore, failover, and readiness drills rather than a developer script that could destroy
local state.

## 4. Production configuration and live security gate

Materialize the deployment environment only on a trusted runner, restrict its permissions,
run the gate, and remove it afterward. The script reports field names and pass/fail state but
never configuration values or the application's potentially secret-bearing validation error.

First validate the materialized production values without contacting an application:

```bash
chmod 600 /tmp/pageragent-production.env
backend/.venv/bin/python scripts/run-security-gate.py \
  --env-file /tmp/pageragent-production.env \
  --skip-live \
  --output /tmp/pageragent-release/security-config.json
```

Then run the combined configuration/live contract against the matching HTTPS staging deployment:

Set `PAGERAGENT_STAGING_ORIGIN` to that deployment's real HTTPS origin first.

```bash
backend/.venv/bin/python scripts/run-security-gate.py \
  --env-file /tmp/pageragent-staging.env \
  --base-url "$PAGERAGENT_STAGING_ORIGIN" \
  --allow-remote \
  --output /tmp/pageragent-release/security-staging.json
```

Configuration validation checks that all hosted identity and KMS boundaries are explicitly
present, no known development secret remains, no production KMS endpoint override exists,
and the environment passes the same `Settings` model used to boot the API. In particular,
that validator requires:

- OIDC authorization-code login with HTTPS issuer, JWKS, authorization, token, callback, and
  frontend URLs;
- same-origin callback/frontend URLs and distinct `__Host-` session and login cookies;
- secure cookies, distinct non-development session/transaction/ingest secrets, an explicitly
  configured ingest organization slug, and explicit HTTPS CORS;
- an exact trusted-host allowlist containing the hosted application hostname;
- a non-local `postgresql+psycopg` URL with exactly one required/verified TLS mode and a non-local
  `rediss://` URL with `ssl_check_hostname=true` and no weakened certificate requirement;
- `DURABLE_MITIGATION_ENABLED=false` until a production action adapter provides durable
  idempotency or target-state reconciliation;
- AWS KMS custody with an exact key ARN, matching region, stable application context, no
  endpoint override, and no local decryption-key ring;
- connector-backed GitHub and Prometheus evidence with bounded HTTPS origins.

The live section then verifies:

- availability, process-liveness, and database/schema-readiness contracts are reachable and
  dependency responses are marked `no-store`;
- baseline anti-sniffing, framing, referrer, and permissions headers, plus hosted HSTS and CSP;
- interactive API documentation is disabled in a hosted environment;
- an untrusted Host header is rejected before application routing;
- a protected incident route returns `401` with a Bearer challenge;
- an invalid ingest key is rejected without reflecting the key or request marker;
- an authenticated but malformed alert returns only the generic validation envelope;
- an untrusted credentialed CORS origin receives no allow-origin header.

Use `--skip-live` to validate only an environment file. A combined hosted run checks that the live
health endpoint reports the same hosted environment as the materialized file, so a local process
cannot be mistaken for the target. Remote targets require HTTPS and explicit `--allow-remote`; the
live malformed request cannot create an incident, but it still uses the configured ingest
credential.

The configuration-only result proves required fields and fail-closed syntax/policy, not that an
IdP, KMS key, database, Redis service, or provider credential exists or is reachable. Reserved or
example values are not deployment evidence: replace them, run the combined live gate, and retain
separate provider/workload-identity qualification before making a hosted claim.

## Release evidence checklist

Store the following together under the release identifier:

- Git commit SHA and migration head;
- full backend and frontend test results;
- real PostgreSQL/Redis integration output;
- load, chaos, and security JSON receipts;
- verified release assets, backend/frontend image digests, image-attestation verification output,
  deployment revision, and runtime-secret revision;
- operator, UTC start/end times, test environment, and any approved threshold exception.

The load, chaos, and security receipts include both `source_revision` and `source_dirty`. Use a
receipt as release or résumé evidence only when the revision matches the candidate and
`source_dirty` is `false`; `null` means Git state could not be determined and needs an independent
provenance record.

Do not commit materialized environment files, API keys, raw provider responses, database
dumps, or access logs. A failed gate blocks the release until it passes or a named owner records
a time-bounded exception with the affected invariant and rollback decision.
