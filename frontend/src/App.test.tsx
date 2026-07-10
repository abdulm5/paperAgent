import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import App from "./App";
import type { IncidentDetail, IncidentSummary } from "./lib/api";

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

function jsonResponse(body: unknown): Response {
  return { ok: true, json: async () => body } as Response;
}

beforeEach(() => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (init?.method === "POST") {
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
      if (path.endsWith(`/incidents/${summary.id}`)) return jsonResponse(detail);
      return jsonResponse([summary]);
    }),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
});

test("renders persisted incident evidence from the API", async () => {
  render(<App />);

  expect(await screen.findByRole("heading", { name: summary.summary })).toBeInTheDocument();
  expect(screen.getByText("13.3%")).toBeInTheDocument();
  expect(screen.getByText("8")).toBeInTheDocument();
  expect(screen.getByText("faulty-v2")).toBeInTheDocument();
  expect(screen.getByText("commit 8fa23c1")).toBeInTheDocument();
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
