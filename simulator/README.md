# Outage simulator

This directory will contain the deterministic toy production environment used in demos and benchmarks. Milestone 1 will add a checkout API, a synthetic traffic generator, deploy events, structured logs, and threshold-based alert emission.

The simulator is deliberately separate from PagerAgent: the copilot must consume the same shaped signals an external monitoring stack would provide, not reach into application internals for the answer.
