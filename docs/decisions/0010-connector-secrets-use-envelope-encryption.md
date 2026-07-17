# ADR 0010: Connector secrets use envelope encryption and write-only APIs

- Status: Accepted
- Date: 2026-07-15

## Context

PagerAgent's first eight phases use local fixtures and process-level environment variables. Real
GitHub, observability, and collaboration adapters need organization-owned credentials that can be
rotated without exposing them through normal reads, audit events, logs, or browser state.

Encrypting one JSON column with one long-lived application key would hide plaintext at rest, but it
would not establish which tenant and connector the ciphertext belongs to, support safe key
rotation, or prevent a copied database value from being accepted in another row.

## Decision

Phase 9A creates a connector control plane separately from the provider data plane.

- Every connector belongs to one organization and all reads and mutations include that ownership
  predicate. Cross-organization identifiers return `404`.
- Incident commanders may inspect connector metadata. Only administrators may create, configure,
  enable, disable, rotate, or validate connectors.
- Provider configuration is non-secret and schema-checked. Configurable origins must match a
  server-managed exact-origin allowlist; production origins use HTTPS.
- Credential request values use secret-aware types and are write-only. Responses expose the
  expected field names and credential revision, never their values.
- Every mutation of an existing connector locks its tenant-scoped row, checks an expected version,
  updates the current record, and appends a redacted audit event in the same transaction. Creation
  writes the disabled connector, initial credential envelope, and first event atomically.
- Connectors are disabled rather than deleted, preserving their custody history.

Each credential write uses envelope encryption:

1. Generate a fresh 256-bit data-encryption key and independent 96-bit nonces.
2. Canonically serialize the provider credentials and encrypt them with AES-256-GCM.
3. Wrap the data key with the active key-encryption key using AES-256-GCM.
4. Authenticate canonical associated data containing the organization ID, connector ID, provider,
   credential revision, and key identifier.
5. Persist only ciphertext, wrapped key, nonces, key identifier, and non-secret field names.

The configured key ring is indexed by exact key identifier; decryption never tries keys until one
works. A new active key can wrap future writes while older keys keep existing envelopes readable.
Bulk rewrapping onto a new key is deferred to a separately audited maintenance job. The local
implementation satisfies the same `CredentialCipher` boundary. Phase 9E now supplies its production
AWS KMS implementation; [ADR 0015](0015-production-credentials-use-aws-kms-data-keys.md) defines
that workload-identity, encryption-context, concurrency, and deployment contract.

Phase 9A validation proves provider-schema correctness and authenticated vault round-tripping. It
does not claim an external GitHub, Prometheus, or Slack handshake; those network adapters arrive in
the subsequent Phase 9 slices. Phase 9B now supplies the GitHub handshake and Phase 9C.1 supplies
the bounded Prometheus handshake; this paragraph records the narrower Phase 9A decision point.

## Alternatives considered

### Store connector credentials in environment variables

Rejected. Environment variables are process-wide, do not model tenant ownership, and cannot
provide per-connector rotation or an operator-visible audit trail.

### Encrypt every credential directly with one application key

Rejected. Direct encryption couples every row to one key and makes rotation an all-at-once
operation. Envelope encryption gives every write an independent data key and records which wrapping
key protects it.

### Return masked credential suffixes

Rejected. Tokens and private keys do not have universally safe or useful suffix semantics. The API
returns only field presence and revision metadata.

### Call providers during Phase 9A validation

Deferred. A generic control plane should not invent shallow health checks. Each provider adapter
will define bounded requests, redirect policy, response limits, idempotency, and sanitized failures
in its own vertical slice.

## Consequences

- Losing a required key-encryption key makes its credentials intentionally undecryptable; key-ring
  backup and rotation become deployment responsibilities.
- The local Compose stack shares one local key through `.env` for convenience. A production
  deployment selects AWS KMS, grants only authorized workloads use of the exact key, and does not
  inject a raw connector master key or static AWS credentials.
- Connector configuration and audit payloads must use explicit allowlists, never arbitrary request
  dictionaries.
- "Append-only" is currently an application and schema guarantee: there is no delete API, foreign
  keys restrict connector/event removal, and connector versions are unique and ordered. A
  privileged database writer could still forge a valid-looking event; production hardening can add
  restricted database grants, an insert-only trigger, and a hash-chained or signed ledger.
- Database downgrade refuses to discard populated connector custody records.
- Provider workers will receive decrypted credentials only at the final adapter boundary and must
  keep them out of workflow payloads, traces, results, and exceptions.
