import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";

import type { PostmortemDetail, PostmortemEditPayload } from "../lib/api";
import { PostmortemPanel } from "./PostmortemPanel";

const evidenceId = "12345678-bc8f-4ff3-a6d5-d898aca654ce";

const postmortem: PostmortemDetail = {
  id: "a2345678-bc8f-4ff3-a6d5-d898aca654ce",
  incident_id: "b2345678-bc8f-4ff3-a6d5-d898aca654ce",
  status: "draft",
  version: 1,
  generator_version: "deterministic-postmortem-v1",
  model_name: "deterministic-template",
  prompt_version: "grounded-postmortem-v1",
  input_hash: "f".repeat(64),
  content: {
    title: "Checkout validation incident",
    summary: { text: "A faulty release caused eight checkout failures.", evidence_ids: [evidenceId] },
    root_cause: { text: "A validation rule was missing.", evidence_ids: [evidenceId] },
    customer_impact: { text: "Eight digital-wallet requests failed.", evidence_ids: [evidenceId] },
    detection: { text: "The error-rate alert crossed its threshold.", evidence_ids: [evidenceId] },
    resolution: { text: "The operator approved and verified a rollback.", evidence_ids: [evidenceId] },
    what_went_well: [{ text: "Recovery canaries covered the failing cohort.", evidence_ids: [evidenceId] }],
    what_went_poorly: [{ text: "Validation did not catch the missing rule.", evidence_ids: [evidenceId] }],
    prevention_items: [{
      title: "Add cohort coverage",
      description: "Test digital-wallet validation in the release gate.",
      owner: "checkout-team",
      priority: "P1",
      status: "open",
      evidence_ids: [evidenceId],
    }],
    timeline: [{
      occurred_at: "2026-07-10T00:48:45Z",
      event_type: "incident.detected",
      actor: "alert-evaluator",
      description: "Monitoring threshold created the incident.",
      evidence_ids: [evidenceId],
    }],
  },
  created_at: "2026-07-10T00:50:00Z",
  updated_at: "2026-07-10T00:50:00Z",
  finalized_at: null,
  finalized_by: null,
  revisions: [{
    id: "c2345678-bc8f-4ff3-a6d5-d898aca654ce",
    version: 1,
    source: "generated",
    editor: "pageragent",
    change_note: "Initial grounded draft",
    created_at: "2026-07-10T00:50:00Z",
  }],
};

afterEach(cleanup);

test("shows the grounded case file and saves an auditable revision", async () => {
  const onSave = vi.fn<(edit: PostmortemEditPayload) => Promise<void>>(async () => undefined);
  const props = {
    acting: false,
    canEdit: true,
    canFinalize: true,
    canGenerate: true,
    error: null,
    incidentStatus: "resolved" as const,
    loading: false,
    onFinalize: vi.fn(async () => undefined),
    onGenerate: vi.fn(async () => undefined),
    onSave,
    postmortem,
  };
  const { rerender } = render(<PostmortemPanel {...props} />);

  expect(screen.getByText(postmortem.content.title)).toBeInTheDocument();
  expect(screen.getAllByText("E-123456").length).toBeGreaterThan(5);
  expect(screen.getByText("v1")).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Export Markdown" })).toHaveAttribute(
    "href",
    `/api/v1/postmortems/${postmortem.id}/export`,
  );

  fireEvent.click(screen.getByRole("button", { name: "Edit draft" }));
  rerender(<PostmortemPanel {...props} postmortem={{ ...postmortem }} />);
  expect(screen.getByLabelText("Summary")).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText("Summary"), {
    target: { value: "Team-reviewed impact summary." },
  });
  const save = screen.getByRole("button", { name: "Save revision" });
  expect(save).toBeDisabled();
  fireEvent.change(screen.getByLabelText("Revision note"), {
    target: { value: "Clarified impact after team review." },
  });
  fireEvent.click(save);

  await waitFor(() => expect(onSave).toHaveBeenCalledOnce());
  expect(onSave.mock.calls[0][0]).toMatchObject({
    summary: "Team-reviewed impact summary.",
    change_note: "Clarified impact after team review.",
  });
});

test("requires explicit review before finalizing the record", async () => {
  const onFinalize = vi.fn(async () => undefined);
  render(
    <PostmortemPanel
      acting={false}
      canEdit={true}
      canFinalize={true}
      canGenerate={true}
      error={null}
      incidentStatus="resolved"
      loading={false}
      onFinalize={onFinalize}
      onGenerate={vi.fn()}
      onSave={vi.fn()}
      postmortem={postmortem}
    />,
  );

  const finalize = screen.getByRole("button", { name: "Finalize record" });
  expect(finalize).toBeDisabled();
  fireEvent.click(screen.getByLabelText("I reviewed the report, citations, and prevention owners."));
  fireEvent.change(screen.getByLabelText("Finalization note"), {
    target: { value: "Reviewed with the checkout team." },
  });
  fireEvent.click(finalize);

  await waitFor(() => expect(onFinalize).toHaveBeenCalledWith("Reviewed with the checkout team."));
});
