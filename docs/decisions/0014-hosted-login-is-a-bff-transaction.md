# ADR 0014: Hosted login is a server-owned OIDC transaction

- Status: Accepted
- Date: 2026-07-18

## Context

Phase 8A verified externally obtained OIDC ID tokens and exchanged them for PagerAgent sessions.
That proved issuer, subject, membership, and RBAC boundaries, but it was not a complete browser
login. A production browser flow must protect the authorization response, PKCE verifier, nonce,
session token, and post-login destination across redirects and retries.

Letting the React client receive provider tokens or choose callback destinations would expand the
trusted surface. A signed internal JWT by itself would also make logout and membership changes
hard to enforce immediately because the token would remain valid until expiration.

## Decision

PagerAgent acts as an OIDC backend-for-frontend and implements Authorization Code with PKCE `S256`.

- The authorization, token, JWKS, issuer, client, callback, and final frontend URLs are fixed
  server configuration. Requests cannot supply an issuer, token endpoint, callback, or return URL.
- Login generates independent high-entropy state, nonce, browser binding, and PKCE verifier values.
  PostgreSQL stores hashes of state, nonce, and browser binding, plus the verifier encrypted with a
  separate 256-bit transaction key. The short-lived browser cookie contains only the binding.
- The organization row serializes login-state cleanup and creation. Expired or consumed
  transactions are removed and live pending transactions have a hard per-organization cap. A
  separate bounded maintenance command prunes stale login transactions and revocable sessions
  across every tenant; unauthenticated login never scans the session ledger. Hosted ingress adds a
  request rate limit on top of the login-state bound.
- The callback verifies the exact browser binding and an unexpired, unconsumed state transaction,
  then marks it consumed before exchanging the code. A replay therefore cannot perform another
  provider request even if the original exchange failed after consumption.
- Token exchange uses the fixed endpoint, client-secret authentication, ordinary TLS validation,
  bounded time and response bytes, no redirects, and no environment proxy inheritance.
- ID-token verification accepts only the configured issuer, RS256 signatures from the configured
  JWKS source, the exact client audience, valid `azp` semantics, the original nonce, and bounded
  time claims. PagerAgent maps only stable issuer plus subject to a pre-provisioned user and active
  organization membership; email is descriptive rather than an account-linking key.
- Provider tokens never enter browser URLs, React state, local storage, PagerAgent cookies, logs,
  or database sessions. Success returns a fixed `303` redirect after setting the internal
  HttpOnly cookie. Responses disable caching and cross-origin referrer disclosure.
- Hosted session and login cookies require the `__Host-` prefix, `Secure`, no `Domain`, and
  `Path=/`. Production therefore serves the frontend and relative `/api/v1` routes through one
  browser origin; split hostnames are rejected at startup. The application redacts callback
  queries from access logs; every upstream proxy must apply the same rule.
- The direct ID-token exchange remains a local/test compatibility surface and returns `404` in a
  hosted environment.

Every PagerAgent JWT now identifies a database session row with the same UUID. Requests require a
matching, unexpired, unrevoked session for the same user and organization in addition to a current
active membership. Logout revokes the row; organization switching revokes the old row and creates
a new one. JWT claims carry identity and CSRF data, but PostgreSQL retains revocation authority.

Membership administration is organization-scoped and admin-only. Provisioning uses the configured
issuer and stable subject, updates require an expected membership version, and every accepted
change appends a sanitized audit event. Self-demotion, self-deactivation, and removal of the last
active admin fail closed. Memberships are deactivated rather than deleted.

The first administrator is established only through an offline, organization-locked bootstrap
command. It requires the exact configured issuer and stable subject, refuses when an active admin
already exists, and appends the same immutable membership receipt with actor `bootstrap:offline`.
PagerAgent exposes no public bootstrap endpoint.

The flow follows [OpenID Connect Core](https://openid.net/specs/openid-connect-core-1_0.html),
[PKCE](https://www.rfc-editor.org/rfc/rfc7636.html), and the current OAuth security best practice in
[RFC 9700](https://www.rfc-editor.org/rfc/rfc9700.html).

## Alternatives considered

### Complete OIDC in React and send the ID token to PagerAgent

Rejected for hosted use. It exposes provider tokens to browser JavaScript and requires another
client-side token-storage and callback-security design. PagerAgent needs only its own HttpOnly
session at the browser boundary.

### Store the PKCE verifier in a readable browser cookie

Rejected. A server-side encrypted transaction keeps the verifier out of browser-readable state and
lets PagerAgent atomically consume it. The temporary cookie proves browser continuity only.

### Trust JWT expiration as logout

Rejected. Incident authority and organization membership can change before a token expires.
Database session revocation provides an immediate server-side decision point on every request.

### Link accounts by email address

Rejected. Email addresses can be changed or reassigned and may not be verified consistently.
Issuer plus subject is the immutable external identity key; administrators explicitly provision
membership for that identity.

## Consequences

- The login transaction, application session, membership, and audit tables become security state
  that requires normal PostgreSQL durability, backup, and retention controls.
- A consumed callback is intentionally not retryable. The user starts a new login instead of
  risking a second code exchange with ambiguous state.
- Hosted deployment must configure an exact IdP application and callback and keep the client
  secret and transaction key in a managed secret boundary.
- The edge must rate-limit login starts and omit OIDC callback queries from access logs.
- PagerAgent does not provision IdP users or groups. Membership administration controls local
  authorization for already identified subjects.
- Local personas remain available only in local/test so the repository demo stays deterministic.
