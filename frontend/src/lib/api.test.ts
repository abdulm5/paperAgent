import { afterEach, expect, test, vi } from "vitest";

import {
  advanceRequestScope,
  createConnector,
  decideCollaborationOutput,
  decideProposal,
  finalizePostmortem,
  getConnector,
  getConnectorEvents,
  getConnectors,
  getAuthSession,
  getIncidents,
  getCollaborationOutputs,
  prepareCollaborationOutputs,
  rotateConnectorCredentials,
  setForbiddenHandler,
  setSessionCsrfToken,
  setUnauthorizedHandler,
  transitionIncident,
  updatePostmortem,
  updateConnector,
  validateConnector,
  type PostmortemDetail,
  type PostmortemEditPayload,
  type CollaborationOutput,
  type MitigationProposal,
} from "./api";

afterEach(() => {
  advanceRequestScope();
  setSessionCsrfToken(null);
  setForbiddenHandler(null);
  setUnauthorizedHandler(null);
  vi.unstubAllGlobals();
});

test("does not notify auth handlers for a response from an earlier request scope", async () => {
  const unauthorized = vi.fn();
  let resolveRequest!: (response: Response) => void;
  const delayedResponse = new Promise<Response>((resolve) => {
    resolveRequest = resolve;
  });
  setUnauthorizedHandler(unauthorized);
  vi.stubGlobal("fetch", vi.fn(() => delayedResponse));

  const incidents = getIncidents();
  advanceRequestScope();
  resolveRequest({
    ok: false,
    status: 401,
    json: async () => ({ detail: "Authentication required" }),
  } as Response);

  await expect(incidents).rejects.toMatchObject({ status: 401 });
  expect(unauthorized).not.toHaveBeenCalled();
});

test("keeps auth-session reads pure until the caller commits the CSRF token", async () => {
  const fetchMock = vi.fn<
    (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>
  >(async (input) => ({
      ok: true,
      status: 200,
      json: async () => String(input).endsWith("/auth/session")
        ? { csrf_token: "uncommitted-new-token" }
        : {},
    } as Response));
  vi.stubGlobal("fetch", fetchMock);
  setSessionCsrfToken("committed-old-token");

  await getAuthSession();
  await transitionIncident("incident-1", "investigating", 1, "Scope proof");

  expect(
    new Headers(fetchMock.mock.calls[1][1]?.headers).get("X-CSRF-Token"),
  ).toBe("committed-old-token");
});

test("uses the tenant-scoped connector custody contract and signs every write", async () => {
  const fetchMock = vi.fn<
    (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>
  >(async () => ({
      ok: true,
      status: 200,
      json: async () => ({}),
    } as Response));
  vi.stubGlobal("fetch", fetchMock);
  setSessionCsrfToken("connector-csrf");

  await getConnectors();
  await getConnector("connector-1");
  await getConnectorEvents("connector-1");
  await createConnector({
    name: "Production GitHub",
    provider: "github",
    configuration: {
      repository: "pageragent/core",
      app_id: "42",
      installation_id: "84",
    },
    credentials: { private_key: "sentinel-private-key" },
  });
  await updateConnector("connector-1", {
    expected_version: 4,
    name: "Primary GitHub",
    enabled: false,
  });
  await rotateConnectorCredentials("connector-1", 5, {
    private_key: "replacement-private-key",
  });
  await validateConnector("connector-1", 6);

  expect(fetchMock.mock.calls.map(([input]) => String(input))).toEqual([
    "/api/v1/connectors",
    "/api/v1/connectors/connector-1",
    "/api/v1/connectors/connector-1/events",
    "/api/v1/connectors",
    "/api/v1/connectors/connector-1",
    "/api/v1/connectors/connector-1/credentials",
    "/api/v1/connectors/connector-1/validate",
  ]);

  const writeCalls = fetchMock.mock.calls.slice(3);
  expect(writeCalls.map(([, init]) => init?.method)).toEqual(["POST", "PATCH", "PUT", "POST"]);
  for (const [, init] of writeCalls) {
    expect(init?.credentials).toBe("include");
    expect(new Headers(init?.headers).get("X-CSRF-Token")).toBe("connector-csrf");
  }
  expect(JSON.parse(String(writeCalls[0][1]?.body))).toEqual({
    name: "Production GitHub",
    provider: "github",
    configuration: {
      repository: "pageragent/core",
      app_id: "42",
      installation_id: "84",
    },
    credentials: { private_key: "sentinel-private-key" },
  });
  expect(JSON.parse(String(writeCalls[2][1]?.body))).toEqual({
    expected_version: 5,
    credentials: { private_key: "replacement-private-key" },
  });
  expect(JSON.parse(String(writeCalls[3][1]?.body))).toEqual({ expected_version: 6 });
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

test("freezes collaboration drafts and signs separate publication decisions", async () => {
  const fetchMock = vi.fn<
    (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>
  >(async () => ({
      ok: true,
      status: 200,
      json: async () => ([]),
    } as Response));
  vi.stubGlobal("fetch", fetchMock);
  setSessionCsrfToken("collaboration-csrf");
  const proposal = {
    id: "proposal-1",
    input_hash: "a".repeat(64),
  } as MitigationProposal;
  const output = {
    id: "output-1",
    version: 3,
    content_sha256: "b".repeat(64),
  } as CollaborationOutput;

  await getCollaborationOutputs("incident-1");
  await prepareCollaborationOutputs("incident-1", proposal, ["slack_update", "github_issue"]);
  await decideCollaborationOutput(output, "approve", "Destination and content reviewed.");

  expect(fetchMock.mock.calls.map(([input]) => String(input))).toEqual([
    "/api/v1/incidents/incident-1/collaboration-outputs",
    "/api/v1/incidents/incident-1/collaboration-outputs",
    "/api/v1/collaboration-outputs/output-1/decisions",
  ]);
  expect(JSON.parse(String(fetchMock.mock.calls[1][1]?.body))).toEqual({
    proposal_id: "proposal-1",
    expected_proposal_hash: "a".repeat(64),
    kinds: ["slack_update", "github_issue"],
  });
  expect(JSON.parse(String(fetchMock.mock.calls[2][1]?.body))).toEqual({
    decision: "approve",
    expected_version: 3,
    expected_content_sha256: "b".repeat(64),
    note: "Destination and content reviewed.",
  });
  for (const [, init] of fetchMock.mock.calls.slice(1)) {
    expect(new Headers(init?.headers).get("X-CSRF-Token")).toBe("collaboration-csrf");
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
