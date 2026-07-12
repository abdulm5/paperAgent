# ADR 0006: Postmortems are versioned learning records

## Status

Accepted.

## Context

A generated postmortem can save incident-command time, but it is not automatically the team's final account. The record combines machine-drafted language, immutable operational facts, human judgment, and prevention commitments. Replacing that document in place would erase who changed the narrative and make it difficult to distinguish generated claims from reviewed conclusions.

## Decision

PagerAgent creates a postmortem only after the incident is resolved and a mitigation execution has verified recovery. Generated sections and prevention items must cite identifiers from the selected investigation, proposal, decision, execution, or incident event. The model does not generate the canonical timeline; the service assembles it directly from append-only incident events.

The current document and its immutable revision snapshots are stored separately. Generation creates version 1, every operator edit requires the expected version plus an actor and change note, and finalization creates the last snapshot and locks the document. Editing preserves the original citation bindings and timeline. A unique `(postmortem_id, version)` database constraint enforces revision identity even under concurrent requests.

## Consequences

The report is useful before review without pretending to be authoritative. Teams can refine language and ownership while retaining a durable account of every version. Optimistic concurrency prevents silent lost updates, and the final record can be exported without losing its evidence index.

Preserving citations through operator edits proves provenance, not semantic entailment of newly written prose. The revision author assumes responsibility for that wording. A later phase can add edit-time entailment checks or a reviewer workflow without changing the versioned storage contract.
