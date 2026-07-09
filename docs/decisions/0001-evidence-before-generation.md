# ADR 0001: Evidence before generation

## Status

Accepted.

## Context

An incident copilot can sound convincing while making unsupported causal claims. That is unacceptable during an outage and difficult to evaluate afterward.

## Decision

Telemetry collection, deploy correlation, ranking, and runbook retrieval will produce typed evidence before any LLM synthesis. Generated content must reference evidence identifiers and state uncertainty when evidence conflicts or is incomplete.

## Consequences

The first implementation is more structured than a chat-first agent, but it is testable: scenario ground truth can be compared directly with the ranker and retrieval outputs.
