import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { setSessionCsrfToken, type AuthSession, type MembershipDetail } from "../lib/api";
import { OrganizationAccessPanel } from "./OrganizationAccessPanel";

const adminId = "00000000-0000-0000-0000-000000000101";
const targetId = "00000000-0000-0000-0000-000000000202";

const adminSession: AuthSession = {
  user: {
    id: adminId,
    email: "avery@example.test",
    display_name: "Avery Admin",
  },
  active_organization: {
    id: "00000000-0000-0000-0000-000000000001",
    slug: "pageragent-labs",
    name: "PagerAgent Labs",
    role: "admin",
  },
  memberships: [],
  permissions: ["memberships.read", "memberships.manage"],
  csrf_token: "membership-csrf",
};

const viewerSession: AuthSession = {
  ...adminSession,
  active_organization: { ...adminSession.active_organization, role: "viewer" },
  permissions: ["incidents.read"],
};

const memberships: MembershipDetail[] = [
  {
    organization_id: adminSession.active_organization.id,
    user: {
      id: adminId,
      issuer: "https://identity.example.test",
      subject: "admin-subject",
      email: "avery@example.test",
      display_name: "Avery Admin",
      is_active: true,
    },
    role: "admin",
    is_active: true,
    version: 1,
    created_at: "2026-07-16T12:00:00Z",
    updated_at: "2026-07-16T12:00:00Z",
  },
  {
    organization_id: adminSession.active_organization.id,
    user: {
      id: targetId,
      issuer: "https://identity.example.test",
      subject: "responder-subject",
      email: "riley@example.test",
      display_name: "Riley Responder",
      is_active: true,
    },
    role: "responder",
    is_active: true,
    version: 3,
    created_at: "2026-07-16T12:01:00Z",
    updated_at: "2026-07-16T12:02:00Z",
  },
];

const audit = [{
  id: "00000000-0000-0000-0000-000000000303",
  organization_id: adminSession.active_organization.id,
  target_user_id: targetId,
  event_type: "membership.provisioned",
  actor: `user:${adminId}`,
  membership_version: 1,
  payload: { role: "responder", is_active: true },
  created_at: "2026-07-16T12:01:00Z",
}];

function jsonResponse(body: unknown): Response {
  return { ok: true, status: 200, json: async () => body } as Response;
}

let conflict = false;

beforeEach(() => {
  conflict = false;
  setSessionCsrfToken("membership-csrf");
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input);
    if (path.endsWith("/memberships/audit")) return jsonResponse(audit);
    if (path.endsWith(`/memberships/${targetId}`) && init?.method === "PATCH") {
      if (conflict) {
        return {
          ok: false,
          status: 409,
          json: async () => ({
            detail: {
              code: "membership_version_conflict",
              message: "Membership changed; current version is 4",
              current_version: 4,
            },
          }),
        } as Response;
      }
      return jsonResponse({ ...memberships[1], role: "incident_commander", version: 4 });
    }
    if (path.endsWith("/memberships")) return jsonResponse(memberships);
    return jsonResponse({});
  }));
});

afterEach(() => {
  cleanup();
  setSessionCsrfToken(null);
  vi.unstubAllGlobals();
});

test("records an optimistic role change with the current version and CSRF proof", async () => {
  render(<OrganizationAccessPanel session={adminSession} />);

  const role = await screen.findByLabelText("Role for Riley Responder");
  fireEvent.change(role, { target: { value: "incident_commander" } });
  fireEvent.click(screen.getByRole("button", { name: "Save access for Riley Responder" }));

  await waitFor(() => {
    expect(vi.mocked(fetch).mock.calls.some(([, init]) => init?.method === "PATCH")).toBe(true);
  });
  const updateCall = vi.mocked(fetch).mock.calls.find(([input, init]) =>
    String(input).endsWith(`/memberships/${targetId}`) && init?.method === "PATCH"
  );
  expect(JSON.parse(String(updateCall?.[1]?.body))).toEqual({
    expected_version: 3,
    role: "incident_commander",
    is_active: true,
  });
  expect(new Headers(updateCall?.[1]?.headers).get("X-CSRF-Token")).toBe("membership-csrf");
  expect(screen.getByRole("list", { name: "Identity administration receipts" })).toHaveTextContent(
    "Membership provisioned",
  );
});

test("refreshes the authority ledger and explains a stale-version conflict", async () => {
  conflict = true;
  render(<OrganizationAccessPanel session={adminSession} />);

  const role = await screen.findByLabelText("Role for Riley Responder");
  fireEvent.change(role, { target: { value: "incident_commander" } });
  fireEvent.click(screen.getByRole("button", { name: "Save access for Riley Responder" }));

  expect(await screen.findByRole("alert")).toHaveTextContent(
    "Membership changed; current version is 4 The organization access ledger was refreshed.",
  );
  expect(screen.getByLabelText("Role for Riley Responder")).toHaveValue("responder");
  expect(
    vi.mocked(fetch).mock.calls.filter(([input]) => String(input).endsWith("/memberships")),
  ).toHaveLength(2);
});

test("does not fetch or render administration controls without the read grant", () => {
  render(<OrganizationAccessPanel session={viewerSession} />);

  expect(
    screen.getByRole("heading", { name: "The identity ledger stays outside your authority." }),
  ).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Provision membership" })).not.toBeInTheDocument();
  expect(fetch).not.toHaveBeenCalled();
});
