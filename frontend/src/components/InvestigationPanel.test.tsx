import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";

import type { EvidenceArtifact, InvestigationDetail } from "../lib/api";
import { InvestigationPanel } from "./InvestigationPanel";

const prometheusArtifact: EvidenceArtifact = {
  id: "77777777-7777-4777-8777-777777777777",
  kind: "prometheus_metric_snapshot",
  source_uri: "prometheus://connector/33333333-3333-4333-8333-333333333333/checkout-api",
  content_hash: "a".repeat(64),
  payload: {
    provider: "prometheus",
    provider_version: "prometheus-http-api-v1",
    catalog_version: "prometheus-query-catalog-v1",
    query_id: "alert.http-server-error-rate.v1",
    metric_name: "http_server_error_rate",
    service: "checkout-api",
    window_started_at: "2026-07-16T16:25:00Z",
    window_ended_at: "2026-07-16T16:30:00Z",
    step_seconds: 15,
    series_count: 2,
    sample_count: 81,
    truncated: false,
    connector_id: "33333333-3333-4333-8333-333333333333",
    connector_version: 5,
    credential_version: 3,
    query: "raw-promql-sentinel",
    bearer_token: "prometheus-token-sentinel",
    series: [
      {
        labels: { forbidden_label: "series-label-sentinel" },
        samples: [{ observed_at: "2026-07-16T16:30:00Z", value: 0.13 }],
      },
    ],
  },
  collected_at: "2026-07-16T16:30:01Z",
};

function investigation(evidence: EvidenceArtifact[]): InvestigationDetail {
  return {
    id: "11111111-1111-4111-8111-111111111111",
    incident_id: "22222222-2222-4222-8222-222222222222",
    status: "completed",
    collector_version: "http-telemetry-v1",
    clusterer_version: "error-cluster-v1",
    ranker_version: "commit-ranker-v1+cause-ranker-v1",
    retrieval_version: "hybrid-runbook-v1",
    input_hash: "b".repeat(64),
    failure_reason: null,
    started_at: "2026-07-16T16:30:00Z",
    completed_at: "2026-07-16T16:30:01Z",
    evidence,
    error_clusters: [],
    cause_candidates: [],
    commit_candidates: [],
    runbook_matches: [],
  };
}

function renderInvestigation(evidence: EvidenceArtifact[]) {
  render(
    <InvestigationPanel
      canRun
      error={null}
      investigation={investigation(evidence)}
      loading={false}
      onRun={vi.fn(async () => {})}
      running={false}
    />,
  );
}

afterEach(cleanup);

test("shows bounded signal coverage and a sanitized Prometheus provenance receipt", () => {
  renderInvestigation([prometheusArtifact]);

  const coverage = screen.getByRole("region", { name: "Signal coverage" });
  const metrics = within(coverage).getByText("Metrics").closest<HTMLElement>(".signal-channel");
  const logs = within(coverage).getByText("Logs").closest<HTMLElement>(".signal-channel");
  const traces = within(coverage).getByText("Traces").closest<HTMLElement>(".signal-channel");
  expect(metrics).not.toBeNull();
  expect(logs).not.toBeNull();
  expect(traces).not.toBeNull();
  expect(within(metrics!).getByText("1 snapshot")).toBeInTheDocument();
  expect(within(metrics!).getByText("E-777777")).toBeInTheDocument();
  expect(within(logs!).getByText("Not collected")).toBeInTheDocument();
  expect(within(traces!).getByText("Not collected")).toBeInTheDocument();

  const receipt = screen.getByRole("region", { name: "Prometheus provenance receipt" });
  expect(receipt).toHaveTextContent("Prometheus HTTP API");
  expect(receipt).toHaveTextContent("prometheus-http-api-v1");
  expect(receipt).toHaveTextContent("prometheus-query-catalog-v1");
  expect(receipt).toHaveTextContent("checkout-api");
  expect(receipt).toHaveTextContent("alert.http-server-error-rate.v1");
  expect(receipt).toHaveTextContent(/Series\s*2/);
  expect(receipt).toHaveTextContent(/Samples\s*81/);
  expect(receipt).toHaveTextContent(/Truncated\s*No/);
  expect(receipt).toHaveTextContent(/Connector version\s*v5/);
  expect(receipt).toHaveTextContent(/Credential version\s*v3/);
  expect(receipt).toHaveTextContent("2026-07-16T16:25:00Z → 2026-07-16T16:30:00Z");
  expect(receipt).toHaveTextContent(prometheusArtifact.source_uri);
  expect(receipt).toHaveTextContent(prometheusArtifact.content_hash);

  expect(document.body).not.toHaveTextContent("raw-promql-sentinel");
  expect(document.body).not.toHaveTextContent("prometheus-token-sentinel");
  expect(document.body).not.toHaveTextContent("series-label-sentinel");
});

test("withholds malformed Prometheus provenance and does not claim signal coverage", () => {
  renderInvestigation([
    {
      ...prometheusArtifact,
      source_uri: "https://metrics.example.com/api/v1/query_range?query=raw-source-sentinel",
      content_hash: "hash-sentinel",
      payload: {
        ...prometheusArtifact.payload,
        provider_version: "provider-version-sentinel<script>",
        catalog_version: { unknown: "catalog-version-sentinel" },
        service: "<script>unknown-payload-sentinel</script>",
        query_id: "unsafe query id",
        connector_id: "not-a-uuid",
      },
    },
  ]);

  const coverage = screen.getByRole("region", { name: "Signal coverage" });
  const metrics = within(coverage).getByText("Metrics").closest<HTMLElement>(".signal-channel");
  expect(metrics).not.toBeNull();
  expect(within(metrics!).getByText("Not collected")).toBeInTheDocument();
  expect(screen.queryByRole("region", { name: "Prometheus provenance receipt" })).not.toBeInTheDocument();
  expect(screen.getByText("Prometheus source withheld")).toBeInTheDocument();
  expect(screen.getByText("Hash unavailable")).toBeInTheDocument();
  expect(document.body).not.toHaveTextContent("raw-source-sentinel");
  expect(document.body).not.toHaveTextContent("unknown-payload-sentinel");
  expect(document.body).not.toHaveTextContent("unsafe query id");
  expect(document.body).not.toHaveTextContent("provider-version-sentinel");
  expect(document.body).not.toHaveTextContent("catalog-version-sentinel");
});
