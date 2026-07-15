import { useState } from "react";

import type {
  GroundedClaim,
  IncidentStatus,
  MitigationProposal,
  ProposalDecision,
} from "../lib/api";
import { formatTimestamp, titleCase } from "../lib/format";

interface ProposalPanelProps {
  proposal: MitigationProposal | null;
  incidentStatus: IncidentStatus;
  loading: boolean;
  acting: boolean;
  error: string | null;
  onGenerate: () => Promise<void>;
  onDecision: (decision: ProposalDecision, note: string) => Promise<void>;
}

function citation(value: string): string {
  return `E-${value.slice(0, 6)}`;
}

function ClaimCitations({ claim }: { claim: GroundedClaim | undefined }) {
  if (!claim) return null;
  return (
    <div className="citation-line">
      {claim.evidence_ids.map((id) => <span key={id}>{citation(id)}</span>)}
    </div>
  );
}

function telemetryState(payload: Record<string, unknown>, featureFlag: string | null): string {
  if (featureFlag) {
    const flags = payload.feature_flags;
    if (typeof flags === "object" && flags !== null && featureFlag in flags) {
      return `${featureFlag}=${String(flags[featureFlag as keyof typeof flags])}`;
    }
  }
  const release = payload.current_release;
  if (typeof release === "object" && release !== null && "name" in release) {
    return String(release.name);
  }
  return "unknown";
}

export function ProposalPanel({
  proposal,
  incidentStatus,
  loading,
  acting,
  error,
  onGenerate,
  onDecision,
}: ProposalPanelProps) {
  const [note, setNote] = useState("");
  const [reviewed, setReviewed] = useState(false);

  if (loading && proposal === null) {
    return <section className="proposal-panel proposal-message">Assembling decision packet…</section>;
  }

  if (proposal === null) {
    return (
      <section className="proposal-panel proposal-empty" aria-labelledby="proposal-title">
        <div>
          <p className="utility-label">Grounded copilot</p>
          <h2 id="proposal-title">No decision packet yet.</h2>
          <p>Convert ranked evidence into a cited brief and an approval-gated action.</p>
        </div>
        <button disabled={acting} onClick={() => void onGenerate()} type="button">
          {acting ? "Generating…" : "Generate incident brief"}
        </button>
        {error ? <p className="proposal-error">{error}</p> : null}
      </section>
    );
  }

  const claims = Object.fromEntries(proposal.claims.map((claim) => [claim.kind, claim]));
  const pending = proposal.status === "pending_approval";
  const advisory = proposal.status === "advisory";
  const verified = proposal.status === "verification_passed" && proposal.execution;
  const canApprove = incidentStatus === "investigating" && reviewed && !acting;
  const responsePayload = proposal.execution?.response_payload;
  const canaryCount = responsePayload?.canary_request_count;
  const failureCount = responsePayload?.recovery_failure_count;
  const isFlagChange = proposal.action.action_type === "disable_feature_flag";
  const actionLabel = isFlagChange ? "feature flag change" : "rollback";

  async function decide(decision: ProposalDecision) {
    await onDecision(decision, note);
    setNote("");
    setReviewed(false);
  }

  return (
    <section className="proposal-panel" aria-labelledby="proposal-title">
      <div className="section-title-row proposal-heading">
        <div>
          <p className="utility-label">Grounded copilot / decision packet</p>
          <h2 id="proposal-title">The evidence says this.</h2>
        </div>
        <div className="proposal-meta">
          <span className={`proposal-status proposal-status-${proposal.status}`}>
            {titleCase(proposal.status)}
          </span>
          <small>{proposal.model_name} · {proposal.prompt_version}</small>
          <time dateTime={proposal.created_at}>{formatTimestamp(proposal.created_at)}</time>
        </div>
      </div>

      <div className="brief-lead">
        <div className="confidence-dial" aria-label={`${Math.round(proposal.confidence * 100)} percent confidence`}>
          <strong>{Math.round(proposal.confidence * 100)}%</strong>
          <span>evidence confidence</span>
        </div>
        <div>
          <p className="brief-label">Probable root cause</p>
          <h3>{proposal.root_cause_summary}</h3>
          <ClaimCitations claim={claims.root_cause} />
        </div>
      </div>

      <div className="brief-grid">
        <article>
          <p className="brief-label">Customer impact</p>
          <p>{proposal.impact_summary}</p>
          <ClaimCitations claim={claims.impact} />
        </article>
        <article>
          <p className="brief-label">Recommended action</p>
          <p>{proposal.recommended_action}</p>
          <ClaimCitations claim={claims.recommendation} />
        </article>
        <article>
          <p className="brief-label">Change risk</p>
          <p>{proposal.risk_summary}</p>
          <ClaimCitations claim={claims.risk} />
        </article>
      </div>

      <details className="slack-draft">
        <summary>Preview Slack incident update</summary>
        <p>{proposal.slack_update}</p>
      </details>

      <div className="authority-boundary">
        <div className="authority-rail">
          <span>Write boundary</span>
          <strong>{advisory ? "Write blocked" : "Human authority required"}</strong>
          <small>
            {advisory
              ? "Evidence points outside this service's safe automation boundary."
              : "The model cannot cross this line."}
          </small>
        </div>
        <div className="action-envelope">
          <p className="brief-label">Typed action envelope</p>
          <dl>
            <div><dt>Action</dt><dd>{proposal.action.action_type}</dd></div>
            <div><dt>Service</dt><dd>{proposal.action.target_service}</dd></div>
            {proposal.action.expected_faulty_commit ? (
              <div><dt>From commit</dt><dd>{proposal.action.expected_faulty_commit}</dd></div>
            ) : null}
            {proposal.action.target_release ? (
              <div><dt>Target</dt><dd>{proposal.action.target_release}</dd></div>
            ) : null}
            {proposal.action.feature_flag ? (
              <div><dt>Feature flag</dt><dd>{proposal.action.feature_flag}</dd></div>
            ) : null}
            <div>
              <dt>Automation</dt>
              <dd>{proposal.action.automation_allowed ? "approval gated" : "not permitted"}</dd>
            </div>
          </dl>
          <ol>
            {proposal.verification_steps.map((step) => <li key={step}>{step}</li>)}
          </ol>
        </div>

        {pending ? (
          <div className="approval-console">
            <p className="brief-label">Operator decision</p>
            {incidentStatus !== "investigating" ? (
              <p className="approval-warning">Begin the investigation before approval is unlocked.</p>
            ) : null}
            <label className="review-check">
              <input
                checked={reviewed}
                onChange={(event) => setReviewed(event.target.checked)}
                type="checkbox"
              />
              I reviewed the cited evidence and {actionLabel} target.
            </label>
            <label>
              Decision note
              <textarea
                onChange={(event) => setNote(event.target.value)}
                placeholder="What did you verify before deciding?"
                value={note}
              />
            </label>
            <div className="decision-buttons">
              <button
                className="approve-button"
                disabled={!canApprove}
                onClick={() => void decide("approve")}
                type="button"
              >
                {acting ? "Executing change…" : `Approve ${actionLabel}`}
              </button>
              <button
                className="reject-button"
                disabled={acting}
                onClick={() => void decide("reject")}
                type="button"
              >
                Reject proposal
              </button>
            </div>
          </div>
        ) : null}

        {advisory ? (
          <div className="decision-receipt advisory-receipt">
            <strong>Advisory only</strong>
            <p>Escalate to the owning service. PagerAgent will not execute this action.</p>
          </div>
        ) : null}

        {verified ? (
          <div className="recovery-receipt">
            <p className="brief-label">Recovery receipt</p>
            <strong>{isFlagChange ? "Feature flag change verified" : "Rollback verified"}</strong>
            <div>
              <span>{telemetryState(proposal.execution?.before_telemetry ?? {}, proposal.action.feature_flag)}</span>
              <b>→</b>
              <span>{telemetryState(proposal.execution?.after_telemetry ?? {}, proposal.action.feature_flag)}</span>
            </div>
            <dl>
              <div><dt>Canaries</dt><dd>{String(canaryCount ?? "—")}</dd></div>
              <div><dt>Failures</dt><dd>{String(failureCount ?? "—")}</dd></div>
            </dl>
            <small>Incident moved to mitigated after telemetry verification.</small>
          </div>
        ) : null}

        {proposal.status === "rejected" ? (
          <div className="decision-receipt rejected-receipt">
            <strong>Proposal rejected</strong>
            <p>No operational action was executed.</p>
          </div>
        ) : null}

        {proposal.status === "execution_failed" ? (
          <div className="decision-receipt failed-receipt">
            <strong>Recovery not verified</strong>
            <p>{proposal.failure_reason ?? "The executor did not pass its recovery checks."}</p>
          </div>
        ) : null}
      </div>

      {error ? <p className="proposal-error">{error}</p> : null}
    </section>
  );
}
