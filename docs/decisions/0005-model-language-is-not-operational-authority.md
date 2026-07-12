# ADR 0005: Model language is not operational authority

## Status

Accepted.

## Context

A grounded incident brief still contains generated language. Treating its recommended action as an executable command would let a hallucination, prompt injection, or malformed output cross directly into an operational write.

## Decision

Synthesis returns a strict `GroundedBriefDraft` with four required claim types and evidence IDs. PagerAgent rejects unknown citations or claims that do not match their rendered brief fields. The model does not define executable parameters. A deterministic policy derives a narrow `ActionEnvelope` from the ranked commit and allow-listed rollback runbook.

The proposal remains `pending_approval` until an operator records an append-only approve or reject decision. Approval requires the incident to be investigating. Only `rollback_service` for `checkout-api` to `stable-v1` is permitted in the first vertical slice. The decision is committed before the executor runs, and recovery telemetry—not the deployment response—determines whether the incident becomes mitigated.

## Consequences

The project can use an LLM for concise synthesis without granting it production authority. Decisions, executions, and recovery results are independently auditable. Adding a new action later requires a typed schema, explicit policy, executor adapter, verification strategy, and tests rather than a prompt edit.
