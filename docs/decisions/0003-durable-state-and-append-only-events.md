# ADR 0003: Durable state with append-only incident events

## Status

Accepted.

## Context

An incident must survive process restarts, and operators need to understand how its state changed. Storing only the current status would discard the response history; storing only events would make current-state queries needlessly expensive for this application.

## Decision

PagerAgent stores current incident state in `incidents`, every monitoring delivery in immutable `alerts`, and every lifecycle change in append-only `incident_events`. Status transitions update the incident and append an event in the same database transaction. Clients send the version they observed so stale updates are rejected.

Active alert fingerprints are unique. Resolving an incident releases its active fingerprint, allowing a later occurrence of the same outage to create a new incident instead of mutating historical data.

## Consequences

The dashboard can query current state efficiently while rendering a complete audit timeline. PostgreSQL row locks and record versions protect concurrent transitions. The extra event table creates more writes, but gives postmortem generation a trustworthy source later.
