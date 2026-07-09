const milestones = [
  "Foundation",
  "Outage simulator",
  "Incident core",
  "Evidence pipeline",
  "Grounded copilot",
  "Evaluation harness",
];

export default function App() {
  return (
    <main className="app-shell">
      <header className="system-header">
        <span>PagerAgent / operator console</span>
        <span>Build 00 · local</span>
      </header>
      <section className="hero" aria-labelledby="project-name">
        <p className="eyebrow">Evidence before action</p>
        <h1 id="project-name">PagerAgent</h1>
        <p className="lede">
          An operator-first copilot that turns an alert into an explainable, human-approved
          incident response.
        </p>
        <div className="signal-ribbon" aria-label="The intended incident response flow">
          <span>alert</span>
          <span>evidence</span>
          <span>hypothesis</span>
          <span>approval</span>
        </div>
        <div className="status-card">
          <span className="status-indicator" aria-hidden="true" />
          <div>
            <p className="status-label">Current build stage</p>
            <p className="status-value">Foundation complete · simulator is next</p>
          </div>
        </div>
      </section>

      <section aria-labelledby="roadmap-title">
        <div className="section-heading">
          <p className="eyebrow">Build sequence</p>
          <h2 id="roadmap-title">One explainable incident loop at a time</h2>
        </div>
        <ol className="milestone-list">
          {milestones.map((milestone, index) => (
            <li className={index === 0 ? "milestone complete" : "milestone"} key={milestone}>
              <span>{String(index + 1).padStart(2, "0")}</span>
              {milestone}
            </li>
          ))}
        </ol>
      </section>
    </main>
  );
}
