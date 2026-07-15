import type { EvaluationScorecard } from "../lib/api";
import { formatTimestamp, titleCase } from "../lib/format";

interface EvaluationPanelProps {
  scorecard: EvaluationScorecard | null;
  loading: boolean;
  error: string | null;
  onRefresh: () => Promise<void>;
}

function percent(value: number): string {
  return `${Math.round(value * 100)}%`;
}

export function EvaluationPanel({
  scorecard,
  loading,
  error,
  onRefresh,
}: EvaluationPanelProps) {
  return (
    <section className="evaluation-bench" aria-labelledby="evaluation-title">
      <header>
        <div>
          <p className="utility-label">Reliability calibration / suite 01</p>
          <h2 id="evaluation-title">Automation earns its authority.</h2>
        </div>
        <div className="evaluation-verdict">
          <span>{scorecard?.passed ? "All gates passing" : loading ? "Calibrating" : "Attention"}</span>
          <strong>{scorecard ? `${scorecard.scenario_count}/${scorecard.scenario_count}` : "—/—"}</strong>
          <button disabled={loading} onClick={() => void onRefresh()} type="button">
            {loading ? "Running…" : "Run suite"}
          </button>
        </div>
      </header>

      {scorecard ? (
        <>
          <div className="calibration-tape" aria-label="Aggregate evaluation gates">
            {scorecard.gates.map((gate) => (
              <div className={gate.passed ? "gate-pass" : "gate-fail"} key={gate.metric}>
                <span>{titleCase(gate.metric)}</span>
                <div><i style={{ width: percent(gate.value) }} /></div>
                <strong>{percent(gate.value)}</strong>
              </div>
            ))}
          </div>

          <div className="scenario-matrix">
            <div className="matrix-header" aria-hidden="true">
              <span>Scenario / failure class</span>
              <span>Top causal signal</span>
              <span>Safe action</span>
              <span>Adversarial probes</span>
            </div>
            {scorecard.scenarios.map((scenario, index) => {
              const action = String(scenario.predicted_action.action_type ?? "unknown");
              const passedProbes = scenario.adversarial_probes.filter((probe) => probe.passed).length;
              return (
                <article key={scenario.scenario_id}>
                  <div>
                    <b>{String(index + 1).padStart(2, "0")}</b>
                    <span><strong>{scenario.title}</strong><small>{scenario.scenario_id}</small></span>
                  </div>
                  <div>
                    <strong>{scenario.predicted_cause.reference}</strong>
                    <small>{titleCase(scenario.predicted_cause.kind)} · {percent(scenario.predicted_cause.score)}</small>
                  </div>
                  <div>
                    <strong>{titleCase(action)}</strong>
                    <small>{scenario.predicted_action.automation_allowed ? "approval gated" : "advisory only"}</small>
                  </div>
                  <div className="probe-cell">
                    <strong>{passedProbes}/{scenario.adversarial_probes.length}</strong>
                    <span>{scenario.passed ? "pass" : "fail"}</span>
                  </div>
                </article>
              );
            })}
          </div>

          <footer>
            <span>schema {scorecard.schema_version} · suite {scorecard.suite_version}</span>
            <time dateTime={scorecard.generated_at}>last run {formatTimestamp(scorecard.generated_at)}</time>
          </footer>
        </>
      ) : (
        <p className="evaluation-message">{error ?? "Loading deterministic regression suite…"}</p>
      )}
      {error && scorecard ? <p className="evaluation-error">{error}</p> : null}
    </section>
  );
}
