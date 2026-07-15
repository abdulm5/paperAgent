import type { IncidentSummary } from "../lib/api";
import { formatTimestamp, titleCase } from "../lib/format";

interface IncidentQueueProps {
  incidents: IncidentSummary[];
  selectedId: string | null;
  loading: boolean;
  onSelect: (incidentId: string) => void;
}

export function IncidentQueue({
  incidents,
  selectedId,
  loading,
  onSelect,
}: IncidentQueueProps) {
  return (
    <aside className="incident-queue" aria-labelledby="incident-queue-title">
      <div className="panel-heading">
        <div>
          <p className="utility-label">Incident queue</p>
          <h2 id="incident-queue-title">Latest signals</h2>
        </div>
        <span className="queue-count" aria-label={`${incidents.length} incidents`}>
          {String(incidents.length).padStart(2, "0")}
        </span>
      </div>

      {loading && incidents.length === 0 ? (
        <div className="queue-message">Reading the incident ledger…</div>
      ) : null}

      {!loading && incidents.length === 0 ? (
        <div className="queue-message empty-queue">
          <p>No incidents recorded.</p>
          <code>./scripts/run-demo.sh</code>
          <span>Run the outage scenario to create the first entry.</span>
        </div>
      ) : null}

      <div className="queue-list">
        {incidents.map((incident) => (
          <button
            aria-current={selectedId === incident.id ? "true" : undefined}
            className={`queue-item${selectedId === incident.id ? " selected" : ""}`}
            key={incident.id}
            onClick={() => onSelect(incident.id)}
            type="button"
          >
            <span className={`severity-mark ${incident.severity}`} aria-hidden="true" />
            <span className="queue-item-copy">
              <span className="queue-item-topline">
                <strong>{incident.service}</strong>
                <time dateTime={incident.detected_at}>{formatTimestamp(incident.detected_at)}</time>
              </span>
              <span className="queue-summary">{incident.summary}</span>
              <span className={`status-label status-${incident.status}`}>
                {titleCase(incident.status)}
              </span>
            </span>
          </button>
        ))}
      </div>
    </aside>
  );
}
