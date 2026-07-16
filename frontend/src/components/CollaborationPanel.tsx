import { useEffect, useMemo, useState } from "react";

import type {
  CollaborationDecision,
  CollaborationOutput,
  CollaborationOutputKind,
  MitigationProposal,
} from "../lib/api";
import { formatTimestamp, titleCase } from "../lib/format";
import { AuthorityNote } from "./AuthorityNote";

interface CollaborationPanelProps {
  acting: string | null;
  canDecide: boolean;
  canPrepare: boolean;
  error: string | null;
  loading: boolean;
  outputs: CollaborationOutput[];
  proposal: MitigationProposal | null;
  onDecision: (
    output: CollaborationOutput,
    decision: CollaborationDecision,
    note: string,
  ) => Promise<void>;
  onPrepare: (kinds: CollaborationOutputKind[]) => Promise<void>;
}

const KIND_ORDER: CollaborationOutputKind[] = ["slack_update", "github_issue"];

const KIND_COPY: Record<CollaborationOutputKind, {
  label: string;
  provider: string;
  description: string;
}> = {
  slack_update: {
    label: "Slack incident update",
    provider: "Slack",
    description: "A concise status update grounded in the proposal evidence.",
  },
  github_issue: {
    label: "GitHub follow-up issue",
    provider: "GitHub",
    description: "A durable engineering issue with verification steps and evidence receipts.",
  },
};

function payloadText(output: CollaborationOutput, key: "text" | "title" | "body"): string {
  const value = output.payload[key];
  return typeof value === "string" ? value : "Preview unavailable.";
}

function providerValue(output: CollaborationOutput, key: string): string | null {
  const value = output.delivery?.provider_receipt[key];
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  return null;
}

function safeGitHubIssueUrl(output: CollaborationOutput): string | null {
  const candidate = providerValue(output, "issue_url");
  if (!candidate) return null;
  try {
    const parsed = new URL(candidate);
    return parsed.protocol === "https:" && parsed.hostname === "github.com" ? parsed.href : null;
  } catch {
    return null;
  }
}

function DeliveryReceipt({ output }: { output: CollaborationOutput }) {
  if (output.status === "rejected") {
    return (
      <div className="collaboration-result collaboration-result-rejected">
        <strong>Publication rejected</strong>
        <p>No workflow, outbox message, or provider write was created.</p>
      </div>
    );
  }

  if (output.status === "dead_lettered") {
    return (
      <div className="collaboration-result collaboration-result-failed" role="status">
        <strong>Delivery moved to the dead-letter queue</strong>
        <p>{output.failure_reason ?? "Retries ended without a confirmed provider receipt."}</p>
        <code>{output.delivery?.last_error_code ?? "delivery_failed"}</code>
      </div>
    );
  }

  if (output.status !== "delivered") {
    if (!output.delivery) return null;
    return (
      <div className="collaboration-result collaboration-result-active" role="status">
        <div>
          <span>Durable delivery</span>
          <strong>{titleCase(output.status)}</strong>
        </div>
        <dl>
          <div><dt>Attempts</dt><dd>{output.delivery.attempt_count}</dd></div>
          <div><dt>Last receipt</dt><dd>{formatTimestamp(output.delivery.updated_at)}</dd></div>
          <div><dt>Delivery ID</dt><dd>{output.delivery.idempotency_key.slice(0, 12)}</dd></div>
        </dl>
        {output.delivery.last_error_code ? (
          <p>Retry receipt: <code>{output.delivery.last_error_code}</code></p>
        ) : null}
      </div>
    );
  }

  const issueUrl = safeGitHubIssueUrl(output);
  const reconciled = providerValue(output, "reconciled") === "true";
  return (
    <div className="collaboration-result collaboration-result-delivered" role="status">
      <div>
        <span>Provider receipt</span>
        <strong>Delivered{reconciled ? " · reconciled" : ""}</strong>
      </div>
      <dl>
        <div>
          <dt>Destination</dt>
          <dd>{providerValue(output, output.provider === "slack" ? "channel_id" : "repository") ?? output.destination}</dd>
        </div>
        <div>
          <dt>{output.provider === "slack" ? "Message timestamp" : "Issue number"}</dt>
          <dd>{providerValue(output, output.provider === "slack" ? "message_ts" : "issue_number") ?? "Recorded"}</dd>
        </div>
        <div><dt>Attempts</dt><dd>{output.delivery?.attempt_count ?? 0}</dd></div>
      </dl>
      {issueUrl ? <a href={issueUrl} rel="noreferrer" target="_blank">Open confirmed issue ↗</a> : null}
    </div>
  );
}

function OutputPreview({ output }: { output: CollaborationOutput }) {
  if (output.kind === "slack_update") {
    return <pre className="collaboration-copy collaboration-copy-slack">{payloadText(output, "text")}</pre>;
  }
  return (
    <div className="collaboration-copy collaboration-copy-github">
      <strong>{payloadText(output, "title")}</strong>
      <pre>{payloadText(output, "body")}</pre>
    </div>
  );
}

export function CollaborationPanel({
  acting,
  canDecide,
  canPrepare,
  error,
  loading,
  outputs,
  proposal,
  onDecision,
  onPrepare,
}: CollaborationPanelProps) {
  const [selectedKinds, setSelectedKinds] = useState<CollaborationOutputKind[]>([]);
  const [reviewed, setReviewed] = useState<Record<string, boolean>>({});
  const [notes, setNotes] = useState<Record<string, string>>({});
  const proposalOutputs = useMemo(
    () => proposal
      ? outputs.filter((output) => output.proposal_id === proposal.id)
      : outputs,
    [outputs, proposal],
  );
  const preparedKinds = useMemo(
    () => new Set(proposalOutputs.map((output) => output.kind)),
    [proposalOutputs],
  );
  const orderedOutputs = useMemo(
    () => [...proposalOutputs].sort(
      (left, right) => KIND_ORDER.indexOf(left.kind) - KIND_ORDER.indexOf(right.kind),
    ),
    [proposalOutputs],
  );

  useEffect(() => {
    setSelectedKinds((current) => current.filter((kind) => !preparedKinds.has(kind)));
  }, [preparedKinds]);

  useEffect(() => {
    setReviewed({});
    setNotes({});
    setSelectedKinds([]);
  }, [proposal?.id]);

  function toggleKind(kind: CollaborationOutputKind) {
    setSelectedKinds((current) => current.includes(kind)
      ? current.filter((candidate) => candidate !== kind)
      : [...current, kind]);
  }

  async function decide(output: CollaborationOutput, decision: CollaborationDecision) {
    if (!canDecide) return;
    await onDecision(output, decision, notes[output.id] ?? "");
    setReviewed((current) => ({ ...current, [output.id]: false }));
    setNotes((current) => ({ ...current, [output.id]: "" }));
  }

  const unpreparedKinds = KIND_ORDER.filter((kind) => !preparedKinds.has(kind));
  const prepareBusy = acting === "prepare";

  return (
    <section className="collaboration-panel" aria-labelledby="collaboration-title">
      <header className="section-title-row collaboration-heading">
        <div>
          <p className="utility-label">External collaboration / separate write authority</p>
          <h2 id="collaboration-title">Publish without losing the evidence trail.</h2>
        </div>
        <span className="collaboration-gate-stamp">Independent approval gate</span>
      </header>

      <p className="collaboration-boundary-note">
        Mitigation approval never authorizes a message or issue. PagerAgent first freezes a
        server-built draft and destination; an authorized operator then decides each external write.
      </p>

      {proposal === null ? (
        <div className="collaboration-empty">
          <strong>A grounded proposal is required.</strong>
          <span>Generate the decision packet above before preparing collaboration drafts.</span>
        </div>
      ) : null}

      {proposal && unpreparedKinds.length > 0 ? (
        <div className="collaboration-preparation">
          <div className="collaboration-preparation-copy">
            <span>Step 1 / freeze drafts</span>
            <strong>Choose destinations to prepare</strong>
            <small>This stores content and connector revisions. Nothing is published.</small>
          </div>
          <fieldset>
            <legend>Collaboration outputs</legend>
            {KIND_ORDER.map((kind) => {
              const copy = KIND_COPY[kind];
              const alreadyPrepared = preparedKinds.has(kind);
              return (
                <label className={alreadyPrepared ? "prepared" : ""} key={kind}>
                  <input
                    checked={alreadyPrepared || selectedKinds.includes(kind)}
                    disabled={alreadyPrepared || prepareBusy || !canPrepare}
                    onChange={() => toggleKind(kind)}
                    type="checkbox"
                  />
                  <span>
                    <strong>{copy.label}</strong>
                    <small>{alreadyPrepared ? "Frozen receipt recorded" : copy.description}</small>
                  </span>
                </label>
              );
            })}
          </fieldset>
          <div className="collaboration-prepare-action">
            <button
              disabled={!canPrepare || prepareBusy || selectedKinds.length === 0}
              onClick={() => void onPrepare(selectedKinds)}
              type="button"
            >
              {prepareBusy ? "Freezing drafts…" : "Prepare selected drafts"}
            </button>
            <AuthorityNote
              allowed={canPrepare}
              message="This role can inspect existing publication receipts but cannot prepare new drafts."
              permission="collaboration.prepare"
            />
          </div>
        </div>
      ) : null}

      {loading && proposalOutputs.length === 0 ? (
        <p className="collaboration-message">Reading collaboration receipts…</p>
      ) : null}

      {orderedOutputs.length > 0 ? (
        <div className="collaboration-output-list">
          {orderedOutputs.map((output, index) => {
            const copy = KIND_COPY[output.kind];
            const outputBusy = acting === output.id;
            const canApprove = canDecide && reviewed[output.id] === true && !outputBusy;
            return (
              <article className={`collaboration-output output-${output.status}`} key={output.id}>
                <header>
                  <span className="collaboration-output-index">{String(index + 1).padStart(2, "0")}</span>
                  <div>
                    <p>{copy.provider} / frozen delivery packet</p>
                    <h3>{copy.label}</h3>
                  </div>
                  <span className={`collaboration-status status-${output.status}`}>
                    {titleCase(output.status)}
                  </span>
                </header>

                <dl className="collaboration-receipt-strip">
                  <div><dt>Destination</dt><dd>{output.destination}</dd></div>
                  <div><dt>Connector</dt><dd>v{output.connector_version}</dd></div>
                  <div><dt>Credential</dt><dd>v{output.credential_version}</dd></div>
                  <div><dt>Content receipt</dt><dd>{output.content_sha256.slice(0, 12)}</dd></div>
                </dl>

                <details className="collaboration-preview" open={output.status === "pending_approval"}>
                  <summary>Review exact frozen content</summary>
                  <OutputPreview output={output} />
                </details>

                {output.status === "pending_approval" ? (
                  <div className="collaboration-decision">
                    <div className="collaboration-decision-spine">
                      <span>Step 2 / authorize write</span>
                      <strong>The draft cannot publish itself.</strong>
                      <small>Approval creates the durable workflow and outbox record.</small>
                    </div>
                    <div className="collaboration-decision-form">
                      <AuthorityNote
                        allowed={canDecide}
                        message="This signed role can review the packet but cannot authorize an external write."
                        permission="collaboration.decide"
                      />
                      <label className="review-check">
                        <input
                          checked={reviewed[output.id] ?? false}
                          disabled={!canDecide || outputBusy}
                          onChange={(event) => setReviewed((current) => ({
                            ...current,
                            [output.id]: event.target.checked,
                          }))}
                          type="checkbox"
                        />
                        I reviewed the frozen {copy.label.toLowerCase()} and destination.
                      </label>
                      <label>
                        Decision note for {copy.label}
                        <textarea
                          disabled={!canDecide || outputBusy}
                          onChange={(event) => setNotes((current) => ({
                            ...current,
                            [output.id]: event.target.value,
                          }))}
                          placeholder="What did you verify before publishing?"
                          value={notes[output.id] ?? ""}
                        />
                      </label>
                      <div className="decision-buttons">
                        <button
                          className="approve-button"
                          disabled={!canApprove}
                          onClick={() => void decide(output, "approve")}
                          type="button"
                        >
                          {outputBusy ? "Recording decision…" : `Approve ${copy.provider} write`}
                        </button>
                        <button
                          className="reject-button"
                          disabled={!canDecide || outputBusy}
                          onClick={() => void decide(output, "reject")}
                          type="button"
                        >
                          Reject publication
                        </button>
                      </div>
                    </div>
                  </div>
                ) : null}

                <DeliveryReceipt output={output} />
              </article>
            );
          })}
        </div>
      ) : null}

      {error ? <p className="collaboration-error" role="alert">{error}</p> : null}
    </section>
  );
}
