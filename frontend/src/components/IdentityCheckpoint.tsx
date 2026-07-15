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
  return (
    <main className="identity-shell">
      <header className="checkpoint-header">
        <span>PagerAgent / authority checkpoint</span>
        <span>Local development only</span>
      </header>

      <section className="checkpoint-intro" aria-labelledby="checkpoint-title">
        <p className="eyebrow">Before the incident ledger opens</p>
        <h1 id="checkpoint-title">Choose who crosses the write boundary.</h1>
        <p>
          Each persona receives a server-signed session, one organization scope, and an exact
          permission receipt. The UI never invents an operator identity.
        </p>
      </section>

      <section className="persona-checkpoint" aria-label="Development personas">
        <header>
          <div>
            <p className="utility-label">Scope / PagerAgent Labs</p>
            <h2>Development identities</h2>
          </div>
          <span className="checkpoint-stamp">Not production auth</span>
        </header>

        {loading ? <p className="checkpoint-message">Reading local identity fixtures…</p> : null}
        {!loading && personas.length === 0 ? (
          <p className="checkpoint-message">
            Persona sign-in is disabled here. Complete sign-in through your deployment&apos;s OIDC
            client, then return; a provider-specific redirect is not bundled with this dashboard.
          </p>
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
