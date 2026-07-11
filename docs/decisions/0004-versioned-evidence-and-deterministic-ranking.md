# ADR 0004: Versioned evidence and deterministic ranking

## Status

Accepted.

## Context

Root-cause suggestions are hard to trust when the source data changes after collection or when a score cannot be reproduced. Calling an LLM directly with raw logs would also make the first investigation impossible to evaluate precisely.

## Decision

Every external input is persisted as an immutable artifact with a canonical SHA-256 content hash and source URI. An investigation records the versions of its collector, clusterer, commit ranker, and runbook retriever. Derived clusters and ranked candidates store evidence identifiers.

Commit ranking is a weighted deterministic function of deploy correlation, service overlap, error-to-diff token similarity, change risk, and code ownership. Runbook retrieval combines structured metadata, lexical overlap, and a deterministic hashed-vector cosine score. External systems sit behind provider interfaces; fixtures are providers, not special cases inside ranking logic.

## Consequences

The same inputs and versions produce the same rankings, feature-level explanations can be shown to operators, and scenario ground truth can enforce objective CI gates. The initial hashed-vector retrieval is intentionally offline and dependency-light. A learned embedding provider can replace it later, but must preserve citations, version metadata, and deterministic evaluation fixtures.
