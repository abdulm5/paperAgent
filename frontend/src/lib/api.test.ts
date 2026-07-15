import { afterEach, expect, test, vi } from "vitest";

import {
  decideProposal,
  finalizePostmortem,
  getIncidents,
  setForbiddenHandler,
  setSessionCsrfToken,
  setUnauthorizedHandler,
  transitionIncident,
  updatePostmortem,
  type PostmortemDetail,
  type PostmortemEditPayload,
} from "./api";

afterEach(() => {
  setSessionCsrfToken(null);
  setForbiddenHandler(null);
  setUnauthorizedHandler(null);
  vi.unstubAllGlobals();
});

test("signs every cookie-authenticated write with CSRF and never accepts a client actor", async () => {
  const fetchMock = vi.fn<
    (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>
  >(async () => ({
      ok: true,
      status: 200,
      json: async () => ({}),
    } as Response));
  vi.stubGlobal("fetch", fetchMock);
  setSessionCsrfToken("csrf-memory-only");

  const postmortem = { id: "postmortem-1", version: 4 } as PostmortemDetail;
  const edit = {
    change_note: "Clarified the recovery sequence.",
    title: "Checkout incident",
    summary: "Summary",
    root_cause: "Root cause",
    customer_impact: "Customer impact",
    detection: "Detection",
    resolution: "Resolution",
    what_went_well: ["Canaries covered the failing cohort."],
    what_went_poorly: ["The release gate missed the regression."],
    prevention_items: [{
      title: "Add release coverage",
      description: "Exercise wallet validation before deploy.",
      owner: "checkout-team",
      priority: "P1" as const,
      status: "open",
    }],
  } satisfies PostmortemEditPayload;

  await transitionIncident("incident-1", "investigating", 2, "Triage started.");
  await decideProposal("proposal-1", "approve", "Evidence reviewed.");
  await updatePostmortem(postmortem, edit);
  await finalizePostmortem(postmortem, "Team reviewed.");

  expect(fetchMock).toHaveBeenCalledTimes(4);
  for (const [, init] of fetchMock.mock.calls) {
    expect(init?.credentials).toBe("include");
    expect(new Headers(init?.headers).get("X-CSRF-Token")).toBe("csrf-memory-only");
    expect(JSON.parse(String(init?.body))).not.toHaveProperty("actor");
  }
});

test.each([
  {
    body: {
      detail: {
        code: "membership_inactive",
        message: "Session membership is inactive or unavailable",
      },
    },
    message: "Session membership is inactive or unavailable",
  },
  {
    body: { detail: "membership_inactive" },
    message: "Your organization membership is no longer active.",
  },
])("treats an inactive-membership 403 as an authentication boundary", async ({ body, message }) => {
  const unauthorized = vi.fn();
  const forbidden = vi.fn();
  setUnauthorizedHandler(unauthorized);
  setForbiddenHandler(forbidden);
  vi.stubGlobal("fetch", vi.fn(async () => ({
    ok: false,
    status: 403,
    json: async () => body,
  } as Response)));

  await expect(getIncidents()).rejects.toMatchObject({
    code: "membership_inactive",
    message,
    status: 403,
  });
  expect(unauthorized).toHaveBeenCalledOnce();
  expect(forbidden).not.toHaveBeenCalled();
});

test("routes an ordinary permission 403 to the authority refresh handler", async () => {
  const unauthorized = vi.fn();
  const forbidden = vi.fn();
  setUnauthorizedHandler(unauthorized);
  setForbiddenHandler(forbidden);
  vi.stubGlobal("fetch", vi.fn(async () => ({
    ok: false,
    status: 403,
    json: async () => ({
      detail: {
        code: "permission_denied",
        message: "Missing permission: incidents.transition",
      },
    }),
  } as Response)));

  await expect(getIncidents()).rejects.toMatchObject({
    code: "permission_denied",
    status: 403,
  });
  expect(forbidden).toHaveBeenCalledOnce();
  expect(unauthorized).not.toHaveBeenCalled();
});
