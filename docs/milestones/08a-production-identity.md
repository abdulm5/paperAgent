# Phase 8A: Identity and tenant boundaries

Phase 8A changes PagerAgent from a trusted single-operator dashboard into an authenticated,
organization-scoped incident command surface. It intentionally stops before external evidence and
action connectors so the security boundary can be explained and tested on its own.

> Historical milestone note: Phase 9E now completes the hosted Authorization Code + PKCE browser
> flow, revocable session registry, and membership administration described as deferred below. See
> [the Phase 9E walkthrough](09e-hosted-identity-and-kms.md) for the current production contract.

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

1. Local development selects a seeded persona. Hosted browsers begin a server-owned OIDC
   Authorization Code + PKCE login.
2. PagerAgent verifies the single-use state, browser binding, nonce, provider signature, issuer,
   audience, and stable subject, then loads active organization membership from PostgreSQL.
3. PagerAgent creates a revocable database session and issues its short-lived JWT as an HTTP-only,
   same-origin cookie. Provider tokens remain server-side and ephemeral.
4. REST, Markdown export, and native workflow `EventSource` requests carry that cookie and require
   the matching unrevoked session row plus current membership.
5. Unsafe cookie-authenticated requests must also present the in-memory CSRF token.

The earlier direct ID-token exchange remains available only to local/test compatibility clients.
Hosted deployments return `404` for that shortcut and use the fixed login/callback transaction.

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

PagerAgent fails startup outside `local` and `test` unless hosted OIDC browser settings, secure
cookies, KMS credential custody, explicit HTTPS origins, and non-development session, transaction,
and ingest secrets are complete. The current exhaustive example lives in the
[Phase 9E production configuration contract](09e-hosted-identity-and-kms.md#production-configuration-contract).

```dotenv
PAGERAGENT_ENV=production
PAGERAGENT_AUTH_MODE=oidc
PAGERAGENT_SESSION_SECRET=<random-secret-at-least-32-characters>
PAGERAGENT_SESSION_COOKIE_SECURE=true
PAGERAGENT_SESSION_COOKIE_NAME=__Host-pageragent_session
PAGERAGENT_OIDC_ISSUER=https://identity.example.com
PAGERAGENT_OIDC_AUDIENCE=pageragent-api
PAGERAGENT_OIDC_JWKS_URL=https://identity.example.com/.well-known/jwks.json
PAGERAGENT_OIDC_CLIENT_ID=pageragent-api
PAGERAGENT_OIDC_CLIENT_SECRET=<managed-client-secret>
PAGERAGENT_OIDC_AUTHORIZATION_URL=https://identity.example.com/oauth2/authorize
PAGERAGENT_OIDC_TOKEN_URL=https://identity.example.com/oauth2/token
PAGERAGENT_OIDC_REDIRECT_URI=https://pageragent.example.com/api/v1/auth/oidc/callback
PAGERAGENT_OIDC_FRONTEND_URL=https://pageragent.example.com
PAGERAGENT_OIDC_DEFAULT_ORGANIZATION_SLUG=pageragent-production
PAGERAGENT_OIDC_TRANSACTION_KEY=<canonical-base64-encoded-32-byte-key>
PAGERAGENT_OIDC_LOGIN_COOKIE_NAME=__Host-pageragent_oidc_login
PAGERAGENT_INGEST_API_KEY=<random-ingest-key-at-least-32-characters>
PAGERAGENT_TELEMETRY_ALLOWED_ORIGINS=https://telemetry.example.com
PAGERAGENT_CONNECTOR_CIPHER_PROVIDER=aws_kms
PAGERAGENT_CONNECTOR_KMS_KEY_ARN=arn:aws:kms:us-east-1:123456789012:key/<key-id>
PAGERAGENT_CONNECTOR_KMS_REGION=us-east-1
PAGERAGENT_CONNECTOR_KMS_APPLICATION_ID=pageragent
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

## Completed after Phase 8A

- Phase 9A added typed connector contracts and envelope encryption.
- Phase 9B added GitHub App evidence and signed webhook ingestion.
- Phase 9E added authorization-code redirect/callback, PKCE, state and nonce transactions,
  revocable sessions, audited membership administration, and production AWS KMS custody.

## Still deferred

- Invitation email delivery and IdP group synchronization
- PostgreSQL row-level security as defense in depth
- Managed deployment automation, quotas, and hosted trace export
