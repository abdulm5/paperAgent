# Phase 9D: Durable collaboration outputs

Phase 9D turns a grounded incident proposal into an explicitly approved Slack update or GitHub
issue. The output uses PagerAgent's existing PostgreSQL outbox, Redis relay, leased worker,
attempt fencing, retries, and dead letters, while adding provider-specific reconciliation for the
external-write crash window.

## What this milestone proves

- External communication has a separate approval from mitigation. A rejected draft never creates
  a workflow, outbox message, or provider request.
- Draft text, destinations, connector selection, and delivery markers are server-owned. The client
  chooses only an output kind for the current immutable proposal.
- Preparation freezes a canonical content hash and the exact connector and credential revisions.
  Later configuration edits, credential rotation, disablement, or proposal changes invalidate the
  stale authority.
- Approval records the human decision and queues delivery in one database transaction. Redis
  contains only the collaboration output ID.
- Slack and GitHub use a stable UUID marker and bounded reconciliation before writing, so an
  ambiguous prior attempt can recover its provider receipt instead of blindly duplicating output.
  The marker is accepted only when the remote text/title/body exactly matches the approved packet.
- Provider responses are normalized into small, tenant-safe receipts. Tokens, raw response bodies,
  draft content, and exception text never enter workflow errors or telemetry.
- Retryable failures follow the existing exponential schedule and provider retry hint. Permanent
  failures and exhausted attempts synchronize the workflow and output into a visible dead letter.

## Domain and authority flow

```text
grounded proposal + incident service
              │ responder chooses Slack and/or GitHub kind
              ▼
server selects one enabled tenant/service connector
              │ build grounded content; freeze destination + revisions + hash
              ▼
pending collaboration output (no provider work yet)
              │
              ├── reject ──► immutable decision; no workflow/outbox
              │
              └── approve ─► decision + workflow + job + outbox (one DB transaction)
                                      │ identifier-only Redis delivery
                                      ▼
leased worker checks connector/revisions → checkpoint `delivering`
                                      │ no DB lock across provider I/O
                                      ▼
bounded marker reconciliation → existing receipt OR one marked write
                                      │
                                      ├── retryable ─► scheduled retry
                                      ├── permanent/exhausted ─► dead letter
                                      └── success ─► normalized fenced receipt
```

The mitigation proposal's `slack_update` prose is intentionally not copied to the provider. The
collaboration draft builder uses only grounded, validated proposal fields such as impact, likely
cause, recommended action, risk, verification steps, and evidence identifiers. This keeps prompt
output downstream of the same deterministic content and authority boundary as the rest of the
incident workflow.

## Provider contracts

### Slack

The connector binds one service to a Slack channel ID, not a mutable display name. Validation calls
the fixed Slack origin to prove bot identity and a bounded channel-history read. Delivery searches
at most one capped recent window for PagerAgent metadata containing the output UUID, then calls
`chat.postMessage` once with the UUID in both `client_msg_id` and message metadata. Redirects,
environment proxies, oversized bodies, malformed duplicate JSON keys, unexpected channels, and
incomplete reconciliation fail closed. Rate limits become bounded workflow retry hints.

The bot needs `chat:write` and enough history access for the selected conversation. Slack's
[`chat.postMessage` reference](https://docs.slack.dev/reference/methods/chat.postMessage/) describes
the write API; production installation and channel membership remain administrator-owned setup.

### GitHub issues

Issue delivery reuses the tenant/service/repository GitHub App connector but requires the explicit
`issue_creation_enabled` flag. Connector validation first proves the Phase 9B repository-read
contract and then proves issue-write authorization without creating an issue. The publisher uses a
short-lived installation token, a fixed GitHub API origin and path, and a hidden
`pageragent-delivery:<UUID>` marker.

Before creation it scans a bounded recent issue window. One exact marker yields a reconciled
receipt; multiple matches, conflicting markers, or an incomplete scan fail closed. The App must
have repository **Issues: write** in addition to the Phase 9B read permissions documented by
GitHub's [issue creation API](https://docs.github.com/en/rest/issues/issues#create-an-issue).

## Durable state and failure semantics

`collaboration_outputs` is the immutable prepared intent plus current lifecycle state.
`collaboration_decisions` records the separate actor decision, expected version, content hash, and
note. `collaboration_deliveries` is the one-to-one operational receipt with the stable idempotency
key, attempt count, normalized provider receipt, safe error code, and timestamps.

Immediately before that checkpoint, the worker locks the exact connector revision, recomputes the
approved packet's canonical hash, verifies its frozen channel/repository, and follows composite
tenant → incident → proposal → workflow foreign keys. The worker records `delivering` before
network I/O. A process crash after the provider write leaves
that checkpoint, and the next leased attempt reconciles the same marker. Workflow fencing prevents
an expired worker from later committing a receipt over its successor. Final workflow failure and
the collaboration receipt update in the same database transaction.

## Operator walkthrough

1. Create and validate a Slack connector for the incident service, or enable GitHub issue creation
   and revalidate the existing GitHub App connector.
2. Run an incident through investigation until the cited proposal is waiting for mitigation
   approval.
3. In **Collaboration outputs**, prepare Slack, GitHub, or both. Inspect the exact destination,
   grounded preview, content hash, and connector revision.
4. As incident commander, approve one output and reject the other. Point out that neither action
   changes the mitigation decision.
5. Follow the queued/delivering status through the workflow stream. Show the Slack timestamp or
   GitHub issue URL receipt when delivered.
6. For the reliability story, force a provider timeout after accepting a request. On retry, show
   the `reconciled` receipt and prove there is only one remote artifact. Then demonstrate a
   permanent permission failure reaching the visible dead-letter state.

## Interview explanation

Lead with this sentence:

> I separated grounded content, human communication authority, durable scheduling, and remote
> delivery into four receipts. Since PostgreSQL cannot transact with Slack or GitHub, every output
> gets a stable provider-visible marker and retries reconcile before they write.

The key distinction is “effectively once for a marked domain output,” not “exactly once.” Show the
approval transaction, the identifier-only outbox payload, the adapter reconciliation branch, and
the final normalized receipt. That sequence demonstrates authorization design, distributed-systems
failure reasoning, secure connector custody, and an operable dead-letter path in one feature.
