import type { EvidenceArtifact, InvestigationDetail } from "../lib/api";
import { formatTimestamp, titleCase } from "../lib/format";
import { AuthorityNote } from "./AuthorityNote";

interface InvestigationPanelProps {
  canRun: boolean;
  investigation: InvestigationDetail | null;
  loading: boolean;
  running: boolean;
  error: string | null;
  onRun: () => Promise<void>;
}

function citation(value: string): string {
  return `E-${value.slice(0, 6)}`;
}

interface GitHubAppProvenance {
  repository: string;
  connectorVersion: string | null;
  credentialVersion: string | null;
}

interface PrometheusProvenance {
  providerVersion: string;
  catalogVersion: string;
  service: string;
  queryId: string;
  windowStartedAt: string;
  windowEndedAt: string;
  seriesCount: number;
  sampleCount: number;
  truncated: boolean;
  connectorVersion: number;
  credentialVersion: number;
  sourceUri: string;
  contentHash: string;
}

const SERVICE_PATTERN = /^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,98}[A-Za-z0-9])?$/;
const QUERY_ID_PATTERN = /^[a-z0-9][a-z0-9._-]{0,99}$/;
const VERSION_PATTERN = /^[a-z0-9][a-z0-9._-]{0,99}$/;
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const SHA256_PATTERN = /^[0-9a-f]{64}$/;
const ZONED_TIMESTAMP_PATTERN = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$/;

function receiptScalar(value: unknown): string | null {
  return typeof value === "string" || typeof value === "number" ? String(value) : null;
}

function githubAppProvenance(artifact: EvidenceArtifact): GitHubAppProvenance | null {
  if (artifact.kind !== "commit_catalog" || artifact.payload.provider !== "github_app") {
    return null;
  }
  const repository = receiptScalar(artifact.payload.repository);
  if (!repository) return null;
  return {
    repository,
    connectorVersion: receiptScalar(artifact.payload.connector_version),
    credentialVersion: receiptScalar(artifact.payload.credential_version),
  };
}

function boundedReceiptString(
  value: unknown,
  pattern: RegExp,
  maximumLength: number,
): string | null {
  return typeof value === "string" && value.length <= maximumLength && pattern.test(value)
    ? value
    : null;
}

function receiptInteger(value: unknown, minimum: number, maximum: number): number | null {
  return typeof value === "number" && Number.isInteger(value) && value >= minimum && value <= maximum
    ? value
    : null;
}

function utcReceiptTimestamp(value: unknown): { iso: string; milliseconds: number } | null {
  if (
    typeof value !== "string" ||
    value.length > 40 ||
    !ZONED_TIMESTAMP_PATTERN.test(value)
  ) return null;
  const milliseconds = Date.parse(value);
  if (!Number.isFinite(milliseconds)) return null;
  return {
    iso: new Date(milliseconds).toISOString().replace(".000Z", "Z"),
    milliseconds,
  };
}

function sanitizedPrometheusSource(
  artifact: EvidenceArtifact,
  service: string,
  connectorId: string,
): string | null {
  const expected = `prometheus://connector/${connectorId}/${service}`;
  return artifact.source_uri === expected ? expected : null;
}

function prometheusProvenance(artifact: EvidenceArtifact): PrometheusProvenance | null {
  if (artifact.kind !== "prometheus_metric_snapshot" || artifact.payload.provider !== "prometheus") {
    return null;
  }

  const service = boundedReceiptString(artifact.payload.service, SERVICE_PATTERN, 100);
  const queryId = boundedReceiptString(artifact.payload.query_id, QUERY_ID_PATTERN, 100);
  const providerVersion = boundedReceiptString(
    artifact.payload.provider_version,
    VERSION_PATTERN,
    100,
  );
  const catalogVersion = boundedReceiptString(
    artifact.payload.catalog_version,
    VERSION_PATTERN,
    100,
  );
  const connectorId = boundedReceiptString(artifact.payload.connector_id, UUID_PATTERN, 36);
  const connectorVersion = receiptInteger(artifact.payload.connector_version, 1, Number.MAX_SAFE_INTEGER);
  const credentialVersion = receiptInteger(artifact.payload.credential_version, 1, Number.MAX_SAFE_INTEGER);
  const seriesCount = receiptInteger(artifact.payload.series_count, 0, 10_000);
  const sampleCount = receiptInteger(artifact.payload.sample_count, 0, 1_000_000);
  const windowStartedAt = utcReceiptTimestamp(artifact.payload.window_started_at);
  const windowEndedAt = utcReceiptTimestamp(artifact.payload.window_ended_at);
  const contentHash = SHA256_PATTERN.test(artifact.content_hash) ? artifact.content_hash : null;
  const truncated = typeof artifact.payload.truncated === "boolean"
    ? artifact.payload.truncated
    : null;

  if (
    !service ||
    !queryId ||
    !providerVersion ||
    !catalogVersion ||
    !connectorId ||
    connectorVersion === null ||
    credentialVersion === null ||
    seriesCount === null ||
    sampleCount === null ||
    !windowStartedAt ||
    !windowEndedAt ||
    windowStartedAt.milliseconds > windowEndedAt.milliseconds ||
    truncated === null ||
    !contentHash
  ) {
    return null;
  }

  const sourceUri = sanitizedPrometheusSource(artifact, service, connectorId);
  if (!sourceUri) return null;

  return {
    providerVersion,
    catalogVersion,
    service,
    queryId,
    windowStartedAt: windowStartedAt.iso,
    windowEndedAt: windowEndedAt.iso,
    seriesCount,
    sampleCount,
    truncated,
    connectorVersion,
    credentialVersion,
    sourceUri,
    contentHash,
  };
}

function artifactSource(artifact: EvidenceArtifact): string {
  if (artifact.kind !== "prometheus_metric_snapshot") return artifact.source_uri;
  const service = boundedReceiptString(artifact.payload.service, SERVICE_PATTERN, 100);
  const connectorId = boundedReceiptString(artifact.payload.connector_id, UUID_PATTERN, 36);
  if (!service || !connectorId) return "Prometheus source withheld";
  return sanitizedPrometheusSource(artifact, service, connectorId) ?? "Prometheus source withheld";
}

function artifactHashPreview(artifact: EvidenceArtifact): string {
  return SHA256_PATTERN.test(artifact.content_hash)
    ? artifact.content_hash.slice(0, 12)
    : "Hash unavailable";
}

export function InvestigationPanel({
  canRun,
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
        <div className="guarded-action">
          <button disabled={running || !canRun} onClick={() => void onRun()} type="button">
            {running ? "Collecting evidence…" : "Run investigation"}
          </button>
          <AuthorityNote
            allowed={canRun}
            message="This role may inspect evidence but cannot start an investigation job."
            permission="investigations.run"
          />
        </div>
        {error ? <p className="investigation-error" role="alert">{error}</p> : null}
      </section>
    );
  }

  const primaryCluster = investigation.error_clusters[0];
  const topCause = investigation.cause_candidates[0];
  const topRunbook = investigation.runbook_matches[0];
  const metricArtifacts = investigation.evidence.filter(
    (artifact) => prometheusProvenance(artifact) !== null,
  );
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
          <button disabled={running || !canRun} onClick={() => void onRun()} type="button">
            {running ? "Running…" : "Rerun"}
          </button>
        </div>
      </div>

      <AuthorityNote
        allowed={canRun}
        message="The preserved investigation remains readable; rerunning evidence collection is not granted."
        permission="investigations.run"
      />

      <section
        aria-labelledby="signal-coverage-title"
        className="signal-coverage"
      >
        <header>
          <span>Cross-signal inputs</span>
          <strong id="signal-coverage-title">Signal coverage</strong>
        </header>
        <dl>
          <div className={metricArtifacts.length > 0 ? "signal-channel collected" : "signal-channel"}>
            <dt>Metrics</dt>
            <dd>{metricArtifacts.length > 0
              ? `${metricArtifacts.length} snapshot${metricArtifacts.length === 1 ? "" : "s"}`
              : "Not collected"}</dd>
            <dd className="signal-channel-detail">
              {metricArtifacts.length > 0 ? "Bounded Prometheus range" : "No immutable artifact"}
            </dd>
            {metricArtifacts.length > 0 ? (
              <dd className="signal-channel-citations">
                {metricArtifacts.slice(0, 4).map((artifact) => (
                  <span key={artifact.id}>{citation(artifact.id)}</span>
                ))}
              </dd>
            ) : null}
          </div>
          <div className="signal-channel">
            <dt>Logs</dt>
            <dd>Not collected</dd>
            <dd className="signal-channel-detail">No immutable artifact</dd>
          </div>
          <div className="signal-channel">
            <dt>Traces</dt>
            <dd>Not collected</dd>
            <dd className="signal-channel-detail">No immutable artifact</dd>
          </div>
        </dl>
      </section>

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
          {investigation.evidence.map((artifact) => {
            const githubReceipt = githubAppProvenance(artifact);
            const prometheusReceipt = prometheusProvenance(artifact);
            return (
              <article key={artifact.id}>
                <span>{citation(artifact.id)}</span>
                <div>
                  <strong>{titleCase(artifact.kind)}</strong>
                  <small>{artifactSource(artifact)}</small>
                </div>
                <code>{artifactHashPreview(artifact)}</code>
                {githubReceipt ? (
                  <dl
                    aria-label="GitHub App provenance receipt"
                    className="provider-provenance-receipt"
                    role="region"
                  >
                    <div>
                      <dt>Provider</dt>
                      <dd>GitHub App</dd>
                    </div>
                    <div>
                      <dt>Repository</dt>
                      <dd>{githubReceipt.repository}</dd>
                    </div>
                    {githubReceipt.connectorVersion ? (
                      <div>
                        <dt>Connector version</dt>
                        <dd>v{githubReceipt.connectorVersion}</dd>
                      </div>
                    ) : null}
                    {githubReceipt.credentialVersion ? (
                      <div>
                        <dt>Credential version</dt>
                        <dd>v{githubReceipt.credentialVersion}</dd>
                      </div>
                    ) : null}
                    <div className="provider-provenance-wide">
                      <dt>Source</dt>
                      <dd>{artifact.source_uri}</dd>
                    </div>
                    <div className="provider-provenance-wide">
                      <dt>Content hash</dt>
                      <dd>{artifact.content_hash}</dd>
                    </div>
                  </dl>
                ) : null}
                {prometheusReceipt ? (
                  <dl
                    aria-label="Prometheus provenance receipt"
                    className="provider-provenance-receipt prometheus-provenance-receipt"
                    role="region"
                  >
                    <div>
                      <dt>Provider</dt>
                      <dd>Prometheus HTTP API</dd>
                    </div>
                    <div>
                      <dt>Provider version</dt>
                      <dd>{prometheusReceipt.providerVersion}</dd>
                    </div>
                    <div>
                      <dt>Catalog version</dt>
                      <dd>{prometheusReceipt.catalogVersion}</dd>
                    </div>
                    <div>
                      <dt>Service</dt>
                      <dd>{prometheusReceipt.service}</dd>
                    </div>
                    <div>
                      <dt>Query ID</dt>
                      <dd>{prometheusReceipt.queryId}</dd>
                    </div>
                    <div>
                      <dt>Truncated</dt>
                      <dd>{prometheusReceipt.truncated ? "Yes" : "No"}</dd>
                    </div>
                    <div>
                      <dt>Series</dt>
                      <dd>{prometheusReceipt.seriesCount}</dd>
                    </div>
                    <div>
                      <dt>Samples</dt>
                      <dd>{prometheusReceipt.sampleCount}</dd>
                    </div>
                    <div>
                      <dt>Connector version</dt>
                      <dd>v{prometheusReceipt.connectorVersion}</dd>
                    </div>
                    <div>
                      <dt>Credential version</dt>
                      <dd>v{prometheusReceipt.credentialVersion}</dd>
                    </div>
                    <div className="provider-provenance-wide">
                      <dt>UTC window</dt>
                      <dd>
                        <time dateTime={prometheusReceipt.windowStartedAt}>
                          {prometheusReceipt.windowStartedAt}
                        </time>
                        {" → "}
                        <time dateTime={prometheusReceipt.windowEndedAt}>
                          {prometheusReceipt.windowEndedAt}
                        </time>
                      </dd>
                    </div>
                    <div className="provider-provenance-wide">
                      <dt>Source</dt>
                      <dd>{prometheusReceipt.sourceUri}</dd>
                    </div>
                    <div className="provider-provenance-full">
                      <dt>Content hash</dt>
                      <dd>{prometheusReceipt.contentHash}</dd>
                    </div>
                  </dl>
                ) : null}
              </article>
            );
          })}
        </div>
      </details>
      {error ? <p className="investigation-error" role="alert">{error}</p> : null}
    </section>
  );
}
