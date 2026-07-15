# Phase 8A: Identity and tenant boundaries

Phase 8A changes PagerAgent from a trusted single-operator dashboard into an authenticated,
organization-scoped incident command surface. It intentionally stops before external evidence and
action connectors so the security boundary can be explained and tested on its own.

## What this milestone proves

- A valid token is necessary but not sufficient: the user must also have an active database
  membership in the selected organization.
- Current database roles, not stale JWT role claims, decide authority.
- The same alert fingerprint can open independent incidents for two organizations.
- Knowing another organization's UUID does not grant read or write access.
- Workflow SSE replay emits only events owned by the authenticated organization, rechecks current
  membership before each poll, and stops when the signed session expires.
- Human audit entries identify the authenticated user rather than trusting an `actor` field.
- The monitoring simulator authenticates as a machine, separately from human responders.

## Roles and permissions

| Role | Operational authority |
| --- | --- |
| Viewer | Read incidents, evidence, workflows, decision packets, and postmortems. |
| Responder | Viewer access plus incident acknowledgement, investigations, proposals, and draft editing. |
| Incident commander | Responder access plus mitigation decisions, resolution, finalization, and evaluation runs. |
| Admin | Incident commander access plus organization-scoped local reset. |

The backend returns permission strings in the session response. The frontend uses those strings to
explain disabled controls, but the API remains the authoritative enforcement point.

## Authentication paths

### Browser session

1. Local development selects a seeded persona, or an external OIDC client submits a verified
   identity token to the exchange endpoint.
2. PagerAgent verifies identity and loads active organization membership from PostgreSQL.
3. PagerAgent issues a short-lived internal JWT as an HTTP-only, same-origin cookie.
4. REST, Markdown export, and native workflow `EventSource` requests carry that cookie.
5. Unsafe cookie-authenticated requests must also present the in-memory CSRF token.

The repository implements the provider-neutral verification and exchange boundary. It does not
pretend to provide a hosted identity-provider login: authorization-code redirect/callback, PKCE,
state, and nonce handling depend on the selected provider and are deferred to the Phase 8B adapter.
Consequently, the included React identity checkpoint is deliberately local-only.

An external OIDC client completes its provider flow, then calls the neutral exchange contract:

```http
POST /api/v1/auth/oidc/exchange
Authorization: Bearer <provider-id-token>
Content-Type: application/json

{"organization_id":"<provisioned-membership-organization-id>"}
```

### CLI session

The local demos request the same signed session and use it as a Bearer token. The server still loads
the current membership and permissions for every call.

### Alert ingestion

The alert evaluator sends a separate ingest key bound by server configuration to one organization.
The alert body cannot choose a tenant.

Telemetry destinations are also server-configured origins rather than tenant-selected trust. The
collector rejects credentials, fragments, non-HTTP(S) schemes, unexpected origins or ports,
redirects, and DNS answers in loopback, private, link-local, multicast, reserved, or unspecified
ranges. Local/test may explicitly allow the Docker simulator's private origin; non-development
environments may not.

## Non-development configuration

PagerAgent fails startup outside `local` and `test` unless OIDC is selected, its issuer, audience,
and JWKS settings are complete HTTPS values, cookies are secure, CORS names explicit HTTPS origins,
the telemetry origin allowlist is non-empty and HTTPS-only, and both the session-signing secret and
ingest key are non-development values of at least 32 characters.

```dotenv
PAGERAGENT_ENV=production
PAGERAGENT_AUTH_MODE=oidc
PAGERAGENT_SESSION_SECRET=<random-secret-at-least-32-characters>
PAGERAGENT_SESSION_COOKIE_SECURE=true
PAGERAGENT_OIDC_ISSUER=https://identity.example.com
PAGERAGENT_OIDC_AUDIENCE=pageragent-api
PAGERAGENT_OIDC_JWKS_URL=https://identity.example.com/.well-known/jwks.json
PAGERAGENT_INGEST_API_KEY=<random-ingest-key-at-least-32-characters>
PAGERAGENT_TELEMETRY_ALLOWED_ORIGINS=https://telemetry.example.com
BACKEND_CORS_ORIGINS=https://pageragent.example.com
```

## Tenant data path

```text
verified session
      │
      ▼
active user + membership ──► explicit permission
      │
      ▼
organization-scoped incident
      │
      ├── alerts / timeline
      ├── investigations / evidence
      ├── proposals / executions
      ├── postmortem / revisions
      └── workflows / SSE events
```

PostgreSQL remains authoritative. Redis messages carry delivery hints, but a worker resolves the
workflow's incident and organization from the database before any domain mutation.

## Demo walkthrough

1. Open the dashboard and choose the local viewer persona.
2. Confirm evidence remains readable while every write control names the missing permission.
3. Sign in as the responder and begin the investigation; mitigation approval remains locked.
4. Switch to the incident commander and approve the typed action.
5. Switch organizations and verify the previous queue, detail, and live stream disappear before the
   new scope loads.
6. Attempt a known incident UUID from the other organization and observe a `404`.
7. Inspect the timeline to confirm the server-derived `user:<id>` actor.

## Interview explanation

The important distinction is authentication versus authorization. OIDC proves who the person is.
PagerAgent's database proves where that person is a member and what they can do now. Every resource
query carries the organization boundary, while explicit permissions protect mutations. The UI
makes that authority visible, but backend denial and tenant-isolation tests are the real control.

## Deferred to Phase 8B

- Provider-specific authorization-code redirect/callback, PKCE, state, and nonce integration
- Production evidence connectors and encrypted connector credentials
- Signed third-party webhooks and per-connector service principals
- Membership administration UI and invitations
- PostgreSQL row-level security as defense in depth
- Managed secret rotation, quotas, and hosted trace export
