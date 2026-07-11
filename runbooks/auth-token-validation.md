---
id: auth-token-validation
service: auth-api
failure_mode: authentication-failures
owner: identity-platform
---

# Authentication token validation failure

## Preconditions

- Confirm the affected token issuer and validation error.
- Compare the active verification dependency with the last stable release.

## Mitigation

1. Restore the known-good authentication client.
2. Verify token success rates across each issuer.
3. Escalate to the identity platform owner if signatures remain invalid.
