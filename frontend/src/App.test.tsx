import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import App from "./App";
import type {
  AuthSession,
  EvaluationScorecard,
  IncidentDetail,
  IncidentSummary,
  InvestigationDetail,
  MitigationProposal,
  WorkflowRun,
} from "./lib/api";

const allPermissions: AuthSession["permissions"] = [
  "incidents.read",
  "incidents.transition",
  "incidents.resolve",
  "investigations.run",
  "proposals.generate",
  "mitigations.decide",
  "postmortems.generate",
  "postmortems.edit",
  "postmortems.finalize",
  "evaluations.run",
  "organization.reset",
];

const authSession: AuthSession = {
  user: {
    id: "00000000-0000-0000-0000-000000000101",
    email: "maya@pageragent.dev",
    display_name: "Maya Chen",
  },
  active_organization: {
    id: "00000000-0000-0000-0000-000000000001",
    slug: "pageragent-labs",
    name: "PagerAgent Labs",
    role: "incident_commander",
  },
  memberships: [
    {
      organization: {
        id: "00000000-0000-0000-0000-000000000001",
        slug: "pageragent-labs",
        name: "PagerAgent Labs",
      },
      role: "incident_commander",
    },
    {
      organization: {
        id: "00000000-0000-0000-0000-000000000002",
        slug: "sandbox-operations",
        name: "Sandbox Operations",
      },
      role: "incident_commander",
    },
  ],
  permissions: allPermissions,
  csrf_token: "csrf-test-token",
};

const sandboxSession: AuthSession = {
  ...authSession,
  active_organization: {
    ...authSession.memberships[1].organization,
    role: "incident_commander",
  },
  csrf_token: "csrf-sandbox-token",
};

const viewerSession: AuthSession = {
  ...authSession,
  active_organization: { ...authSession.active_organization, role: "viewer" },
  memberships: authSession.memberships.map((membership) => ({ ...membership, role: "viewer" })),
  permissions: ["incidents.read"],
};

const summary: IncidentSummary = {
  id: "342e18be-e415-4883-b09b-ca7d0ed4d604",
  status: "detected",
  service: "checkout-api",
  severity: "critical",
  summary: "Checkout API error rate is 13.3%, above the 5.0% threshold.",
  started_at: "2026-07-10T00:48:40Z",
  detected_at: "2026-07-10T00:48:45Z",
  received_at: "2026-07-10T00:48:45Z",
  updated_at: "2026-07-10T00:48:45Z",
  resolved_at: null,
  version: 1,
};

const detail: IncidentDetail = {
  ...summary,
  alert_count: 1,
  alert: {
    fingerprint: "checkout-api:http-server-error-rate:faulty-v2",
    source: "simulated-threshold-evaluator",
    service: "checkout-api",
    severity: "critical",
    summary: summary.summary,
    started_at: summary.started_at,
    detected_at: summary.detected_at,
    metric: {
      name: "http_server_error_rate",
      value: 0.133333,
      threshold: 0.05,
      window_seconds: 300,
      request_count: 60,
      failed_request_count: 8,
    },
    release: {
      name: "faulty-v2",
      commit_sha: "8fa23c1",
      deployed_at: "2026-07-10T00:48:34Z",
    },
    telemetry_url: "http://checkout-api:8100/telemetry",
  },
  events: [
    {
      id: "d9eea318-02e2-4fd2-8223-38e522d92e5b",
      event_type: "incident.detected",
      actor: "simulated-threshold-evaluator",
      from_status: null,
      to_status: "detected",
      note: "Monitoring threshold created the incident.",
      payload: {},
      created_at: "2026-07-10T00:48:45Z",
    },
  ],
};

const investigation: InvestigationDetail = {
  id: "68a1e0d3-bc8f-4ff3-a6d5-d898aca654ce",
  incident_id: summary.id,
  status: "completed",
  collector_version: "http-telemetry-v1",
  clusterer_version: "error-cluster-v1",
  ranker_version: "commit-ranker-v1",
  retrieval_version: "hybrid-runbook-v1",
  input_hash: "a".repeat(64),
  failure_reason: null,
  started_at: "2026-07-10T00:48:46Z",
  completed_at: "2026-07-10T00:48:47Z",
  evidence: [
    {
      id: "12345678-bc8f-4ff3-a6d5-d898aca654ce",
      kind: "telemetry_snapshot",
      source_uri: "http://checkout-api:8100/telemetry",
      content_hash: "b".repeat(64),
      payload: {},
      collected_at: "2026-07-10T00:48:46Z",
    },
  ],
  error_clusters: [
    {
      id: "23456789-bc8f-4ff3-a6d5-d898aca654ce",
      signature: "8ea217ad7bf23119",
      error_type: "ValidationRuleMissing",
      endpoint: "/checkout",
      affected_attributes: { payment_methods: ["digital_wallet"] },
      failure_count: 8,
      first_seen_at: "2026-07-10T00:48:40Z",
      last_seen_at: "2026-07-10T00:48:45Z",
      sample_request_ids: ["outage-traffic-000005"],
      evidence_ids: ["12345678-bc8f-4ff3-a6d5-d898aca654ce"],
    },
  ],
  cause_candidates: [
    {
      id: "33456789-bc8f-4ff3-a6d5-d898aca654ce",
      kind: "code_change",
      reference: "8fa23c1",
      title: "Refactor digital wallet validation rules",
      rank: 1,
      score: 0.96,
      explanation: ["The active commit matches the failing validation path."],
      evidence_ids: ["12345678-bc8f-4ff3-a6d5-d898aca654ce"],
    },
  ],
  commit_candidates: [
    {
      id: "34567890-bc8f-4ff3-a6d5-d898aca654ce",
      commit_sha: "8fa23c1",
      rank: 1,
      total_score: 0.91,
      title: "Refactor digital wallet validation rules",
      author: "Maya Chen",
      committed_at: "2026-07-10T00:39:34Z",
      files_changed: ["services/checkout/validation/payment_methods.py"],
      diff_summary: "Missing rules now raise ValidationRuleMissing.",
      feature_scores: {
        deploy_correlation: 1,
        service_overlap: 1,
        error_diff_similarity: 0.7,
        change_risk: 1,
        ownership_relevance: 1,
      },
      explanation: ["Matches the commit recorded on the active release."],
      evidence_ids: ["12345678-bc8f-4ff3-a6d5-d898aca654ce"],
    },
  ],
  runbook_matches: [
    {
      id: "45678901-bc8f-4ff3-a6d5-d898aca654ce",
      runbook_id: "checkout-api-rollback",
      rank: 1,
      title: "Checkout API rollback",
      service: "checkout-api",
      failure_mode: "elevated-500-errors",
      total_score: 0.88,
      score_breakdown: { metadata: 1, lexical: 0.7, vector: 0.6 },
      matched_sections: [
        {
          heading: "Mitigation",
          excerpt: "Roll checkout-api back to the previous stable release.",
        },
      ],
      content_hash: "c".repeat(64),
      evidence_ids: ["12345678-bc8f-4ff3-a6d5-d898aca654ce"],
    },
  ],
};

const proposal: MitigationProposal = {
  id: "56789012-bc8f-4ff3-a6d5-d898aca654ce",
  incident_id: summary.id,
  investigation_id: investigation.id,
  status: "pending_approval",
  synthesizer_version: "deterministic-brief-v1",
  model_name: "deterministic-template",
  prompt_version: "grounded-incident-brief-v1",
  input_hash: "d".repeat(64),
  root_cause_summary: "Commit 8fa23c1 matches the active deploy and validation failures.",
  confidence: 0.96,
  impact_summary: "8 of 60 observed checkout requests failed in the digital_wallet cohort.",
  recommended_action: "Roll checkout-api back to stable-v1 and run canary traffic.",
  risk_summary: "The rollback may remove unrelated changes shipped in faulty-v2.",
  verification_steps: [
    "Confirm stable-v1 is active.",
    "Send digital-wallet canary requests.",
    "Verify a 0% canary error rate.",
  ],
  slack_update: "Checkout incident: 8 of 60 requests failed after commit 8fa23c1.",
  claims: [
    { kind: "root_cause", text: "Commit 8fa23c1 matches the active deploy and validation failures.", evidence_ids: [investigation.evidence[0].id] },
    { kind: "impact", text: "8 of 60 observed checkout requests failed in the digital_wallet cohort.", evidence_ids: [investigation.evidence[0].id] },
    { kind: "recommendation", text: "Roll checkout-api back to stable-v1 and run canary traffic.", evidence_ids: [investigation.evidence[0].id] },
    { kind: "risk", text: "The rollback may remove unrelated changes shipped in faulty-v2.", evidence_ids: [investigation.evidence[0].id] },
  ],
  action: {
    action_type: "rollback_service",
    target_service: "checkout-api",
    target_release: "stable-v1",
    expected_faulty_commit: "8fa23c1",
    feature_flag: null,
    automation_allowed: true,
  },
  failure_reason: null,
  created_at: "2026-07-10T00:48:48Z",
  decided_at: null,
  decisions: [],
  execution: null,
};

const workflowRun: WorkflowRun = {
  id: "89012345-bc8f-4ff3-a6d5-d898aca654ce",
  incident_id: summary.id,
  workflow_type: "incident_response",
  status: "running",
  current_step: "generate_proposal",
  dedupe_key: `incident-response:${summary.id}`,
  trace_id: "1f8e7d6c5b4a39281716151413121110",
  version: 2,
  failure_reason: null,
  created_at: "2026-07-10T00:48:45Z",
  updated_at: "2026-07-10T00:48:47Z",
  completed_at: null,
  jobs: [
    {
      id: "90123456-bc8f-4ff3-a6d5-d898aca654ce",
      workflow_run_id: "89012345-bc8f-4ff3-a6d5-d898aca654ce",
      step_type: "investigate",
      status: "completed",
      payload: {},
      result: {},
      idempotency_key: `investigate:${summary.id}`,
      attempt_count: 1,
      max_attempts: 3,
      available_at: "2026-07-10T00:48:45Z",
      lease_owner: "worker-demo-01",
      lease_expires_at: null,
      last_error: null,
      created_at: "2026-07-10T00:48:45Z",
      updated_at: "2026-07-10T00:48:46Z",
      started_at: "2026-07-10T00:48:45Z",
      completed_at: "2026-07-10T00:48:46Z",
      deliveries: [
        {
          id: "b1234567-bc8f-4ff3-a6d5-d898aca654ce",
          workflow_job_id: "90123456-bc8f-4ff3-a6d5-d898aca654ce",
          topic: "pageragent.workflow.jobs",
          payload: {},
          dispatch_attempt: 1,
          available_at: "2026-07-10T00:48:45Z",
          published_at: "2026-07-10T00:48:45Z",
          publish_attempts: 1,
          stream_message_id: "1712345678901-0",
          last_error: null,
          created_at: "2026-07-10T00:48:45Z",
          updated_at: "2026-07-10T00:48:45Z",
        },
      ],
    },
    {
      id: "a0123456-bc8f-4ff3-a6d5-d898aca654ce",
      workflow_run_id: "89012345-bc8f-4ff3-a6d5-d898aca654ce",
      step_type: "generate_proposal",
      status: "running",
      payload: {},
      result: {},
      idempotency_key: `proposal:${summary.id}`,
      attempt_count: 2,
      max_attempts: 3,
      available_at: "2026-07-10T00:48:46Z",
      lease_owner: "worker-demo-01",
      lease_expires_at: "2026-07-10T00:49:17Z",
      last_error: null,
      created_at: "2026-07-10T00:48:46Z",
      updated_at: "2026-07-10T00:48:47Z",
      started_at: "2026-07-10T00:48:46Z",
      completed_at: null,
      deliveries: [
        {
          id: "c2345678-bc8f-4ff3-a6d5-d898aca654ce",
          workflow_job_id: "a0123456-bc8f-4ff3-a6d5-d898aca654ce",
          topic: "pageragent.workflow.jobs",
          payload: {},
          dispatch_attempt: 1,
          available_at: "2026-07-10T00:48:46Z",
          published_at: "2026-07-10T00:48:46Z",
          publish_attempts: 2,
          stream_message_id: "1712345678902-0",
          last_error: null,
          created_at: "2026-07-10T00:48:46Z",
          updated_at: "2026-07-10T00:48:47Z",
        },
      ],
    },
  ],
  events: [
    {
      id: 1,
      workflow_run_id: "89012345-bc8f-4ff3-a6d5-d898aca654ce",
      workflow_job_id: null,
      sequence: 1,
      event_type: "workflow.started",
      payload: {},
      created_at: "2026-07-10T00:48:45Z",
    },
  ],
};

const scorecard: EvaluationScorecard = {
  schema_version: "1.0",
  suite_version: "1.0",
  generated_at: "2026-07-14T18:00:00Z",
  passed: true,
  scenario_count: 3,
  aggregate_metrics: {
    cause_top_1: 1,
    runbook_mrr: 1,
    action_safety: 1,
  },
  gates: [
    { metric: "cause_top_1", value: 1, threshold: 1, passed: true },
    { metric: "runbook_mrr", value: 1, threshold: 1, passed: true },
    { metric: "action_safety", value: 1, threshold: 1, passed: true },
  ],
  scenarios: [
    {
      scenario_id: "checkout-validation-bug",
      title: "Bad deploy breaks wallet validation",
      passed: true,
      predicted_cause: investigation.cause_candidates[0],
      predicted_runbook: "checkout-api-rollback",
      predicted_action: {
        action_type: "rollback_service",
        automation_allowed: true,
      },
      metrics: { cause_top_1: 1 },
      adversarial_probes: [
        { case: "missing_evidence", passed: true, observation: "Write blocked." },
      ],
      duration_ms: 4.2,
    },
    {
      scenario_id: "payment-provider-timeout",
      title: "Provider timeout behind a healthy deploy",
      passed: true,
      predicted_cause: {
        ...investigation.cause_candidates[0],
        kind: "upstream_dependency",
        reference: "payment-gateway",
        title: "Payment gateway timeout",
        score: 0.98,
      },
      predicted_runbook: "payment-provider-degradation",
      predicted_action: { action_type: "escalate_only", automation_allowed: false },
      metrics: { cause_top_1: 1 },
      adversarial_probes: [
        { case: "red_herring_deploy", passed: true, observation: "Deploy bypassed." },
      ],
      duration_ms: 3.8,
    },
    {
      scenario_id: "checkout-feature-flag-regression",
      title: "Feature flag enables invalid wallet path",
      passed: true,
      predicted_cause: {
        ...investigation.cause_candidates[0],
        kind: "configuration_change",
        reference: "wallet_validation_v2",
        title: "wallet_validation_v2 enabled",
        score: 0.97,
      },
      predicted_runbook: "checkout-feature-flag-rollback",
      predicted_action: {
        action_type: "disable_feature_flag",
        automation_allowed: true,
      },
      metrics: { cause_top_1: 1 },
      adversarial_probes: [
        { case: "low_confidence_action", passed: true, observation: "Write blocked." },
      ],
      duration_ms: 3.9,
    },
  ],
};

const verifiedProposal: MitigationProposal = {
  ...proposal,
  status: "verification_passed",
  decided_at: "2026-07-10T00:49:20Z",
  decisions: [
    {
      id: "67890123-bc8f-4ff3-a6d5-d898aca654ce",
      decision: "approve",
      actor: "demo-operator",
      note: "Evidence and rollback target reviewed.",
      created_at: "2026-07-10T00:49:20Z",
    },
  ],
  execution: {
    id: "78901234-bc8f-4ff3-a6d5-d898aca654ce",
    status: "completed",
    executor_version: "checkout-simulator-executor-v1",
    idempotency_key: `proposal-${proposal.id}`,
    request_payload: {},
    response_payload: { canary_request_count: 15, recovery_failure_count: 0 },
    before_telemetry: { current_release: { name: "faulty-v2" } },
    after_telemetry: { current_release: { name: "stable-v1" } },
    recovery_verified: true,
    failure_reason: null,
    started_at: "2026-07-10T00:49:20Z",
    completed_at: "2026-07-10T00:49:21Z",
  },
};

function jsonResponse(body: unknown): Response {
  return { ok: true, json: async () => body } as Response;
}

function errorResponse(status: number, detail: unknown): Response {
  return { ok: false, status, json: async () => ({ detail }) } as Response;
}

let authenticated = true;
let currentSession = authSession;
let transitionUnauthorized = false;
let transitionForbidden: { code: string; message: string } | null = null;

class MockEventSource {
  static instances: MockEventSource[] = [];

  readonly url: string;
  onopen: ((event: Event) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  close = vi.fn();
  private listeners = new Map<string, Set<(event: Event) => void>>();

  constructor(url: string | URL) {
    this.url = String(url);
    MockEventSource.instances.push(this);
  }

  addEventListener(type: string, listener: EventListenerOrEventListenerObject | null) {
    if (typeof listener !== "function") return;
    const listeners = this.listeners.get(type) ?? new Set<(event: Event) => void>();
    listeners.add(listener);
    this.listeners.set(type, listeners);
  }

  removeEventListener(type: string, listener: EventListenerOrEventListenerObject | null) {
    if (typeof listener === "function") this.listeners.get(type)?.delete(listener);
  }

  open() {
    this.onopen?.(new Event("open"));
  }

  emitWorkflow(payload: unknown) {
    const event = new MessageEvent("workflow", { data: JSON.stringify(payload) });
    this.listeners.get("workflow")?.forEach((listener) => listener(event));
  }
}

beforeEach(() => {
  authenticated = true;
  currentSession = authSession;
  transitionUnauthorized = false;
  transitionForbidden = null;
  MockEventSource.instances = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.endsWith("/auth/session") && (init?.method ?? "GET") === "GET") {
        return authenticated
          ? jsonResponse(currentSession)
          : errorResponse(401, "Authentication required");
      }
      if (path.endsWith("/auth/dev/personas")) {
        return jsonResponse({
          personas: [
            {
              slug: "incident-commander",
              email: authSession.user.email,
              display_name: authSession.user.display_name,
              role: authSession.active_organization.role,
            },
          ],
        });
      }
      if (path.endsWith("/auth/dev/session") && init?.method === "POST") {
        authenticated = true;
        currentSession = authSession;
        return jsonResponse({ session: authSession, access_token: "dev-access-token" });
      }
      if (path.endsWith("/auth/session/switch") && init?.method === "POST") {
        currentSession = sandboxSession;
        return jsonResponse(sandboxSession);
      }
      if (path.endsWith("/evaluations/scorecard")) return jsonResponse(scorecard);
      if (path.endsWith("/workflows")) return jsonResponse([workflowRun]);
      if (path.endsWith("/investigations/latest")) return jsonResponse(investigation);
      if (path.endsWith("/proposals/latest")) return jsonResponse(proposal);
      if (path.endsWith("/investigations") && init?.method === "POST") {
        return jsonResponse(investigation);
      }
      if (path.endsWith("/proposals") && init?.method === "POST") {
        return jsonResponse(proposal);
      }
      if (path.endsWith("/decisions") && init?.method === "POST") {
        return jsonResponse(verifiedProposal);
      }
      if (path.endsWith("/transitions") && init?.method === "POST") {
        if (transitionUnauthorized) return errorResponse(401, "Session expired");
        if (transitionForbidden) return errorResponse(403, transitionForbidden);
        return jsonResponse({
          ...detail,
          status: "investigating",
          version: 2,
          events: [
            ...detail.events,
            {
              ...detail.events[0],
              id: "f3e2a318-02e2-4fd2-8223-38e522d92e5b",
              event_type: "incident.status_changed",
              actor: "demo-operator",
              from_status: "detected",
              to_status: "investigating",
              note: "Confirmed the failure cohort.",
            },
          ],
        });
      }
      if (path.endsWith("/postmortem")) return errorResponse(404, "Postmortem not found");
      if (path.endsWith(`/incidents/${summary.id}`)) return jsonResponse(detail);
      return jsonResponse([summary]);
    }),
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

test("renders persisted incident evidence from the API", async () => {
  render(<App />);

  expect(await screen.findByRole("heading", { name: summary.summary })).toBeInTheDocument();
  expect(screen.getByRole("region", { name: "Signed authority receipt" })).toBeInTheDocument();
  expect(screen.getByText(authSession.user.email)).toBeInTheDocument();
  expect(screen.getByLabelText("Organization scope")).toHaveValue(
    authSession.active_organization.id,
  );
  expect(screen.getByText(`${allPermissions.length} exact grants`)).toBeInTheDocument();
  expect(screen.getByText("13.3%")).toBeInTheDocument();
  expect(screen.getAllByText("8").length).toBeGreaterThanOrEqual(2);
  expect(screen.getByText("faulty-v2")).toBeInTheDocument();
  expect(screen.getByText("commit 8fa23c1")).toBeInTheDocument();
  expect(await screen.findByText("ValidationRuleMissing")).toBeInTheDocument();
  expect(
    screen.getAllByText("Refactor digital wallet validation rules").length,
  ).toBeGreaterThanOrEqual(2);
  expect(screen.getByText("Checkout API rollback")).toBeInTheDocument();
  expect(screen.getByText(proposal.root_cause_summary)).toBeInTheDocument();
  expect(screen.getByText("Human authority required")).toBeInTheDocument();
  expect(screen.getByText("All gates passing")).toBeInTheDocument();
  expect(screen.getByText("payment-gateway")).toBeInTheDocument();
  expect(document.querySelector(".queue-item.selected")).toHaveAttribute("aria-current", "true");
});

test("shows the durable workflow snapshot when live streaming is unavailable", async () => {
  vi.stubGlobal("EventSource", undefined);
  render(<App />);

  expect(await screen.findByRole("heading", { name: "Work survives the process." })).toBeInTheDocument();
  expect(screen.getByText("Incident Response")).toBeInTheDocument();
  expect(screen.getByText("30 sec reconcile")).toBeInTheDocument();
  expect(screen.getByText("2/3")).toBeInTheDocument();
  expect(screen.getAllByText("worker-demo-01").length).toBeGreaterThan(0);
  expect(screen.getByText("Published")).toBeInTheDocument();
  expect(
    screen.getAllByText(/stream 1712345678902-0 · publish 2/).length,
  ).toBeGreaterThan(0);
});

test("applies only newer workflow stream updates and closes the flight recorder connection", async () => {
  MockEventSource.instances = [];
  vi.stubGlobal("EventSource", MockEventSource);
  const { unmount } = render(<App />);

  await screen.findByRole("heading", { name: "Work survives the process." });
  const stream = MockEventSource.instances[0];
  expect(stream.url).toBe("/api/v1/workflows/events");

  act(() => stream.open());
  expect(screen.getByText("Stream live")).toBeInTheDocument();

  const completedWorkflow: WorkflowRun = {
    ...workflowRun,
    status: "completed",
    current_step: null,
    version: 3,
    completed_at: "2026-07-10T00:48:49Z",
    updated_at: "2026-07-10T00:48:49Z",
    jobs: workflowRun.jobs.map((job) => ({
      ...job,
      status: "completed",
      completed_at: "2026-07-10T00:48:49Z",
      lease_expires_at: null,
    })),
  };

  act(() => {
    stream.emitWorkflow({
      id: 7,
      workflow_id: completedWorkflow.id,
      incident_id: summary.id,
      sequence: 7,
      event_type: "workflow.completed",
      payload: {},
      created_at: completedWorkflow.updated_at,
      workflow: completedWorkflow,
    });
  });

  expect(await screen.findByText("Completed", { selector: ".workflow-status" })).toBeInTheDocument();

  act(() => {
    for (const version of [2, 3]) {
      stream.emitWorkflow({
        id: 8 + version,
        workflow_id: workflowRun.id,
        incident_id: summary.id,
        sequence: 8 + version,
        event_type: "workflow.step_started",
        payload: {},
        created_at: workflowRun.updated_at,
        workflow: { ...workflowRun, version },
      });
    }
  });
  expect(screen.getByText("Completed", { selector: ".workflow-status" })).toBeInTheDocument();
  expect(screen.queryByText("Running", { selector: ".workflow-status" })).not.toBeInTheDocument();

  unmount();
  expect(stream.close).toHaveBeenCalledOnce();
});

test("records an operator lifecycle transition", async () => {
  render(<App />);
  const action = await screen.findByRole("button", { name: "Begin investigation" });
  fireEvent.change(screen.getByLabelText("Timeline note"), {
    target: { value: "Confirmed the failure cohort." },
  });
  fireEvent.click(action);

  await waitFor(() => {
    expect(screen.getByText("Confirmed the failure cohort.")).toBeInTheDocument();
  });
  expect(screen.getAllByText("Investigating").length).toBeGreaterThan(0);

  const transitionCall = vi.mocked(fetch).mock.calls.find(([input, init]) =>
    String(input).endsWith("/transitions") && init?.method === "POST"
  );
  expect(transitionCall).toBeDefined();
  const [, transitionInit] = transitionCall!;
  expect(transitionInit?.credentials).toBe("include");
  expect(new Headers(transitionInit?.headers).get("X-CSRF-Token")).toBe("csrf-test-token");
  expect(JSON.parse(String(transitionInit?.body))).toEqual({
    to_status: "investigating",
    expected_version: 1,
    note: "Confirmed the failure cohort.",
  });
});

test("requires evidence review before approving and shows recovery receipt", async () => {
  render(<App />);
  fireEvent.click(await screen.findByRole("button", { name: "Begin investigation" }));
  await screen.findByText("Confirmed the failure cohort.");

  const approve = screen.getByRole("button", { name: "Approve rollback" });
  expect(approve).toBeDisabled();
  fireEvent.click(
    screen.getByLabelText("I reviewed the cited evidence and rollback target."),
  );
  expect(approve).toBeEnabled();
  fireEvent.click(approve);

  expect(await screen.findByText("Rollback verified")).toBeInTheDocument();
  expect(screen.getByText("15")).toBeInTheDocument();
  expect(screen.getByText("0")).toBeInTheDocument();
});

test("signs in through the local identity checkpoint before loading tenant data", async () => {
  authenticated = false;
  render(<App />);

  expect(
    await screen.findByRole("heading", { name: "Choose who crosses the write boundary." }),
  ).toBeInTheDocument();
  expect(screen.queryByText(summary.summary)).not.toBeInTheDocument();

  fireEvent.click(
    screen.getByRole("button", { name: `Continue as ${authSession.user.display_name}` }),
  );

  expect(await screen.findByRole("heading", { name: summary.summary })).toBeInTheDocument();
  const loginCall = vi.mocked(fetch).mock.calls.find(([input]) =>
    String(input).endsWith("/auth/dev/session")
  );
  expect(loginCall).toBeDefined();
  expect(JSON.parse(String(loginCall?.[1]?.body))).toEqual({
    persona: "incident-commander",
    organization_slug: "pageragent-labs",
  });
});

test("shows exact read-only boundaries for a viewer and removes manual mitigation", async () => {
  currentSession = viewerSession;
  render(<App />);

  expect(await screen.findByRole("button", { name: "Begin investigation" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Run suite" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Rerun" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Approve rollback" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Reject proposal" })).toBeDisabled();
  expect(screen.getAllByText("incidents.transition").length).toBeGreaterThan(0);
  expect(screen.getAllByText("investigations.run").length).toBeGreaterThan(0);
  expect(screen.getAllByText("mitigations.decide").length).toBeGreaterThan(0);
  expect(screen.queryByRole("button", { name: "Mark mitigated" })).not.toBeInTheDocument();
});

test("closes the tenant stream before switching organization scope", async () => {
  vi.stubGlobal("EventSource", MockEventSource);
  render(<App />);

  await screen.findByRole("heading", { name: summary.summary });
  const stream = MockEventSource.instances[0];
  fireEvent.change(screen.getByLabelText("Organization scope"), {
    target: { value: sandboxSession.active_organization.id },
  });

  expect(stream.close).toHaveBeenCalled();
  await waitFor(() => {
    expect(screen.getByLabelText("Organization scope")).toHaveValue(
      sandboxSession.active_organization.id,
    );
  });
  const switchCall = vi.mocked(fetch).mock.calls.find(([input]) =>
    String(input).endsWith("/auth/session/switch")
  );
  expect(switchCall).toBeDefined();
  expect(new Headers(switchCall?.[1]?.headers).get("X-CSRF-Token")).toBe("csrf-test-token");
  expect(JSON.parse(String(switchCall?.[1]?.body))).toEqual({
    organization_id: sandboxSession.active_organization.id,
  });
});

test("closes live data and returns to the checkpoint on a 401", async () => {
  vi.stubGlobal("EventSource", MockEventSource);
  render(<App />);

  const transition = await screen.findByRole("button", { name: "Begin investigation" });
  const stream = MockEventSource.instances[0];
  transitionUnauthorized = true;
  fireEvent.click(transition);

  expect(
    await screen.findByRole("heading", { name: "Choose who crosses the write boundary." }),
  ).toBeInTheDocument();
  expect(screen.queryByText(summary.summary)).not.toBeInTheDocument();
  expect(stream.close).toHaveBeenCalled();
  expect(screen.getByRole("alert")).toHaveTextContent("Your session ended");
});

test("clears tenant data when the active membership is revoked with a typed 403", async () => {
  vi.stubGlobal("EventSource", MockEventSource);
  render(<App />);

  const transition = await screen.findByRole("button", { name: "Begin investigation" });
  const stream = MockEventSource.instances[0];
  transitionForbidden = {
    code: "membership_inactive",
    message: "Session membership is inactive or unavailable",
  };
  fireEvent.click(transition);

  expect(
    await screen.findByRole("heading", { name: "Choose who crosses the write boundary." }),
  ).toBeInTheDocument();
  expect(screen.queryByText(summary.summary)).not.toBeInTheDocument();
  expect(stream.close).toHaveBeenCalled();
  expect(screen.getByRole("alert")).toHaveTextContent("Your session ended");
});

test("refreshes the authority receipt after an ordinary permission 403", async () => {
  render(<App />);

  const transition = await screen.findByRole("button", { name: "Begin investigation" });
  currentSession = viewerSession;
  transitionForbidden = {
    code: "permission_denied",
    message: "Missing permission: incidents.transition",
  };
  fireEvent.click(transition);

  await waitFor(() => expect(transition).toBeDisabled());
  expect(screen.getByText("1 exact grants")).toBeInTheDocument();
  expect(screen.getByRole("alert")).toHaveTextContent(
    "Missing permission: incidents.transition",
  );
});
