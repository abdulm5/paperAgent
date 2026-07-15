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

export type CausalKind =
  | "code_change"
  | "configuration_change"
  | "upstream_dependency"
  | "unknown";

export interface CauseCandidate {
  id: string | null;
  kind: CausalKind;
  reference: string;
  title: string;
  rank: number;
  score: number;
  explanation: string[];
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
  cause_candidates: CauseCandidate[];
  commit_candidates: CommitCandidate[];
  runbook_matches: RunbookMatch[];
}

export type ProposalStatus =
  | "pending_approval"
  | "advisory"
  | "rejected"
  | "approved"
  | "executing"
  | "verification_passed"
  | "execution_failed";

export type ProposalDecision = "approve" | "reject";

export interface GroundedClaim {
  kind: "root_cause" | "impact" | "recommendation" | "risk";
  text: string;
  evidence_ids: string[];
}

export interface ProposalDecisionDetail {
  id: string;
  decision: ProposalDecision;
  actor: string;
  note: string | null;
  created_at: string;
}

export interface MitigationExecution {
  id: string;
  status: string;
  executor_version: string;
  idempotency_key: string;
  request_payload: Record<string, unknown>;
  response_payload: Record<string, unknown>;
  before_telemetry: Record<string, unknown>;
  after_telemetry: Record<string, unknown>;
  recovery_verified: boolean;
  failure_reason: string | null;
  started_at: string;
  completed_at: string | null;
}

export interface MitigationProposal {
  id: string;
  incident_id: string;
  investigation_id: string;
  status: ProposalStatus;
  synthesizer_version: string;
  model_name: string;
  prompt_version: string;
  input_hash: string;
  root_cause_summary: string;
  confidence: number;
  impact_summary: string;
  recommended_action: string;
  risk_summary: string;
  verification_steps: string[];
  slack_update: string;
  claims: GroundedClaim[];
  action: {
    action_type: "rollback_service" | "disable_feature_flag" | "escalate_only";
    target_service: "checkout-api";
    target_release: string | null;
    expected_faulty_commit: string | null;
    feature_flag: string | null;
    automation_allowed: boolean;
  };
  failure_reason: string | null;
  created_at: string;
  decided_at: string | null;
  decisions: ProposalDecisionDetail[];
  execution: MitigationExecution | null;
}

export type PostmortemStatus = "draft" | "final";
export type PreventionPriority = "P0" | "P1" | "P2" | "P3";

export interface GroundedPostmortemSection {
  text: string;
  evidence_ids: string[];
}

export interface PostmortemObservation extends GroundedPostmortemSection {}

export interface PreventionItem {
  title: string;
  description: string;
  owner: string;
  priority: PreventionPriority;
  status: string;
  evidence_ids: string[];
}

export interface PostmortemTimelineEntry {
  occurred_at: string;
  event_type: string;
  actor: string;
  description: string;
  evidence_ids: string[];
}

export interface PostmortemContent {
  title: string;
  summary: GroundedPostmortemSection;
  root_cause: GroundedPostmortemSection;
  customer_impact: GroundedPostmortemSection;
  detection: GroundedPostmortemSection;
  resolution: GroundedPostmortemSection;
  what_went_well: PostmortemObservation[];
  what_went_poorly: PostmortemObservation[];
  prevention_items: PreventionItem[];
  timeline: PostmortemTimelineEntry[];
}

export interface PostmortemRevision {
  id: string;
  version: number;
  source: string;
  editor: string;
  change_note: string;
  created_at: string;
}

export interface PostmortemDetail {
  id: string;
  incident_id: string;
  status: PostmortemStatus;
  version: number;
  generator_version: string;
  model_name: string;
  prompt_version: string;
  input_hash: string;
  content: PostmortemContent;
  created_at: string;
  updated_at: string;
  finalized_at: string | null;
  finalized_by: string | null;
  revisions: PostmortemRevision[];
}

export interface PostmortemEditPayload {
  change_note: string;
  title: string;
  summary: string;
  root_cause: string;
  customer_impact: string;
  detection: string;
  resolution: string;
  what_went_well: string[];
  what_went_poorly: string[];
  prevention_items: Array<{
    title: string;
    description: string;
    owner: string;
    priority: PreventionPriority;
    status: string;
  }>;
}

export interface AdversarialProbeResult {
  case: string;
  passed: boolean;
  observation: string;
}

export interface ScenarioEvaluationResult {
  scenario_id: string;
  title: string;
  passed: boolean;
  predicted_cause: CauseCandidate;
  predicted_runbook: string | null;
  predicted_action: Record<string, string | boolean | null>;
  metrics: Record<string, number>;
  adversarial_probes: AdversarialProbeResult[];
  duration_ms: number;
}

export interface EvaluationGate {
  metric: string;
  value: number;
  threshold: number;
  passed: boolean;
}

export interface EvaluationScorecard {
  schema_version: "1.0";
  suite_version: string;
  generated_at: string;
  passed: boolean;
  scenario_count: number;
  aggregate_metrics: Record<string, number>;
  gates: EvaluationGate[];
  scenarios: ScenarioEvaluationResult[];
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

export function getEvaluationScorecard(): Promise<EvaluationScorecard> {
  return request<EvaluationScorecard>("/api/v1/evaluations/scorecard");
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

export async function getLatestProposal(
  incidentId: string,
): Promise<MitigationProposal | null> {
  try {
    return await request<MitigationProposal>(
      `/api/v1/incidents/${incidentId}/proposals/latest`,
    );
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) return null;
    throw error;
  }
}

export function generateProposal(incidentId: string): Promise<MitigationProposal> {
  return request<MitigationProposal>(`/api/v1/incidents/${incidentId}/proposals`, {
    method: "POST",
  });
}

export function decideProposal(
  proposalId: string,
  decision: ProposalDecision,
  note: string,
): Promise<MitigationProposal> {
  return request<MitigationProposal>(`/api/v1/proposals/${proposalId}/decisions`, {
    method: "POST",
    body: JSON.stringify({
      decision,
      actor: "demo-operator",
      note: note || null,
    }),
  });
}

export async function getPostmortem(
  incidentId: string,
): Promise<PostmortemDetail | null> {
  try {
    return await request<PostmortemDetail>(`/api/v1/incidents/${incidentId}/postmortem`);
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) return null;
    throw error;
  }
}

export function generatePostmortem(incidentId: string): Promise<PostmortemDetail> {
  return request<PostmortemDetail>(`/api/v1/incidents/${incidentId}/postmortem`, {
    method: "POST",
  });
}

export function updatePostmortem(
  postmortem: PostmortemDetail,
  edit: PostmortemEditPayload,
): Promise<PostmortemDetail> {
  return request<PostmortemDetail>(`/api/v1/postmortems/${postmortem.id}`, {
    method: "PUT",
    body: JSON.stringify({
      ...edit,
      expected_version: postmortem.version,
      actor: "demo-incident-commander",
    }),
  });
}

export function finalizePostmortem(
  postmortem: PostmortemDetail,
  note: string,
): Promise<PostmortemDetail> {
  return request<PostmortemDetail>(`/api/v1/postmortems/${postmortem.id}/finalize`, {
    method: "POST",
    body: JSON.stringify({
      expected_version: postmortem.version,
      actor: "demo-incident-commander",
      note: note || null,
    }),
  });
}

export function postmortemExportUrl(postmortemId: string): string {
  return `/api/v1/postmortems/${postmortemId}/export`;
}
