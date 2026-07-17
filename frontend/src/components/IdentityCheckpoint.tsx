import type { DevPersona } from "../lib/api";

interface IdentityCheckpointProps {
  error: string | null;
  loading: boolean;
  onSignIn: (persona: string) => Promise<void>;
  personas: DevPersona[];
  signingIn: string | null;
}

const roleCopy: Record<string, string> = {
  admin: "Full organizational authority, including operational decisions and evaluation runs.",
  incident_commander: "May coordinate response, approve mitigations, and control incident records.",
  responder: "May investigate and prepare evidence, without crossing every write boundary.",
  viewer: "Read-only access to the incident ledger and its preserved evidence.",
};

export function IdentityCheckpoint({
  error,
  loading,
  onSignIn,
  personas,
  signingIn,
}: IdentityCheckpointProps) {
  const hostedIdentity = !loading && personas.length === 0;

  return (
    <main className="identity-shell">
      <header className="checkpoint-header">
        <span>PagerAgent / authority checkpoint</span>
        <span>{hostedIdentity ? "Hosted OIDC / PKCE" : "Local development only"}</span>
      </header>

      <section className="checkpoint-intro" aria-labelledby="checkpoint-title">
        <p className="eyebrow">Before the incident ledger opens</p>
        <h1 id="checkpoint-title">Choose who crosses the write boundary.</h1>
        <p>
          Every operator receives a server-signed session, one organization scope, and an exact
          permission receipt. The UI never invents an operator identity.
        </p>
      </section>

      <section
        className="persona-checkpoint"
        aria-label={hostedIdentity ? "Organization sign in" : "Development personas"}
      >
        <header>
          <div>
            <p className="utility-label">
              {hostedIdentity ? "Hosted identity boundary" : "Scope / PagerAgent Labs"}
            </p>
            <h2>{hostedIdentity ? "Organization identity" : "Development identities"}</h2>
          </div>
          <span className="checkpoint-stamp">
            {hostedIdentity ? "Authorization code + PKCE" : "Not production auth"}
          </span>
        </header>

        {loading ? <p className="checkpoint-message">Reading local identity fixtures…</p> : null}
        {hostedIdentity ? (
          <div className="hosted-identity-entry">
            <div>
              <p className="utility-label">Verified upstream · scoped downstream</p>
              <h2>Continue with your organization.</h2>
              <p>
                Start the same-origin OIDC flow. PagerAgent returns with a revocable session bound
                to your provisioned membership and active organization.
              </p>
            </div>
            <a href="/api/v1/auth/oidc/login">
              <span>Sign in with organization identity</span>
              <small>Secure redirect →</small>
            </a>
          </div>
        ) : null}
        <div className="persona-list">
          {personas.map((persona, index) => (
            <article className="persona-ticket" key={persona.slug}>
              <span className="persona-index">{String(index + 1).padStart(2, "0")}</span>
              <div>
                <span>{persona.role.replaceAll("_", " ")}</span>
                <h2>{persona.display_name}</h2>
                <p>{roleCopy[persona.role] ?? "A scoped development identity for authorization testing."}</p>
                <code>{persona.email}</code>
              </div>
              <button
                disabled={signingIn !== null}
                onClick={() => void onSignIn(persona.slug)}
                type="button"
              >
                {signingIn === persona.slug ? "Signing receipt…" : `Continue as ${persona.display_name}`}
              </button>
            </article>
          ))}
        </div>
        {error ? <p className="checkpoint-error" role="alert">{error}</p> : null}
      </section>

      <footer className="checkpoint-footnote">
        Session cookie: HttpOnly · SameSite strict <span>CSRF: per-session write token</span>
      </footer>
    </main>
  );
}
