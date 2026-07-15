import type {
  WorkflowJob,
  WorkflowRun,
  WorkflowStreamStatus,
} from "../lib/api";
import { formatTimestamp, titleCase } from "../lib/format";

interface WorkflowDispatchProps {
  workflows: WorkflowRun[];
  loading: boolean;
  error: string | null;
  streamStatus: WorkflowStreamStatus;
}

const streamLabels: Record<WorkflowStreamStatus, string> = {
  connecting: "Stream connecting",
  live: "Stream live",
  reconnecting: "Stream reconnecting",
  unsupported: "30 sec reconcile",
};

const routeLabels = ["Committed", "Published", "Settled"] as const;

function latestJob(workflow: WorkflowRun): WorkflowJob | null {
  const current = workflow.current_step
    ? [...workflow.jobs].reverse().find((job) => job.step_type === workflow.current_step)
    : null;
  return current ?? workflow.jobs.at(-1) ?? null;
}

function activeRouteIndex(workflow: WorkflowRun): number {
  if (workflow.status === "completed" || workflow.status === "dead_lettered") return 2;
  const published = workflow.jobs.some((job) =>
    job.deliveries.some((delivery) => delivery.published_at !== null),
  );
  return published || workflow.status !== "queued" ? 1 : 0;
}

function routeClass(workflow: WorkflowRun, index: number): string {
  const activeIndex = activeRouteIndex(workflow);
  const classes = ["workflow-route-stop"];
  if (index <= activeIndex) classes.push("reached");
  if (index === activeIndex) classes.push("active");
  if (workflow.status === "dead_lettered" && index === activeIndex) classes.push("failed");
  if (workflow.status === "retry_scheduled" && index === activeIndex) classes.push("retrying");
  return classes.join(" ");
}

function routeState(workflow: WorkflowRun, index: number): string {
  const activeIndex = activeRouteIndex(workflow);
  if (index < activeIndex || (index === activeIndex && workflow.status === "completed")) {
    return "complete";
  }
  if (index > activeIndex) return "pending";
  if (workflow.status === "dead_lettered") return "failed";
  if (workflow.status === "retry_scheduled") return "retrying";
  return "current";
}

function jobAvailability(job: WorkflowJob): string {
  if (job.lease_expires_at) return `lease to ${formatTimestamp(job.lease_expires_at)}`;
  if (job.completed_at) return `saved ${formatTimestamp(job.completed_at)}`;
  return `available ${formatTimestamp(job.available_at)}`;
}

function transportReceipt(job: WorkflowJob | null): string {
  const delivery = job?.deliveries.at(-1);
  if (!delivery) return "outbox pending";
  if (delivery.last_error) return `publish retry · attempt ${delivery.publish_attempts}`;
  if (delivery.stream_message_id) {
    return `stream ${delivery.stream_message_id} · publish ${delivery.publish_attempts}`;
  }
  return "outbox committed";
}

export function WorkflowDispatch({
  workflows,
  loading,
  error,
  streamStatus,
}: WorkflowDispatchProps) {
  return (
    <section className="workflow-dispatch" aria-labelledby="workflow-dispatch-title">
      <header className="workflow-dispatch-header">
        <div>
          <p className="utility-label">Durable dispatch / flight recorder</p>
          <h2 id="workflow-dispatch-title">Work survives the process.</h2>
        </div>
        <span
          aria-live="polite"
          className={`workflow-stream-state stream-${streamStatus}`}
          role="status"
        >
          {streamLabels[streamStatus]}
        </span>
      </header>

      {loading && workflows.length === 0 ? (
        <p className="workflow-dispatch-message">Reading the workflow recorder…</p>
      ) : null}

      {!loading && workflows.length === 0 ? (
        <p className="workflow-dispatch-message">
          No durable work has been dispatched for this incident.
        </p>
      ) : null}

      {workflows.length > 0 ? (
        <ol className="workflow-run-list">
          {workflows.map((workflow, index) => {
            const job = latestJob(workflow);
            const failed = workflow.status === "dead_lettered";
            const transportError = job?.deliveries.at(-1)?.last_error ?? null;
            return (
              <li className={`workflow-run workflow-${workflow.status}`} key={workflow.id}>
                <span className="workflow-run-index">
                  {String(index + 1).padStart(2, "0")}
                </span>

                <div className="workflow-run-body">
                  <div className="workflow-run-heading">
                    <div>
                      <strong>{titleCase(workflow.workflow_type)}</strong>
                      <code title={workflow.trace_id ?? undefined}>
                        {workflow.trace_id ? `trace ${workflow.trace_id.slice(0, 12)}` : "trace pending"}
                      </code>
                    </div>
                    <span className={`workflow-status workflow-status-${workflow.status}`}>
                      {failed ? "Manual replay required" : titleCase(workflow.status)}
                    </span>
                  </div>

                  <ol className="workflow-route" aria-label={`${titleCase(workflow.workflow_type)} delivery route`}>
                    {routeLabels.map((label, routeIndex) => (
                      <li
                        aria-current={
                          !["completed", "dead_lettered"].includes(workflow.status) &&
                          routeIndex === activeRouteIndex(workflow)
                            ? "step"
                            : undefined
                        }
                        aria-label={`${label}: ${routeState(workflow, routeIndex)}`}
                        className={routeClass(workflow, routeIndex)}
                        key={label}
                      >
                        <i aria-hidden="true" />
                        <span>{label}</span>
                      </li>
                    ))}
                  </ol>

                  <div className="workflow-run-meta">
                    <span><b>Step</b>{titleCase(workflow.current_step ?? job?.step_type ?? "awaiting worker")}</span>
                    <span><b>Attempt</b>{job ? `${job.attempt_count}/${job.max_attempts}` : "—"}</span>
                    <span><b>Lease</b>{job?.lease_owner ?? "awaiting worker"}</span>
                    <span title={transportReceipt(job)}><b>Transport</b>{transportReceipt(job)}</span>
                  </div>

                  {workflow.failure_reason || job?.last_error || transportError ? (
                    <p className="workflow-failure" role="alert">
                      {workflow.failure_reason ?? job?.last_error ?? transportError}
                    </p>
                  ) : null}

                  <details className="workflow-job-drawer">
                    <summary>
                      Inspect delivery record · {workflow.jobs.length} steps · {workflow.events.length} events
                    </summary>
                    <ol>
                      {workflow.jobs.map((item) => (
                        <li key={item.id}>
                          <strong>{titleCase(item.step_type)}</strong>
                          <span>{titleCase(item.status)}</span>
                          <span>attempt {item.attempt_count}/{item.max_attempts}</span>
                          <small>
                            {transportReceipt(item)} · {item.lease_owner ?? jobAvailability(item)}
                          </small>
                        </li>
                      ))}
                    </ol>
                  </details>
                </div>
              </li>
            );
          })}
        </ol>
      ) : null}

      {error ? <p className="workflow-dispatch-error" role="alert">{error}</p> : null}
    </section>
  );
}
