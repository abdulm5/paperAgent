export type IncidentStatus = "detected" | "investigating" | "mitigated" | "resolved";
export type AlertSeverity = "critical" | "high" | "medium" | "low";

export interface IncidentSummary {
  id: string;
  status: IncidentStatus;
  service: string;
  severity: AlertSeverity;
  summary: string;
  started_at: string;
  detected_at: string;
  received_at: string;
  updated_at: string;
  resolved_at: string | null;
  version: number;
}

export interface AlertPayload {
  fingerprint: string;
  source: string;
  service: string;
  severity: AlertSeverity;
  summary: string;
  started_at: string;
  detected_at: string;
  metric: {
    name: string;
    value: number;
    threshold: number;
    window_seconds: number;
    request_count: number;
    failed_request_count: number;
  };
  release: {
    name: string;
    commit_sha: string;
    deployed_at: string;
  };
  telemetry_url: string;
}

export interface IncidentEvent {
  id: string;
  event_type: string;
  actor: string;
  from_status: IncidentStatus | null;
  to_status: IncidentStatus | null;
  note: string | null;
  payload: Record<string, unknown>;
  created_at: string;
}

export interface IncidentDetail extends IncidentSummary {
  alert: AlertPayload;
  alert_count: number;
  events: IncidentEvent[];
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...init?.headers },
    ...init,
  });
  if (!response.ok) {
    const body = (await response.json().catch(() => null)) as { detail?: string } | null;
    throw new Error(body?.detail ?? `Request failed with status ${response.status}`);
  }
  return response.json() as Promise<T>;
}

export function getIncidents(): Promise<IncidentSummary[]> {
  return request<IncidentSummary[]>("/api/v1/incidents");
}

export function getIncident(incidentId: string): Promise<IncidentDetail> {
  return request<IncidentDetail>(`/api/v1/incidents/${incidentId}`);
}

export function transitionIncident(
  incidentId: string,
  toStatus: IncidentStatus,
  expectedVersion: number,
  note: string,
): Promise<IncidentDetail> {
  return request<IncidentDetail>(`/api/v1/incidents/${incidentId}/transitions`, {
    method: "POST",
    body: JSON.stringify({
      to_status: toStatus,
      expected_version: expectedVersion,
      actor: "demo-operator",
      note: note || null,
    }),
  });
}
