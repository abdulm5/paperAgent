import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";

import {
  ApiError,
  getMembershipAudit,
  getMemberships,
  hasPermission,
  provisionMembership,
  updateMembership,
  type AuthSession,
  type IdentityAuditEvent,
  type MembershipDetail,
  type MembershipRole,
} from "../lib/api";
import { formatTimestamp, titleCase } from "../lib/format";

interface OrganizationAccessPanelProps {
  session: AuthSession;
}

interface MembershipDraft {
  role: MembershipRole;
  isActive: boolean;
}

const ROLES: MembershipRole[] = ["viewer", "responder", "incident_commander", "admin"];

function draftsFor(memberships: MembershipDetail[]): Record<string, MembershipDraft> {
  return Object.fromEntries(
    memberships.map((membership) => [
      membership.user.id,
      { role: membership.role, isActive: membership.is_active },
    ]),
  );
}

function errorMessage(error: unknown, fallback: string): string {
  if (error instanceof ApiError && error.status === 409) {
    return `${error.message} The organization access ledger was refreshed.`;
  }
  return error instanceof Error ? error.message : fallback;
}

export function OrganizationAccessPanel({ session }: OrganizationAccessPanelProps) {
  const canRead = hasPermission(session, "memberships.read");
  const canManage = hasPermission(session, "memberships.manage");
  const [memberships, setMemberships] = useState<MembershipDetail[]>([]);
  const [audit, setAudit] = useState<IdentityAuditEvent[]>([]);
  const [drafts, setDrafts] = useState<Record<string, MembershipDraft>>({});
  const [loading, setLoading] = useState(false);
  const [acting, setActing] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [issuer, setIssuer] = useState("");
  const [subject, setSubject] = useState("");
  const [email, setEmail] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [role, setRole] = useState<MembershipRole>("viewer");
  const organizationRef = useRef(session.active_organization.id);
  const generationRef = useRef(0);
  organizationRef.current = session.active_organization.id;

  const loadLedger = useCallback(async (organizationId: string) => {
    const generation = ++generationRef.current;
    setLoading(true);
    try {
      const [nextMemberships, nextAudit] = await Promise.all([
        getMemberships(),
        getMembershipAudit(),
      ]);
      if (
        generation !== generationRef.current
        || organizationId !== organizationRef.current
      ) return;
      setMemberships(nextMemberships);
      setDrafts(draftsFor(nextMemberships));
      setAudit(nextAudit);
      setError(null);
    } catch (nextError) {
      if (
        generation !== generationRef.current
        || organizationId !== organizationRef.current
      ) return;
      setError(errorMessage(nextError, "Organization access records are unavailable."));
    } finally {
      if (
        generation === generationRef.current
        && organizationId === organizationRef.current
      ) setLoading(false);
    }
  }, []);

  useEffect(() => {
    generationRef.current += 1;
    setMemberships([]);
    setAudit([]);
    setDrafts({});
    setError(null);
    setActing(null);
    setLoading(false);
    if (!canRead) return;
    void loadLedger(session.active_organization.id);
    return () => {
      generationRef.current += 1;
    };
  }, [canRead, loadLedger, session.active_organization.id]);

  if (!canRead) {
    return (
      <section className="access-boundary" aria-labelledby="access-boundary-title">
        <p className="eyebrow">Organization access</p>
        <h1 id="access-boundary-title">The identity ledger stays outside your authority.</h1>
        <p>Only organization administrators receive the <code>memberships.read</code> grant.</p>
      </section>
    );
  }

  async function handleProvision(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const organizationId = organizationRef.current;
    setActing("provision");
    setError(null);
    try {
      await provisionMembership({
        issuer,
        subject,
        email,
        display_name: displayName,
        role,
      });
      if (organizationId !== organizationRef.current) return;
      setSubject("");
      setEmail("");
      setDisplayName("");
      setRole("viewer");
      await loadLedger(organizationId);
    } catch (nextError) {
      if (organizationId !== organizationRef.current) return;
      const message = errorMessage(nextError, "The stable identity could not be provisioned.");
      if (nextError instanceof ApiError && nextError.status === 409) {
        await loadLedger(organizationId);
      }
      if (organizationId === organizationRef.current) setError(message);
    } finally {
      if (organizationId === organizationRef.current) setActing(null);
    }
  }

  async function handleUpdate(membership: MembershipDetail) {
    const organizationId = organizationRef.current;
    const draft = drafts[membership.user.id];
    if (!draft) return;
    setActing(membership.user.id);
    setError(null);
    try {
      await updateMembership(membership.user.id, {
        expected_version: membership.version,
        role: draft.role,
        is_active: draft.isActive,
      });
      if (organizationId !== organizationRef.current) return;
      await loadLedger(organizationId);
    } catch (nextError) {
      if (organizationId !== organizationRef.current) return;
      const message = errorMessage(nextError, "The membership change could not be recorded.");
      if (nextError instanceof ApiError && nextError.status === 409) {
        await loadLedger(organizationId);
      }
      if (organizationId === organizationRef.current) setError(message);
    } finally {
      if (organizationId === organizationRef.current) setActing(null);
    }
  }

  const memberNames = new Map(
    memberships.map((membership) => [membership.user.id, membership.user.display_name]),
  );

  return (
    <section className="organization-access" aria-labelledby="organization-access-title">
      <header className="access-masthead">
        <div>
          <p className="eyebrow">Hosted identity administration</p>
          <h1 id="organization-access-title">Who can cross this tenant boundary.</h1>
          <p>
            Memberships attach to an issuer and stable subject. Email is profile data, never an
            account-linking key.
          </p>
        </div>
        <div className="access-count" aria-label={`${memberships.length} membership records`}>
          <strong>{String(memberships.length).padStart(2, "0")}</strong>
          <span>membership records</span>
        </div>
      </header>

      {error ? <p className="access-error" role="alert">{error}</p> : null}

      {canManage ? (
        <form className="access-provision" onSubmit={handleProvision}>
          <header>
            <div>
              <p className="utility-label">Stable identity binding</p>
              <h2>Provision an organization member.</h2>
            </div>
            <span>Admin write / audited</span>
          </header>
          <div className="access-form-grid">
            <label>
              OIDC issuer
              <input
                autoComplete="url"
                maxLength={500}
                onChange={(event) => setIssuer(event.target.value)}
                placeholder="https://identity.pageragent.local"
                required
                type="url"
                value={issuer}
              />
            </label>
            <label>
              Stable subject
              <input
                autoComplete="off"
                maxLength={500}
                onChange={(event) => setSubject(event.target.value)}
                required
                value={subject}
              />
            </label>
            <label>
              Display name
              <input
                autoComplete="name"
                maxLength={200}
                onChange={(event) => setDisplayName(event.target.value)}
                required
                value={displayName}
              />
            </label>
            <label>
              Email claim
              <input
                autoComplete="email"
                maxLength={320}
                onChange={(event) => setEmail(event.target.value)}
                required
                type="email"
                value={email}
              />
            </label>
            <label>
              Initial role
              <select onChange={(event) => setRole(event.target.value as MembershipRole)} value={role}>
                {ROLES.map((item) => <option key={item} value={item}>{titleCase(item)}</option>)}
              </select>
            </label>
          </div>
          <footer>
            <p>The server accepts only its configured issuer and writes version 1 atomically.</p>
            <button disabled={acting !== null} type="submit">
              {acting === "provision" ? "Recording identity…" : "Provision membership"}
            </button>
          </footer>
        </form>
      ) : null}

      <section className="access-ledger" aria-labelledby="membership-ledger-title">
        <header>
          <div>
            <p className="utility-label">Current authority</p>
            <h2 id="membership-ledger-title">Versioned memberships</h2>
          </div>
          {loading ? <span aria-live="polite">Reconciling…</span> : <span>Tenant scoped</span>}
        </header>
        <div className="access-table-scroll">
          <table>
            <thead>
              <tr>
                <th>Identity</th>
                <th>Stable binding</th>
                <th>Role</th>
                <th>Status</th>
                <th>Receipt</th>
              </tr>
            </thead>
            <tbody>
              {memberships.map((membership) => {
                const draft = drafts[membership.user.id] ?? {
                  role: membership.role,
                  isActive: membership.is_active,
                };
                const isSelf = membership.user.id === session.user.id;
                const changed = draft.role !== membership.role
                  || draft.isActive !== membership.is_active;
                return (
                  <tr key={membership.user.id}>
                    <td>
                      <strong>{membership.user.display_name}</strong>
                      <small>{membership.user.email}</small>
                    </td>
                    <td>
                      <code title={membership.user.issuer}>{membership.user.issuer}</code>
                      <small title={membership.user.subject}>sub / {membership.user.subject}</small>
                    </td>
                    <td>
                      <label className="sr-only" htmlFor={`role-${membership.user.id}`}>
                        Role for {membership.user.display_name}
                      </label>
                      <select
                        disabled={!canManage || isSelf || acting !== null}
                        id={`role-${membership.user.id}`}
                        onChange={(event) => setDrafts((current) => ({
                          ...current,
                          [membership.user.id]: {
                            ...draft,
                            role: event.target.value as MembershipRole,
                          },
                        }))}
                        value={draft.role}
                      >
                        {ROLES.map((item) => (
                          <option key={item} value={item}>{titleCase(item)}</option>
                        ))}
                      </select>
                    </td>
                    <td>
                      <label className="access-status-control">
                        <input
                          checked={draft.isActive}
                          disabled={!canManage || isSelf || acting !== null}
                          onChange={(event) => setDrafts((current) => ({
                            ...current,
                            [membership.user.id]: {
                              ...draft,
                              isActive: event.target.checked,
                            },
                          }))}
                          type="checkbox"
                        />
                        Active for {membership.user.display_name}
                      </label>
                    </td>
                    <td>
                      <span>v{membership.version}</span>
                      <small>{formatTimestamp(membership.updated_at)}</small>
                      {isSelf ? <em>Current administrator</em> : null}
                      {canManage && !isSelf ? (
                        <button
                          disabled={!changed || acting !== null}
                          onClick={() => void handleUpdate(membership)}
                          type="button"
                        >
                          {acting === membership.user.id
                            ? "Recording…"
                            : `Save access for ${membership.user.display_name}`}
                        </button>
                      ) : null}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        {!loading && memberships.length === 0 ? (
          <p className="access-empty">No memberships are recorded for this organization.</p>
        ) : null}
      </section>

      <section className="identity-audit" aria-labelledby="identity-audit-title">
        <header>
          <div>
            <p className="utility-label">Append-only receipts</p>
            <h2 id="identity-audit-title">Identity administration audit</h2>
          </div>
          <span>{audit.length} receipts</span>
        </header>
        <ol aria-label="Identity administration receipts">
          {audit.map((event) => (
            <li key={event.id}>
              <span className="audit-seal">✓</span>
              <div>
                <strong>{event.event_type.replace("membership.", "Membership ")}</strong>
                <small>
                  {memberNames.get(event.target_user_id) ?? event.target_user_id} · v
                  {event.membership_version}
                </small>
              </div>
              <div>
                <span>{String(event.payload.role ?? "role retained")}</span>
                <small>{event.payload.is_active === false ? "Inactive" : "Active"}</small>
              </div>
              <div>
                <code>{event.actor}</code>
                <time dateTime={event.created_at}>{formatTimestamp(event.created_at)}</time>
              </div>
            </li>
          ))}
        </ol>
        {!loading && audit.length === 0 ? (
          <p className="access-empty">No administrative writes have been recorded yet.</p>
        ) : null}
      </section>
    </section>
  );
}
