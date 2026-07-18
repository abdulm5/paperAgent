# Managed release and deployment

PagerAgent ships as two immutable images and four long-running workloads:

| Runtime | Image | Replicas | Responsibility |
| --- | --- | ---: | --- |
| API | `pageragent-backend` | 2 | HTTP/BFF, webhooks, SSE, health |
| Workflow worker | `pageragent-backend` | 2 | Durable incident and collaboration jobs |
| Outbox relay | `pageragent-backend` | 2 | PostgreSQL outbox to Redis stream relay |
| Dashboard | `pageragent-frontend` | 2 | Static same-origin browser application |

Every release also runs a one-shot compatibility-aware migration Job from the exact backend image
digest used by the API and workers. PostgreSQL, Redis, the OIDC provider, AWS KMS, Prometheus, and
telemetry sources are managed dependencies rather than in-cluster demo services.

## Deployment contract

The provider-neutral Kubernetes manifests live in `deploy/kubernetes`:

- `bootstrap` owns only the Namespace and is applied once by a cluster administrator.
- `foundation` owns five process-specific service accounts, non-secret configuration, Services, TLS
  Ingress, and disruption budgets inside that Namespace.
- `migration` owns only the bounded `python -m app.db.migrate` Job.
- `workloads` owns the API, worker, relay, and frontend Deployments.
- the root kustomization renders the complete release for review or archival.

The separation is operationally significant. Namespace-scoped deployment credentials never apply
`bootstrap`; they apply foundation resources, wait for the compatibility-aware migration command to
complete, and only then start a rolling workload update. That command upgrades an older database to
the bundled head, no-ops when the current or a future schema explicitly permits this application
generation, and rejects a future incompatible schema. A migration-gate failure leaves the previous
workloads serving traffic.

The pods meet Kubernetes' restricted security profile: non-root users, read-only root filesystems, `RuntimeDefault` seccomp, no privilege escalation, and all Linux capabilities dropped. Each workload declares CPU/memory requests and limits. The API and frontend spread replicas across nodes and use disruption budgets.

The production dashboard image serves its SPA through an unprivileged Nginx process with a
same-origin content-security policy, HSTS, frame denial, MIME-sniffing protection, a restrictive
permissions policy, and a dedicated `/healthz` probe. Native progress elements preserve the score
visuals without inline style attributes, so scripts, styles, and network connections remain
same-origin only.

No Kubernetes `Secret` is committed. The manifests reference four separately managed Secret
objects—database, transport, API identity, and connector custody—which must already exist in the
`pageragent` Namespace. The API, worker, relay, migration, and frontend each have a separate service
account with token automount disabled; a provider overlay may add workload-identity projection only
to identities that need it.

## Prerequisites

Provide these platform capabilities before the first deployment:

1. A Kubernetes cluster with an Ingress controller, a real public DNS record routed to it, a valid
   TLS secret named `pageragent-tls`, and a cluster administrator who can run
   `kubectl apply -k deploy/kubernetes/bootstrap` once. Protected deployment verifies the public
   HTTPS API and dashboard, so DNS and certificate validation must work before promotion.
2. Managed PostgreSQL and Redis endpoints with encryption in transit, peer/hostname verification,
   backups, and restore testing.
3. An external alerting system that can send the typed PagerAgent alert contract to
   `POST /api/v1/alerts` with a dedicated ingest key and an allow-listed telemetry URL. The local
   simulator's threshold evaluator is not a hosted workload.
4. An OIDC client whose callback is `https://<public-host>/api/v1/auth/oidc/callback`.
5. An AWS KMS key and provider workload identity for only `pageragent-api` and
   `pageragent-workflow-worker`. Grant only the KMS operations used by envelope encryption; do not
   store long-lived AWS access keys in a Kubernetes Secret.
6. Network paths from PagerAgent to OIDC, KMS, PostgreSQL, Redis, GitHub, Slack, Prometheus,
   telemetry, the workload-identity token exchange, and the model provider if enabled.
7. Registry pull access for the two release images. Add an `imagePullSecret` patch if the GHCR
   packages are private.

Kubernetes NetworkPolicy rules are intentionally environment-owned. DNS-aware egress and
Ingress-controller selectors are provider-specific; apply a default-deny policy only after
explicitly allowing cluster DNS, managed data stores, KMS, OIDC, evidence providers, and
collaboration providers.

Ingress hardening is also controller-owned. Require HTTP-to-HTTPS redirection, rate-limit the OIDC
login path, and ensure proxy/load-balancer access logs omit the callback query string before using
hosted login; the application redacts its own callback access log but cannot control upstream logs.

## Runtime secret contract

Materialize these objects from a cloud secret manager or an External Secrets operator. The split is
an authorization boundary, not just file organization:

| Secret | Required values | Loaded by |
| --- | --- | --- |
| `pageragent-database-secrets` | `DATABASE_URL`: non-local `postgresql+psycopg` URL with `sslmode=require`, `verify-ca`, or `verify-full` | API, worker, relay, migration |
| `pageragent-transport-secrets` | `REDIS_URL`: non-local `rediss://` URL with `ssl_check_hostname=true` for the durable workflow stream | API, worker, relay |
| `pageragent-api-secrets` | distinct session and ingest secrets; explicit ingest organization slug; OIDC issuer, audience, JWKS, client ID/secret, authorization/token URLs, callback/frontend URLs, default organization, and canonical 32-byte transaction key | API only |
| `pageragent-connector-secrets` | exact KMS key ARN and matching region; optional model-provider key when configured | API and worker only |

The API object uses these exact keys:

```text
PAGERAGENT_SESSION_SECRET
PAGERAGENT_INGEST_API_KEY
PAGERAGENT_INGEST_ORGANIZATION_SLUG
PAGERAGENT_OIDC_ISSUER
PAGERAGENT_OIDC_AUDIENCE
PAGERAGENT_OIDC_JWKS_URL
PAGERAGENT_OIDC_CLIENT_ID
PAGERAGENT_OIDC_CLIENT_SECRET
PAGERAGENT_OIDC_AUTHORIZATION_URL
PAGERAGENT_OIDC_TOKEN_URL
PAGERAGENT_OIDC_REDIRECT_URI
PAGERAGENT_OIDC_FRONTEND_URL
PAGERAGENT_OIDC_DEFAULT_ORGANIZATION_SLUG
PAGERAGENT_OIDC_TRANSACTION_KEY
```

The session and ingest secrets must be different because the ingest credential is shared with a
machine alert sender and must never become a session-signing key. The ingest organization slug is
server-owned routing authority: it must name an existing organization intended to own machine
incidents. For a single-tenant deployment, set it to the same organization established by the
offline bootstrap and used as the OIDC default. If they differ, explicitly provision responders in
the ingest organization before accepting alerts.

`REDIS_URL` must use a non-local TLS endpoint and include hostname checking, for example
`rediss://user:password@redis.acme.internal:6379/0?ssl_check_hostname=true`. If
`ssl_cert_reqs` is present it must be `required`; query parameters may not override the URL's host
or port or weaken certificate verification.

The connector object uses `PAGERAGENT_CONNECTOR_KMS_KEY_ARN` and
`PAGERAGENT_CONNECTOR_KMS_REGION`; add `OPENAI_API_KEY` only when model synthesis is enabled.

The relay needs database and transport access but no browser identity or KMS custody. Migration needs
only the database. Frontend receives no runtime Secret. Connector provider credentials such as
GitHub or Slack tokens remain encrypted connector revisions in PostgreSQL; they are not copied into
these objects.

`OPENAI_API_KEY` is optional when deterministic synthesis is used. Production KMS custody must not
be mixed with local connector master/decryption keys. Keep
`DURABLE_MITIGATION_ENABLED=false` until the target action adapter has durable provider idempotency
or target-state reconciliation.

Before promotion, confirm DNS, the external secrets, TLS secret, workload identity, OIDC redirect
registration, ingest organization, and external alert sender all refer to the same environment.
Never copy production session, ingest, OIDC transaction, or connector credentials into staging.

## GitHub Environment contract

Create protected `staging` and `production` GitHub Environments. Require reviewers for production and configure:

| Type | Name | Purpose |
| --- | --- | --- |
| Secret | `KUBE_CONFIG_B64` | Base64 kubeconfig restricted to the PagerAgent namespace |
| Variable | `PAGERAGENT_PUBLIC_HOST` | Real public DNS name, without scheme |
| Variable | `PAGERAGENT_PROMETHEUS_ORIGIN` | Exact HTTPS Prometheus origin allowed for connectors |
| Variable | `PAGERAGENT_TELEMETRY_ORIGIN` | Exact HTTPS telemetry origin allowed for evidence collection |

The deploy dispatch also requires a `release_tag` and a non-secret `runtime_secret_revision` from the
external secret manager. Changing the secret revision updates every pod-template annotation and
therefore creates an observable rollout without putting secret material in Git.

The kubeconfig is the provider-neutral fallback. It needs rights only inside the pre-created
`pageragent` Namespace; it does not need permission to create Namespaces. Prefer short-lived GitHub
OIDC federation to the target cloud when adopting a specific provider. The deployment script
rejects reserved/example domains, wildcard trusted hosts, mutable image tags, malformed origins,
missing environment values, and unsafe revision strings.

If the cluster's workload identity requires ServiceAccount annotations or projected tokens, add a
reviewed provider overlay for `pageragent-api` and `pageragent-workflow-worker`; do not enable it on
relay, migration, or frontend. Keep annotations free of credentials.

Enable GitHub **immutable releases** for the repository. Release publication intentionally fails if
`gh release verify` cannot verify the tagged release assets. GitHub artifact attestations are
available to public repositories across current plans; a private or internal repository needs
GitHub Enterprise Cloud. Make the résumé repository public before exercising this pipeline, or use
an eligible private organization—do not weaken the attestation gate to make an ineligible run pass.

## CI, release, and deployment flow

`.github/workflows/ci.yml` is both the pull-request gate and a reusable release gate. It verifies:

- Python lint, unit tests, migration up/down/up behavior, and the PostgreSQL/Redis integration suite;
- frontend tests and production build;
- simulator tests;
- shell syntax, Compose resolution, Kubernetes rendering, deployment invariants, production image
  builds, non-root image users, response headers, and blocking HIGH/CRITICAL image scans. Every
  external action is pinned to a verified full commit; Dependabot proposes reviewed updates rather
  than allowing mutable major tags to change a release run silently.

`.github/workflows/release-evidence.yml` is intentionally manual. It builds an isolated local
Compose stack, runs the bounded load, local live-security, and exact-workflow chaos gates, uploads
their revision/run-attempt-named receipts for 30 days, and tears the stack down even after failure.
That artifact is repeatable local resilience evidence, not a substitute for target-environment
hosted-configuration or managed-service qualification.

A semantic tag such as `v1.0.0` starts `.github/workflows/release.yml`. The workflow reruns the full
quality gate, builds and scans multi-architecture backend and frontend images, publishes both to
GHCR with SBOM metadata, and creates an OCI provenance attestation for each digest. It publishes an
immutable GitHub Release containing:

- `pageragent-release.yaml`, pinned to both immutable image digests;
- `release-metadata.txt`, mapping the Git revision and release tag to the two full
  `ghcr.io/...@sha256:...` references.

The release workflow does not deploy automatically. Start the protected `deploy` workflow with the
published `release_tag`, target GitHub Environment, and external `runtime_secret_revision`. It does
not accept arbitrary image input. The workflow:

1. checks out the tag and resolves it to a full source commit;
2. uses `gh release verify`, downloads only the release manifest and metadata, and verifies each
   local asset with `gh release verify-asset`;
3. binds the tag, source revision, expected GHCR repository names, full digests, and manifest through
   `deploy/verify_release.py`;
4. verifies both OCI attestations against this repository's `release.yml`, the tag ref, the source
   commit, and GitHub-hosted runner identity;
5. configures environment host/origin and release/secret revision annotations, renders, and
   statically validates the immutable release;
6. applies namespace-scoped foundation resources and verifies all four runtime Secrets plus the TLS
   Secret exist;
7. deletes the prior completed migration Job, runs `python -m app.db.migrate` from the exact backend
   digest, and waits up to ten minutes;
8. applies Deployments and waits for every rollout to become ready; and
9. verifies the public HTTPS API readiness receipt reports production/database/schema readiness and
   the dashboard health endpoint returns `204`.

This explicit promotion step lets the same verified release move from staging to production without
rebuilding it. Requiring a new secret revision on each rotation also avoids the silent “Secret
changed but existing pods did not restart” failure mode.

## Health and rollout behavior

The API exposes two intentionally different probes:

- `/api/v1/health/live` proves the process can serve HTTP and is safe for liveness/startup checks.
- `/api/v1/health/ready` verifies PostgreSQL connectivity and one application-compatible Alembic
  head. Migration `20260718_0013` creates singleton
  `pageragent_schema_contract.minimum_application_generation=12`. The image's own head reports
  `current`; one future linear head reports `forward_compatible` only when its explicit marker is
  positive and no greater than this application's generation 12. Missing or higher markers report
  `application_incompatible`; multiple, malformed, or older heads report `migration_required`.
  Redis is deliberately excluded so an interruption does not remove every API replica while durable
  jobs wait for recovery.

Probe requests and the image-level health check send the configured public Host header;
`PAGERAGENT_TRUSTED_HOSTS` and `PAGERAGENT_HEALTH_HOST` are both bound to that exact public host.
The API rolling strategy permits zero
unavailable replicas. Workers and relays can briefly overlap during rollout because their leases,
fences, outbox claims, and idempotency records provide the concurrency boundary.

This is an expand/contract contract, not permission for arbitrary future schemas. Migrations are one
linear, monotonically generated head; additive changes precede dependent code and retain the oldest
supported application generation, while an incompatible migration raises the marker. Destructive
column/table cleanup must wait for a separately coordinated maintenance release after older images
are retired.

## Rollback and recovery

Application rollback means promoting a prior verified release tag and its attested image digests; it
does not mean mutating an existing tag or pasting an arbitrary digest. Migration `20260718_0013` is
the boundary where PagerAgent begins recording and enforcing compatibility-aware rollback: a future
database is safe for this application only when its explicit minimum generation is at most 12.
Never infer compatibility for a release older than this contract from revision ordering alone;
qualify it separately. Database downgrades are never automatic and require a separately reviewed
recovery plan and backup.

If the migration gate fails, inspect `job/pageragent-migrate`; the deployment workflow prints its
logs and does not update workloads. If a rollout fails after a compatible migration, the prior
ReplicaSet remains in revision history. Restore it only when that release is covered by the marker
contract or has been separately qualified, then pin the verified prior digests through the
deployment workflow.

Treat restore drills, KMS denial drills, Redis interruption tests, and dead-letter reconciliation as release evidence rather than one-time setup tasks.

## Local release-image checks

The backend image uses named build contexts so the same digest contains the versioned runbooks and seeded evaluation scenarios:

```bash
docker build \
  --target production \
  --build-context runbooks=./runbooks \
  --build-context scenarios=./scenarios \
  --tag pageragent-backend:local \
  ./backend

docker build --target production --tag pageragent-frontend:local ./frontend
docker compose config --quiet
```

The development Compose stack supplies the same named contexts automatically.

## Primary references

- [Kubernetes Deployments](https://kubernetes.io/docs/concepts/workloads/controllers/deployment/)
- [Kubernetes Jobs](https://kubernetes.io/docs/concepts/workloads/controllers/job/)
- [Kubernetes liveness, readiness, and startup probes](https://kubernetes.io/docs/concepts/configuration/liveness-readiness-startup-probes/)
- [Kubernetes Pod Security Standards](https://kubernetes.io/docs/concepts/security/pod-security-standards/)
- [GitHub deployment environments](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/manage-environments)
- [GitHub artifact attestations](https://docs.github.com/en/actions/concepts/security/artifact-attestations)
- [Docker Build attestations](https://docs.docker.com/build/metadata/attestations/)
