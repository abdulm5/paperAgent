# ADR 0002: Human approval is a domain event

## Status

Accepted.

## Context

Recommendations such as a rollback or feature-flag change have operational consequences. Treating approval as a frontend button would lose the decision trail.

## Decision

PagerAgent will record a recommendation, supporting evidence, and an explicit operator decision. The system may generate an approved action request, but it will not directly write to production.

## Consequences

The incident timeline can explain what was proposed, what a human chose, and when. It also creates useful feedback labels for later ranking improvements.
