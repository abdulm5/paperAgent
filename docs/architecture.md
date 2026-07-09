# PagerAgent architecture

## Design goal

PagerAgent supports an on-call engineer; it does not autonomously operate production systems. A recommendation is only useful if the operator can see why it was made, inspect its source evidence, and approve or reject it.

## Target request path

```text
Simulator → alert ingestion → incident timeline
                             ↓
          logs / metrics / traces / deploys / runbooks
                             ↓
                    evidence normalization
                             ↓
            deterministic rankers and retrieval services
                             ↓
           LLM synthesis constrained by cited evidence
                             ↓
               policy check → human approval → audit log
```

The LLM is deliberately downstream of evidence gathering. It can summarize, compare hypotheses, and draft communication. It cannot establish facts without attached evidence, and it cannot perform production writes.

## Components and ownership

| Component | Initial responsibility | Later responsibility |
| --- | --- | --- |
| `simulator/` | Generate a checkout failure and alert | Reproduce all benchmark scenarios |
| `backend/` | Receive alerts and expose health | Persist incidents, collect evidence, rank hypotheses, enforce policy |
| `frontend/` | Show the build shell | Serve as the incident command center |
| `runbooks/` | Hold versioned operational knowledge | Source grounded mitigation steps |
| `scenarios/` | Describe known outage ground truth | Define reproducible test fixtures |
| `evals/` | Define measurement approach | Run regression benchmarks in CI |

## First vertical slice

The first full incident is `checkout-validation-bug`. It has one service, one intentionally faulty deployment, a known alert threshold, one rollback runbook, and known impact. Narrow scope lets us verify each inference before we generalize it.

## Data boundaries

The future persistence layer will keep raw telemetry immutable and store derived claims separately. A claim such as `suspected_commit` must include its score, ranker version, and references to supporting telemetry and deploy records. That data model enables reproducibility and makes postmortems auditable.
