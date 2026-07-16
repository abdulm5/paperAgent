# ADR 0011: GitHub deliveries are authenticated, idempotent inputs

- Status: Accepted
- Date: 2026-07-16

## Context

GitHub can provide PagerAgent with two complementary forms of change evidence. Its REST API can
return a bounded point-in-time catalog of commits, pull requests, deployments, and releases, while
webhooks can announce changes as they happen. Neither channel is trustworthy merely because it
uses GitHub-shaped JSON.

A webhook endpoint is public, deliveries may be retried, and a delivery identifier can be replayed.
An installation token is privileged, short-lived material derived from an even more sensitive App
private key. Raw provider documents may contain large patches, user prose, URLs, or fields that do
not belong in an incident evidence graph.

## Decision

PagerAgent treats the connector UUID as routing information, never authentication. For every
GitHub webhook it:

1. applies a hard request-body limit while reading the exact bytes;
2. loads one enabled GitHub connector and derives its organization on the server;
3. verifies `X-Hub-Signature-256` with HMAC-SHA256 and constant-time comparison before JSON parsing;
4. locks the current connector, rejects a changed runtime, and resolves an existing delivery by its
   durable event/body hash before applying the current parser contract;
5. for a new delivery, checks the event, action, installation ID, and repository against strict
   allowlists and the connector contract;
6. normalizes only bounded evidence fields and retains the original body hash; and
7. inserts a PostgreSQL inbox row unique on `(connector_id, delivery_id)`.

A retry with the same delivery ID and body hash returns an idempotent success. Reusing the ID with
a different hash or event fails closed. Invalid signatures create no trusted row. PagerAgent never
stores the raw body. Each row retains the connector and credential revisions that authenticated it,
and a composite `(organization_id, connector_id)` foreign key enforces tenant ownership at rest.

The REST adapter authenticates as a GitHub App with a short-lived RS256 JWT, first resolves the
configured repository's installation and requires its ID to match exactly, then requests a
repository-scoped installation token with read-only contents, pull-request, and deployment
permissions. This separate App-authenticated lookup prevents access to a public repository from
being mistaken for proof of the configured installation. Installation tokens stay in process
memory and are refreshed before expiry or once after a `401`; private keys and tokens never enter
artifacts, workflow messages, traces, audit events, or provider exceptions.

Every outbound request uses the fixed `https://api.github.com` origin, an explicit API version,
TLS verification, disabled environment proxies, disabled redirects, serial execution, strict
timeouts, a total request budget, one bounded page per resource, and a streamed response-byte cap.
The adapter does not sleep or loop through rate limits. Sanitized retryable failures pass to the
existing durable workflow retry policy.

PagerAgent persists normalized provider results as content-hashed evidence snapshots. It discards
raw patches, pull-request and release bodies, asset URLs, and arbitrary provider links. GitHub
deployment evidence corroborates telemetry; it does not replace telemetry's observed active
release or independently authorize a mitigation.

The incident selector snapshots the unique tenant/service connector, ends its read transaction,
and performs provider I/O. It then locks and compare-and-swaps the connector configuration,
connector revision, and credential revision. The lock remains through the evidence transaction,
so revocation or rotation either wins before persistence and discards the stale result, or waits
until the already-authorized evidence commit completes.

## Alternatives considered

### Trust the source IP range

Rejected. Source ranges change and can be obscured by a reverse proxy. The shared webhook secret
authenticates the exact payload bytes end to end.

### Deduplicate deliveries in Redis or process memory

Rejected. Either store can disappear or race across replicas. PostgreSQL owns the durable replay
key and normalized evidence receipt.

### Store the full webhook and REST documents

Rejected. Full documents are unnecessarily large, mutable provider contracts and may include prose,
patches, or links that are irrelevant to causal ranking. Explicit normalizers make the evidence
boundary reviewable.

### Use a personal access token

Rejected. A GitHub App gives repository-scoped installation identity, short-lived tokens, explicit
permissions, webhook integration, and organization-owned revocation.

## Consequences

- GitHub connectors require a service-to-repository binding, App ID, installation ID, multiline
  private key, and independent webhook secret before they can pass the Phase 9B handshake.
- Existing Phase 9A GitHub envelopes remain encrypted but cannot be enabled until their credentials
  are rotated to include the webhook secret and the connector is revalidated. Migration 0009
  disables those rows, clears their old local-only validation receipts, increments their versions,
  and appends a sanitized migration event; downgrade never silently re-enables them.
- Only one enabled GitHub connector may own a service binding within an organization. Enable
  operations serialize on the organization row before checking ownership, preventing concurrent
  requests from both observing an unowned service.
- The webhook inbox is an append-only provider receipt separate from the low-volume connector
  custody audit. A populated inbox blocks migration downgrade.
- GitHub.com is the supported Phase 9B runtime. GitHub Enterprise Server needs an explicit API-path,
  certificate, and deployment-egress design rather than weakening the fixed-origin boundary.
- Network-level egress policy remains a deployment responsibility in addition to the application
  controls described here.
