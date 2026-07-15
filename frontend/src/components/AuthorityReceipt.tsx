import type { AuthSession } from "../lib/api";

interface AuthorityReceiptProps {
  busy: boolean;
  connectionError: string | null;
  error: string | null;
  onLogout: () => Promise<void>;
  onSwitchOrganization: (organizationId: string) => Promise<void>;
  session: AuthSession;
}

function initials(displayName: string): string {
  return displayName
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0]?.toUpperCase())
    .join("");
}

export function AuthorityReceipt({
  busy,
  connectionError,
  error,
  onLogout,
  onSwitchOrganization,
  session,
}: AuthorityReceiptProps) {
  const active = session.active_organization;

  return (
    <header className="system-header authority-header">
      <div className="ledger-signature">
        <span className="brand-mark">PagerAgent / incident ledger</span>
        <span className={connectionError ? "connection-state offline" : "connection-state"}>
          {connectionError ? "API unavailable" : "Live record"}
        </span>
      </div>

      <section className="authority-receipt" aria-label="Signed authority receipt">
        <div className="receipt-seal" aria-hidden="true">{initials(session.user.display_name)}</div>
        <div className="receipt-cell receipt-principal">
          <span>Signed principal</span>
          <strong>{session.user.display_name}</strong>
          <small>{session.user.email}</small>
        </div>
        <label className="receipt-cell receipt-scope">
          <span>Organization scope</span>
          <select
            aria-label="Organization scope"
            disabled={busy || session.memberships.length < 2}
            onChange={(event) => void onSwitchOrganization(event.target.value)}
            value={active.id}
          >
            {session.memberships.map((membership) => (
              <option key={membership.organization.id} value={membership.organization.id}>
                {membership.organization.name}
              </option>
            ))}
          </select>
          <small>{active.role.replaceAll("_", " ")}</small>
        </label>
        <details className="receipt-grants">
          <summary>{session.permissions.length} exact grants</summary>
          <ul>
            {session.permissions.map((permission) => <li key={permission}>{permission}</li>)}
          </ul>
        </details>
        <button className="receipt-logout" disabled={busy} onClick={() => void onLogout()} type="button">
          {busy ? "Changing scope…" : "End session"}
        </button>
        {error ? <p className="receipt-error" role="alert">{error}</p> : null}
      </section>
    </header>
  );
}
