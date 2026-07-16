import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { ConnectorCustodyPanel } from "./ConnectorCustodyPanel";
import {
  setSessionCsrfToken,
  type AuthSession,
  type ConnectorDetail,
  type ConnectorEvent,
} from "../lib/api";

const baseSession: AuthSession = {
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
  memberships: [],
  permissions: ["connectors.read"],
  csrf_token: "connector-csrf",
};

const adminSession: AuthSession = {
  ...baseSession,
  active_organization: { ...baseSession.active_organization, role: "admin" },
  permissions: ["connectors.read", "connectors.manage", "connectors.validate"],
};

const viewerSession: AuthSession = {
  ...baseSession,
  active_organization: { ...baseSession.active_organization, role: "viewer" },
  permissions: ["incidents.read"],
};

const connector: ConnectorDetail = {
  id: "11111111-1111-4111-8111-111111111111",
  name: "Production evidence",
  provider: "github",
  status: "disabled",
  enabled: false,
  configuration: {
    service: "checkout-api",
    repository: "pageragent/core",
    app_id: "42",
    installation_id: "84",
    issue_creation_enabled: false,
  },
  credential_fields: ["private_key"],
  credential_version: 1,
  version: 3,
  last_validated_at: null,
  last_validation_ok: null,
  last_validation_message: null,
  created_at: "2026-07-15T13:00:00Z",
  updated_at: "2026-07-15T13:00:00Z",
};

const auditEvent: ConnectorEvent = {
  id: "22222222-2222-4222-8222-222222222222",
  event_type: "connector.credentials_updated",
  actor: "user:00000000-0000-0000-0000-000000000101",
  connector_version: 3,
  payload: {
    credential_fields: ["private_key"],
    never_render_this_value: "server-sentinel-secret",
  },
  created_at: "2026-07-15T13:05:00Z",
};

function jsonResponse(body: unknown): Response {
  return { ok: true, status: 200, json: async () => body } as Response;
}

function errorResponse(status: number, message: string): Response {
  return {
    ok: false,
    status,
    json: async () => ({ detail: { code: "invalid_connector", message } }),
  } as Response;
}

let rejectRotation = false;

beforeEach(() => {
  rejectRotation = false;
  setSessionCsrfToken("connector-csrf");
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input);
    if (path.endsWith("/events")) return jsonResponse([auditEvent]);
    if (path.endsWith("/credentials") && init?.method === "PUT") {
      if (rejectRotation) {
        return errorResponse(400, "Provider rejected ui-sentinel-private-key");
      }
      return jsonResponse({ ...connector, credential_version: 2, version: 4 });
    }
    if (path.endsWith("/validate") && init?.method === "POST") {
      return jsonResponse({
        ...connector,
        status: "configured",
        version: 4,
        last_validated_at: "2026-07-15T13:10:00Z",
        last_validation_ok: true,
        last_validation_message: "Credential accepted",
      });
    }
    if (path.endsWith(`/connectors/${connector.id}`)) return jsonResponse(connector);
    if (path.endsWith("/connectors") && init?.method === "POST") {
      return jsonResponse({
        ...connector,
        id: "33333333-3333-4333-8333-333333333333",
        name: "Incident communications",
        provider: "slack",
        configuration: { service: "checkout-api", channel: "C0123456789" },
        credential_fields: ["bot_token"],
        version: 1,
      });
    }
    if (path.endsWith("/connectors")) return jsonResponse([connector]);
    return jsonResponse(connector);
  }));
});

afterEach(() => {
  cleanup();
  setSessionCsrfToken(null);
  vi.unstubAllGlobals();
});

test("does not request connector data below the read boundary", () => {
  render(<ConnectorCustodyPanel session={viewerSession} />);

  expect(screen.getByRole("heading", { name: "The vault stays outside your authority." })).toBeInTheDocument();
  expect(screen.getByText("connectors.read")).toBeInTheDocument();
  expect(fetch).not.toHaveBeenCalled();
});

test("gives an incident commander a value-free custody receipt and audit", async () => {
  render(<ConnectorCustodyPanel session={baseSession} />);

  expect(await screen.findByRole("heading", { name: connector.name })).toBeInTheDocument();
  expect(screen.getByRole("list", { name: "Credential custody chain" })).toBeInTheDocument();
  expect(screen.getAllByText("private_key").length).toBeGreaterThan(0);
  expect(screen.getByLabelText("Sealed credential")).toHaveTextContent("Sealed · write-only");
  expect(
    screen.getByText("user:00000000-0000-0000-0000-000000000101 · record v3"),
  ).toBeInTheDocument();
  expect(screen.getByText("credential_fields · never_render_this_value")).toBeInTheDocument();
  expect(screen.queryByText("server-sentinel-secret")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Rotate credentials" })).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Validate custody" })).toBeDisabled();
  expect(screen.getByText("connectors.manage")).toBeInTheDocument();
  expect(screen.getByText("connectors.validate")).toBeInTheDocument();
});

test("creates the selected provider contract with a blank-after-submit secret input", async () => {
  render(<ConnectorCustodyPanel session={adminSession} />);
  await screen.findByRole("heading", { name: connector.name });

  fireEvent.click(screen.getByRole("button", { name: "New contract" }));
  const form = screen.getByRole("heading", { name: "Declare a provider contract." }).closest("form");
  expect(form).not.toBeNull();
  const createForm = within(form!);
  fireEvent.change(createForm.getByLabelText("Connector name"), {
    target: { value: "Incident communications" },
  });
  fireEvent.change(createForm.getByLabelText("Provider"), { target: { value: "slack" } });
  fireEvent.change(createForm.getByLabelText(/Service binding/), {
    target: { value: "checkout-api" },
  });
  fireEvent.change(createForm.getByLabelText(/Channel ID/), {
    target: { value: "C0123456789" },
  });
  fireEvent.change(createForm.getByLabelText("Bot token (bot_token, write only)"), {
    target: { value: "xoxb-ui-sentinel" },
  });
  fireEvent.click(createForm.getByRole("button", { name: "Seal custody record" }));

  await waitFor(() => {
    expect(screen.queryByRole("heading", { name: "Declare a provider contract." })).not.toBeInTheDocument();
  });
  const createCall = vi.mocked(fetch).mock.calls.find(([input, init]) =>
    String(input).endsWith("/connectors") && init?.method === "POST"
  );
  expect(JSON.parse(String(createCall?.[1]?.body))).toEqual({
    name: "Incident communications",
    provider: "slack",
    configuration: { service: "checkout-api", channel: "C0123456789" },
    credentials: { bot_token: "xoxb-ui-sentinel" },
  });
  expect(document.body).not.toHaveTextContent("xoxb-ui-sentinel");
});

test("includes the service binding in a write-only Prometheus connector contract", async () => {
  const bearerToken = "prometheus-ui-token-sentinel";
  render(<ConnectorCustodyPanel session={adminSession} />);
  await screen.findByRole("heading", { name: connector.name });

  fireEvent.click(screen.getByRole("button", { name: "New contract" }));
  const form = screen.getByRole("heading", { name: "Declare a provider contract." }).closest("form");
  expect(form).not.toBeNull();
  const createForm = within(form!);
  fireEvent.change(createForm.getByLabelText("Connector name"), {
    target: { value: "Checkout metrics evidence" },
  });
  fireEvent.change(createForm.getByLabelText("Provider"), { target: { value: "prometheus" } });
  fireEvent.change(createForm.getByLabelText(/Service binding/), {
    target: { value: "checkout-api" },
  });
  fireEvent.change(createForm.getByLabelText(/Base URL/), {
    target: { value: "https://metrics.example.com" },
  });
  fireEvent.change(createForm.getByLabelText("Bearer token (bearer_token, write only)"), {
    target: { value: bearerToken },
  });
  fireEvent.click(createForm.getByRole("button", { name: "Seal custody record" }));

  await waitFor(() => {
    expect(screen.queryByRole("heading", { name: "Declare a provider contract." })).not.toBeInTheDocument();
  });
  const createCall = vi.mocked(fetch).mock.calls.find(([input, init]) =>
    String(input).endsWith("/connectors") && init?.method === "POST"
  );
  expect(JSON.parse(String(createCall?.[1]?.body))).toEqual({
    name: "Checkout metrics evidence",
    provider: "prometheus",
    configuration: {
      service: "checkout-api",
      base_url: "https://metrics.example.com",
    },
    credentials: { bearer_token: bearerToken },
  });
  expect(document.body).not.toHaveTextContent(bearerToken);
});

test("preserves an LF PEM exactly when sealing the current GitHub App contract", async () => {
  const privateKey = "-----BEGIN PRIVATE KEY-----\nline-one\nline-two\n-----END PRIVATE KEY-----\n";
  const webhookSecret = "github-webhook-secret-with-enough-entropy";
  render(<ConnectorCustodyPanel session={adminSession} />);
  await screen.findByRole("heading", { name: connector.name });

  fireEvent.click(screen.getByRole("button", { name: "New contract" }));
  const form = screen.getByRole("heading", { name: "Declare a provider contract." }).closest("form");
  expect(form).not.toBeNull();
  const createForm = within(form!);
  fireEvent.change(createForm.getByLabelText("Connector name"), {
    target: { value: "Checkout repository evidence" },
  });
  fireEvent.change(createForm.getByLabelText(/Service binding/), {
    target: { value: "checkout-api" },
  });
  fireEvent.change(createForm.getByLabelText(/Repository/), {
    target: { value: "pageragent/core" },
  });
  fireEvent.change(createForm.getByLabelText(/App ID/), { target: { value: "42" } });
  fireEvent.change(createForm.getByLabelText(/Installation ID/), { target: { value: "84" } });
  fireEvent.click(createForm.getByRole("checkbox", { name: /Allow incident issue creation/ }));
  const privateKeyInput = createForm.getByLabelText("Private key (private_key, write only)");
  expect(privateKeyInput.tagName).toBe("TEXTAREA");
  fireEvent.paste(privateKeyInput, {
    clipboardData: { getData: () => privateKey },
  });
  fireEvent.change(createForm.getByLabelText("Webhook secret (webhook_secret, write only)"), {
    target: { value: webhookSecret },
  });
  fireEvent.click(createForm.getByRole("button", { name: "Seal custody record" }));

  await waitFor(() => {
    expect(screen.queryByRole("heading", { name: "Declare a provider contract." })).not.toBeInTheDocument();
  });
  const createCall = vi.mocked(fetch).mock.calls.find(([input, init]) =>
    String(input).endsWith("/connectors") && init?.method === "POST"
  );
  expect(JSON.parse(String(createCall?.[1]?.body))).toEqual({
    name: "Checkout repository evidence",
    provider: "github",
    configuration: {
      service: "checkout-api",
      repository: "pageragent/core",
      app_id: "42",
      installation_id: "84",
      issue_creation_enabled: true,
    },
    credentials: { private_key: privateKey, webhook_secret: webhookSecret },
  });
  expect(screen.queryByDisplayValue(privateKey)).not.toBeInTheDocument();
  expect(screen.queryByDisplayValue(webhookSecret)).not.toBeInTheDocument();
  expect(document.body).not.toHaveTextContent(webhookSecret);
});

test("unions legacy GitHub credential fields and preserves a CRLF PEM during rotation", async () => {
  const privateKey = "-----BEGIN PRIVATE KEY-----\r\nlegacy-line\r\n-----END PRIVATE KEY-----\r\n";
  const webhookSecret = "rotated-github-webhook-secret-with-entropy";
  render(<ConnectorCustodyPanel session={adminSession} />);
  await screen.findByRole("heading", { name: connector.name });

  const privateKeyInput = screen.getByLabelText("Private key (private_key, write only)");
  const webhookSecretInput = screen.getByLabelText("Webhook secret (webhook_secret, write only)");
  expect(privateKeyInput.tagName).toBe("TEXTAREA");
  expect(webhookSecretInput).toHaveAttribute("type", "password");
  fireEvent.paste(privateKeyInput, {
    clipboardData: { getData: () => privateKey },
  });
  fireEvent.change(webhookSecretInput, { target: { value: webhookSecret } });
  fireEvent.click(screen.getByRole("button", { name: "Rotate credentials" }));

  await waitFor(() => {
    expect(
      vi.mocked(fetch).mock.calls.some(([input, init]) =>
        String(input).endsWith("/credentials") && init?.method === "PUT"
      ),
    ).toBe(true);
  });
  const rotationCall = vi.mocked(fetch).mock.calls.find(([input, init]) =>
    String(input).endsWith("/credentials") && init?.method === "PUT"
  );
  expect(JSON.parse(String(rotationCall?.[1]?.body))).toEqual({
    expected_version: connector.version,
    credentials: { private_key: privateKey, webhook_secret: webhookSecret },
  });
  expect(screen.getByLabelText("Private key (private_key, write only)")).toHaveValue("");
  expect(screen.getByLabelText("Webhook secret (webhook_secret, write only)")).toHaveValue("");
  expect(document.body).not.toHaveTextContent(webhookSecret);
});

test("clears a replacement secret and sanitizes a provider error", async () => {
  rejectRotation = true;
  render(<ConnectorCustodyPanel session={adminSession} />);
  await screen.findByRole("heading", { name: connector.name });

  const secretInput = screen.getByLabelText("Private key (private_key, write only)");
  fireEvent.change(secretInput, { target: { value: "ui-sentinel-private-key" } });
  fireEvent.change(screen.getByLabelText("Webhook secret (webhook_secret, write only)"), {
    target: { value: "ui-sentinel-webhook-secret-with-entropy" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Rotate credentials" }));

  expect(await screen.findByRole("alert")).toHaveTextContent("Credentials could not be rotated.");
  expect(screen.getByLabelText("Private key (private_key, write only)")).toHaveValue("");
  expect(screen.getByLabelText("Webhook secret (webhook_secret, write only)")).toHaveValue("");
  expect(document.body).not.toHaveTextContent("ui-sentinel-private-key");
});

test("renders the sanitized GitHub handshake receipt and allows enablement only after success", async () => {
  const handshake = {
    ...connector,
    status: "configured" as const,
    last_validated_at: "2026-07-15T13:10:00Z",
    last_validation_ok: true,
    last_validation_message: "Authenticated installation <84>; repository access confirmed.",
  };
  vi.mocked(fetch).mockImplementation(async (input: RequestInfo | URL) => {
    const path = String(input);
    if (path.endsWith("/events")) return jsonResponse([auditEvent]);
    if (path.endsWith(`/connectors/${connector.id}`)) return jsonResponse(handshake);
    if (path.endsWith("/connectors")) return jsonResponse([handshake]);
    return jsonResponse(handshake);
  });

  render(<ConnectorCustodyPanel session={adminSession} />);

  expect(await screen.findByText("GitHub App provider handshake succeeded.")).toBeInTheDocument();
  expect(
    screen.getByText("Validation receipt: Authenticated installation <84>; repository access confirmed."),
  ).toBeInTheDocument();
  expect(document.querySelector(".custody-validation script")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Enable connector" })).toBeEnabled();
});

test("renders a sanitized Prometheus handshake receipt", async () => {
  const prometheusConnector: ConnectorDetail = {
    ...connector,
    provider: "prometheus",
    name: "Checkout metrics evidence",
    status: "configured",
    configuration: {
      service: "checkout-api",
      base_url: "https://metrics.example.com",
    },
    credential_fields: ["bearer_token"],
    last_validated_at: "2026-07-16T16:20:00Z",
    last_validation_ok: true,
    last_validation_message: "Prometheus query API and metric catalog are readable.",
  };
  vi.mocked(fetch).mockImplementation(async (input: RequestInfo | URL) => {
    const path = String(input);
    if (path.endsWith("/events")) return jsonResponse([auditEvent]);
    if (path.endsWith(`/connectors/${prometheusConnector.id}`)) {
      return jsonResponse(prometheusConnector);
    }
    if (path.endsWith("/connectors")) return jsonResponse([prometheusConnector]);
    return jsonResponse(prometheusConnector);
  });

  render(<ConnectorCustodyPanel session={adminSession} />);

  expect(await screen.findByText("Prometheus provider handshake succeeded.")).toBeInTheDocument();
  expect(
    screen.getByText(
      "Validation receipt: Prometheus query API and metric catalog are readable.",
    ),
  ).toBeInTheDocument();
  expect(screen.getAllByText("checkout-api").length).toBeGreaterThan(0);
  expect(screen.getByRole("button", { name: "Enable connector" })).toBeEnabled();
});

test("keeps enablement unavailable when the server records an unsuccessful validation", async () => {
  const invalid = {
    ...connector,
    status: "invalid" as const,
    last_validated_at: "2026-07-15T13:10:00Z",
    last_validation_ok: false,
    last_validation_message: "GitHub App installation is not authorized for pageragent/core.",
  };
  vi.mocked(fetch).mockImplementation(async (input: RequestInfo | URL) => {
    const path = String(input);
    if (path.endsWith("/events")) return jsonResponse([auditEvent]);
    if (path.endsWith(`/connectors/${connector.id}`)) return jsonResponse(invalid);
    if (path.endsWith("/connectors")) return jsonResponse([invalid]);
    return jsonResponse(invalid);
  });

  render(<ConnectorCustodyPanel session={adminSession} />);

  expect(await screen.findByText(/Validation failed/)).toBeInTheDocument();
  expect(
    screen.getByText("Validation receipt: GitHub App installation is not authorized for pageragent/core."),
  ).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Enable connector" })).toBeDisabled();
});

test("drops transient secret state when manage authority is refreshed away", async () => {
  const view = render(<ConnectorCustodyPanel session={adminSession} />);
  await screen.findByRole("heading", { name: connector.name });

  fireEvent.change(screen.getByLabelText("Private key (private_key, write only)"), {
    target: { value: "authority-loss-sentinel" },
  });
  view.rerender(<ConnectorCustodyPanel session={baseSession} />);
  expect(screen.queryByRole("button", { name: "Rotate credentials" })).not.toBeInTheDocument();

  view.rerender(<ConnectorCustodyPanel session={adminSession} />);
  expect(screen.getByLabelText("Private key (private_key, write only)")).toHaveValue("");
  expect(document.body).not.toHaveTextContent("authority-loss-sentinel");
});

test("keeps a slower previous receipt from replacing or mutating the current connector", async () => {
  const slowConnector: ConnectorDetail = {
    ...connector,
    id: "44444444-4444-4444-8444-444444444444",
    name: "Slow GitHub receipt",
  };
  const currentConnector: ConnectorDetail = {
    ...connector,
    id: "55555555-5555-4555-8555-555555555555",
    name: "Current GitHub receipt",
    configuration: {
      ...connector.configuration,
      repository: "pageragent/current",
    },
  };
  let resolveSlowReceipt!: (response: Response) => void;
  const slowReceipt = new Promise<Response>((resolve) => {
    resolveSlowReceipt = resolve;
  });
  const raceFetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input);
    if (path.endsWith(`/connectors/${currentConnector.id}`) && init?.method === "PATCH") {
      return jsonResponse({
        ...currentConnector,
        name: "Current receipt renamed",
        version: currentConnector.version + 1,
      });
    }
    if (path.endsWith(`/connectors/${slowConnector.id}/events`)) {
      return jsonResponse([{ ...auditEvent, connector_version: slowConnector.version }]);
    }
    if (path.endsWith(`/connectors/${currentConnector.id}/events`)) {
      return jsonResponse([{
        ...auditEvent,
        actor: "user:current@pageragent.dev",
        connector_version: currentConnector.version,
      }]);
    }
    if (path.endsWith(`/connectors/${slowConnector.id}`)) return slowReceipt;
    if (path.endsWith(`/connectors/${currentConnector.id}`)) return jsonResponse(currentConnector);
    if (path.endsWith("/connectors")) return jsonResponse([slowConnector, currentConnector]);
    return jsonResponse([]);
  });
  vi.stubGlobal("fetch", raceFetch);

  render(<ConnectorCustodyPanel session={adminSession} />);
  fireEvent.click(await screen.findByRole("button", { name: /Current GitHub receipt/ }));

  expect(
    await screen.findByRole("heading", { name: currentConnector.name }),
  ).toBeInTheDocument();
  expect(screen.getByText("user:current@pageragent.dev · record v3")).toBeInTheDocument();

  await act(async () => {
    resolveSlowReceipt(jsonResponse(slowConnector));
    await slowReceipt;
  });
  expect(
    screen.queryByRole("heading", { name: slowConnector.name }),
  ).not.toBeInTheDocument();
  expect(screen.getByRole("heading", { name: currentConnector.name })).toBeInTheDocument();

  fireEvent.change(screen.getByLabelText("Connector name"), {
    target: { value: "Current receipt renamed" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Save contract" }));

  await waitFor(() => {
    expect(
      raceFetch.mock.calls.some(([input, init]) =>
        String(input).endsWith(`/connectors/${currentConnector.id}`) && init?.method === "PATCH"
      ),
    ).toBe(true);
  });
  expect(
    raceFetch.mock.calls.some(([input, init]) =>
      String(input).endsWith(`/connectors/${slowConnector.id}`) && init?.method === "PATCH"
    ),
  ).toBe(false);
});
