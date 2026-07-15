# ADR 0009: Scope human access through database-backed organization membership

- Status: Accepted
- Date: 2026-07-14

## Context

PagerAgent's first seven phases assumed one trusted local operator. API routes could read every
incident, workflow events were streamed globally, and mutation bodies supplied their own `actor`
string. Adding a login screen alone would not fix those authorization and audit-integrity gaps.

The production boundary needs to answer three independent questions on every request:

1. Is the identity token authentic?
2. Is that user an active member of the selected organization?
3. Does the membership's current role grant this exact operation?

## Decision

PagerAgent uses the organization as its first tenant boundary.

- Users are keyed by `(issuer, subject)`, not email.
- Membership and role are loaded from PostgreSQL for every request, so deactivation and role
  changes take effect without waiting for a token to expire.
- Roles map to explicit permission strings; handlers do not rely on numeric role ordering.
- `incidents.organization_id` is the tenant root. Descendant records are authorized by joining
  through their owning incident.
- Active-alert deduplication is unique per organization, not globally.
- Cross-organization identifiers return `404` so resource existence is not disclosed.
- Human audit actors are derived from the verified principal. Browser request bodies cannot
  impersonate another responder.

The browser receives a short-lived PagerAgent session JWT in a same-origin, HTTP-only,
`SameSite=Strict` cookie. Mutations made with that cookie also require the session's CSRF token in
an `X-CSRF-Token` header. The same signed session may be used as a Bearer token by local demo
scripts, where browser CSRF does not apply. Tokens are never put in URLs.

Production OIDC tokens are verified against one configured issuer, audience, JWKS endpoint, and
fixed algorithm allow-list, then exchanged for the PagerAgent session after database membership is
confirmed. Local persona issuance exists only in local and test environments; PagerAgent does not
implement passwords. This decision defines the provider-neutral verification/exchange boundary;
the selected provider's browser authorization-code redirect, callback, PKCE, state, and nonce
adapter remains explicit Phase 8B integration work.

Alert ingestion is a separate machine boundary. The simulator supplies a tenant-bound ingest
credential, while human roles authorize the interactive incident-response APIs.
The credential does not make an alert-provided URL trustworthy: evidence collection additionally
restricts telemetry to server-configured origins and rejects unsafe redirects or network targets.

Health remains public. Every incident, investigation, proposal, postmortem, workflow, export, and
workflow-event stream is authenticated and organization-scoped.

## Alternatives considered

### Trust organization and role claims from the identity provider

Rejected. PagerAgent needs immediate local revocation and cannot assume every provider models its
authorization policy. The external token proves identity; PostgreSQL decides PagerAgent access.

### Put a Bearer token in the EventSource URL

Rejected. URLs leak through history, logs, traces, and referrers. A same-origin HTTP-only cookie
authenticates the native event stream without exposing credentials.

### Add PostgreSQL row-level security immediately

Deferred. RLS would add defense in depth, but worker bypass, migration, and connection-pool context
must be designed carefully. Explicit query scoping plus adversarial isolation tests is the smaller
auditable Phase 8A boundary. RLS can be added later without changing the public authorization
contract.

### Duplicate organization identifiers on every descendant table

Rejected for this phase. The incident is already the aggregate root. Repeating tenant identifiers
would create new mismatch states without replacing the need to validate ownership joins.

## Consequences

- API and service methods must carry a concrete organization context.
- Background workers derive organization ownership from PostgreSQL rather than trusting Redis job
  payloads.
- Organization switching must close the old stream and clear all visible incident state before
  loading the new scope.
- A long-lived event stream must revalidate membership before polling and end at session expiry.
- The local demos must authenticate both the simulator's machine delivery and the operator's CLI
  actions.
- Future connector credentials, audit exports, and quotas have a stable tenant root.
