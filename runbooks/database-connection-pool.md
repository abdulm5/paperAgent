---
id: database-connection-pool
service: payment-api
failure_mode: database-timeouts
owner: payments-platform
---

# Database connection pool saturation

## Preconditions

- Confirm connection acquisition timeouts and pool utilization.
- Check database health before changing application capacity.

## Mitigation

1. Reduce nonessential database traffic.
2. Restore the last known-good pool configuration.
3. Watch acquisition latency and timeout rate for 10 minutes.
