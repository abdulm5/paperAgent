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

| Component | Current responsibility | Later responsibility |
| --- | --- | --- |
| `simulator/` | Generate a checkout failure and alert | Reproduce all benchmark scenarios |
| `backend/` | Persist incidents, collect immutable evidence, cluster errors, and rank commits/runbooks | Add grounded synthesis and enforce mitigation policy |
| `frontend/` | Present incident state, ranked investigation, citations, and operator controls | Add generated briefs and approval workflows |
| `runbooks/` | Supply versioned procedures to hybrid retrieval | Source grounded mitigation steps |
| `scenarios/` | Describe known outage ground truth | Define reproducible test fixtures |
| `evals/` | Score ranking, retrieval, impact, and traceability against ground truth | Expand regression scenarios and model-quality checks |

## First vertical slice

The first full incident is `checkout-validation-bug`. It has one service, one intentionally faulty deployment, a known alert threshold, one rollback runbook, and known impact. Narrow scope lets us verify each inference before we generalize it.

## Data boundaries

PostgreSQL separates current incident state from immutable alert deliveries and append-only lifecycle events. The API updates current state and appends its matching event in one transaction.

The evidence layer stores collection snapshots as content-hashed artifacts and stores derived clusters, commit candidates, and runbook matches separately. Each investigation captures its collector, clusterer, ranker, and retriever versions plus an input hash. Each derived record carries evidence identifiers, so a score can be traced back to telemetry, deploy history, commit metadata, and the runbook corpus.

Provider interfaces isolate evidence collection from analysis. The demo uses HTTP telemetry, a fixture-backed Git provider, and local Markdown runbooks; production integrations can replace those providers without changing the deterministic ranking contracts.

## Phase 3 investigation path

```text
threshold alert
    → HTTP telemetry snapshot + deployment history
    → failure signature clustering
    → versioned Git candidate provider
    → weighted, explainable commit ranker
    → metadata + lexical + hashed-vector runbook retrieval
    → persisted evidence ledger and dashboard citations
    → scenario ground-truth quality gate
```
