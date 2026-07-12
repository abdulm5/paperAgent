# Milestone 4: grounded copilot and human-approved mitigation

## Outcome

PagerAgent now closes the loop from evidence to verified recovery. The checkout scenario produces a four-claim cited brief, proposes a typed rollback from faulty commit `8fa23c1` to `stable-v1`, waits for an operator decision, runs the rollback once, sends 15 canaries including five digital-wallet requests, and marks the incident mitigated only when the recovery cohort has zero failures.

## Control boundaries

The phase separates four kinds of authority:

1. Deterministic investigation establishes ranked facts and citations.
2. A synthesizer turns those facts into readable root-cause, impact, recommendation, risk, and Slack copy.
3. A citation validator rejects unsupported output, while deterministic policy creates the only executable action envelope.
4. A human approval unlocks the allow-listed executor; canary telemetry decides whether mitigation succeeded.

This division is more important than the prompt. The OpenAI provider uses the Responses API with strict structured output, low reasoning effort, and `store: false`. The offline provider implements the same protocol, so tests and demos do not depend on network access or model variability. See the official [Structured Outputs guide](https://developers.openai.com/api/docs/guides/structured-outputs) and [current model guidance](https://developers.openai.com/api/docs/guides/latest-model).

## Interview explanation

Start with the threat model: generated text must not become an operational command. Show that model output and action parameters are different types. Walk through the unknown-citation test, the approval state machine, the committed decision record, the executor allow-list, and the recovery receipt. Explain that a deployment HTTP 200 only proves the request was accepted; post-change canaries prove whether users recovered.

The provider interface also gives a clean systems-design discussion. The deterministic synthesizer makes CI reproducible, while the OpenAI adapter demonstrates a current structured-output integration. Both are downstream of the same evidence pipeline and upstream of the same guardrails.

## Demo narration

1. Replay the bad deployment and show the eight digital-wallet failures.
2. Show the ranked commit and rollback runbook from Phase 3.
3. Move to the decision packet: root cause, impact, recommendation, risk, Slack draft, and citations.
4. Point out the dark authority rail—the model cannot cross it.
5. Begin the investigation, check the evidence-review acknowledgment, add a note, and approve.
6. Show the recovery receipt: `faulty-v2 → stable-v1`, 15 canaries, 0 failures.
7. End on the timeline containing detection, investigation, proposal, human approval, and verified mitigation.

For a terminal-only walkthrough, `./scripts/run-demo.sh --approve` exercises the same explicit approval API and prints the recovery result.
