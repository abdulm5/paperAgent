import { useCallback, useEffect, useState } from "react";

import { IncidentDetailPanel } from "./components/IncidentDetailPanel";
import { IncidentQueue } from "./components/IncidentQueue";
import {
  getIncident,
  getIncidents,
  getLatestInvestigation,
  runInvestigation,
  transitionIncident,
  type IncidentDetail,
  type IncidentStatus,
  type IncidentSummary,
  type InvestigationDetail,
} from "./lib/api";

export default function App() {
  const [incidents, setIncidents] = useState<IncidentSummary[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<IncidentDetail | null>(null);
  const [loadingQueue, setLoadingQueue] = useState(true);
  const [loadingDetail, setLoadingDetail] = useState(false);
  const [connectionError, setConnectionError] = useState<string | null>(null);
  const [transitionError, setTransitionError] = useState<string | null>(null);
  const [transitioning, setTransitioning] = useState(false);
  const [investigation, setInvestigation] = useState<InvestigationDetail | null>(null);
  const [investigationLoading, setInvestigationLoading] = useState(false);
  const [investigationRunning, setInvestigationRunning] = useState(false);
  const [investigationError, setInvestigationError] = useState<string | null>(null);

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

  useEffect(() => {
    void loadQueue();
    const refreshTimer = window.setInterval(() => void loadQueue(), 5_000);
    return () => window.clearInterval(refreshTimer);
  }, [loadQueue]);

  useEffect(() => {
    if (selectedId) {
      setInvestigation(null);
      void loadDetail(selectedId);
      void loadInvestigation(selectedId);
    } else {
      setDetail(null);
      setInvestigation(null);
    }
  }, [loadDetail, loadInvestigation, selectedId]);

  useEffect(() => {
    if (!selectedId) return;
    const investigationTimer = window.setInterval(
      () => void loadInvestigation(selectedId),
      5_000,
    );
    return () => window.clearInterval(investigationTimer);
  }, [loadInvestigation, selectedId]);

  async function handleTransition(toStatus: IncidentStatus, note: string) {
    if (!detail) return false;
    setTransitioning(true);
    setTransitionError(null);
    try {
      const updated = await transitionIncident(detail.id, toStatus, detail.version, note);
      setDetail(updated);
      await loadQueue();
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
          onTransition={handleTransition}
          onRunInvestigation={handleRunInvestigation}
          transitionError={transitionError}
          transitioning={transitioning}
        />
      </div>
    </main>
  );
}
