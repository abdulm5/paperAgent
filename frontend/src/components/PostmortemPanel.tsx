import { useEffect, useState } from "react";

import type {
  GroundedPostmortemSection,
  IncidentStatus,
  PostmortemDetail,
  PostmortemEditPayload,
  PreventionItem,
  PreventionPriority,
} from "../lib/api";
import { postmortemExportUrl } from "../lib/api";
import { formatTimestamp, titleCase } from "../lib/format";

interface PostmortemPanelProps {
  postmortem: PostmortemDetail | null;
  incidentStatus: IncidentStatus;
  loading: boolean;
  acting: boolean;
  error: string | null;
  onGenerate: () => Promise<void>;
  onSave: (edit: PostmortemEditPayload) => Promise<void>;
  onFinalize: (note: string) => Promise<void>;
}

function citation(value: string): string {
  return `E-${value.slice(0, 6)}`;
}

function Citations({ section }: { section: { evidence_ids: string[] } }) {
  return (
    <div className="citation-line">
      {section.evidence_ids.map((id) => <span key={id}>{citation(id)}</span>)}
    </div>
  );
}

function toEdit(postmortem: PostmortemDetail): PostmortemEditPayload {
  const content = postmortem.content;
  return {
    change_note: "",
    title: content.title,
    summary: content.summary.text,
    root_cause: content.root_cause.text,
    customer_impact: content.customer_impact.text,
    detection: content.detection.text,
    resolution: content.resolution.text,
    what_went_well: content.what_went_well.map((item) => item.text),
    what_went_poorly: content.what_went_poorly.map((item) => item.text),
    prevention_items: content.prevention_items.map((item) => ({
      title: item.title,
      description: item.description,
      owner: item.owner,
      priority: item.priority,
      status: item.status,
    })),
  };
}

function ReportSection({
  heading,
  section,
}: {
  heading: string;
  section: GroundedPostmortemSection;
}) {
  return (
    <section className="report-section">
      <p className="brief-label">{heading}</p>
      <p>{section.text}</p>
      <Citations section={section} />
    </section>
  );
}

function PreventionCard({ item }: { item: PreventionItem }) {
  return (
    <article className="prevention-card">
      <div>
        <span>{item.priority}</span>
        <small>{item.status}</small>
      </div>
      <h4>{item.title}</h4>
      <p>{item.description}</p>
      <footer>
        <strong>{item.owner}</strong>
        <Citations section={item} />
      </footer>
    </article>
  );
}

export function PostmortemPanel({
  postmortem,
  incidentStatus,
  loading,
  acting,
  error,
  onGenerate,
  onSave,
  onFinalize,
}: PostmortemPanelProps) {
  const [editing, setEditing] = useState(false);
  const [edit, setEdit] = useState<PostmortemEditPayload | null>(null);
  const [reviewed, setReviewed] = useState(false);
  const [finalNote, setFinalNote] = useState("");

  useEffect(() => {
    if (postmortem) setEdit(toEdit(postmortem));
    setEditing(false);
    setReviewed(false);
  }, [postmortem?.id, postmortem?.version]);

  if (incidentStatus !== "resolved") return null;

  if (loading && postmortem === null) {
    return <section className="postmortem-panel postmortem-message">Opening case file…</section>;
  }

  if (postmortem === null) {
    return (
      <section className="postmortem-panel postmortem-empty" aria-labelledby="postmortem-title">
        <div>
          <p className="utility-label">Incident learning</p>
          <h2 id="postmortem-title">The incident is closed. Capture what changed.</h2>
          <p>Generate a cited, blameless draft from the preserved response record.</p>
        </div>
        <button disabled={acting} onClick={() => void onGenerate()} type="button">
          {acting ? "Generating case file…" : "Generate postmortem"}
        </button>
        {error ? <p className="postmortem-error">{error}</p> : null}
      </section>
    );
  }

  const content = postmortem.content;
  const isDraft = postmortem.status === "draft";

  function setField(field: keyof PostmortemEditPayload, value: unknown) {
    setEdit((current) => current ? { ...current, [field]: value } : current);
  }

  function setListItem(field: "what_went_well" | "what_went_poorly", index: number, value: string) {
    if (!edit) return;
    const values = [...edit[field]];
    values[index] = value;
    setEdit({ ...edit, [field]: values });
  }

  function setPrevention(index: number, field: string, value: string) {
    if (!edit) return;
    const items = edit.prevention_items.map((item, itemIndex) => (
      itemIndex === index ? { ...item, [field]: value } : item
    ));
    setEdit({ ...edit, prevention_items: items });
  }

  async function save() {
    if (!edit || !edit.change_note.trim()) return;
    await onSave(edit);
  }

  async function finalize() {
    if (!reviewed) return;
    await onFinalize(finalNote);
    setFinalNote("");
  }

  return (
    <section className="postmortem-panel" aria-labelledby="postmortem-title">
      <div className="section-title-row postmortem-heading">
        <div>
          <p className="utility-label">Incident learning / case file</p>
          <h2 id="postmortem-title">Postmortem</h2>
        </div>
        <div className="postmortem-toolbar">
          <span className={`postmortem-status postmortem-status-${postmortem.status}`}>
            {postmortem.status === "final" ? "Final record" : "Working draft"}
          </span>
          {isDraft && !editing ? (
            <button disabled={acting} onClick={() => setEditing(true)} type="button">
              Edit draft
            </button>
          ) : null}
          <a href={postmortemExportUrl(postmortem.id)}>Export Markdown</a>
        </div>
      </div>

      <div className="postmortem-casefile">
        <aside className="revision-spine" aria-label="Document revisions">
          <span>Revision record</span>
          <ol>
            {postmortem.revisions.map((revision) => (
              <li key={revision.id}>
                <strong>v{revision.version}</strong>
                <small>{revision.source.replaceAll("_", " ")}</small>
                <time dateTime={revision.created_at}>{formatTimestamp(revision.created_at)}</time>
              </li>
            ))}
          </ol>
          <code>{postmortem.input_hash.slice(0, 12)}</code>
        </aside>

        <div className="postmortem-document">
          {postmortem.status === "final" ? (
            <div className="final-seal">
              <span>Reviewed</span>
              <strong>Final</strong>
              <small>{postmortem.finalized_by}</small>
            </div>
          ) : null}

          {editing && edit ? (
            <form className="postmortem-editor" onSubmit={(event) => event.preventDefault()}>
              <label>
                Report title
                <input value={edit.title} onChange={(event) => setField("title", event.target.value)} />
              </label>
              {([
                ["summary", "Summary"],
                ["root_cause", "Root cause"],
                ["customer_impact", "Customer impact"],
                ["detection", "Detection"],
                ["resolution", "Resolution"],
              ] as const).map(([field, label]) => (
                <label key={field}>
                  {label}
                  <textarea value={edit[field]} onChange={(event) => setField(field, event.target.value)} />
                </label>
              ))}

              <fieldset>
                <legend>What went well</legend>
                {edit.what_went_well.map((value, index) => (
                  <textarea
                    aria-label={`What went well ${index + 1}`}
                    key={index}
                    onChange={(event) => setListItem("what_went_well", index, event.target.value)}
                    value={value}
                  />
                ))}
              </fieldset>
              <fieldset>
                <legend>What went poorly</legend>
                {edit.what_went_poorly.map((value, index) => (
                  <textarea
                    aria-label={`What went poorly ${index + 1}`}
                    key={index}
                    onChange={(event) => setListItem("what_went_poorly", index, event.target.value)}
                    value={value}
                  />
                ))}
              </fieldset>
              <fieldset className="prevention-editor">
                <legend>Prevention items</legend>
                {edit.prevention_items.map((item, index) => (
                  <div key={index}>
                    <input
                      aria-label={`Prevention item ${index + 1} title`}
                      onChange={(event) => setPrevention(index, "title", event.target.value)}
                      value={item.title}
                    />
                    <textarea
                      aria-label={`Prevention item ${index + 1} description`}
                      onChange={(event) => setPrevention(index, "description", event.target.value)}
                      value={item.description}
                    />
                    <input
                      aria-label={`Prevention item ${index + 1} owner`}
                      onChange={(event) => setPrevention(index, "owner", event.target.value)}
                      value={item.owner}
                    />
                    <select
                      aria-label={`Prevention item ${index + 1} priority`}
                      onChange={(event) => setPrevention(index, "priority", event.target.value)}
                      value={item.priority}
                    >
                      {(["P0", "P1", "P2", "P3"] as PreventionPriority[]).map((priority) => (
                        <option key={priority}>{priority}</option>
                      ))}
                    </select>
                  </div>
                ))}
              </fieldset>
              <label>
                Revision note
                <input
                  placeholder="What did you change and why?"
                  value={edit.change_note}
                  onChange={(event) => setField("change_note", event.target.value)}
                />
              </label>
              <div className="editor-actions">
                <button
                  disabled={acting || !edit.change_note.trim()}
                  onClick={() => void save()}
                  type="button"
                >
                  {acting ? "Saving revision…" : "Save revision"}
                </button>
                <button onClick={() => { setEdit(toEdit(postmortem)); setEditing(false); }} type="button">
                  Cancel
                </button>
              </div>
            </form>
          ) : (
            <>
              <header className="report-masthead">
                <span>{postmortem.model_name} · v{postmortem.version}</span>
                <h3>{content.title}</h3>
                <p>Blameless review · evidence preserved with every conclusion</p>
              </header>

              <div className="report-grid">
                <ReportSection heading="Executive summary" section={content.summary} />
                <ReportSection heading="Root cause" section={content.root_cause} />
                <ReportSection heading="Customer impact" section={content.customer_impact} />
                <ReportSection heading="Detection" section={content.detection} />
                <ReportSection heading="Resolution" section={content.resolution} />
              </div>

              <div className="learning-columns">
                <section>
                  <p className="brief-label">What went well</p>
                  <ul>
                    {content.what_went_well.map((item) => (
                      <li key={item.text}><span>{item.text}</span><Citations section={item} /></li>
                    ))}
                  </ul>
                </section>
                <section>
                  <p className="brief-label">What went poorly</p>
                  <ul>
                    {content.what_went_poorly.map((item) => (
                      <li key={item.text}><span>{item.text}</span><Citations section={item} /></li>
                    ))}
                  </ul>
                </section>
              </div>

              <section className="prevention-section">
                <div className="subsection-heading">
                  <div>
                    <p className="utility-label">Follow-through</p>
                    <h3>Prevention register</h3>
                  </div>
                  <span>{content.prevention_items.length} open actions</span>
                </div>
                <div className="prevention-grid">
                  {content.prevention_items.map((item) => <PreventionCard item={item} key={item.title} />)}
                </div>
              </section>

              <details className="postmortem-timeline">
                <summary>Inspect exact incident timeline · {content.timeline.length} events</summary>
                <ol>
                  {content.timeline.map((item) => (
                    <li key={item.evidence_ids[0]}>
                      <time dateTime={item.occurred_at}>{formatTimestamp(item.occurred_at)}</time>
                      <div><strong>{titleCase(item.event_type.replaceAll(".", " "))}</strong><p>{item.description}</p></div>
                      <Citations section={item} />
                    </li>
                  ))}
                </ol>
              </details>
            </>
          )}
        </div>
      </div>

      {isDraft && !editing ? (
        <div className="finalize-strip">
          <div>
            <p className="brief-label">Document control</p>
            <strong>Finalize only after team review.</strong>
          </div>
          <label className="review-check">
            <input checked={reviewed} onChange={(event) => setReviewed(event.target.checked)} type="checkbox" />
            I reviewed the report, citations, and prevention owners.
          </label>
          <input
            aria-label="Finalization note"
            onChange={(event) => setFinalNote(event.target.value)}
            placeholder="Optional finalization note"
            value={finalNote}
          />
          <button disabled={!reviewed || acting} onClick={() => void finalize()} type="button">
            {acting ? "Finalizing…" : "Finalize record"}
          </button>
        </div>
      ) : null}

      {error ? <p className="postmortem-error">{error}</p> : null}
    </section>
  );
}
