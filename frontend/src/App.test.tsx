import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import App from "./App";
import type {
  IncidentDetail,
  IncidentSummary,
  InvestigationDetail,
  MitigationProposal,
} from "./lib/api";

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
  },
  failure_reason: null,
  created_at: "2026-07-10T00:48:48Z",
  decided_at: null,
  decisions: [],
  execution: null,
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

function errorResponse(status: number, detail: string): Response {
  return { ok: false, status, json: async () => ({ detail }) } as Response;
}

beforeEach(() => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
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
  expect(screen.getByText("13.3%")).toBeInTheDocument();
  expect(screen.getAllByText("8").length).toBeGreaterThanOrEqual(2);
  expect(screen.getByText("faulty-v2")).toBeInTheDocument();
  expect(screen.getByText("commit 8fa23c1")).toBeInTheDocument();
  expect(await screen.findByText("ValidationRuleMissing")).toBeInTheDocument();
  expect(screen.getByText("Refactor digital wallet validation rules")).toBeInTheDocument();
  expect(screen.getByText("Checkout API rollback")).toBeInTheDocument();
  expect(screen.getByText(proposal.root_cause_summary)).toBeInTheDocument();
  expect(screen.getByText("Human authority required")).toBeInTheDocument();
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
