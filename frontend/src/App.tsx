import { useCallback, useEffect, useRef, useState } from "react";

import { AuthorityReceipt } from "./components/AuthorityReceipt";
import { ConnectorCustodyPanel } from "./components/ConnectorCustodyPanel";
import { IncidentDetailPanel } from "./components/IncidentDetailPanel";
import { IncidentQueue } from "./components/IncidentQueue";
import { EvaluationPanel } from "./components/EvaluationPanel";
import { IdentityCheckpoint } from "./components/IdentityCheckpoint";
import { OrganizationAccessPanel } from "./components/OrganizationAccessPanel";
import { ResponseProofRail } from "./components/ResponseProofRail";
import {
  advanceRequestScope,
  ApiError,
  createDevSession,
  decideCollaborationOutput,
  decideProposal,
  deleteAuthSession,
  finalizePostmortem,
  getAuthSession,
  getDevPersonas,
  generateProposal,
  generatePostmortem,
  getIncident,
  getIncidents,
  getEvaluationScorecard,
  getLatestInvestigation,
  getLatestProposal,
  getCollaborationOutputs,
  getPostmortem,
  getIncidentWorkflows,
  hasPermission,
  prepareCollaborationOutputs,
  runInvestigation,
  setForbiddenHandler,
  setSessionCsrfToken,
  setUnauthorizedHandler,
  switchOrganization,
  transitionIncident,
  updatePostmortem,
  type AuthSession,
  type CollaborationDecision,
  type CollaborationOutput,
  type CollaborationOutputKind,
  type DevPersona,
  type IncidentDetail,
  type IncidentStatus,
  type IncidentSummary,
  type EvaluationScorecard,
  type InvestigationDetail,
  type MitigationProposal,
  type PostmortemDetail,
  type PostmortemEditPayload,
  type ProposalDecision,
  type WorkflowRun,
  type WorkflowStreamEvent,
  type WorkflowStreamStatus,
} from "./lib/api";

const RECONCILIATION_INTERVAL_MS = 30_000;
const SESSION_REVALIDATION_INTERVAL_MS = 15_000;
const WORKFLOW_STREAM_URL = "/api/v1/workflows/events";
type AuthStatus = "checking" | "signed_out" | "signed_in" | "switching";
type IncidentResource =
  | "detail"
  | "investigation"
  | "proposal"
  | "collaboration"
  | "postmortem"
  | "workflows";
type IncidentAction =
  | "transition"
  | "investigation"
  | "proposal"
  | "collaboration"
  | "postmortem";

interface IncidentScopeToken {
  incidentId: string;
  selectionGeneration: number;
}

interface ResourceRequestToken extends IncidentScopeToken {
  requestGeneration: number;
  resource: IncidentResource;
}

interface IncidentActionToken extends IncidentScopeToken {
  action: IncidentAction;
  actionGeneration: number;
}

function newestWorkflowFirst(left: WorkflowRun, right: WorkflowRun): number {
  return new Date(right.created_at).getTime() - new Date(left.created_at).getTime();
}

function upsertWorkflow(current: WorkflowRun[], incoming: WorkflowRun): WorkflowRun[] {
  const existing = current.find((workflow) => workflow.id === incoming.id);
  if (existing && existing.version >= incoming.version) return current;
  return [...current.filter((workflow) => workflow.id !== incoming.id), incoming].sort(
    newestWorkflowFirst,
  );
}

function isSessionBoundaryError(error: unknown): boolean {
  return error instanceof ApiError && (
    error.status === 401 || error.code === "membership_inactive"
  );
}

export default function App() {
  const [authStatus, setAuthStatus] = useState<AuthStatus>("checking");
  const [session, setSession] = useState<AuthSession | null>(null);
  const [personas, setPersonas] = useState<DevPersona[]>([]);
  const [personasLoading, setPersonasLoading] = useState(false);
  const [signingIn, setSigningIn] = useState<string | null>(null);
  const [authError, setAuthError] = useState<string | null>(null);
  const streamRef = useRef<EventSource | null>(null);
  const authenticatedOnceRef = useRef(false);
  const scopeActiveRef = useRef(false);
  const authorityRefreshInFlightRef = useRef(false);
  const authorityGenerationRef = useRef(0);

  const advanceAuthorityBoundary = useCallback((): number => {
    advanceRequestScope();
    scopeActiveRef.current = false;
    authorityRefreshInFlightRef.current = false;
    authorityGenerationRef.current += 1;
    return authorityGenerationRef.current;
  }, []);

  const isCurrentAuthority = useCallback((generation: number): boolean => (
    authorityGenerationRef.current === generation
  ), []);

  const commitAuthoritySession = useCallback((
    nextSession: AuthSession,
    generation: number,
  ): boolean => {
    if (!isCurrentAuthority(generation)) return false;
    scopeActiveRef.current = true;
    setSessionCsrfToken(nextSession.csrf_token);
    setSession(nextSession);
    return true;
  }, [isCurrentAuthority]);

  const handleStreamChange = useCallback((stream: EventSource | null) => {
    streamRef.current = stream;
  }, []);

  const closeTenantStream = useCallback(() => {
    streamRef.current?.close();
    streamRef.current = null;
  }, []);

  const loadPersonas = useCallback(async () => {
    setPersonasLoading(true);
    try {
      setPersonas(await getDevPersonas());
    } catch (error) {
      setPersonas([]);
      if (!(error instanceof ApiError && error.status === 404)) {
        setAuthError(error instanceof Error ? error.message : "Development identities are unavailable.");
      }
    } finally {
      setPersonasLoading(false);
    }
  }, []);

  const handleUnauthorized = useCallback(() => {
    advanceAuthorityBoundary();
    closeTenantStream();
    setSessionCsrfToken(null);
    setSession(null);
    setSigningIn(null);
    setAuthStatus("signed_out");
    setAuthError(
      authenticatedOnceRef.current
        ? "Your session ended. Sign in again before accessing an organization ledger."
        : null,
    );
    void loadPersonas();
  }, [advanceAuthorityBoundary, closeTenantStream, loadPersonas]);

  const handleForbidden = useCallback(() => {
    if (!scopeActiveRef.current || authorityRefreshInFlightRef.current) return;
    const authorityGeneration = authorityGenerationRef.current;
    authorityRefreshInFlightRef.current = true;
    void getAuthSession()
      .then((nextSession) => {
        if (
          !scopeActiveRef.current
          || !commitAuthoritySession(nextSession, authorityGeneration)
        ) return;
        setAuthError(null);
      })
      .catch((error) => {
        if (
          !scopeActiveRef.current
          || !isCurrentAuthority(authorityGeneration)
        ) return;
        if (isSessionBoundaryError(error)) return;
        setAuthError(
          error instanceof Error
            ? `Authority receipt could not be refreshed: ${error.message}`
            : "Authority receipt could not be refreshed.",
        );
      })
      .finally(() => {
        if (isCurrentAuthority(authorityGeneration)) {
          authorityRefreshInFlightRef.current = false;
        }
      });
  }, [commitAuthoritySession, isCurrentAuthority]);

  useEffect(() => {
    const authorityGeneration = advanceAuthorityBoundary();
    setSessionCsrfToken(null);
    setUnauthorizedHandler(handleUnauthorized);
    setForbiddenHandler(handleForbidden);
    void (async () => {
      try {
        const nextSession = await getAuthSession();
        if (!commitAuthoritySession(nextSession, authorityGeneration)) return;
        authenticatedOnceRef.current = true;
        setAuthStatus("signed_in");
        setAuthError(null);
      } catch (error) {
        if (!isCurrentAuthority(authorityGeneration)) return;
        if (!isSessionBoundaryError(error)) {
          setAuthStatus("signed_out");
          setAuthError(error instanceof Error ? error.message : "The identity service is unavailable.");
          await loadPersonas();
        }
      }
    })();
    return () => {
      advanceAuthorityBoundary();
      setSessionCsrfToken(null);
      setUnauthorizedHandler(null);
      setForbiddenHandler(null);
    };
  }, [
    advanceAuthorityBoundary,
    commitAuthoritySession,
    handleForbidden,
    handleUnauthorized,
    isCurrentAuthority,
    loadPersonas,
  ]);

  useEffect(() => {
    if (authStatus !== "signed_in") return;
    const timer = window.setInterval(() => {
      if (!scopeActiveRef.current) return;
      const authorityGeneration = authorityGenerationRef.current;
      void getAuthSession()
        .then((nextSession) => {
          if (
            scopeActiveRef.current
            && commitAuthoritySession(nextSession, authorityGeneration)
          ) {
            setAuthError(null);
          }
        })
        .catch((error) => {
          if (isSessionBoundaryError(error)) return;
          if (
            scopeActiveRef.current
            && isCurrentAuthority(authorityGeneration)
          ) {
            setAuthError(
              error instanceof Error
                ? `Session authority could not be revalidated: ${error.message}`
                : "Session authority could not be revalidated.",
            );
          }
        });
    }, SESSION_REVALIDATION_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [authStatus, commitAuthoritySession, isCurrentAuthority]);

  async function handleSignIn(persona: string) {
    const authorityGeneration = advanceAuthorityBoundary();
    setSessionCsrfToken(null);
    setSigningIn(persona);
    setAuthError(null);
    try {
      const nextSession = await createDevSession(persona);
      if (!commitAuthoritySession(nextSession, authorityGeneration)) return;
      authenticatedOnceRef.current = true;
      setAuthStatus("signed_in");
    } catch (error) {
      if (!isCurrentAuthority(authorityGeneration)) return;
      setAuthError(error instanceof Error ? error.message : "This identity could not be signed in.");
    } finally {
      if (isCurrentAuthority(authorityGeneration)) setSigningIn(null);
    }
  }

  async function handleSwitchOrganization(organizationId: string) {
    if (!session || organizationId === session.active_organization.id) return;
    const authorityGeneration = advanceAuthorityBoundary();
    closeTenantStream();
    setSession(null);
    setAuthStatus("switching");
    setAuthError(null);
    try {
      const switchRequest = switchOrganization(organizationId);
      setSessionCsrfToken(null);
      const nextSession = await switchRequest;
      if (!commitAuthoritySession(nextSession, authorityGeneration)) return;
      setAuthStatus("signed_in");
    } catch (error) {
      if (!isCurrentAuthority(authorityGeneration)) return;
      if (isSessionBoundaryError(error)) return;
      setAuthError(error instanceof Error ? error.message : "Organization scope could not be changed.");
      try {
        const restoredSession = await getAuthSession();
        if (!commitAuthoritySession(restoredSession, authorityGeneration)) return;
        setAuthStatus("signed_in");
      } catch (sessionError) {
        if (
          isCurrentAuthority(authorityGeneration)
          && !isSessionBoundaryError(sessionError)
        ) {
          handleUnauthorized();
        }
      }
    }
  }

  async function handleLogout() {
    const authorityGeneration = advanceAuthorityBoundary();
    closeTenantStream();
    setSession(null);
    setAuthStatus("switching");
    setAuthError(null);
    try {
      const logoutRequest = deleteAuthSession();
      setSessionCsrfToken(null);
      await logoutRequest;
      if (!isCurrentAuthority(authorityGeneration)) return;
      setAuthStatus("signed_out");
      await loadPersonas();
    } catch (error) {
      if (!isCurrentAuthority(authorityGeneration)) return;
      if (isSessionBoundaryError(error)) return;
      setAuthError(error instanceof Error ? error.message : "The session could not be closed.");
      try {
        const restoredSession = await getAuthSession();
        if (!commitAuthoritySession(restoredSession, authorityGeneration)) return;
        setAuthStatus("signed_in");
      } catch (sessionError) {
        if (
          isCurrentAuthority(authorityGeneration)
          && !isSessionBoundaryError(sessionError)
        ) {
          handleUnauthorized();
        }
      }
    }
  }

  if (authStatus === "checking" || authStatus === "switching") {
    return (
      <main className="identity-shell identity-transition" aria-live="polite">
        <span className="brand-mark">PagerAgent / authority checkpoint</span>
        <div>
          <p className="eyebrow">{authStatus === "checking" ? "Session bootstrap" : "Tenant boundary"}</p>
          <h1>{authStatus === "checking" ? "Verifying the signed session." : "Clearing the previous ledger."}</h1>
          <p>{authStatus === "checking" ? "No incident data loads before identity and scope are known." : "The live stream is closed before the next organization is opened."}</p>
        </div>
      </main>
    );
  }

  if (!session) {
    return (
      <IdentityCheckpoint
        error={authError}
        loading={personasLoading}
        onSignIn={handleSignIn}
        personas={personas}
        signingIn={signingIn}
      />
    );
  }

  return (
    <IncidentLedger
      authError={authError}
      onLogout={handleLogout}
      onStreamChange={handleStreamChange}
      onSwitchOrganization={handleSwitchOrganization}
      session={session}
    />
  );
}

interface IncidentLedgerProps {
  authError: string | null;
  onLogout: () => Promise<void>;
  onStreamChange: (stream: EventSource | null) => void;
  onSwitchOrganization: (organizationId: string) => Promise<void>;
  session: AuthSession;
}

function IncidentLedger({
  authError,
  onLogout,
  onStreamChange,
  onSwitchOrganization,
  session,
}: IncidentLedgerProps) {
  const [surface, setSurface] = useState<"incidents" | "connectors" | "access">("incidents");
  const [incidents, setIncidents] = useState<IncidentSummary[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<IncidentDetail | null>(null);
  const [loadingQueue, setLoadingQueue] = useState(true);
  const [loadingDetail, setLoadingDetail] = useState(false);
  const [connectionError, setConnectionError] = useState<string | null>(null);
  const [scorecard, setScorecard] = useState<EvaluationScorecard | null>(null);
  const [evaluationLoading, setEvaluationLoading] = useState(true);
  const [evaluationError, setEvaluationError] = useState<string | null>(null);
  const [transitionError, setTransitionError] = useState<string | null>(null);
  const [transitioning, setTransitioning] = useState(false);
  const [investigation, setInvestigation] = useState<InvestigationDetail | null>(null);
  const [investigationLoading, setInvestigationLoading] = useState(false);
  const [investigationRunning, setInvestigationRunning] = useState(false);
  const [investigationError, setInvestigationError] = useState<string | null>(null);
  const [proposal, setProposal] = useState<MitigationProposal | null>(null);
  const [proposalLoading, setProposalLoading] = useState(false);
  const [proposalActing, setProposalActing] = useState(false);
  const [proposalError, setProposalError] = useState<string | null>(null);
  const [collaborationOutputs, setCollaborationOutputs] = useState<CollaborationOutput[]>([]);
  const [collaborationLoading, setCollaborationLoading] = useState(false);
  const [collaborationActing, setCollaborationActing] = useState<string | null>(null);
  const [collaborationError, setCollaborationError] = useState<string | null>(null);
  const [postmortem, setPostmortem] = useState<PostmortemDetail | null>(null);
  const [postmortemLoading, setPostmortemLoading] = useState(false);
  const [postmortemActing, setPostmortemActing] = useState(false);
  const [postmortemError, setPostmortemError] = useState<string | null>(null);
  const [workflows, setWorkflows] = useState<WorkflowRun[]>([]);
  const [workflowLoading, setWorkflowLoading] = useState(false);
  const [workflowError, setWorkflowError] = useState<string | null>(null);
  const [workflowStreamStatus, setWorkflowStreamStatus] = useState<WorkflowStreamStatus>(
    () => (typeof EventSource === "undefined" ? "unsupported" : "connecting"),
  );
  const selectedIdRef = useRef<string | null>(selectedId);
  const selectionGenerationRef = useRef(0);
  const resourceGenerationRef = useRef<Record<IncidentResource, number>>({
    detail: 0,
    investigation: 0,
    proposal: 0,
    collaboration: 0,
    postmortem: 0,
    workflows: 0,
  });
  const actionGenerationRef = useRef<Record<IncidentAction, number>>({
    transition: 0,
    investigation: 0,
    proposal: 0,
    collaboration: 0,
    postmortem: 0,
  });
  const queueRequestGenerationRef = useRef(0);
  if (selectedIdRef.current !== selectedId) {
    selectedIdRef.current = selectedId;
    selectionGenerationRef.current += 1;
  }

  const beginResourceRequest = useCallback(
    (resource: IncidentResource, incidentId: string): ResourceRequestToken => ({
      incidentId,
      resource,
      selectionGeneration: selectionGenerationRef.current,
      requestGeneration: ++resourceGenerationRef.current[resource],
    }),
    [],
  );

  const isCurrentScope = useCallback((token: IncidentScopeToken): boolean => (
    selectedIdRef.current === token.incidentId
    && selectionGenerationRef.current === token.selectionGeneration
  ), []);

  const isCurrentResourceRequest = useCallback((token: ResourceRequestToken): boolean => (
    isCurrentScope(token)
    && resourceGenerationRef.current[token.resource] === token.requestGeneration
  ), [isCurrentScope]);

  const beginIncidentAction = useCallback(
    (action: IncidentAction, incidentId: string): IncidentActionToken => ({
      action,
      incidentId,
      selectionGeneration: selectionGenerationRef.current,
      actionGeneration: ++actionGenerationRef.current[action],
    }),
    [],
  );

  const isCurrentIncidentAction = useCallback((token: IncidentActionToken): boolean => (
    isCurrentScope(token)
    && actionGenerationRef.current[token.action] === token.actionGeneration
  ), [isCurrentScope]);

  const commitIncidentResource = useCallback(
    (resource: IncidentResource, token: IncidentActionToken, apply: () => void): boolean => {
      if (!isCurrentIncidentAction(token)) return false;
      resourceGenerationRef.current[resource] += 1;
      apply();
      return true;
    },
    [isCurrentIncidentAction],
  );

  const loadQueue = useCallback(async () => {
    const requestGeneration = ++queueRequestGenerationRef.current;
    try {
      const nextIncidents = await getIncidents();
      if (queueRequestGenerationRef.current === requestGeneration) {
        setIncidents(nextIncidents);
        setSelectedId((current) => {
          if (current && nextIncidents.some((incident) => incident.id === current)) return current;
          return nextIncidents[0]?.id ?? null;
        });
        setConnectionError(null);
      }
    } catch (error) {
      if (queueRequestGenerationRef.current === requestGeneration) {
        setConnectionError(
          error instanceof Error ? error.message : "PagerAgent API is unavailable.",
        );
      }
    } finally {
      if (queueRequestGenerationRef.current === requestGeneration) setLoadingQueue(false);
    }
  }, []);

  const loadEvaluation = useCallback(async () => {
    setEvaluationLoading(true);
    try {
      setScorecard(await getEvaluationScorecard());
      setEvaluationError(null);
    } catch (error) {
      setEvaluationError(
        error instanceof Error ? error.message : "Evaluation suite is unavailable.",
      );
    } finally {
      setEvaluationLoading(false);
    }
  }, []);

  const loadDetail = useCallback(async (incidentId: string) => {
    const request = beginResourceRequest("detail", incidentId);
    setLoadingDetail(true);
    try {
      const nextDetail = await getIncident(incidentId);
      if (isCurrentResourceRequest(request)) {
        setDetail(nextDetail);
        setConnectionError(null);
      }
    } catch (error) {
      if (isCurrentResourceRequest(request)) {
        setConnectionError(
          error instanceof Error ? error.message : "Incident evidence is unavailable.",
        );
      }
    } finally {
      if (isCurrentResourceRequest(request)) setLoadingDetail(false);
    }
  }, [beginResourceRequest, isCurrentResourceRequest]);

  const loadInvestigation = useCallback(async (incidentId: string) => {
    const request = beginResourceRequest("investigation", incidentId);
    setInvestigationLoading(true);
    try {
      const nextInvestigation = await getLatestInvestigation(incidentId);
      if (isCurrentResourceRequest(request)) {
        setInvestigation(nextInvestigation);
        setInvestigationError(null);
      }
    } catch (error) {
      if (isCurrentResourceRequest(request)) {
        setInvestigationError(
          error instanceof Error ? error.message : "Investigation evidence is unavailable.",
        );
      }
    } finally {
      if (isCurrentResourceRequest(request)) setInvestigationLoading(false);
    }
  }, [beginResourceRequest, isCurrentResourceRequest]);

  const loadProposal = useCallback(async (incidentId: string) => {
    const request = beginResourceRequest("proposal", incidentId);
    setProposalLoading(true);
    try {
      const nextProposal = await getLatestProposal(incidentId);
      if (isCurrentResourceRequest(request)) {
        setProposal(nextProposal);
        setProposalError(null);
      }
    } catch (error) {
      if (isCurrentResourceRequest(request)) {
        setProposalError(
          error instanceof Error ? error.message : "Copilot brief is unavailable.",
        );
      }
    } finally {
      if (isCurrentResourceRequest(request)) setProposalLoading(false);
    }
  }, [beginResourceRequest, isCurrentResourceRequest]);

  const loadCollaboration = useCallback(async (incidentId: string) => {
    const request = beginResourceRequest("collaboration", incidentId);
    setCollaborationLoading(true);
    try {
      const nextOutputs = await getCollaborationOutputs(incidentId);
      if (isCurrentResourceRequest(request)) {
        setCollaborationOutputs(nextOutputs);
        setCollaborationError(null);
      }
    } catch (error) {
      if (isCurrentResourceRequest(request)) {
        setCollaborationError(
          error instanceof Error ? error.message : "Collaboration receipts are unavailable.",
        );
      }
    } finally {
      if (isCurrentResourceRequest(request)) setCollaborationLoading(false);
    }
  }, [beginResourceRequest, isCurrentResourceRequest]);

  const loadPostmortem = useCallback(async (incidentId: string) => {
    const request = beginResourceRequest("postmortem", incidentId);
    setPostmortemLoading(true);
    try {
      const nextPostmortem = await getPostmortem(incidentId);
      if (isCurrentResourceRequest(request)) {
        setPostmortem(nextPostmortem);
        setPostmortemError(null);
      }
    } catch (error) {
      if (isCurrentResourceRequest(request)) {
        setPostmortemError(
          error instanceof Error ? error.message : "Postmortem is unavailable.",
        );
      }
    } finally {
      if (isCurrentResourceRequest(request)) setPostmortemLoading(false);
    }
  }, [beginResourceRequest, isCurrentResourceRequest]);

  const loadWorkflows = useCallback(async (incidentId: string) => {
    const request = beginResourceRequest("workflows", incidentId);
    setWorkflowLoading(true);
    try {
      const nextWorkflows = await getIncidentWorkflows(incidentId);
      if (isCurrentResourceRequest(request)) {
        setWorkflows((current) => {
          const merged = new Map(nextWorkflows.map((workflow) => [workflow.id, workflow]));
          current.forEach((workflow) => {
            const snapshot = merged.get(workflow.id);
            if (!snapshot || workflow.version > snapshot.version) merged.set(workflow.id, workflow);
          });
          return [...merged.values()].sort(newestWorkflowFirst);
        });
        setWorkflowError(null);
      }
    } catch (error) {
      if (isCurrentResourceRequest(request)) {
        setWorkflowError(
          error instanceof Error ? error.message : "Durable workflow records are unavailable.",
        );
      }
    } finally {
      if (isCurrentResourceRequest(request)) setWorkflowLoading(false);
    }
  }, [beginResourceRequest, isCurrentResourceRequest]);

  useEffect(() => {
    void loadQueue();
    if (hasPermission(session, "evaluations.run")) {
      void loadEvaluation();
    } else {
      setEvaluationLoading(false);
      setScorecard(null);
      setEvaluationError(null);
    }
    const refreshTimer = window.setInterval(
      () => void loadQueue(),
      RECONCILIATION_INTERVAL_MS,
    );
    return () => window.clearInterval(refreshTimer);
  }, [loadEvaluation, loadQueue, session]);

  useEffect(() => {
    setDetail(null);
    setLoadingDetail(false);
    setTransitionError(null);
    setTransitioning(false);
    setInvestigation(null);
    setInvestigationError(null);
    setInvestigationLoading(false);
    setInvestigationRunning(false);
    setProposal(null);
    setProposalError(null);
    setProposalLoading(false);
    setProposalActing(false);
    setCollaborationOutputs([]);
    setCollaborationError(null);
    setCollaborationLoading(false);
    setCollaborationActing(null);
    setPostmortem(null);
    setPostmortemError(null);
    setPostmortemLoading(false);
    setPostmortemActing(false);
    setWorkflows([]);
    setWorkflowError(null);
    setWorkflowLoading(false);
    if (selectedId) {
      void loadDetail(selectedId);
      void loadInvestigation(selectedId);
      void loadProposal(selectedId);
      void loadCollaboration(selectedId);
      void loadPostmortem(selectedId);
      void loadWorkflows(selectedId);
    }
  }, [loadCollaboration, loadDetail, loadInvestigation, loadPostmortem, loadProposal, loadWorkflows, selectedId]);

  useEffect(() => {
    if (!selectedId) return;
    const reconciliationTimer = window.setInterval(
      () => {
        void loadDetail(selectedId);
        void loadInvestigation(selectedId);
        void loadProposal(selectedId);
        void loadCollaboration(selectedId);
        void loadPostmortem(selectedId);
        void loadWorkflows(selectedId);
      },
      RECONCILIATION_INTERVAL_MS,
    );
    return () => window.clearInterval(reconciliationTimer);
  }, [loadCollaboration, loadDetail, loadInvestigation, loadPostmortem, loadProposal, loadWorkflows, selectedId]);

  useEffect(() => {
    if (typeof EventSource === "undefined") {
      setWorkflowStreamStatus("unsupported");
      return;
    }

    let stream: EventSource;
    try {
      stream = new EventSource(WORKFLOW_STREAM_URL);
    } catch {
      setWorkflowStreamStatus("unsupported");
      return;
    }
    onStreamChange(stream);

    setWorkflowStreamStatus("connecting");
    stream.onopen = () => setWorkflowStreamStatus("live");
    stream.onerror = () => setWorkflowStreamStatus("reconnecting");

    const handleWorkflow = (event: Event) => {
      try {
        const update = JSON.parse((event as MessageEvent<string>).data) as WorkflowStreamEvent;
        if (!update.workflow || update.workflow.id !== update.workflow_id) {
          throw new Error("Workflow stream payload is incomplete");
        }

        const changedResources = new Set(
          Array.isArray(update.payload.changed_resources)
            ? update.payload.changed_resources.filter(
                (resource): resource is string => typeof resource === "string",
              )
            : [],
        );
        const activeIncidentId = selectedIdRef.current;
        if (update.incident_id === activeIncidentId) {
          setWorkflows((current) => upsertWorkflow(current, update.workflow));
          setWorkflowError(null);
          const refreshes: Array<Promise<void>> = [];
          if (changedResources.has("incident")) {
            refreshes.push(loadDetail(update.incident_id));
          }
          if (changedResources.has("investigation")) {
            refreshes.push(loadInvestigation(update.incident_id));
          }
          if (changedResources.has("proposal")) {
            refreshes.push(loadProposal(update.incident_id));
          }
          if (changedResources.has("collaboration")) {
            refreshes.push(loadCollaboration(update.incident_id));
          }
          if (changedResources.has("postmortem")) {
            refreshes.push(loadPostmortem(update.incident_id));
          }
          if (refreshes.length > 0) void Promise.all(refreshes);
        }
        if (
          update.event_type === "workflow.queued" ||
          changedResources.has("incident")
        ) {
          void loadQueue();
        }
      } catch {
        setWorkflowError("A live workflow update could not be read; reconciliation remains active.");
      }
    };

    stream.addEventListener("workflow", handleWorkflow);
    return () => {
      stream.removeEventListener("workflow", handleWorkflow);
      stream.close();
      onStreamChange(null);
    };
  }, [loadCollaboration, loadDetail, loadInvestigation, loadPostmortem, loadProposal, loadQueue, onStreamChange]);

  async function handleTransition(toStatus: IncidentStatus, note: string) {
    if (!detail) return false;
    const incidentId = detail.id;
    const token = beginIncidentAction("transition", incidentId);
    setTransitioning(true);
    setTransitionError(null);
    try {
      const updated = await transitionIncident(incidentId, toStatus, detail.version, note);
      const current = commitIncidentResource("detail", token, () => setDetail(updated));
      await loadQueue();
      if (
        current
        && toStatus === "resolved"
        && isCurrentIncidentAction(token)
      ) {
        await loadPostmortem(incidentId);
      }
      return true;
    } catch (error) {
      if (isCurrentIncidentAction(token) && !isSessionBoundaryError(error)) {
        setTransitionError(error instanceof Error ? error.message : "Status change failed.");
        await loadDetail(incidentId);
      }
      return false;
    } finally {
      if (isCurrentIncidentAction(token)) setTransitioning(false);
    }
  }

  async function handleRunInvestigation() {
    if (!selectedId) return;
    const incidentId = selectedId;
    const token = beginIncidentAction("investigation", incidentId);
    setInvestigationRunning(true);
    setInvestigationError(null);
    try {
      const updated = await runInvestigation(incidentId);
      if (commitIncidentResource("investigation", token, () => setInvestigation(updated))) {
        await loadDetail(incidentId);
      }
    } catch (error) {
      if (isCurrentIncidentAction(token) && !isSessionBoundaryError(error)) {
        setInvestigationError(error instanceof Error ? error.message : "Investigation failed.");
      }
    } finally {
      if (isCurrentIncidentAction(token)) setInvestigationRunning(false);
    }
  }

  async function handleGenerateProposal() {
    if (!selectedId) return;
    const incidentId = selectedId;
    const token = beginIncidentAction("proposal", incidentId);
    setProposalActing(true);
    setProposalError(null);
    try {
      const updated = await generateProposal(incidentId);
      if (commitIncidentResource("proposal", token, () => setProposal(updated))) {
        await loadDetail(incidentId);
      }
    } catch (error) {
      if (isCurrentIncidentAction(token) && !isSessionBoundaryError(error)) {
        setProposalError(error instanceof Error ? error.message : "Brief generation failed.");
      }
    } finally {
      if (isCurrentIncidentAction(token)) setProposalActing(false);
    }
  }

  async function handleProposalDecision(decision: ProposalDecision, note: string) {
    if (!proposal || !selectedId) return;
    const incidentId = selectedId;
    const proposalSnapshot = proposal;
    const token = beginIncidentAction("proposal", incidentId);
    setProposalActing(true);
    setProposalError(null);
    try {
      const updated = await decideProposal(proposalSnapshot.id, decision, note);
      const current = commitIncidentResource("proposal", token, () => setProposal(updated));
      await Promise.all([current ? loadDetail(incidentId) : Promise.resolve(), loadQueue()]);
    } catch (error) {
      if (isCurrentIncidentAction(token) && !isSessionBoundaryError(error)) {
        setProposalError(error instanceof Error ? error.message : "Decision could not be recorded.");
        await loadProposal(incidentId);
      }
    } finally {
      if (isCurrentIncidentAction(token)) setProposalActing(false);
    }
  }

  async function handlePrepareCollaboration(kinds: CollaborationOutputKind[]) {
    if (!proposal || !selectedId) return;
    const incidentId = selectedId;
    const proposalSnapshot = proposal;
    const token = beginIncidentAction("collaboration", incidentId);
    setCollaborationActing("prepare");
    setCollaborationError(null);
    try {
      const prepared = await prepareCollaborationOutputs(incidentId, proposalSnapshot, kinds);
      commitIncidentResource("collaboration", token, () => {
        setCollaborationOutputs((current) => {
          const merged = new Map(current.map((output) => [output.id, output]));
          prepared.forEach((output) => merged.set(output.id, output));
          return [...merged.values()];
        });
      });
    } catch (error) {
      if (isCurrentIncidentAction(token) && !isSessionBoundaryError(error)) {
        setCollaborationError(
          error instanceof Error ? error.message : "Collaboration drafts could not be prepared.",
        );
        await loadCollaboration(incidentId);
      }
    } finally {
      if (isCurrentIncidentAction(token)) setCollaborationActing(null);
    }
  }

  async function handleCollaborationDecision(
    output: CollaborationOutput,
    decision: CollaborationDecision,
    note: string,
  ) {
    if (!selectedId) return;
    const incidentId = selectedId;
    const token = beginIncidentAction("collaboration", incidentId);
    setCollaborationActing(output.id);
    setCollaborationError(null);
    try {
      const updated = await decideCollaborationOutput(output, decision, note);
      const current = commitIncidentResource("collaboration", token, () => {
        setCollaborationOutputs((current) => current.map((candidate) => (
          candidate.id === updated.id ? updated : candidate
        )));
      });
      if (current) await loadWorkflows(incidentId);
    } catch (error) {
      if (isCurrentIncidentAction(token) && !isSessionBoundaryError(error)) {
        setCollaborationError(
          error instanceof Error ? error.message : "Publication decision could not be recorded.",
        );
        await loadCollaboration(incidentId);
      }
    } finally {
      if (isCurrentIncidentAction(token)) setCollaborationActing(null);
    }
  }

  async function handleGeneratePostmortem() {
    if (!selectedId) return;
    const incidentId = selectedId;
    const token = beginIncidentAction("postmortem", incidentId);
    setPostmortemActing(true);
    setPostmortemError(null);
    try {
      const updated = await generatePostmortem(incidentId);
      if (commitIncidentResource("postmortem", token, () => setPostmortem(updated))) {
        await loadDetail(incidentId);
      }
    } catch (error) {
      if (isCurrentIncidentAction(token) && !isSessionBoundaryError(error)) {
        setPostmortemError(error instanceof Error ? error.message : "Generation failed.");
      }
    } finally {
      if (isCurrentIncidentAction(token)) setPostmortemActing(false);
    }
  }

  async function handleSavePostmortem(edit: PostmortemEditPayload) {
    if (!postmortem || !selectedId) return;
    const incidentId = selectedId;
    const postmortemSnapshot = postmortem;
    const token = beginIncidentAction("postmortem", incidentId);
    setPostmortemActing(true);
    setPostmortemError(null);
    try {
      const updated = await updatePostmortem(postmortemSnapshot, edit);
      if (commitIncidentResource("postmortem", token, () => setPostmortem(updated))) {
        await loadDetail(incidentId);
      }
    } catch (error) {
      if (isCurrentIncidentAction(token) && !isSessionBoundaryError(error)) {
        setPostmortemError(error instanceof Error ? error.message : "Draft could not be saved.");
        await loadPostmortem(incidentId);
      }
    } finally {
      if (isCurrentIncidentAction(token)) setPostmortemActing(false);
    }
  }

  async function handleFinalizePostmortem(note: string) {
    if (!postmortem || !selectedId) return;
    const incidentId = selectedId;
    const postmortemSnapshot = postmortem;
    const token = beginIncidentAction("postmortem", incidentId);
    setPostmortemActing(true);
    setPostmortemError(null);
    try {
      const updated = await finalizePostmortem(postmortemSnapshot, note);
      if (commitIncidentResource("postmortem", token, () => setPostmortem(updated))) {
        await loadDetail(incidentId);
      }
    } catch (error) {
      if (isCurrentIncidentAction(token) && !isSessionBoundaryError(error)) {
        setPostmortemError(error instanceof Error ? error.message : "Finalization failed.");
        await loadPostmortem(incidentId);
      }
    } finally {
      if (isCurrentIncidentAction(token)) setPostmortemActing(false);
    }
  }

  const activeCount = incidents.filter((incident) => incident.status !== "resolved").length;

  return (
    <main className="app-shell">
      <AuthorityReceipt
        busy={false}
        connectionError={connectionError}
        error={authError}
        onLogout={onLogout}
        onSwitchOrganization={onSwitchOrganization}
        session={session}
      />

      <nav className="surface-switch" aria-label="PagerAgent surfaces">
        <span>Operational surface</span>
        <div>
          <button
            aria-pressed={surface === "incidents"}
            onClick={() => setSurface("incidents")}
            type="button"
          >
            Incident ledger
          </button>
          <button
            aria-pressed={surface === "connectors"}
            onClick={() => setSurface("connectors")}
            type="button"
          >
            Connector custody
          </button>
          {hasPermission(session, "memberships.read") ? (
            <button
              aria-pressed={surface === "access"}
              onClick={() => setSurface("access")}
              type="button"
            >
              Organization access
            </button>
          ) : null}
        </div>
      </nav>

      {surface === "access" ? (
        <OrganizationAccessPanel session={session} />
      ) : surface === "connectors" ? (
        <ConnectorCustodyPanel session={session} />
      ) : (
        <>

      <section className="command-header" aria-labelledby="page-title">
        <div>
          <p className="eyebrow">Incident command</p>
          <h1 id="page-title">What needs attention now.</h1>
        </div>
        <div className="active-counter">
          <strong>{String(activeCount).padStart(2, "0")}</strong>
          <span>active incidents</span>
        </div>
      </section>

      <ResponseProofRail
        incident={detail}
        investigation={investigation}
        postmortem={postmortem}
        proposal={proposal}
      />

      <EvaluationPanel
        canRun={hasPermission(session, "evaluations.run")}
        error={evaluationError}
        loading={evaluationLoading}
        onRefresh={loadEvaluation}
        scorecard={scorecard}
      />

      {connectionError ? (
        <div className="connection-error" role="alert">
          <span>{connectionError}</span>
          <button onClick={() => void loadQueue()} type="button">
            Retry connection
          </button>
        </div>
      ) : null}

      <div className="control-room">
        <IncidentQueue
          incidents={incidents}
          loading={loadingQueue}
          onSelect={setSelectedId}
          selectedId={selectedId}
        />
        <IncidentDetailPanel
          permissions={session.permissions}
          incident={detail}
          loading={loadingDetail}
          investigation={investigation}
          investigationError={investigationError}
          investigationLoading={investigationLoading}
          investigationRunning={investigationRunning}
          proposal={proposal}
          proposalActing={proposalActing}
          proposalError={proposalError}
          proposalLoading={proposalLoading}
          collaborationActing={collaborationActing}
          collaborationError={collaborationError}
          collaborationLoading={collaborationLoading}
          collaborationOutputs={collaborationOutputs}
          postmortem={postmortem}
          postmortemActing={postmortemActing}
          postmortemError={postmortemError}
          postmortemLoading={postmortemLoading}
          workflowError={workflowError}
          workflowLoading={workflowLoading}
          workflows={workflows}
          workflowStreamStatus={workflowStreamStatus}
          onGeneratePostmortem={handleGeneratePostmortem}
          onSavePostmortem={handleSavePostmortem}
          onFinalizePostmortem={handleFinalizePostmortem}
          onGenerateProposal={handleGenerateProposal}
          onProposalDecision={handleProposalDecision}
          onPrepareCollaboration={handlePrepareCollaboration}
          onCollaborationDecision={handleCollaborationDecision}
          onTransition={handleTransition}
          onRunInvestigation={handleRunInvestigation}
          transitionError={transitionError}
          transitioning={transitioning}
        />
      </div>
        </>
      )}
    </main>
  );
}
