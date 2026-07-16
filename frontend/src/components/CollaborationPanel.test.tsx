import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";

import type { CollaborationOutput, MitigationProposal } from "../lib/api";
import { CollaborationPanel } from "./CollaborationPanel";

const proposal: MitigationProposal = {
  id: "11111111-1111-4111-8111-111111111111",
  incident_id: "22222222-2222-4222-8222-222222222222",
  investigation_id: "33333333-3333-4333-8333-333333333333",
  status: "pending_approval",
  synthesizer_version: "grounded-v1",
  model_name: "deterministic-template",
  prompt_version: "incident-v1",
  input_hash: "a".repeat(64),
  root_cause_summary: "A checkout deploy removed wallet validation rules.",
  confidence: 0.96,
  impact_summary: "Eight checkout requests failed.",
  recommended_action: "Roll back checkout-api.",
  risk_summary: "The rollback removes unrelated changes.",
  verification_steps: ["Run canary checkout requests."],
  slack_update: "This proposal draft must not be published directly.",
  claims: [],
  action: {
    action_type: "rollback_service",
    target_service: "checkout-api",
    target_release: "stable-v1",
    expected_faulty_commit: "8fa23c1",
    feature_flag: null,
    automation_allowed: true,
  },
  failure_reason: null,
  created_at: "2026-07-16T12:00:00Z",
  decided_at: null,
  decisions: [],
  execution: null,
};

const pendingSlack: CollaborationOutput = {
  id: "44444444-4444-4444-8444-444444444444",
  incident_id: proposal.incident_id,
  proposal_id: proposal.id,
  connector_id: "55555555-5555-4555-8555-555555555555",
  workflow_run_id: null,
  kind: "slack_update",
  provider: "slack",
  status: "pending_approval",
  version: 1,
  destination: "C0123456789",
  payload: { text: "Frozen server-built Slack update with evidence E-123456." },
  content_sha256: "b".repeat(64),
  connector_version: 7,
  credential_version: 3,
  requested_by: "user:maya@pageragent.dev",
  requested_at: "2026-07-16T12:01:00Z",
  decided_at: null,
  delivered_at: null,
  failure_reason: null,
  decisions: [],
  delivery: null,
};

const noop = async () => undefined;

afterEach(cleanup);

test("keeps draft preparation separate from external write approval", async () => {
  const onPrepare = vi.fn(noop);
  render(
    <CollaborationPanel
      acting={null}
      canDecide
      canPrepare
      error={null}
      loading={false}
      onDecision={noop}
      onPrepare={onPrepare}
      outputs={[]}
      proposal={proposal}
    />,
  );

  expect(screen.getByText(/Mitigation approval never authorizes/)).toBeInTheDocument();
  const prepare = screen.getByRole("button", { name: "Prepare selected drafts" });
  expect(prepare).toBeDisabled();
  fireEvent.click(screen.getByRole("checkbox", { name: /Slack incident update/ }));
  fireEvent.click(screen.getByRole("checkbox", { name: /GitHub follow-up issue/ }));
  expect(prepare).toBeEnabled();
  fireEvent.click(prepare);

  await waitFor(() => expect(onPrepare).toHaveBeenCalledWith(["slack_update", "github_issue"]));
  expect(screen.queryByRole("button", { name: /Approve .* write/ })).not.toBeInTheDocument();
});

test("requires review of exact frozen content before authorizing a provider write", async () => {
  const onDecision = vi.fn(noop);
  render(
    <CollaborationPanel
      acting={null}
      canDecide
      canPrepare
      error={null}
      loading={false}
      onDecision={onDecision}
      onPrepare={noop}
      outputs={[pendingSlack]}
      proposal={proposal}
    />,
  );

  expect(screen.getByText("Frozen server-built Slack update with evidence E-123456.")).toBeInTheDocument();
  expect(screen.getByText(/Content receipt/).parentElement).toHaveTextContent("bbbbbbbbbbbb");
  const approve = screen.getByRole("button", { name: "Approve Slack write" });
  expect(approve).toBeDisabled();

  fireEvent.change(
    screen.getByLabelText("Decision note for Slack incident update"),
    { target: { value: "Confirmed channel and customer impact." } },
  );
  fireEvent.click(
    screen.getByLabelText("I reviewed the frozen slack incident update and destination."),
  );
  expect(approve).toBeEnabled();
  fireEvent.click(approve);

  await waitFor(() => expect(onDecision).toHaveBeenCalledWith(
    pendingSlack,
    "approve",
    "Confirmed channel and customer impact.",
  ));
});

test("renders durable attempts, dead letters, and provider receipts without trusting arbitrary links", () => {
  const deliveredGitHub: CollaborationOutput = {
    ...pendingSlack,
    id: "66666666-6666-4666-8666-666666666666",
    kind: "github_issue",
    provider: "github",
    status: "delivered",
    destination: "pageragent/core",
    payload: { title: "[PagerAgent][HIGH] checkout-api incident", body: "Grounded issue body." },
    delivered_at: "2026-07-16T12:03:00Z",
    delivery: {
      idempotency_key: "collaboration:66666666-6666-4666-8666-666666666666",
      status: "delivered",
      attempt_count: 2,
      provider_receipt: {
        repository: "pageragent/core",
        issue_number: 42,
        issue_url: "https://github.com/pageragent/core/issues/42",
        reconciled: true,
      },
      last_error_code: null,
      started_at: "2026-07-16T12:02:00Z",
      updated_at: "2026-07-16T12:03:00Z",
      delivered_at: "2026-07-16T12:03:00Z",
    },
  };
  const deadLetteredSlack: CollaborationOutput = {
    ...pendingSlack,
    status: "dead_lettered",
    failure_reason: "Provider confirmation remained ambiguous.",
    delivery: {
      idempotency_key: "collaboration:44444444-4444-4444-8444-444444444444",
      status: "dead_lettered",
      attempt_count: 5,
      provider_receipt: {},
      last_error_code: "slack_delivery_ambiguous",
      started_at: "2026-07-16T12:02:00Z",
      updated_at: "2026-07-16T12:08:00Z",
      delivered_at: null,
    },
  };

  render(
    <CollaborationPanel
      acting={null}
      canDecide
      canPrepare
      error={null}
      loading={false}
      onDecision={noop}
      onPrepare={noop}
      outputs={[deadLetteredSlack, deliveredGitHub]}
      proposal={proposal}
    />,
  );

  expect(screen.getByText("Delivery moved to the dead-letter queue")).toBeInTheDocument();
  expect(screen.getByText("slack_delivery_ambiguous")).toBeInTheDocument();
  expect(screen.getByText("Delivered · reconciled")).toBeInTheDocument();
  expect(screen.getByText("42")).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Open confirmed issue ↗" })).toHaveAttribute(
    "href",
    "https://github.com/pageragent/core/issues/42",
  );
});

test("shows exact permission boundaries to read-only operators", () => {
  render(
    <CollaborationPanel
      acting={null}
      canDecide={false}
      canPrepare={false}
      error={null}
      loading={false}
      onDecision={noop}
      onPrepare={noop}
      outputs={[pendingSlack]}
      proposal={proposal}
    />,
  );

  expect(screen.getByRole("button", { name: "Prepare selected drafts" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Approve Slack write" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Reject publication" })).toBeDisabled();
  expect(screen.getByText("collaboration.prepare")).toBeInTheDocument();
  expect(screen.getByText("collaboration.decide")).toBeInTheDocument();
});
