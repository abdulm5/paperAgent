import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, test } from "vitest";

import type {
  IncidentDetail,
  InvestigationDetail,
  MitigationProposal,
  PostmortemDetail,
} from "../lib/api";
import { ResponseProofRail } from "./ResponseProofRail";

afterEach(cleanup);

const incident = {
  id: "342e18be-e415-4883-b09b-ca7d0ed4d604",
  status: "detected",
  alert: {
    metric: { value: 0.133, threshold: 0.05 },
  },
} as IncidentDetail;

test("marks the first missing receipt as the active response boundary", () => {
  render(
    <ResponseProofRail
      incident={incident}
      investigation={null}
      postmortem={null}
      proposal={null}
    />,
  );

  expect(screen.getByLabelText("Detect: complete")).toHaveAttribute("data-state", "complete");
  expect(screen.getByLabelText("Ground: active")).toHaveAttribute("data-state", "active");
  expect(screen.getByLabelText("Decide: waiting")).toHaveAttribute("data-state", "waiting");
  expect(screen.getByText("13.3% observed / 5.0% limit")).toBeInTheDocument();
});

test("shows the completed end-to-end receipt chain", () => {
  const investigation = {
    status: "completed",
    evidence: [{ id: "evidence-1" }, { id: "evidence-2" }],
  } as InvestigationDetail;
  const proposal = {
    status: "verification_passed",
    decisions: [{ decision: "approve" }],
    execution: { recovery_verified: true },
  } as MitigationProposal;
  const postmortem = {
    status: "final",
    version: 3,
    revisions: [{ id: "revision-1" }],
  } as unknown as PostmortemDetail;

  render(
    <ResponseProofRail
      incident={{ ...incident, status: "resolved" }}
      investigation={investigation}
      postmortem={postmortem}
      proposal={proposal}
    />,
  );

  expect(screen.getAllByText("✓")).toHaveLength(5);
  expect(screen.getByText("2 immutable artifacts")).toBeInTheDocument();
  expect(screen.getByText("Human approval recorded")).toBeInTheDocument();
  expect(screen.getByText("Canary telemetry passed")).toBeInTheDocument();
  expect(screen.getByText("Finalized at revision 3")).toBeInTheDocument();
});
