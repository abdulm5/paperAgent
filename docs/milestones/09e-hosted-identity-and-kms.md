# Phase 9E: Hosted identity, administration, and KMS custody

Phase 9E closes two production trust boundaries: the browser completes a server-owned OIDC
Authorization Code + PKCE flow, and connector data keys move behind AWS KMS. PagerAgent also adds
revocable sessions and audited organization membership administration so identity changes take
effect before a signed token expires.

## What this milestone proves

- OIDC login is a complete backend-for-frontend transaction, not an ID-token handoff to React.
- State, nonce, browser binding, and PKCE verifier are independent and single use. Only hashes and
  an encrypted verifier are retained server-side; the browser receives a temporary HttpOnly
  binding cookie. Organization-serialized cleanup and a hard pending-transaction cap bound this
  unauthenticated durable state.
- The callback is consumed before provider network I/O, uses fixed endpoints and redirects, and
  never exposes provider tokens to browser URLs, storage, logs, or PagerAgent session records.
- Every internal JWT maps to an unexpired, unrevoked PostgreSQL session. Logout and organization
  switching revoke the old row, membership deactivation revokes every active row for that user and
  tenant, and even open SSE streams recheck both records before every event.
- Admins provision stable issuer/subject identities and change roles or active state with
  optimistic versions. Tenant checks, self/last-admin guards, and append-only audit receipts make
  authority changes reviewable.
- Production connector writes call KMS `GenerateDataKey`; reads call `Decrypt` with the same exact
  key ARN and tenant/revision encryption context. KMS and provider calls occur outside database
  transactions, organization authority and revisions are rechecked afterward, and stale results
  are rejected. Bounded SDK timeouts/retries keep transient custody failures retryable without
  turning integrity or unknown-key failures into retries.
- Production settings fail startup when hosted OIDC, secure cookies, KMS custody, or HTTPS origin
  boundaries are incomplete.

## Hosted browser path

```text
browser → GET /auth/oidc/login
             │ independent state + nonce + binding + PKCE verifier
             ├── PostgreSQL: hashes + encrypted verifier + expiry
             └── Secure HttpOnly binding cookie
                         │
                         ▼
              fixed IdP authorization URL
                         │ code + state
                         ▼
browser → GET /auth/oidc/callback
             │ verify browser/state/expiry; atomically consume transaction
             ▼
fixed token endpoint ← server-held verifier + client authentication
             │ verify RS256 / issuer / audience / azp / nonce / time
             ▼
stable issuer + subject → pre-provisioned active membership
             │
             ├── PostgreSQL revocable session row
             └── PagerAgent HttpOnly cookie → fixed 303 frontend redirect
```

The callback never accepts a return URL or organization UUID. The configured organization slug is
part of the login transaction, and PagerAgent resolves current membership after identity
verification. RSA keys smaller than 2048 bits and ambiguous control characters in mapped identity
claims are rejected. The direct bearer exchange remains available only to local/test automation.

The application redacts the callback query from Uvicorn access logs. A hosted reverse proxy or
load balancer must likewise log the callback path without its query string, because the
authorization code and state arrive in that query.

## Membership administration path

```text
active organization admin
        │ current membership rechecked under organization lock
        ├── provision configured issuer + stable subject
        └── update role/status + expected membership version
                         │
             self-demotion + last-admin guards
                         │
                         ▼
membership version + sanitized immutable audit receipt (one DB commit)
```

The dashboard's **Organization access** surface exposes only non-secret identity metadata, current
role/status/version, and audit receipts. UI visibility is explanatory; API permissions and tenant
predicates remain authoritative. There is no membership delete or email-based account linking.

### First hosted administrator

There is deliberately no unauthenticated bootstrap API. After applying migrations, an operator
with database deployment authority runs the one-shot offline command from `backend/`:

```bash
python -m app.memberships.bootstrap \
  --organization pageragent-production \
  --organization-name "PagerAgent Production" \
  --issuer https://identity.example.com \
  --subject 00u-stable-idp-subject \
  --email admin@example.com \
  --display-name "PagerAgent Admin"
```

The command requires `PAGERAGENT_AUTH_MODE=oidc`, requires the issuer to exactly match configured
OIDC, creates the explicitly named organization when necessary (or locks the existing one), refuses
to run when an active admin already exists, never links by email, and writes the first membership
plus a `bootstrap:offline` audit receipt atomically.

## KMS credential path

```text
typed write-only credential
        │ no database transaction
        ▼
AWS KMS GenerateDataKey(AES_256, exact key ARN, immutable context)
        │ plaintext data key             │ KMS ciphertext blob
        ▼                                │
AES-256-GCM credential payload           │
        └──────────► aws-kms-v1 envelope ┘
                         │
              lock + compare revisions
                         ▼
                  PostgreSQL row

runtime snapshot → end DB transaction → KMS Decrypt + AES-GCM authenticate
        │
        └── final enabled/version check → ephemeral provider adapter
```

The local stack intentionally keeps `local-aesgcm-v1`; it proves the same cipher interface without
requiring AWS credentials. A hosted deployment rotates connector credentials into KMS envelopes
and is designed to receive AWS credentials from provider workload identity. PagerAgent does not
create IAM, workload-identity, or KMS infrastructure.

## Production configuration contract

At minimum, hosted configuration supplies exact OIDC and KMS values:

```dotenv
PAGERAGENT_ENV=production
PAGERAGENT_AUTH_MODE=oidc
PAGERAGENT_SESSION_COOKIE_SECURE=true
PAGERAGENT_SESSION_COOKIE_NAME=__Host-pageragent_session
PAGERAGENT_SESSION_SECRET=<managed-random-secret-at-least-32-characters>
PAGERAGENT_INGEST_API_KEY=<different-managed-random-secret-at-least-32-characters>
PAGERAGENT_INGEST_ORGANIZATION_SLUG=pageragent-production
PAGERAGENT_OIDC_ISSUER=https://identity.example.com
PAGERAGENT_OIDC_AUDIENCE=pageragent-web
PAGERAGENT_OIDC_JWKS_URL=https://identity.example.com/.well-known/jwks.json
PAGERAGENT_OIDC_CLIENT_ID=pageragent-web
PAGERAGENT_OIDC_CLIENT_SECRET=<managed-client-secret>
PAGERAGENT_OIDC_AUTHORIZATION_URL=https://identity.example.com/oauth2/authorize
PAGERAGENT_OIDC_TOKEN_URL=https://identity.example.com/oauth2/token
PAGERAGENT_OIDC_REDIRECT_URI=https://pageragent.example.com/api/v1/auth/oidc/callback
PAGERAGENT_OIDC_FRONTEND_URL=https://pageragent.example.com
PAGERAGENT_OIDC_DEFAULT_ORGANIZATION_SLUG=pageragent-production
PAGERAGENT_OIDC_TRANSACTION_KEY=<canonical-base64-encoded-32-byte-key>
PAGERAGENT_OIDC_LOGIN_COOKIE_NAME=__Host-pageragent_oidc_login
PAGERAGENT_CONNECTOR_CIPHER_PROVIDER=aws_kms
PAGERAGENT_CONNECTOR_KMS_KEY_ARN=arn:aws:kms:us-east-1:123456789012:key/<key-id>
PAGERAGENT_CONNECTOR_KMS_REGION=us-east-1
PAGERAGENT_CONNECTOR_KMS_APPLICATION_ID=pageragent
PAGERAGENT_CONNECTOR_KMS_CONNECT_TIMEOUT_SECONDS=3
PAGERAGENT_CONNECTOR_KMS_READ_TIMEOUT_SECONDS=5
PAGERAGENT_CONNECTOR_KMS_MAX_ATTEMPTS=3
PAGERAGENT_CONNECTOR_DECRYPTION_KEYS={}
```

The frontend and API are served through one browser origin so host-only `__Host-` cookies cover the
relative API routes and callback without a parent-domain cookie. The full validator requires that
shared origin, `__Host-` cookies, the exact callback path, production evidence
modes, HTTPS connector/telemetry/CORS origins, and a non-development ingest credential distinct from
the session secret. The server-owned ingest organization must exist and should match the bootstrapped
organization unless responders are explicitly provisioned in both. Custom KMS endpoints are
local/test only. Apply an ingress rate limit to `/auth/oidc/login` in addition to the application's
durable-state cap.

Run `python -m app.auth.maintenance` from a scheduled maintenance job to delete one bounded batch
of expired/revoked identity state across all organizations. It is intentionally separate from the
unauthenticated login endpoint.

## Demo walkthrough

### Deterministic local proof

1. Sign in as the local admin and open **Organization access**.
2. Provision a test subject using the stock issuer `https://identity.pageragent.local`, change its
   role with the displayed expected version, and show the matching immutable audit receipt.
3. Attempt a stale update, self-demotion, and last-admin deactivation; point out the `409` boundary.
4. Log out and show that the old database session is revoked rather than merely losing a cookie.
5. Run the KMS adapter and integration tests to show exact encryption context, no static AWS
   credentials, no DB transaction during KMS calls, and stale-result rejection.

### Hosted IdP proof

1. Register the exact PagerAgent callback with a test OIDC provider and configure the production
   values above.
2. Run the offline first-admin command and show its immutable bootstrap audit receipt.
3. Begin login and inspect the redirect's `code_challenge_method=S256`, state, and nonce without
   exposing the verifier.
4. Complete login and show the fixed frontend redirect, HttpOnly PagerAgent cookie, and database
   session receipt. Confirm provider tokens are absent from browser storage and URLs.
5. Replay the callback and show that the consumed transaction fails before a second token request.
6. Deactivate the membership or revoke the session and show that the next API request—and an
   already-open event stream—is denied.

## Interview explanation

Lead with this sentence:

> I treated hosted login, application sessions, membership authority, and credential key custody
> as four separate security records instead of trusting one long-lived browser token or app key.

Then trace the OIDC state transaction, the revocable session lookup, one versioned membership
change and audit receipt, and the KMS snapshot/call/compare-and-swap path. That sequence demonstrates
OAuth/OIDC threat modeling, database authorization, optimistic concurrency, managed key custody,
and production deployment boundaries in one coherent slice.
