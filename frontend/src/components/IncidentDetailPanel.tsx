import { useState } from "react";

import type {
  CollaborationDecision,
  CollaborationOutput,
  CollaborationOutputKind,
  IncidentDetail,
  IncidentStatus,
  InvestigationDetail,
  MitigationProposal,
  Permission,
  PostmortemDetail,
  PostmortemEditPayload,
  ProposalDecision,
  WorkflowRun,
  WorkflowStreamStatus,
} from "../lib/api";
import { formatDuration, formatTimestamp, titleCase } from "../lib/format";
import { InvestigationPanel } from "./InvestigationPanel";
import { CollaborationPanel } from "./CollaborationPanel";
import { ProposalPanel } from "./ProposalPanel";
import { PostmortemPanel } from "./PostmortemPanel";
import { WorkflowDispatch } from "./WorkflowDispatch";
import { AuthorityNote } from "./AuthorityNote";

const nextStatus: Partial<Record<IncidentStatus, IncidentStatus>> = {
  detected: "investigating",
  mitigated: "resolved",
};

const actionLabel: Partial<Record<IncidentStatus, string>> = {
  detected: "Begin investigation",
  mitigated: "Resolve incident",
};

interface IncidentDetailPanelProps {
  permissions: Permission[];
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
  proposal: MitigationProposal | null;
  proposalLoading: boolean;
  proposalActing: boolean;
  proposalError: string | null;
  onGenerateProposal: () => Promise<void>;
  onProposalDecision: (decision: ProposalDecision, note: string) => Promise<void>;
  collaborationOutputs: CollaborationOutput[];
  collaborationLoading: boolean;
  collaborationActing: string | null;
  collaborationError: string | null;
  onPrepareCollaboration: (kinds: CollaborationOutputKind[]) => Promise<void>;
  onCollaborationDecision: (
    output: CollaborationOutput,
    decision: CollaborationDecision,
    note: string,
  ) => Promise<void>;
  postmortem: PostmortemDetail | null;
  postmortemLoading: boolean;
  postmortemActing: boolean;
  postmortemError: string | null;
  onGeneratePostmortem: () => Promise<void>;
  onSavePostmortem: (edit: PostmortemEditPayload) => Promise<void>;
  onFinalizePostmortem: (note: string) => Promise<void>;
  workflows: WorkflowRun[];
  workflowLoading: boolean;
  workflowError: string | null;
  workflowStreamStatus: WorkflowStreamStatus;
}

export function IncidentDetailPanel({
  permissions,
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
  proposal,
  proposalLoading,
  proposalActing,
  proposalError,
  onGenerateProposal,
  onProposalDecision,
  collaborationOutputs,
  collaborationLoading,
  collaborationActing,
  collaborationError,
  onPrepareCollaboration,
  onCollaborationDecision,
  postmortem,
  postmortemLoading,
  postmortemActing,
  postmortemError,
  onGeneratePostmortem,
  onSavePostmortem,
  onFinalizePostmortem,
  workflows,
  workflowLoading,
  workflowError,
  workflowStreamStatus,
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
  const transitionPermission: Permission | null = upcomingStatus === "resolved"
    ? "incidents.resolve"
    : upcomingStatus
      ? "incidents.transition"
      : null;
  const canTransition = transitionPermission === null || permissions.includes(transitionPermission);
  const metric = incident.alert.metric;

  async function submitTransition() {
    if (upcomingStatus === undefined || !canTransition) return;
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

      <WorkflowDispatch
        error={workflowError}
        loading={workflowLoading}
        streamStatus={workflowStreamStatus}
        workflows={workflows}
      />

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
        canRun={permissions.includes("investigations.run")}
        error={investigationError}
        investigation={investigation}
        loading={investigationLoading}
        onRun={onRunInvestigation}
        running={investigationRunning}
      />

      <ProposalPanel
        acting={proposalActing}
        canDecide={permissions.includes("mitigations.decide")}
        canGenerate={permissions.includes("proposals.generate")}
        error={proposalError}
        incidentStatus={incident.status}
        loading={proposalLoading}
        onDecision={onProposalDecision}
        onGenerate={onGenerateProposal}
        proposal={proposal}
      />

      <CollaborationPanel
        acting={collaborationActing}
        canDecide={permissions.includes("collaboration.decide")}
        canPrepare={permissions.includes("collaboration.prepare")}
        error={collaborationError}
        loading={collaborationLoading}
        onDecision={onCollaborationDecision}
        onPrepare={onPrepareCollaboration}
        outputs={collaborationOutputs}
        proposal={proposal}
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
          <button disabled={transitioning || !canTransition} onClick={submitTransition} type="button">
            {transitioning ? "Recording…" : actionLabel[incident.status]}
          </button>
          {transitionPermission ? (
            <AuthorityNote
              allowed={canTransition}
              message={upcomingStatus === "resolved"
                ? "Only a responder with closure authority can resolve this incident."
                : "This role can inspect the incident but cannot advance its response state."}
              permission={transitionPermission}
            />
          ) : null}
          {transitionError ? <p className="action-error" role="alert">{transitionError}</p> : null}
        </section>
      ) : incident.status === "investigating" ? (
        <div className="verification-banner">
          <strong>Mitigation state is execution-owned.</strong>
          <span>Approve a grounded action above; only verified recovery telemetry can mark this incident mitigated.</span>
        </div>
      ) : (
        <div className="resolved-banner">Incident closed · timeline preserved for review</div>
      )}

      <PostmortemPanel
        acting={postmortemActing}
        canEdit={permissions.includes("postmortems.edit")}
        canFinalize={permissions.includes("postmortems.finalize")}
        canGenerate={permissions.includes("postmortems.generate")}
        error={postmortemError}
        incidentStatus={incident.status}
        loading={postmortemLoading}
        onFinalize={onFinalizePostmortem}
        onGenerate={onGeneratePostmortem}
        onSave={onSavePostmortem}
        postmortem={postmortem}
      />
    </section>
  );
}
