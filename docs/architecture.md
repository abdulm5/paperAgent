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
       structured synthesis constrained by cited evidence
                             ↓
       policy check → human approval → typed executor
                             ↓
                 recovery canaries → resolution
                             ↓
        grounded postmortem → revision log → final record
```

The LLM is deliberately downstream of evidence gathering. It can summarize, compare hypotheses, and draft communication. It cannot establish facts without attached evidence, and it cannot perform production writes.

## Components and ownership

| Component | Current responsibility | Later responsibility |
| --- | --- | --- |
| `simulator/` | Generate a checkout failure and alert | Reproduce all benchmark scenarios |
| `backend/` | Persist incidents, investigate evidence, validate grounded briefs, enforce approval policy, verify recovery, and version postmortems | Add action adapters and more incident scenarios |
| `frontend/` | Present evidence, decision packets, the human authority boundary, recovery receipts, and postmortem document control | Add cross-incident evaluation views |
| `runbooks/` | Supply versioned procedures to hybrid retrieval | Source grounded mitigation steps |
| `scenarios/` | Describe known outage ground truth | Define reproducible test fixtures |
| `evals/` | Score ranking, retrieval, impact, and traceability against ground truth | Expand regression scenarios and model-quality checks |

## First vertical slice

The first full incident is `checkout-validation-bug`. It has one service, one intentionally faulty deployment, a known alert threshold, one rollback runbook, and known impact. Narrow scope lets us verify each inference before we generalize it.

## Data boundaries

PostgreSQL separates current incident state from immutable alert deliveries and append-only lifecycle events. The API updates current state and appends its matching event in one transaction.

The evidence layer stores collection snapshots as content-hashed artifacts and stores derived clusters, commit candidates, and runbook matches separately. Each investigation captures its collector, clusterer, ranker, and retriever versions plus an input hash. Each derived record carries evidence identifiers, so a score can be traced back to telemetry, deploy history, commit metadata, and the runbook corpus.

Provider interfaces isolate evidence collection from analysis. The demo uses HTTP telemetry, a fixture-backed Git provider, and local Markdown runbooks; production integrations can replace those providers without changing the deterministic ranking contracts.

The synthesis provider is also replaceable. With an API key, the OpenAI adapter uses the Responses API and a strict structured-output schema. Without a key, the deterministic provider creates the same typed contract for offline demos. Both outputs pass through the same citation validator. The model produces language only; a deterministic policy derives the action envelope from the top commit and allow-listed runbook.

Approval and execution are separate durable records. An approval is committed before any external write. The checkout simulator executor accepts only `rollback_service`, `checkout-api`, and `stable-v1`, uses a proposal-scoped idempotency key, then sends a canary cohort that includes digital wallets. A successful HTTP call is insufficient: telemetry verification must pass before the incident moves from investigating to mitigated.

Postmortem generation is gated twice: the incident must be resolved, and it must contain a verified mitigation execution. The narrative generator can draft prose and prevention work, but the service constructs the timeline directly from append-only incident events and rejects citations outside the incident's evidence graph. Each generation, operator edit, and finalization stores an immutable snapshot with an increasing version. Optimistic version checks prevent silent overwrites, and finalization permanently closes the editing path. Operator edits retain their original evidence bindings and exact timeline; the revision author and reason make the human-authored change explicit.

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

## Phase 4 mitigation path

```text
completed investigation
    → structured grounded brief
    → claim-to-evidence validation
    → typed rollback envelope (still read-only)
    → append-only human approve/reject decision
    → allow-list policy check
    → idempotent simulator rollback
    → digital-wallet recovery canaries
    → verified telemetry → incident mitigated
```

## Phase 5 learning path

```text
verified mitigation + resolved lifecycle
    → structured blameless narrative
    → citation allow-list validation
    → exact timeline assembled from incident events
    → persisted draft + immutable v1 snapshot
    → optimistic, attributed operator revisions
    → explicit team-review acknowledgment
    → locked final record + Markdown export
```
