---
id: checkout-feature-flag-rollback
service: checkout-api
failure_mode: feature-flag-regression
owner: payments-platform
---

# Checkout feature flag rollback

## Preconditions

- Confirm the failing cohort entered the flagged path after the configuration change.
- Verify the application release did not change during the failure window.
- Obtain incident commander approval before changing runtime configuration.

## Mitigation

1. Disable `wallet_validation_v2` through the typed feature-flag control.
2. Send digital-wallet canaries through the restored path.
3. Verify the cohort error rate returns to zero before marking the incident mitigated.

## Follow-up

- Add a configuration rollout check for every supported payment method.
