import { useCallback, useEffect, useState } from "react";

import { IncidentDetailPanel } from "./components/IncidentDetailPanel";
import { IncidentQueue } from "./components/IncidentQueue";
import { EvaluationPanel } from "./components/EvaluationPanel";
import {
  decideProposal,
  finalizePostmortem,
  generateProposal,
  generatePostmortem,
  getIncident,
  getIncidents,
  getEvaluationScorecard,
  getLatestInvestigation,
  getLatestProposal,
  getPostmortem,
  runInvestigation,
  transitionIncident,
  updatePostmortem,
  type IncidentDetail,
  type IncidentStatus,
  type IncidentSummary,
  type EvaluationScorecard,
  type InvestigationDetail,
  type MitigationProposal,
  type PostmortemDetail,
  type PostmortemEditPayload,
  type ProposalDecision,
} from "./lib/api";

export default function App() {
  const [incidents, setIncidents] = useState<IncidentSummary[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<IncidentDetail | null>(null);
  const [loadingQueue, setLoadingQueue] = useState(true);
  const [loadingDetail, setLoadingDetail] = useState(false);
  const [connectionError, setConnectionError] = useState<string | null>(null);
  const [scorecard, setScorecard] = useState<EvaluationScorecard | null>(null);
  const [evaluationLoading, setEvaluationLoading] = useState(true);
  const [evaluationError, setEvaluationError] = useState<string | null>(null);
  const [transitionError, setTransitionError] = useState<string | null>(null);
  const [transitioning, setTransitioning] = useState(false);
  const [investigation, setInvestigation] = useState<InvestigationDetail | null>(null);
  const [investigationLoading, setInvestigationLoading] = useState(false);
  const [investigationRunning, setInvestigationRunning] = useState(false);
  const [investigationError, setInvestigationError] = useState<string | null>(null);
  const [proposal, setProposal] = useState<MitigationProposal | null>(null);
  const [proposalLoading, setProposalLoading] = useState(false);
  const [proposalActing, setProposalActing] = useState(false);
  const [proposalError, setProposalError] = useState<string | null>(null);
  const [postmortem, setPostmortem] = useState<PostmortemDetail | null>(null);
  const [postmortemLoading, setPostmortemLoading] = useState(false);
  const [postmortemActing, setPostmortemActing] = useState(false);
  const [postmortemError, setPostmortemError] = useState<string | null>(null);

  const loadQueue = useCallback(async () => {
    try {
      const nextIncidents = await getIncidents();
      setIncidents(nextIncidents);
      setSelectedId((current) => {
        if (current && nextIncidents.some((incident) => incident.id === current)) return current;
        return nextIncidents[0]?.id ?? null;
      });
      setConnectionError(null);
    } catch (error) {
      setConnectionError(error instanceof Error ? error.message : "PagerAgent API is unavailable.");
    } finally {
      setLoadingQueue(false);
    }
  }, []);

  const loadEvaluation = useCallback(async () => {
    setEvaluationLoading(true);
    try {
      setScorecard(await getEvaluationScorecard());
      setEvaluationError(null);
    } catch (error) {
      setEvaluationError(
        error instanceof Error ? error.message : "Evaluation suite is unavailable.",
      );
    } finally {
      setEvaluationLoading(false);
    }
  }, []);

  const loadDetail = useCallback(async (incidentId: string) => {
    setLoadingDetail(true);
    try {
      setDetail(await getIncident(incidentId));
      setConnectionError(null);
    } catch (error) {
      setConnectionError(error instanceof Error ? error.message : "Incident evidence is unavailable.");
    } finally {
      setLoadingDetail(false);
    }
  }, []);

  const loadInvestigation = useCallback(async (incidentId: string) => {
    setInvestigationLoading(true);
    try {
      setInvestigation(await getLatestInvestigation(incidentId));
      setInvestigationError(null);
    } catch (error) {
      setInvestigationError(
        error instanceof Error ? error.message : "Investigation evidence is unavailable.",
      );
    } finally {
      setInvestigationLoading(false);
    }
  }, []);

  const loadProposal = useCallback(async (incidentId: string) => {
    setProposalLoading(true);
    try {
      setProposal(await getLatestProposal(incidentId));
      setProposalError(null);
    } catch (error) {
      setProposalError(error instanceof Error ? error.message : "Copilot brief is unavailable.");
    } finally {
      setProposalLoading(false);
    }
  }, []);

  const loadPostmortem = useCallback(async (incidentId: string) => {
    setPostmortemLoading(true);
    try {
      setPostmortem(await getPostmortem(incidentId));
      setPostmortemError(null);
    } catch (error) {
      setPostmortemError(error instanceof Error ? error.message : "Postmortem is unavailable.");
    } finally {
      setPostmortemLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadQueue();
    void loadEvaluation();
    const refreshTimer = window.setInterval(() => void loadQueue(), 5_000);
    return () => window.clearInterval(refreshTimer);
  }, [loadEvaluation, loadQueue]);

  useEffect(() => {
    if (selectedId) {
      setInvestigation(null);
      setProposal(null);
      setPostmortem(null);
      void loadDetail(selectedId);
      void loadInvestigation(selectedId);
      void loadProposal(selectedId);
      void loadPostmortem(selectedId);
    } else {
      setDetail(null);
      setInvestigation(null);
      setProposal(null);
      setPostmortem(null);
    }
  }, [loadDetail, loadInvestigation, loadPostmortem, loadProposal, selectedId]);

  useEffect(() => {
    if (!selectedId) return;
    const investigationTimer = window.setInterval(
      () => {
        void loadInvestigation(selectedId);
        void loadProposal(selectedId);
        void loadPostmortem(selectedId);
      },
      5_000,
    );
    return () => window.clearInterval(investigationTimer);
  }, [loadInvestigation, loadPostmortem, loadProposal, selectedId]);

  async function handleTransition(toStatus: IncidentStatus, note: string) {
    if (!detail) return false;
    setTransitioning(true);
    setTransitionError(null);
    try {
      const updated = await transitionIncident(detail.id, toStatus, detail.version, note);
      setDetail(updated);
      await loadQueue();
      if (toStatus === "resolved") await loadPostmortem(detail.id);
      return true;
    } catch (error) {
      setTransitionError(error instanceof Error ? error.message : "Status change failed.");
      await loadDetail(detail.id);
      return false;
    } finally {
      setTransitioning(false);
    }
  }

  async function handleRunInvestigation() {
    if (!selectedId) return;
    setInvestigationRunning(true);
    setInvestigationError(null);
    try {
      setInvestigation(await runInvestigation(selectedId));
      await loadDetail(selectedId);
    } catch (error) {
      setInvestigationError(error instanceof Error ? error.message : "Investigation failed.");
    } finally {
      setInvestigationRunning(false);
    }
  }

  async function handleGenerateProposal() {
    if (!selectedId) return;
    setProposalActing(true);
    setProposalError(null);
    try {
      setProposal(await generateProposal(selectedId));
      await loadDetail(selectedId);
    } catch (error) {
      setProposalError(error instanceof Error ? error.message : "Brief generation failed.");
    } finally {
      setProposalActing(false);
    }
  }

  async function handleProposalDecision(decision: ProposalDecision, note: string) {
    if (!proposal || !selectedId) return;
    setProposalActing(true);
    setProposalError(null);
    try {
      setProposal(await decideProposal(proposal.id, decision, note));
      await Promise.all([loadDetail(selectedId), loadQueue()]);
    } catch (error) {
      setProposalError(error instanceof Error ? error.message : "Decision could not be recorded.");
      await loadProposal(selectedId);
    } finally {
      setProposalActing(false);
    }
  }

  async function handleGeneratePostmortem() {
    if (!selectedId) return;
    setPostmortemActing(true);
    setPostmortemError(null);
    try {
      setPostmortem(await generatePostmortem(selectedId));
      await loadDetail(selectedId);
    } catch (error) {
      setPostmortemError(error instanceof Error ? error.message : "Generation failed.");
    } finally {
      setPostmortemActing(false);
    }
  }

  async function handleSavePostmortem(edit: PostmortemEditPayload) {
    if (!postmortem || !selectedId) return;
    setPostmortemActing(true);
    setPostmortemError(null);
    try {
      setPostmortem(await updatePostmortem(postmortem, edit));
      await loadDetail(selectedId);
    } catch (error) {
      setPostmortemError(error instanceof Error ? error.message : "Draft could not be saved.");
      await loadPostmortem(selectedId);
    } finally {
      setPostmortemActing(false);
    }
  }

  async function handleFinalizePostmortem(note: string) {
    if (!postmortem || !selectedId) return;
    setPostmortemActing(true);
    setPostmortemError(null);
    try {
      setPostmortem(await finalizePostmortem(postmortem, note));
      await loadDetail(selectedId);
    } catch (error) {
      setPostmortemError(error instanceof Error ? error.message : "Finalization failed.");
      await loadPostmortem(selectedId);
    } finally {
      setPostmortemActing(false);
    }
  }

  const activeCount = incidents.filter((incident) => incident.status !== "resolved").length;

  return (
    <main className="app-shell">
      <header className="system-header">
        <span className="brand-mark">PagerAgent / incident ledger</span>
        <span className={connectionError ? "connection-state offline" : "connection-state"}>
          {connectionError ? "API unavailable" : "Live record"}
        </span>
      </header>

      <section className="command-header" aria-labelledby="page-title">
        <div>
          <p className="eyebrow">Incident command</p>
          <h1 id="page-title">What needs attention now.</h1>
        </div>
        <div className="active-counter">
          <strong>{String(activeCount).padStart(2, "0")}</strong>
          <span>active incidents</span>
        </div>
      </section>

      <EvaluationPanel
        error={evaluationError}
        loading={evaluationLoading}
        onRefresh={loadEvaluation}
        scorecard={scorecard}
      />

      {connectionError ? (
        <div className="connection-error" role="alert">
          <span>{connectionError}</span>
          <button onClick={() => void loadQueue()} type="button">
            Retry connection
          </button>
        </div>
      ) : null}

      <div className="control-room">
        <IncidentQueue
          incidents={incidents}
          loading={loadingQueue}
          onSelect={setSelectedId}
          selectedId={selectedId}
        />
        <IncidentDetailPanel
          incident={detail}
          loading={loadingDetail}
          investigation={investigation}
          investigationError={investigationError}
          investigationLoading={investigationLoading}
          investigationRunning={investigationRunning}
          proposal={proposal}
          proposalActing={proposalActing}
          proposalError={proposalError}
          proposalLoading={proposalLoading}
          postmortem={postmortem}
          postmortemActing={postmortemActing}
          postmortemError={postmortemError}
          postmortemLoading={postmortemLoading}
          onGeneratePostmortem={handleGeneratePostmortem}
          onSavePostmortem={handleSavePostmortem}
          onFinalizePostmortem={handleFinalizePostmortem}
          onGenerateProposal={handleGenerateProposal}
          onProposalDecision={handleProposalDecision}
          onTransition={handleTransition}
          onRunInvestigation={handleRunInvestigation}
          transitionError={transitionError}
          transitioning={transitioning}
        />
      </div>
    </main>
  );
}
