import type { InvestigationDetail } from "../lib/api";
import { formatTimestamp, titleCase } from "../lib/format";

interface InvestigationPanelProps {
  investigation: InvestigationDetail | null;
  loading: boolean;
  running: boolean;
  error: string | null;
  onRun: () => Promise<void>;
}

function citation(value: string): string {
  return `E-${value.slice(0, 6)}`;
}

export function InvestigationPanel({
  investigation,
  loading,
  running,
  error,
  onRun,
}: InvestigationPanelProps) {
  if (loading && investigation === null) {
    return <section className="investigation-panel investigation-message">Reading evidence ledger…</section>;
  }

  if (investigation === null) {
    return (
      <section className="investigation-panel investigation-empty" aria-labelledby="investigation-title">
        <div>
          <p className="utility-label">Deterministic investigator</p>
          <h2 id="investigation-title">Evidence has not been collected yet.</h2>
          <p>
            Snapshot telemetry, cluster failures, rank recent commits, and retrieve a grounded
            runbook.
          </p>
        </div>
        <button disabled={running} onClick={() => void onRun()} type="button">
          {running ? "Collecting evidence…" : "Run investigation"}
        </button>
        {error ? <p className="investigation-error">{error}</p> : null}
      </section>
    );
  }

  const primaryCluster = investigation.error_clusters[0];
  const topCause = investigation.cause_candidates[0];
  const topRunbook = investigation.runbook_matches[0];
  const paymentMethods = primaryCluster?.affected_attributes.payment_methods;
  const cohort = Array.isArray(paymentMethods) ? paymentMethods.join(", ") : "unknown cohort";

  return (
    <section className="investigation-panel" aria-labelledby="investigation-title">
      <div className="section-title-row investigation-heading">
        <div>
          <p className="utility-label">Evidence ledger</p>
          <h2 id="investigation-title">Ranked investigation</h2>
        </div>
        <div className="investigation-run-meta">
          <span>{investigation.status}</span>
          <time dateTime={investigation.completed_at ?? investigation.started_at}>
            {formatTimestamp(investigation.completed_at ?? investigation.started_at)}
          </time>
          <button disabled={running} onClick={() => void onRun()} type="button">
            {running ? "Running…" : "Rerun"}
          </button>
        </div>
      </div>

      {primaryCluster ? (
        <div className="cluster-strip">
          <div className="cluster-count">
            <strong>{primaryCluster.failure_count}</strong>
            <span>clustered failures</span>
          </div>
          <div>
            <span className="cluster-signature">{primaryCluster.signature}</span>
            <h3>{primaryCluster.error_type}</h3>
            <p>
              Every failure occurred on <code>{primaryCluster.endpoint}</code> for the
              {" "}<strong>{cohort}</strong> cohort.
            </p>
          </div>
          <div className="citation-stack">
            {primaryCluster.evidence_ids.map((id) => (
              <span key={id}>{citation(id)}</span>
            ))}
          </div>
        </div>
      ) : null}

      {topCause ? (
        <section className="causal-stack" aria-labelledby="causal-stack-title">
          <div>
            <p className="utility-label">Cross-signal causal ranker</p>
            <h3 id="causal-stack-title">{topCause.title}</h3>
            <code>{titleCase(topCause.kind)} / {topCause.reference}</code>
          </div>
          <div className="causal-score">
            <strong>{Math.round(topCause.score * 100)}%</strong>
            <span>causal confidence</span>
          </div>
          <ol>
            {investigation.cause_candidates.slice(0, 4).map((cause) => (
              <li className={cause.rank === 1 ? "active" : ""} key={cause.id ?? cause.reference}>
                <span>{String(cause.rank).padStart(2, "0")}</span>
                <div>
                  <strong>{cause.reference}</strong>
                  <small>{titleCase(cause.kind)}</small>
                </div>
                <b>{Math.round(cause.score * 100)}</b>
              </li>
            ))}
          </ol>
        </section>
      ) : null}

      <div className="investigation-grid">
        <section className="candidate-dossier" aria-labelledby="candidate-title">
          <div className="subsection-heading">
            <div>
              <p className="utility-label">Deploy correlation</p>
              <h3 id="candidate-title">Commit dossier</h3>
            </div>
            <span>Top {investigation.commit_candidates.length}</span>
          </div>
          <ol>
            {investigation.commit_candidates.map((candidate) => (
              <li className={candidate.rank === 1 ? "candidate top-candidate" : "candidate"} key={candidate.id}>
                <div className="candidate-rank">{String(candidate.rank).padStart(2, "0")}</div>
                <div className="candidate-body">
                  <div className="candidate-title-row">
                    <div>
                      <code>{candidate.commit_sha}</code>
                      <strong>{candidate.title}</strong>
                    </div>
                    <span>{Math.round(candidate.total_score * 100)}%</span>
                  </div>
                  <div className="score-track" aria-label={`${Math.round(candidate.total_score * 100)} percent suspicion score`}>
                    <span style={{ width: `${candidate.total_score * 100}%` }} />
                  </div>
                  <ul className="reason-list">
                    {candidate.explanation.map((reason) => <li key={reason}>{reason}</li>)}
                  </ul>
                  <div className="feature-line">
                    {Object.entries(candidate.feature_scores).map(([name, score]) => (
                      <span key={name}>{titleCase(name)} {Math.round(score * 100)}</span>
                    ))}
                  </div>
                  <div className="citation-line">
                    {candidate.evidence_ids.slice(0, 4).map((id) => <span key={id}>{citation(id)}</span>)}
                  </div>
                </div>
              </li>
            ))}
          </ol>
        </section>

        <aside className="runbook-result" aria-labelledby="runbook-title">
          <div className="subsection-heading">
            <div>
              <p className="utility-label">Retrieved procedure</p>
              <h3 id="runbook-title">Grounded next steps</h3>
            </div>
          </div>
          {topRunbook ? (
            <>
              <div className="runbook-score">
                <span>Rank 01</span>
                <strong>{Math.round(topRunbook.total_score * 100)}%</strong>
              </div>
              <h4>{topRunbook.title}</h4>
              <p className="runbook-identity">{topRunbook.runbook_id} · {topRunbook.failure_mode}</p>
              <div className="runbook-sections">
                {topRunbook.matched_sections.map((section) => (
                  <section key={section.heading}>
                    <strong>{section.heading}</strong>
                    <p>{section.excerpt}</p>
                  </section>
                ))}
              </div>
              <div className="citation-line">
                {topRunbook.evidence_ids.map((id) => <span key={id}>{citation(id)}</span>)}
              </div>
            </>
          ) : <p>No matching runbook was found.</p>}
        </aside>
      </div>

      <details className="provenance-drawer">
        <summary>Inspect provenance · {investigation.evidence.length} immutable artifacts</summary>
        <div>
          {investigation.evidence.map((artifact) => (
            <article key={artifact.id}>
              <span>{citation(artifact.id)}</span>
              <div>
                <strong>{titleCase(artifact.kind)}</strong>
                <small>{artifact.source_uri}</small>
              </div>
              <code>{artifact.content_hash.slice(0, 12)}</code>
            </article>
          ))}
        </div>
      </details>
      {error ? <p className="investigation-error">{error}</p> : null}
    </section>
  );
}
