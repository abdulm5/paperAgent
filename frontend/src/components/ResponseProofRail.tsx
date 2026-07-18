import type {
  IncidentDetail,
  InvestigationDetail,
  MitigationProposal,
  PostmortemDetail,
} from "../lib/api";

type ProofState = "complete" | "active" | "waiting";

interface ResponseProofRailProps {
  incident: IncidentDetail | null;
  investigation: InvestigationDetail | null;
  postmortem: PostmortemDetail | null;
  proposal: MitigationProposal | null;
}

interface ProofStep {
  detail: string;
  done: boolean;
  label: string;
  title: string;
}

function titleCase(value: string): string {
  return value.replaceAll("_", " ").replace(/\b\w/g, (character) => character.toUpperCase());
}

function decisionComplete(proposal: MitigationProposal | null): boolean {
  if (!proposal) return false;
  return proposal.status === "advisory" || proposal.decisions.length > 0;
}

function decisionDetail(proposal: MitigationProposal | null): string {
  if (!proposal) return "No grounded packet yet";
  if (proposal.status === "advisory") return "Write authority withheld";
  const decision = proposal.decisions.at(-1)?.decision;
  if (decision === "approve") return "Human approval recorded";
  if (decision === "reject") return "Rejected; no write queued";
  return "Approval required";
}

function recoveryComplete(
  incident: IncidentDetail | null,
  proposal: MitigationProposal | null,
): boolean {
  return Boolean(
    proposal?.execution?.recovery_verified
      || incident?.status === "mitigated"
      || incident?.status === "resolved",
  );
}

function recoveryDetail(
  incident: IncidentDetail | null,
  proposal: MitigationProposal | null,
): string {
  if (proposal?.execution?.recovery_verified) return "Canary telemetry passed";
  if (proposal?.status === "execution_failed") return "Execution failed closed";
  if (incident?.status === "mitigated" || incident?.status === "resolved") {
    return `${titleCase(incident.status)} state recorded`;
  }
  return "No side effect authorized";
}

function proofSteps({
  incident,
  investigation,
  postmortem,
  proposal,
}: ResponseProofRailProps): ProofStep[] {
  const metric = incident?.alert.metric;
  const signalDetail = metric
    ? `${(metric.value * 100).toFixed(1)}% observed / ${(metric.threshold * 100).toFixed(1)}% limit`
    : "Awaiting a threshold receipt";
  const evidenceDetail = investigation?.status === "completed"
    ? `${investigation.evidence.length} immutable artifact${investigation.evidence.length === 1 ? "" : "s"}`
    : investigation?.status === "running"
      ? "Collectors are in flight"
      : investigation?.status === "failed"
        ? "Investigation failed closed"
        : "No evidence snapshot yet";
  const learningDetail = postmortem
    ? postmortem.status === "final"
      ? `Finalized at revision ${postmortem.version}`
      : `${postmortem.revisions.length} immutable revision${postmortem.revisions.length === 1 ? "" : "s"}`
    : "Opens after resolution";

  return [
    {
      label: "Detect",
      title: "Signal accepted",
      detail: signalDetail,
      done: incident !== null,
    },
    {
      label: "Ground",
      title: "Evidence ranked",
      detail: evidenceDetail,
      done: investigation?.status === "completed",
    },
    {
      label: "Decide",
      title: "Human authority",
      detail: decisionDetail(proposal),
      done: decisionComplete(proposal),
    },
    {
      label: "Recover",
      title: "Change verified",
      detail: recoveryDetail(incident, proposal),
      done: recoveryComplete(incident, proposal),
    },
    {
      label: "Learn",
      title: "Case file retained",
      detail: learningDetail,
      done: postmortem !== null,
    },
  ];
}

export function ResponseProofRail(props: ResponseProofRailProps) {
  const steps = proofSteps(props);
  const firstIncomplete = steps.findIndex((step) => !step.done);

  return (
    <section className="response-proof" aria-labelledby="response-proof-title">
      <header className="response-proof-header">
        <div>
          <p className="utility-label">Chain of custody / selected incident</p>
          <h2 id="response-proof-title">Every stage leaves a receipt.</h2>
        </div>
        <span className="response-proof-reference">
          {props.incident ? `case ${props.incident.id.slice(0, 8)}` : "ledger standing by"}
        </span>
      </header>
      <div className="response-proof-scroll">
        <ol className="response-proof-list">
          {steps.map((step, index) => {
            const state: ProofState = step.done
              ? "complete"
              : index === firstIncomplete
                ? "active"
                : "waiting";
            return (
              <li
                aria-label={`${step.label}: ${state}`}
                className={`response-proof-step proof-${state}`}
                data-state={state}
                key={step.label}
              >
                <div className="proof-step-mark" aria-hidden="true">
                  <span>{String(index + 1).padStart(2, "0")}</span>
                  <i>{state === "complete" ? "✓" : state === "active" ? "•" : "—"}</i>
                </div>
                <p>{step.label}</p>
                <h3>{step.title}</h3>
                <span>{step.detail}</span>
              </li>
            );
          })}
        </ol>
      </div>
    </section>
  );
}
