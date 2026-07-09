---
id: checkout-api-rollback
service: checkout-api
failure_mode: elevated-500-errors
owner: payments-platform
---

# Checkout API rollback

## Preconditions

- Confirm the deploy correlation and review the affected endpoints.
- Verify the previous release is known-good.
- Obtain incident commander approval before requesting a rollback.

## Mitigation

1. Roll `checkout-api` back to the previous stable release.
2. Confirm the deploy event is recorded in the incident timeline.
3. Watch 5xx rate, checkout completion rate, and p95 latency for 10 minutes.
4. Mark the incident mitigated only after the error rate remains below the alert threshold.

## Follow-up

- Link the reverted change to the incident.
- Open a prevention task for a regression test covering the failing validation input.
