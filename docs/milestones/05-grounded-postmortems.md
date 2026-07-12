# Milestone 5: grounded postmortems and incident learning

## Outcome

PagerAgent now completes the learning loop. Once recovery is verified and the incident commander resolves the incident, it generates a blameless report containing summary, root cause, customer impact, detection, resolution, strengths, gaps, an exact response timeline, and three owned prevention items. Every conclusion remains connected to the incident evidence graph.

The dashboard presents this as a case file rather than another chat response. A revision spine shows document history, evidence labels sit beside each claim, prevention work has owner and priority, and the exact timeline can be expanded for inspection. Operators can edit a draft only with a revision note, must acknowledge review before finalizing, and can export the result as Markdown.

## Reliability boundaries

1. Resolution and verified recovery are hard generation prerequisites.
2. The generator returns a strict typed narrative; unknown evidence IDs reject the entire draft.
3. Timeline entries come from database events, not generated text.
4. Each revision is an immutable snapshot with a unique document version.
5. Updates use optimistic concurrency and preserve citations and the canonical timeline.
6. Finalization is explicit and irreversible through the API.

The OpenAI implementation shares the same Responses API Structured Outputs adapter as the mitigation brief. The deterministic implementation produces the identical contract for repeatable local demos and CI. Both pass through the same grounding validator.

## Interview explanation

Frame this phase as a data-provenance and document-control problem, not just text generation. Start with the generation gates, then show the split between generated narrative and deterministic timeline. Explain how the evidence allow-list prevents invented citations, why current state and revision snapshots are separate tables, and how expected-version checks stop two incident commanders from silently overwriting one another.

The nuanced tradeoff is worth calling out: an operator can change prose while its evidence references remain attached. PagerAgent records who made that judgment and why; the citations establish provenance, while the human revision establishes authorship. That is more honest than relabeling edited language as model-verified.

## Demo narration

1. Finish the Phase 4 rollback and show the verified recovery receipt.
2. Resolve the incident and watch the postmortem case file appear automatically.
3. Point to the revision spine and evidence labels, then expand the exact incident timeline.
4. Edit the customer-impact wording and assign a prevention owner; add a meaningful revision note.
5. Show version 2 and its immutable operator-edit entry.
6. Acknowledge team review and finalize the report, producing the locked final revision.
7. Export Markdown and end on the evidence index and prevention register.

For the terminal path, `./scripts/run-demo.sh --approve` now runs through resolution, waits for automatic generation, prints report metrics, and verifies the Markdown export.
