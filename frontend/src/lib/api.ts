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

export type InvestigationStatus = "running" | "completed" | "failed";

export interface EvidenceArtifact {
  id: string;
  kind: string;
  source_uri: string;
  content_hash: string;
  payload: Record<string, unknown>;
  collected_at: string;
}

export interface ErrorCluster {
  id: string;
  signature: string;
  error_type: string;
  endpoint: string;
  affected_attributes: Record<string, unknown>;
  failure_count: number;
  first_seen_at: string;
  last_seen_at: string;
  sample_request_ids: string[];
  evidence_ids: string[];
}

export interface CommitCandidate {
  id: string;
  commit_sha: string;
  rank: number;
  total_score: number;
  title: string;
  author: string;
  committed_at: string;
  files_changed: string[];
  diff_summary: string;
  feature_scores: Record<string, number>;
  explanation: string[];
  evidence_ids: string[];
}

export interface RunbookMatch {
  id: string;
  runbook_id: string;
  rank: number;
  title: string;
  service: string;
  failure_mode: string;
  total_score: number;
  score_breakdown: Record<string, number>;
  matched_sections: Array<{ heading: string; excerpt: string }>;
  content_hash: string;
  evidence_ids: string[];
}

export interface InvestigationDetail {
  id: string;
  incident_id: string;
  status: InvestigationStatus;
  collector_version: string;
  clusterer_version: string;
  ranker_version: string;
  retrieval_version: string;
  input_hash: string;
  failure_reason: string | null;
  started_at: string;
  completed_at: string | null;
  evidence: EvidenceArtifact[];
  error_clusters: ErrorCluster[];
  commit_candidates: CommitCandidate[];
  runbook_matches: RunbookMatch[];
}

export class ApiError extends Error {
  status: number;

  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...init?.headers },
    ...init,
  });
  if (!response.ok) {
    const body = (await response.json().catch(() => null)) as { detail?: string } | null;
    throw new ApiError(body?.detail ?? `Request failed with status ${response.status}`, response.status);
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

export async function getLatestInvestigation(
  incidentId: string,
): Promise<InvestigationDetail | null> {
  try {
    return await request<InvestigationDetail>(
      `/api/v1/incidents/${incidentId}/investigations/latest`,
    );
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) return null;
    throw error;
  }
}

export function runInvestigation(incidentId: string): Promise<InvestigationDetail> {
  return request<InvestigationDetail>(`/api/v1/incidents/${incidentId}/investigations`, {
    method: "POST",
  });
}
