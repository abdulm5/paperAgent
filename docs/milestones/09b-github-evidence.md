# Phase 9B: Authenticated GitHub evidence

Phase 9B connects the encrypted GitHub custody record from Phase 9A to PagerAgent's evidence
pipeline. An administrator can now prove that an App installation can read exactly one configured
repository, accept signed change notifications without replaying them, and use bounded live GitHub
metadata during an incident investigation.

## What this milestone proves

- A multiline GitHub App private key survives browser submission and authenticated encryption
  without becoming readable through any API.
- Connector validation performs an App-authenticated exact installation lookup, token exchange,
  and repository-read handshake without holding a database row lock across the network.
- A concurrent configuration or credential change invalidates an in-flight handshake result.
- Webhook signatures cover the exact raw bytes, and repository plus installation identity are bound
  to the server-owned connector contract.
- PostgreSQL absorbs same-body delivery retries and rejects a reused delivery ID with different
  content.
- Live commits, pull requests, deployments, deployment status, releases, and webhook receipts are
  normalized into immutable, content-hashed investigation evidence.
- Provider request counts, pages, objects, file metadata, response bytes, timeouts, redirects, and
  error messages are all bounded.
- Organization and service binding choose the connector deterministically; production never falls
  back silently to fixture evidence.
- A connector revocation or credential rotation that completes during collection invalidates the
  stale provider result before any evidence is persisted.

## Trust paths

```text
admin writes PEM + webhook secret
              │
              ▼
Phase 9A envelope vault ──► two-phase validation snapshot
                                  │ no DB transaction
                                  ▼
                       App JWT → exact repository installation lookup
                       → repository-scoped token → repository GET
                                  │
                version + credential revision still current?
                         │ yes                 │ no
                         ▼                     ▼
             sanitized handshake receipt     409 / discard

GitHub webhook raw bytes + delivery ID
              │ size/header limits
              ▼
     HMAC-SHA256 verification before JSON
              │ repository + installation binding
              ▼
   bounded normalization → PostgreSQL inbox uniqueness
              │ same ID/hash          │ same ID/different hash
              ▼                       ▼
        idempotent 2xx              sanitized 409

incident worker → tenant + service connector selection
              │ decrypt only at adapter boundary
              ▼
 bounded GitHub REST snapshots + verified webhook receipts
              │ re-lock exact connector + credential revisions
              ▼
content hashes → deterministic commit/cause ranking → cited brief
```

## GitHub connector contract

| Boundary | Fields |
| --- | --- |
| Inspectable configuration | service, `owner/repository`, App ID, installation ID, fixed public API origin |
| Write-only credentials | unencrypted RSA App private key, independent high-entropy webhook secret |
| Read receipt | field names, credential revision, validation status/message, connector version |
| Never returned or persisted as evidence | private key, webhook secret, App JWT, installation token |

New connectors are disabled. A successful GitHub handshake still requires the administrator's
separate enable mutation. Credential rotation clears the validation receipt and disables the
connector again. The Phase 9B migration applies that same fail-closed state to every preexisting
Phase 9A GitHub connector and records the transition in its custody ledger.

## Bounded provider collection

PagerAgent mints a nine-minute App JWT with a clock-skew allowance, proves the configured
repository resolves to the exact installation ID, exchanges it for an expiring repository-scoped
token, and sends serial requests with the explicit GitHub API version. It reads only one page for
each evidence class and fetches bounded commit/deployment detail records. The installation token is
cached only inside the adapter instance and refreshed once after an authentication rejection.

The persisted models retain causal metadata—SHA, first-line title, author login, timestamps,
bounded filenames and stats, pull-request merge state, deployment environment/status, and release
identity. They intentionally discard patches, bodies, assets, and provider-supplied navigation
URLs. Rate limits and transient provider failures become sanitized workflow failures; the adapter
does not create its own retry loop.

`GITHUB_EVIDENCE_MODE` makes fixture behavior explicit:

- `fixture` always uses the deterministic scenario catalog;
- `auto` uses a unique enabled service binding when present and otherwise uses fixtures, and is
  allowed only for local/test operation; and
- `connector` requires exactly one enabled matching GitHub connector. Non-local environments fail
  startup unless this mode is selected.

The benchmark remains fixture-backed so quality scores are reproducible and do not depend on a
provider account.

## Durable webhook inbox

The public endpoint is `POST /api/v1/webhooks/github/{connector_id}`. Required headers are
`X-Hub-Signature-256`, `X-GitHub-Delivery`, and `X-GitHub-Event`. The endpoint accepts only the
change events PagerAgent knows how to normalize: push, pull request, deployment, deployment status,
and release.

The inbox stores organization, connector, connector revision, credential revision, delivery ID,
event/action, canonical repository, installation ID, body SHA-256, bounded normalized fields, and
receipt time. A composite tenant/connector foreign key prevents cross-organization rows at rest.
It does not perform a GitHub API call in the request. Incident commanders and administrators can
inspect the tenant-scoped normalized receipts; other roles receive no connector visibility.

## Evidence graph

A live investigation keeps telemetry as the observed operational source and adds:

- `commit_catalog` with `provider=github_app` and connector provenance;
- `github_pull_request_history`;
- `github_deployment_history`;
- `github_release_history`; and
- `github_webhook_history` containing authenticated delivery receipts.

Every artifact has a canonical content hash. Commit and cause candidates cite the GitHub artifacts
alongside telemetry, deployment, cluster, configuration/dependency, and runbook evidence.

## Interview walkthrough

1. Create the GitHub connector and point out that the PEM textarea is write-only and multiline.
2. Show the disabled record, encrypted envelope revision, and absence of secret values in the API.
3. Validate it. Explain App JWT versus installation token and why the network call occurs outside
   the database lock.
4. Enable the connector and show the service-to-repository ownership rule.
5. Send a correctly signed webhook, then resend the same delivery ID. Show one inbox row and an
   idempotent replay response.
6. Change one payload byte without changing the delivery ID and show the sanitized conflict.
7. Run the checkout incident with connector evidence enabled. Open the GitHub source receipt,
   immutable hashes, normalized deploy/PR/release snapshots, and ranked commit explanation.
8. Rotate the credentials and show that the connector immediately returns to disabled/unvalidated.

The key design sentence is: **GitHub supplies authenticated, bounded evidence; telemetry and
deterministic policy still decide what PagerAgent may claim or propose.**

## Deferred work

- GitHub Enterprise Server API-path and certificate policy
- asynchronous provider-inbox enrichment for very high webhook volume
- deployment-level network egress enforcement and managed credential KMS/HSM
- durable GitHub issue creation, which is a Phase 9D write adapter with separate approval and
  idempotency requirements
