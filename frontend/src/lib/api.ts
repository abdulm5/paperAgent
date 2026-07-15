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

export type WorkflowStatus =
  | "queued"
  | "running"
  | "retry_scheduled"
  | "completed"
  | "dead_lettered";

export interface WorkflowDelivery {
  id: string;
  workflow_job_id: string;
  topic: string;
  payload: Record<string, unknown>;
  dispatch_attempt: number;
  available_at: string;
  published_at: string | null;
  publish_attempts: number;
  stream_message_id: string | null;
  last_error: string | null;
  created_at: string;
  updated_at: string;
}

export interface WorkflowJob {
  id: string;
  workflow_run_id: string;
  step_type: string;
  status: WorkflowStatus;
  payload: Record<string, unknown>;
  result: Record<string, unknown>;
  idempotency_key: string;
  attempt_count: number;
  max_attempts: number;
  available_at: string;
  lease_owner: string | null;
  lease_expires_at: string | null;
  last_error: string | null;
  created_at: string;
  updated_at: string;
  started_at: string | null;
  completed_at: string | null;
  deliveries: WorkflowDelivery[];
}

export interface WorkflowRunEvent {
  id: number;
  workflow_run_id: string;
  workflow_job_id: string | null;
  sequence: number;
  event_type: string;
  payload: Record<string, unknown>;
  created_at: string;
}

export interface WorkflowRun {
  id: string;
  incident_id: string;
  workflow_type: string;
  status: WorkflowStatus;
  current_step: string | null;
  dedupe_key: string;
  trace_id: string | null;
  version: number;
  failure_reason: string | null;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
  jobs: WorkflowJob[];
  events: WorkflowRunEvent[];
}

export interface WorkflowStreamEvent {
  id: number;
  workflow_id: string;
  incident_id: string;
  sequence: number;
  event_type: string;
  payload: Record<string, unknown>;
  created_at: string;
  workflow: WorkflowRun;
}

export type WorkflowStreamStatus =
  | "connecting"
  | "live"
  | "reconnecting"
  | "unsupported";

export type Permission =
  | "incidents.read"
  | "incidents.transition"
  | "incidents.resolve"
  | "investigations.run"
  | "proposals.generate"
  | "mitigations.decide"
  | "postmortems.generate"
  | "postmortems.edit"
  | "postmortems.finalize"
  | "evaluations.run"
  | "organization.reset"
  | "connectors.read"
  | "connectors.manage"
  | "connectors.validate";

export type ConnectorProvider = "github" | "prometheus" | "slack";
export type ConnectorStatus = "configured" | "disabled" | "invalid";

export interface ConnectorSummary {
  id: string;
  name: string;
  provider: ConnectorProvider;
  status: ConnectorStatus;
  enabled: boolean;
  version: number;
  last_validated_at: string | null;
  last_validation_message: string | null;
  updated_at: string;
}

export interface ConnectorDetail extends ConnectorSummary {
  configuration: Record<string, unknown>;
  credential_fields: string[];
  credential_version: number;
  created_at: string;
}

export interface ConnectorEvent {
  id: string;
  event_type: string;
  actor: string;
  connector_version: number;
  payload: Record<string, unknown>;
  created_at: string;
}

export interface ConnectorCreatePayload {
  name: string;
  provider: ConnectorProvider;
  configuration: Record<string, string>;
  credentials: Record<string, string>;
}

export interface ConnectorUpdatePayload {
  expected_version: number;
  name?: string;
  configuration?: Record<string, string>;
  enabled?: boolean;
}

export interface AuthUser {
  id: string;
  email: string;
  display_name: string;
}

export interface OrganizationSummary {
  id: string;
  slug: string;
  name: string;
}

export interface OrganizationMembership {
  organization: OrganizationSummary;
  role: string;
}

export interface ActiveOrganization extends OrganizationSummary {
  role: string;
}

export interface AuthSession {
  user: AuthUser;
  active_organization: ActiveOrganization;
  memberships: OrganizationMembership[];
  permissions: Permission[];
  csrf_token: string;
}

export interface DevPersona {
  slug: string;
  email: string;
  display_name: string;
  role: string;
}

interface AuthSessionEnvelope {
  session: AuthSession;
  access_token: string;
}

export class ApiError extends Error {
  status: number;
  code: string | null;

  constructor(message: string, status: number, code: string | null = null) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

let csrfToken: string | null = null;
let unauthorizedHandler: (() => void) | null = null;
let forbiddenHandler: ((error: ApiError) => void) | null = null;

const MUTATING_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);

export function setSessionCsrfToken(token: string | null): void {
  csrfToken = token;
}

export function setUnauthorizedHandler(handler: (() => void) | null): void {
  unauthorizedHandler = handler;
}

export function setForbiddenHandler(handler: ((error: ApiError) => void) | null): void {
  forbiddenHandler = handler;
}

interface ErrorEnvelope {
  code?: unknown;
  detail?: unknown;
  message?: unknown;
}

function parseApiError(body: ErrorEnvelope | null, status: number): ApiError {
  const detail = body?.detail;
  const nested = typeof detail === "object" && detail !== null
    ? detail as ErrorEnvelope
    : null;
  const codeCandidate = nested?.code ?? body?.code;
  const detailString = typeof detail === "string" ? detail : null;
  const code = typeof codeCandidate === "string"
    ? codeCandidate
    : detailString === "membership_inactive"
      ? detailString
      : null;
  const messageCandidate = nested?.message ?? nested?.detail ?? body?.message;
  const message = typeof messageCandidate === "string"
    ? messageCandidate
    : detailString && detailString !== code
      ? detailString
      : code === "membership_inactive"
        ? "Your organization membership is no longer active."
        : `Request failed with status ${status}`;
  return new ApiError(message, status, code);
}

export function hasPermission(
  session: Pick<AuthSession, "permissions">,
  permission: Permission,
): boolean {
  return session.permissions.includes(permission);
}

async function request<T>(
  path: string,
  init?: RequestInit,
  notifyUnauthorized = true,
): Promise<T> {
  const method = (init?.method ?? "GET").toUpperCase();
  const headers = new Headers(init?.headers);
  if (init?.body !== undefined && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  if (MUTATING_METHODS.has(method) && csrfToken !== null) {
    headers.set("X-CSRF-Token", csrfToken);
  }
  const response = await fetch(path, {
    ...init,
    credentials: "include",
    headers,
  });
  if (!response.ok) {
    const body = (await response.json().catch(() => null)) as ErrorEnvelope | null;
    const error = parseApiError(body, response.status);
    if (notifyUnauthorized) {
      if (response.status === 401 || error.code === "membership_inactive") {
        unauthorizedHandler?.();
      } else if (response.status === 403) {
        forbiddenHandler?.(error);
      }
    }
    throw error;
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export async function getAuthSession(): Promise<AuthSession> {
  const session = await request<AuthSession>("/api/v1/auth/session");
  setSessionCsrfToken(session.csrf_token);
  return session;
}

export async function getDevPersonas(): Promise<DevPersona[]> {
  const response = await request<{ personas: DevPersona[] }>(
    "/api/v1/auth/dev/personas",
    undefined,
    false,
  );
  return response.personas;
}

export async function createDevSession(
  persona: string,
  organizationSlug = "pageragent-labs",
): Promise<AuthSession> {
  const response = await request<AuthSessionEnvelope>(
    "/api/v1/auth/dev/session",
    {
      method: "POST",
      body: JSON.stringify({ persona, organization_slug: organizationSlug }),
    },
    false,
  );
  setSessionCsrfToken(response.session.csrf_token);
  return response.session;
}

export async function switchOrganization(organizationId: string): Promise<AuthSession> {
  const session = await request<AuthSession>("/api/v1/auth/session/switch", {
    method: "POST",
    body: JSON.stringify({ organization_id: organizationId }),
  });
  setSessionCsrfToken(session.csrf_token);
  return session;
}

export async function deleteAuthSession(): Promise<void> {
  await request<void>("/api/v1/auth/session", { method: "DELETE" });
  setSessionCsrfToken(null);
}

export function getConnectors(): Promise<ConnectorSummary[]> {
  return request<ConnectorSummary[]>("/api/v1/connectors");
}

export function getConnector(connectorId: string): Promise<ConnectorDetail> {
  return request<ConnectorDetail>(`/api/v1/connectors/${connectorId}`);
}

export function getConnectorEvents(connectorId: string): Promise<ConnectorEvent[]> {
  return request<ConnectorEvent[]>(`/api/v1/connectors/${connectorId}/events`);
}

export function createConnector(payload: ConnectorCreatePayload): Promise<ConnectorDetail> {
  return request<ConnectorDetail>("/api/v1/connectors", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function updateConnector(
  connectorId: string,
  payload: ConnectorUpdatePayload,
): Promise<ConnectorDetail> {
  return request<ConnectorDetail>(`/api/v1/connectors/${connectorId}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export function rotateConnectorCredentials(
  connectorId: string,
  expectedVersion: number,
  credentials: Record<string, string>,
): Promise<ConnectorDetail> {
  return request<ConnectorDetail>(`/api/v1/connectors/${connectorId}/credentials`, {
    method: "PUT",
    body: JSON.stringify({ expected_version: expectedVersion, credentials }),
  });
}

export function validateConnector(
  connectorId: string,
  expectedVersion: number,
): Promise<ConnectorDetail> {
  return request<ConnectorDetail>(`/api/v1/connectors/${connectorId}/validate`, {
    method: "POST",
    body: JSON.stringify({ expected_version: expectedVersion }),
  });
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

export function getIncidentWorkflows(incidentId: string): Promise<WorkflowRun[]> {
  return request<WorkflowRun[]>(`/api/v1/incidents/${incidentId}/workflows`);
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
      note: note || null,
    }),
  });
}

export function postmortemExportUrl(postmortemId: string): string {
  return `/api/v1/postmortems/${postmortemId}/export`;
}
