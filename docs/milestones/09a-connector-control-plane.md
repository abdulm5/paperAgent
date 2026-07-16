# Phase 9A: Secure connector control plane

Phase 9A gives PagerAgent a tenant-scoped place to register future GitHub, Prometheus, and Slack
integrations without putting provider secrets into incident records, workflow messages, or normal
API responses. It builds the custody boundary first; provider network calls remain separate,
testable slices.

## What this milestone proves

- Connector metadata and audit history are isolated by organization.
- Only an administrator can cross the credential write boundary.
- Submitted credential values are never returned by create, read, rotate, validation, or event APIs.
- Database rows contain authenticated ciphertext and a wrapped random data key rather than provider
  plaintext.
- Moving or modifying a credential envelope causes authenticated decryption to fail.
- An expected connector version prevents two administrators from silently overwriting each other.
- Disabling a connector preserves its full audit trail.
- Key identifiers select one exact decryption key and allow a controlled wrapping-key rotation.
- Populated connector custody tables cannot be silently destroyed by a migration downgrade.

## Roles and permissions

| Role | Connector authority |
| --- | --- |
| Viewer | No connector metadata access. |
| Responder | No connector metadata access. |
| Incident commander | Read connector status, configuration, credential-field presence, and audit history. |
| Admin | Commander access plus create, edit, enable/disable, rotate, and validate. |

The frontend explains missing authority, while the API remains authoritative.

## Custody path

```text
admin create / credential rotation + current database role
              │
              ▼
typed provider contract ── reject secret-shaped configuration / unapproved origins
              │
              ▼
random data key ── AES-GCM ──► credential ciphertext
       │
       └──────── active wrapping key ──► wrapped data key + exact key ID
              │
              ▼
connector row + credential envelope + redacted audit event (one transaction)
              │
              ▼
read API: metadata + field names + revision only
```

Authenticated associated data binds the organization, connector, provider, credential revision,
and key ID. Copying a valid envelope into another connector therefore fails closed.

## Provider contracts

Phase 9A reserves typed, non-secret configuration and credential fields:

| Provider | Non-secret configuration | Write-only credentials |
| --- | --- | --- |
| GitHub | repository, app ID, installation ID, optional approved API origin | App private key |
| Prometheus | service binding, approved base origin | Bearer token |
| Slack | channel, optional approved API origin | Bot token |

The validation action decrypts the envelope and rechecks this contract. Its receipt explicitly says
that provider handshake is pending so a local schema check cannot be mistaken for production
connectivity.

> Historical boundary: Phase 9B extends the GitHub row with an explicit service binding and
> write-only webhook secret, fixes the runtime to GitHub's public API origin, and replaces this
> local-only GitHub receipt with a real installation/repository handshake. Phase 9C.1 likewise
> adds a Prometheus service binding, a fixed live query handshake, and bounded metric evidence.
> Slack still uses the original Phase 9A behavior.

## Demo walkthrough

1. Sign in as an admin and open **Connector custody**.
2. Create a disabled Prometheus connector with a demo token.
3. Inspect the response and database receipt: only `bearer_token` field presence, revision, key ID,
   and ciphertext length are visible.
4. Validate custody, then explicitly enable the connector.
5. Rotate its credential and observe the connector return to a safe disabled state.
6. Validate and enable the new revision.
7. Inspect the append-only events and their server-derived `user:<id>` actor.
8. Switch organizations and verify that the connector ledger clears and the known UUID returns
   `404`.

The dedicated connector demo script performs the same API sequence, checks that the submitted
token never appears in any response or audit payload, and now completes the fixed Phase 9C.1 live
Prometheus read handshake before enablement.

## Deferred Phase 9 slices

- **9B (complete):** multiline-safe GitHub App private-key ingress, installation authorization,
  signed webhook verification, delivery replay protection, and real commit/deployment evidence
- **9C.1 (complete):** bounded Prometheus range evidence and immutable snapshots
- **9C.2:** backend-specific log and trace evidence
- **9D:** durable Slack updates and GitHub issue creation with downstream idempotency
- **9E:** provider-specific OIDC authorization-code/PKCE login and membership administration

Every networked adapter must add redirect refusal, connect/response limits, and an enforceable
outbound boundary before using a stored origin. Phase 9C.1 relies on exact origin allowlisting plus
deployment egress policy; a future application transport may instead pin validated DNS answers.
Exact root-origin validation in 9A is a control-plane prerequisite, not a complete runtime SSRF
defense.
