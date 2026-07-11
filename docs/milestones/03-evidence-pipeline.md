# Milestone 3: evidence and root-cause investigation

## Outcome

PagerAgent now converts the checkout alert into a persisted, inspectable investigation. The reference scenario produces one `ValidationRuleMissing` cluster covering 8 digital-wallet failures, ranks faulty commit `8fa23c1` first, and retrieves `checkout-api-rollback` first.

## What happens after an alert

1. The alert creates or updates a durable incident and schedules an investigation.
2. The telemetry collector snapshots the failing service's request window and deployment history.
3. SHA-256 hashes make those inputs tamper-evident and reproducible.
4. The clusterer groups failures by error type, endpoint, and release, then extracts affected attributes.
5. A Git provider supplies recent commits. The ranker scores each candidate using deploy correlation (30%), service overlap (25%), error/diff similarity (25%), change risk (10%), and ownership relevance (10%).
6. The runbook retriever combines metadata (50%), lexical overlap (30%), and deterministic hashed-vector similarity (20%).
7. The API persists the ranked results with evidence citations, and the dashboard renders the evidence ledger and scoring breakdown.

## Why this design is interview-worthy

The important engineering decision is that the future LLM is downstream of evidence. Phase 3 does not ask a model to guess a cause. It builds a typed retrieval and ranking system whose answer can be reproduced, inspected, and scored against known truth. Provider interfaces make the demo fixtures replaceable with GitHub, Datadog, or another telemetry system later.

The dashboard follows an incident flight-recorder metaphor: the cluster is the observed symptom, the ranked dossier explains causal candidates, the runbook is the operational next step, and the provenance drawer exposes the original artifacts and hashes.

## Quality gate

The checkout benchmark requires:

- the faulty commit at rank 1 and inside the top 3;
- the rollback runbook at rank 1;
- exactly 8 impacted requests and `digital_wallet` as the affected attribute;
- evidence citations on every cluster, commit candidate, and runbook match.

These checks run with the backend tests and fail CI when ranking behavior regresses.

## Demo narration

Start with the injected deploy and 60-request traffic window. Show the alert becoming an incident, then move left to right through the error cluster, top commit, feature scores, cited evidence, and retrieved rollback procedure. End by rerunning the investigation to demonstrate identical rankings and explaining that the next milestone adds grounded synthesis and human-approved mitigation.
