# ADR 0015: Production credentials use AWS KMS data keys

- Status: Accepted
- Date: 2026-07-18

## Context

Phase 9A introduced per-revision envelope encryption behind a `CredentialCipher` interface. Its
local AES-GCM key ring is useful for development, but a hosted process should not receive a
long-lived raw key-encryption key through application configuration. Production also needs a
clear workload-identity, key-policy, audit, and rotation boundary.

Calling a remote key service while a connector row is locked would create a second problem: KMS
latency or failure could hold database locks, while a concurrent rotation or revocation could make
the returned result stale.

## Decision

Production connector credential custody uses AWS KMS envelope encryption.

1. PagerAgent calls `GenerateDataKey` with `AES_256`, an exact configured KMS key ARN, and a
   non-secret encryption context containing application, environment, organization, connector,
   provider, and credential revision.
2. It encrypts the canonical credential document locally with AES-256-GCM and the returned
   plaintext data key. Only the payload ciphertext, nonce, KMS ciphertext blob, exact returned key
   ARN, field names, and `aws-kms-v1` scheme are persisted.
3. Decryption sends that stored ciphertext blob, exact key ARN, and identical context to KMS, then
   authenticates the payload locally. Copied rows, edited context, unexpected key IDs, malformed
   envelopes, and unsupported algorithms fail closed.
4. KMS calls happen after the database read transaction ends. Credential rotation re-locks and
   rechecks current admin authority before comparing connector plus credential revisions and
   writing the new envelope. Runtime loads and validation perform the same final revision check
   after decryption, discarding stale results.

The AWS SDK uses its standard workload credential provider chain. PagerAgent has no settings for
AWS access-key IDs or secret access keys. Production configuration requires an exact key ARN and
matching region, forbids custom KMS endpoints, rejects the local decryption key ring, and stores no
KMS plaintext data key. The local cipher and local envelope rows remain supported for development;
hosted migration requires rotating active credentials into KMS envelopes before enabling them.
The API refuses to enable an envelope created under the inactive custody scheme.

The KMS client is process-scoped and uses explicit connect/read timeouts plus a bounded standard
retry policy. A stored envelope may reference only the configured key ARN; rejection happens before
network I/O. Transient SDK/custody outages remain retryable in workflow delivery and return `503`
at synchronous boundaries, while authentication failures, malformed envelopes, and unknown keys
remain permanent fail-closed results.
Production clients ignore SDK/shared-profile endpoint overrides; only the explicit local/test
endpoint setting can replace AWS's regional KMS endpoint.

The encryption context uses a dedicated `PAGERAGENT_CONNECTOR_KMS_APPLICATION_ID`, not the
process-specific telemetry `SERVICE_NAME`. API, outbox, and worker processes therefore authenticate
the same envelope context even though their span/log service names differ.

The KMS API contract is based on AWS's
[`GenerateDataKey`](https://docs.aws.amazon.com/kms/latest/APIReference/API_GenerateDataKey.html),
[`Decrypt`](https://docs.aws.amazon.com/kms/latest/APIReference/API_Decrypt.html), and
[encryption-context](https://docs.aws.amazon.com/kms/latest/developerguide/encrypt_context.html)
documentation.

## Deployment boundary

The application selects and uses a key; it does not create the KMS key, IAM role, key policy,
CloudTrail trail, or network boundary. A production deployment must provide:

- a dedicated symmetric KMS key in the configured region;
- a workload role allowed only the required `kms:GenerateDataKey` and `kms:Decrypt` operations on
  that key, constrained by the PagerAgent encryption context where practical;
- no static AWS credentials in PagerAgent environment variables or images;
- CloudTrail monitoring for KMS use and alarms for denied or anomalous calls;
- egress policy or a KMS VPC endpoint appropriate to the hosting environment;
- a reviewed key-rotation and recovery procedure, including how old ciphertext remains readable.

AWS KMS automatic rotation changes backing key material without changing the logical key ARN. A
future explicit key migration may use KMS `ReEncrypt` or credential rotation, but PagerAgent does
not silently rewrite existing envelopes in normal request paths.

## Alternatives considered

### Put a production master key in an environment variable

Rejected. That gives every process with the environment a raw long-lived key and moves audit,
policy, and rotation responsibility into application configuration.

### Encrypt the credential document directly with KMS

Rejected. KMS is the key-custody boundary, not a bulk data store. Per-write data keys preserve the
existing bounded payload contract and minimize plaintext handled by the remote service.

### Hold the connector lock through KMS calls

Rejected. External latency would extend lock duration and could serialize unrelated administrator
work. Snapshot, release, external call, and compare-and-swap make the race explicit.

### Configure a KMS alias instead of a key ARN

Rejected. Alias retargeting could silently change authority. PagerAgent records and verifies the
exact returned key ARN for each envelope.

## Consequences

- KMS unavailability fails credential creation/rotation closed and makes existing connectors
  temporarily unavailable; provider details are reduced to sanitized PagerAgent errors.
- Encryption context is visible in KMS audit records and therefore must remain non-secret.
- Credential rows now carry an explicit cipher scheme and a nullable local wrapping nonce. The
  migration preserves existing local envelope bytes and refuses downgrade while KMS rows exist.
- Rotation, backup, key deletion scheduling, IAM policy, CloudTrail retention, and regional
  recovery are infrastructure responsibilities and must be exercised before hosted release.
