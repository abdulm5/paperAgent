# ADR 0013: Collaboration delivery is reconciled, not exactly once

- Status: Accepted
- Date: 2026-07-17

## Context

An incident commander may want PagerAgent to publish an update to Slack or open a GitHub issue.
Those writes cross two independent authority boundaries: the grounded mitigation proposal is not
permission to speak externally, and a successful PostgreSQL commit is not proof that a remote API
accepted a write.

The hardest failure happens after a provider accepts a request but before PagerAgent records the
receipt. Retrying blindly can create duplicate messages or issues. Holding a database lock across
the network does not solve that crash window and would make database health depend on provider
latency. Redis delivery is also at least once, so a worker must remain safe when a job is repeated.

## Decision

PagerAgent treats collaboration as a separate, explicitly approved domain workflow:

1. The server builds Slack and GitHub drafts only from the immutable grounded proposal. A browser
   cannot supply the destination, message, issue title, issue body, connector, or credential.
2. Preparation freezes the output kind, provider, destination, content hash, connector revision,
   and credential revision. Mitigation approval does not approve collaboration.
3. An authorized collaboration decision atomically records the decision, creates a delivery
   receipt, creates a workflow/job, and appends the outbox message in PostgreSQL. Rejection creates
   no workflow or provider work.
4. Redis carries only PagerAgent identifiers. Draft content, provider credentials, and provider
   receipts remain in PostgreSQL or the encrypted connector envelope.
5. Every output receives one stable UUID delivery marker. Slack receives it as `client_msg_id` and
   message metadata. GitHub receives it as an HTML comment in the issue body.
6. Before every write, the adapter performs a bounded recent-history reconciliation. Exactly one
   marker with the exact approved text/title/body becomes a reconciled receipt; a marker attached
   to altered content, contradictory matches, or an incomplete search fail closed. No match permits
   one write.
7. Provider calls use fixed origins and paths, disabled redirects and environment proxies, hard
   request/response/time limits, sanitized errors, and bounded retry hints. The connector and
   credential revisions are checked again immediately before delivery.
8. A successful provider receipt is normalized and fenced to the active workflow attempt. A
   retryable error schedules the existing exponential retry; permanent or exhausted failures move
   both the workflow and collaboration receipt to the dead-letter state.

Slack documents `client_msg_id` on [`chat.postMessage`](https://docs.slack.dev/reference/methods/chat.postMessage/),
but PagerAgent still reconciles by its opaque metadata marker because an ambiguous transport result
cannot be proven from the local request alone. GitHub issue creation requires repository-level
**Issues: write** permission according to the
[`POST /repos/{owner}/{repo}/issues` contract](https://docs.github.com/en/rest/issues/issues#create-an-issue).
That permission is validated only when issue creation is explicitly enabled on the connector.

## Alternatives considered

### Reuse mitigation approval

Rejected. Authority to roll back or disable a flag is not authority to notify a broad channel or
create a durable repository artifact. Separate decisions make the actor and intent auditable.

### Claim exactly-once delivery from an idempotency key

Rejected. PagerAgent cannot atomically commit its database and either provider's database. Stable
markers plus bounded reconciliation provide replay safety, while the documentation keeps the
remaining ambiguity honest.

### Publish directly in the approval HTTP request

Rejected. A slow or unavailable provider would tie external latency to the operator request and
lose the durable retry/dead-letter semantics already implemented by the workflow engine.

### Put complete payloads in Redis

Rejected. The database is the source of truth. Identifier-only stream messages reduce data
exposure and let relay repair reconstruct work without copying mutable content between systems.

## Consequences

- The UI has a visible two-step prepare/approve flow and shows content hashes, attempts, normalized
  receipts, retries, and dead-letter outcomes.
- Slack connectors must bind an organization service to a channel ID and prove bot identity plus a
  bounded history read before enablement. Migration 0011 disables legacy Slack rows for explicit
  revalidation.
- GitHub issue creation is opt-in on an existing service/repository connector. Enabling it expands
  the App permission boundary and therefore forces a new validation cycle.
- Reconciliation deliberately refuses to write when the bounded search is incomplete. Operators
  can inspect the dead-letter receipt instead of receiving a false success or a likely duplicate.
- Delivery recomputes the frozen content hash and checks the exact provider destination under the
  connector revision lock. Composite tenant/incident/proposal/workflow foreign keys prevent a
  corrupted row from combining authority receipts from different incident aggregates.
- This is effectively-once behavior for a marked domain output over at-least-once execution, not a
  distributed exactly-once guarantee.
