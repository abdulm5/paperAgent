import { useState } from "react";

import type { IncidentDetail, IncidentStatus, InvestigationDetail } from "../lib/api";
import { formatDuration, formatTimestamp, titleCase } from "../lib/format";
import { InvestigationPanel } from "./InvestigationPanel";

const nextStatus: Partial<Record<IncidentStatus, IncidentStatus>> = {
  detected: "investigating",
  investigating: "mitigated",
  mitigated: "resolved",
};

const actionLabel: Partial<Record<IncidentStatus, string>> = {
  detected: "Begin investigation",
  investigating: "Mark mitigated",
  mitigated: "Resolve incident",
};

interface IncidentDetailPanelProps {
  incident: IncidentDetail | null;
  loading: boolean;
  transitionError: string | null;
  transitioning: boolean;
  onTransition: (toStatus: IncidentStatus, note: string) => Promise<boolean>;
  investigation: InvestigationDetail | null;
  investigationLoading: boolean;
  investigationRunning: boolean;
  investigationError: string | null;
  onRunInvestigation: () => Promise<void>;
}

export function IncidentDetailPanel({
  incident,
  loading,
  transitionError,
  transitioning,
  onTransition,
  investigation,
  investigationLoading,
  investigationRunning,
  investigationError,
  onRunInvestigation,
}: IncidentDetailPanelProps) {
  const [note, setNote] = useState("");

  if (loading && incident === null) {
    return <section className="incident-detail detail-message">Loading incident evidence…</section>;
  }

  if (incident === null) {
    return (
      <section className="incident-detail detail-message">
        Select an incident to inspect its evidence and response timeline.
      </section>
    );
  }

  const upcomingStatus = nextStatus[incident.status];
  const metric = incident.alert.metric;

  async function submitTransition() {
    if (upcomingStatus === undefined) return;
    if (await onTransition(upcomingStatus, note)) setNote("");
  }

  return (
    <section className="incident-detail" aria-labelledby="incident-title">
      <div className="incident-masthead">
        <div>
          <p className="utility-label">
            {incident.severity} / {incident.service}
          </p>
          <h1 id="incident-title">{incident.summary}</h1>
        </div>
        <div className={`status-stamp status-${incident.status}`}>
          <span>State</span>
          <strong>{titleCase(incident.status)}</strong>
        </div>
      </div>

      <div className="incident-facts">
        <div>
          <span>Detected</span>
          <strong>{formatTimestamp(incident.detected_at)}</strong>
        </div>
        <div>
          <span>Alert deliveries</span>
          <strong>{incident.alert_count}</strong>
        </div>
        <div>
          <span>Record version</span>
          <strong>v{incident.version}</strong>
        </div>
        <div>
          <span>Incident ID</span>
          <strong className="mono-value">{incident.id.slice(0, 8)}</strong>
        </div>
      </div>

      <section className="evidence-section" aria-labelledby="evidence-title">
        <div className="section-title-row">
          <div>
            <p className="utility-label">Monitoring evidence</p>
            <h2 id="evidence-title">Threshold breach</h2>
          </div>
          <a href={incident.alert.telemetry_url} rel="noreferrer" target="_blank">
            Open source telemetry ↗
          </a>
        </div>

        <div className="evidence-tape">
          <div className="evidence-cell critical-reading">
            <span>Error rate</span>
            <strong>{(metric.value * 100).toFixed(1)}%</strong>
            <small>{(metric.threshold * 100).toFixed(1)}% threshold</small>
          </div>
          <div className="evidence-cell">
            <span>Failed requests</span>
            <strong>{metric.failed_request_count}</strong>
            <small>of {metric.request_count} observed</small>
          </div>
          <div className="evidence-cell">
            <span>Window</span>
            <strong>{formatDuration(metric.window_seconds)}</strong>
            <small>{metric.name.replaceAll("_", " ")}</small>
          </div>
          <div className="evidence-cell release-cell">
            <span>Active release</span>
            <strong>{incident.alert.release.name}</strong>
            <small>commit {incident.alert.release.commit_sha}</small>
          </div>
        </div>
      </section>

      <InvestigationPanel
        error={investigationError}
        investigation={investigation}
        loading={investigationLoading}
        onRun={onRunInvestigation}
        running={investigationRunning}
      />

      <section className="timeline-section" aria-labelledby="timeline-title">
        <div className="section-title-row">
          <div>
            <p className="utility-label">Append-only record</p>
            <h2 id="timeline-title">Response timeline</h2>
          </div>
        </div>
        <ol className="timeline">
          {incident.events.map((event, index) => (
            <li key={event.id}>
              <span className="timeline-index">{String(index + 1).padStart(2, "0")}</span>
              <div className="timeline-copy">
                <div>
                  <strong>
                    {titleCase(event.event_type.split(".").at(-1) ?? event.event_type)}
                  </strong>
                  <time dateTime={event.created_at}>{formatTimestamp(event.created_at)}</time>
                </div>
                <p>{event.note ?? "Incident record updated."}</p>
                <span>
                  {event.actor}
                  {event.from_status && event.to_status
                    ? ` · ${event.from_status} → ${event.to_status}`
                    : ""}
                </span>
              </div>
            </li>
          ))}
        </ol>
      </section>

      {upcomingStatus ? (
        <section className="operator-action" aria-labelledby="operator-action-title">
          <div>
            <p className="utility-label">Human decision</p>
            <h2 id="operator-action-title">Advance the response</h2>
          </div>
          <label>
            Timeline note
            <input
              onChange={(event) => setNote(event.target.value)}
              placeholder="What did the operator verify?"
              value={note}
            />
          </label>
          <button disabled={transitioning} onClick={submitTransition} type="button">
            {transitioning ? "Recording…" : actionLabel[incident.status]}
          </button>
          {transitionError ? <p className="action-error">{transitionError}</p> : null}
        </section>
      ) : (
        <div className="resolved-banner">Incident closed · timeline preserved for review</div>
      )}
    </section>
  );
}
