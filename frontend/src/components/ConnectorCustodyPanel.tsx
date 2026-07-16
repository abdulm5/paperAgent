import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  ApiError,
  createConnector,
  getConnector,
  getConnectorEvents,
  getConnectors,
  hasPermission,
  rotateConnectorCredentials,
  updateConnector,
  validateConnector,
  type AuthSession,
  type ConnectorDetail,
  type ConnectorEvent,
  type ConnectorProvider,
  type ConnectorSummary,
} from "../lib/api";
import { formatTimestamp, titleCase } from "../lib/format";
import { AuthorityNote } from "./AuthorityNote";

interface ContractField {
  key: string;
  label: string;
  hint: string;
  inputMode?: "text" | "url";
  rendering?: "single-line" | "multiline";
}

interface ProviderContract {
  label: string;
  noun: string;
  configuration: ContractField[];
  credentials: ContractField[];
}

const PROVIDER_CONTRACTS: Record<ConnectorProvider, ProviderContract> = {
  github: {
    label: "GitHub App",
    noun: "repository evidence",
    configuration: [
      { key: "service", label: "Service binding", hint: "checkout-api" },
      { key: "repository", label: "Repository", hint: "owner/repository" },
      { key: "app_id", label: "App ID", hint: "GitHub App identifier" },
      { key: "installation_id", label: "Installation ID", hint: "Organization installation" },
    ],
    credentials: [
      {
        key: "private_key",
        label: "Private key",
        hint: "PEM-encoded GitHub App key",
        rendering: "multiline",
      },
      {
        key: "webhook_secret",
        label: "Webhook secret",
        hint: "GitHub webhook signing secret",
      },
    ],
  },
  prometheus: {
    label: "Prometheus",
    noun: "telemetry evidence",
    configuration: [
      { key: "base_url", label: "Base URL", hint: "https://metrics.example.com", inputMode: "url" },
    ],
    credentials: [
      { key: "bearer_token", label: "Bearer token", hint: "Read-only metrics token" },
    ],
  },
  slack: {
    label: "Slack",
    noun: "incident communications",
    configuration: [
      { key: "channel", label: "Channel", hint: "#incidents" },
    ],
    credentials: [
      { key: "bot_token", label: "Bot token", hint: "Workspace-scoped bot token" },
    ],
  },
};

interface ConnectorCustodyPanelProps {
  session: AuthSession;
}

function emptyValues(fields: ContractField[]): Record<string, string> {
  return Object.fromEntries(fields.map((field) => [field.key, ""]));
}

function configurationValues(
  detail: ConnectorDetail,
  fields: ContractField[],
): Record<string, string> {
  return Object.fromEntries(
    fields.map((field) => {
      const value = detail.configuration[field.key];
      return [field.key, typeof value === "string" || typeof value === "number" ? String(value) : ""];
    }),
  );
}

function messageFor(error: unknown, fallback: string): string {
  if (error instanceof ApiError && error.status === 409) {
    return `${fallback} The record changed elsewhere, so the custody receipt was refreshed.`;
  }
  return error instanceof ApiError ? fallback : error instanceof Error ? error.message : fallback;
}

function statusLabel(connector: Pick<ConnectorSummary, "status" | "enabled">): string {
  return connector.enabled ? titleCase(connector.status) : "Disabled";
}

export function ConnectorCustodyPanel({ session }: ConnectorCustodyPanelProps) {
  const canRead = hasPermission(session, "connectors.read");
  const canManage = hasPermission(session, "connectors.manage");
  const canValidate = hasPermission(session, "connectors.validate");
  const [connectors, setConnectors] = useState<ConnectorSummary[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<ConnectorDetail | null>(null);
  const [events, setEvents] = useState<ConnectorEvent[]>([]);
  const [loading, setLoading] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [acting, setActing] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [createProvider, setCreateProvider] = useState<ConnectorProvider>("github");
  const [createName, setCreateName] = useState("");
  const [createConfiguration, setCreateConfiguration] = useState<Record<string, string>>(
    () => emptyValues(PROVIDER_CONTRACTS.github.configuration),
  );
  const [createCredentials, setCreateCredentials] = useState<Record<string, string>>(
    () => emptyValues(PROVIDER_CONTRACTS.github.credentials),
  );
  const [editName, setEditName] = useState("");
  const [editConfiguration, setEditConfiguration] = useState<Record<string, string>>({});
  const [rotationCredentials, setRotationCredentials] = useState<Record<string, string>>({});
  const organizationIdRef = useRef(session.active_organization.id);
  const selectedIdRef = useRef<string | null>(selectedId);
  const canReadRef = useRef(canRead);
  const canManageRef = useRef(canManage);
  const canValidateRef = useRef(canValidate);
  const listGenerationRef = useRef(0);
  const receiptGenerationRef = useRef(0);
  const mutationGenerationRef = useRef(0);
  organizationIdRef.current = session.active_organization.id;
  selectedIdRef.current = selectedId;
  canReadRef.current = canRead;
  canManageRef.current = canManage;
  canValidateRef.current = canValidate;

  const activeContract = detail ? PROVIDER_CONTRACTS[detail.provider] : null;
  const createContract = PROVIDER_CONTRACTS[createProvider];
  const declaredCredentialFields = useMemo(() => {
    if (!detail) return [];
    return detail.credential_fields.length > 0
      ? detail.credential_fields
      : PROVIDER_CONTRACTS[detail.provider].credentials.map((field) => field.key);
  }, [detail]);
  const rotationCredentialFields = useMemo(() => {
    if (!detail) return [];
    const currentContractFields = PROVIDER_CONTRACTS[detail.provider].credentials.map(
      (field) => field.key,
    );
    return [...new Set([...declaredCredentialFields, ...currentContractFields])];
  }, [declaredCredentialFields, detail]);

  const loadList = useCallback(async (
    preferredId?: string,
    organizationId = organizationIdRef.current,
    expectedSelectedId?: string | null,
  ) => {
    const generation = ++listGenerationRef.current;
    let applied = false;
    const isCurrent = () => (
      generation === listGenerationRef.current &&
      organizationId === organizationIdRef.current &&
      canReadRef.current &&
      (expectedSelectedId === undefined || expectedSelectedId === selectedIdRef.current)
    );
    setLoading(true);
    try {
      const next = await getConnectors();
      if (!isCurrent()) return;
      setConnectors(next);
      const current = selectedIdRef.current;
      const nextSelectedId = preferredId && next.some((connector) => connector.id === preferredId)
        ? preferredId
        : current && next.some((connector) => connector.id === current)
          ? current
          : next[0]?.id ?? null;
      if (nextSelectedId !== current) {
        receiptGenerationRef.current += 1;
        mutationGenerationRef.current += 1;
        selectedIdRef.current = nextSelectedId;
        setSelectedId(nextSelectedId);
        setActing(null);
        setDetail(null);
        setEvents([]);
        setDetailLoading(nextSelectedId !== null);
      }
      setError(null);
      applied = true;
    } catch (nextError) {
      if (!isCurrent()) return;
      setError(messageFor(nextError, "Connector custody records are unavailable."));
    } finally {
      if (
        generation === listGenerationRef.current &&
        organizationId === organizationIdRef.current &&
        canReadRef.current &&
        (applied || expectedSelectedId === undefined || expectedSelectedId === selectedIdRef.current)
      ) {
        setLoading(false);
      }
    }
  }, []);

  const loadReceipt = useCallback(async (
    connectorId: string,
    organizationId = organizationIdRef.current,
  ) => {
    const generation = ++receiptGenerationRef.current;
    const isCurrent = () => (
      generation === receiptGenerationRef.current &&
      organizationId === organizationIdRef.current &&
      connectorId === selectedIdRef.current &&
      canReadRef.current
    );
    setDetailLoading(true);
    try {
      const [nextDetail, nextEvents] = await Promise.all([
        getConnector(connectorId),
        getConnectorEvents(connectorId),
      ]);
      if (!isCurrent()) return;
      setDetail(nextDetail);
      setEvents(nextEvents);
      setError(null);
    } catch (nextError) {
      if (!isCurrent()) return;
      setError(messageFor(nextError, "The selected custody receipt is unavailable."));
    } finally {
      if (isCurrent()) setDetailLoading(false);
    }
  }, []);

  useEffect(() => {
    listGenerationRef.current += 1;
    receiptGenerationRef.current += 1;
    mutationGenerationRef.current += 1;
    selectedIdRef.current = null;
    setSelectedId(null);
    setConnectors([]);
    setDetail(null);
    setEvents([]);
    setError(null);
    setActing(null);
    setCreating(false);
    setLoading(false);
    setDetailLoading(false);
    setCreateCredentials(emptyValues(PROVIDER_CONTRACTS[createProvider].credentials));
    setRotationCredentials({});
    if (!canRead) {
      return;
    }
    void loadList(undefined, session.active_organization.id);
    return () => {
      listGenerationRef.current += 1;
      receiptGenerationRef.current += 1;
      mutationGenerationRef.current += 1;
    };
  }, [canRead, loadList, session.active_organization.id]);

  useEffect(() => {
    if (canManage) return;
    setCreating(false);
    setCreateCredentials(emptyValues(PROVIDER_CONTRACTS[createProvider].credentials));
    setRotationCredentials({});
  }, [canManage, createProvider]);

  useEffect(() => {
    if (!canRead || !selectedId) {
      receiptGenerationRef.current += 1;
      setDetail(null);
      setEvents([]);
      setDetailLoading(false);
      return;
    }
    void loadReceipt(selectedId, session.active_organization.id);
    return () => {
      receiptGenerationRef.current += 1;
    };
  }, [canRead, loadReceipt, selectedId, session.active_organization.id]);

  useEffect(() => {
    if (!detail || !activeContract) return;
    setEditName(detail.name);
    setEditConfiguration(configurationValues(detail, activeContract.configuration));
    setRotationCredentials(Object.fromEntries(rotationCredentialFields.map((field) => [field, ""])));
  }, [activeContract, detail, rotationCredentialFields]);

  function changeCreateProvider(provider: ConnectorProvider) {
    setCreateProvider(provider);
    setCreateConfiguration(emptyValues(PROVIDER_CONTRACTS[provider].configuration));
    setCreateCredentials(emptyValues(PROVIDER_CONTRACTS[provider].credentials));
  }

  function isCurrentMutation(
    generation: number,
    organizationId: string,
    connectorId?: string,
  ): boolean {
    return generation === mutationGenerationRef.current &&
      organizationId === organizationIdRef.current &&
      canReadRef.current &&
      (connectorId === undefined || connectorId === selectedIdRef.current);
  }

  async function handleCreate(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const organizationId = organizationIdRef.current;
    const selectedAtStart = selectedIdRef.current;
    const generation = ++mutationGenerationRef.current;
    setActing("create");
    setError(null);
    const submittedCredentials = { ...createCredentials };
    setCreateCredentials(emptyValues(createContract.credentials));
    try {
      const created = await createConnector({
        name: createName.trim(),
        provider: createProvider,
        configuration: createConfiguration,
        credentials: submittedCredentials,
      });
      if (!isCurrentMutation(generation, organizationId) || !canManageRef.current) return;
      setCreateName("");
      setCreating(false);
      await loadList(created.id, organizationId, selectedAtStart);
    } catch (nextError) {
      if (!isCurrentMutation(generation, organizationId) || !canManageRef.current) return;
      setError(messageFor(nextError, "The connector could not be created."));
    } finally {
      if (isCurrentMutation(generation, organizationId)) setActing(null);
    }
  }

  async function handleSaveConfiguration(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!detail) return;
    const target = detail;
    const organizationId = organizationIdRef.current;
    const generation = ++mutationGenerationRef.current;
    setActing("configuration");
    setError(null);
    try {
      const updated = await updateConnector(target.id, {
        expected_version: target.version,
        name: editName.trim(),
        configuration: editConfiguration,
      });
      if (!isCurrentMutation(generation, organizationId, target.id) || !canManageRef.current) return;
      setDetail(updated);
      await Promise.all([
        loadList(updated.id, organizationId, target.id),
        loadReceipt(updated.id, organizationId),
      ]);
    } catch (nextError) {
      if (!isCurrentMutation(generation, organizationId, target.id) || !canManageRef.current) return;
      setError(messageFor(nextError, "Connector configuration could not be saved."));
      if (nextError instanceof ApiError && nextError.status === 409) {
        void loadReceipt(target.id, organizationId);
      }
    } finally {
      if (isCurrentMutation(generation, organizationId, target.id)) setActing(null);
    }
  }

  async function handleToggleEnabled() {
    if (!detail) return;
    const target = detail;
    const organizationId = organizationIdRef.current;
    const generation = ++mutationGenerationRef.current;
    setActing("enabled");
    setError(null);
    try {
      const updated = await updateConnector(target.id, {
        expected_version: target.version,
        enabled: !target.enabled,
      });
      if (!isCurrentMutation(generation, organizationId, target.id) || !canManageRef.current) return;
      setDetail(updated);
      await Promise.all([
        loadList(updated.id, organizationId, target.id),
        loadReceipt(updated.id, organizationId),
      ]);
    } catch (nextError) {
      if (!isCurrentMutation(generation, organizationId, target.id) || !canManageRef.current) return;
      setError(messageFor(nextError, "Connector availability could not be changed."));
      if (nextError instanceof ApiError && nextError.status === 409) {
        void loadReceipt(target.id, organizationId);
      }
    } finally {
      if (isCurrentMutation(generation, organizationId, target.id)) setActing(null);
    }
  }

  async function handleRotateCredentials(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!detail) return;
    const target = detail;
    const organizationId = organizationIdRef.current;
    const generation = ++mutationGenerationRef.current;
    setActing("credentials");
    setError(null);
    const submittedCredentials = { ...rotationCredentials };
    setRotationCredentials(Object.fromEntries(rotationCredentialFields.map((field) => [field, ""])));
    try {
      const updated = await rotateConnectorCredentials(
        target.id,
        target.version,
        submittedCredentials,
      );
      if (!isCurrentMutation(generation, organizationId, target.id) || !canManageRef.current) return;
      setDetail(updated);
      await Promise.all([
        loadList(updated.id, organizationId, target.id),
        loadReceipt(updated.id, organizationId),
      ]);
    } catch (nextError) {
      if (!isCurrentMutation(generation, organizationId, target.id) || !canManageRef.current) return;
      setError(messageFor(nextError, "Credentials could not be rotated."));
      if (nextError instanceof ApiError && nextError.status === 409) {
        void loadReceipt(target.id, organizationId);
      }
    } finally {
      if (isCurrentMutation(generation, organizationId, target.id)) setActing(null);
    }
  }

  async function handleValidate() {
    if (!detail) return;
    const target = detail;
    const organizationId = organizationIdRef.current;
    const generation = ++mutationGenerationRef.current;
    setActing("validate");
    setError(null);
    try {
      const updated = await validateConnector(target.id, target.version);
      if (!isCurrentMutation(generation, organizationId, target.id) || !canValidateRef.current) return;
      setDetail(updated);
      await Promise.all([
        loadList(updated.id, organizationId, target.id),
        loadReceipt(updated.id, organizationId),
      ]);
    } catch (nextError) {
      if (!isCurrentMutation(generation, organizationId, target.id) || !canValidateRef.current) return;
      setError(messageFor(nextError, "Credential custody could not be validated."));
      if (nextError instanceof ApiError && nextError.status === 409) {
        void loadReceipt(target.id, organizationId);
      }
    } finally {
      if (isCurrentMutation(generation, organizationId, target.id)) setActing(null);
    }
  }

  function closeCreateForm() {
    mutationGenerationRef.current += 1;
    setActing(null);
    setCreateCredentials(emptyValues(createContract.credentials));
    setCreating(false);
  }

  function selectConnector(connectorId: string) {
    if (connectorId === selectedIdRef.current) return;
    listGenerationRef.current += 1;
    receiptGenerationRef.current += 1;
    mutationGenerationRef.current += 1;
    selectedIdRef.current = connectorId;
    setActing(null);
    setError(null);
    setLoading(false);
    setDetailLoading(true);
    setRotationCredentials({});
    setDetail(null);
    setEvents([]);
    setSelectedId(connectorId);
  }

  if (!canRead) {
    return (
      <section className="custody-locked" aria-labelledby="custody-title">
        <p className="eyebrow">Connector custody</p>
        <h1 id="custody-title">The vault stays outside your authority.</h1>
        <p>
          Connector identities and audit receipts are limited to incident commanders and
          organization administrators. No connector data was requested for this role.
        </p>
        <code>connectors.read</code>
      </section>
    );
  }

  return (
    <section className="custody-surface" aria-labelledby="custody-title">
      <header className="custody-intro">
        <div>
          <p className="eyebrow">Connector custody</p>
          <h1 id="custody-title">Know what crosses the boundary.</h1>
        </div>
        <p>
          GitHub App connections now contribute repository evidence through a provider handshake.
          PagerAgent records every public contract, sealed credential change, and validation receipt.
        </p>
      </header>

      <ol className="custody-chain" aria-label="Credential custody chain">
        <li>
          <span>01 / provider contract</span>
          <strong>Declare the destination</strong>
          <small>Configuration remains inspectable.</small>
        </li>
        <li className="custody-vault-step">
          <span>02 / sealed vault</span>
          <strong>Write secret material once</strong>
          <small>Values never return to the browser.</small>
        </li>
        <li>
          <span>03 / permission-bound use</span>
          <strong>Authorize, validate, audit</strong>
          <small>Every change carries actor and version.</small>
        </li>
      </ol>

      {error ? <p className="custody-error" role="alert">{error}</p> : null}

      {creating && canManage ? (
        <form className="connector-create-ledger" onSubmit={handleCreate}>
          <header>
            <div>
              <p className="utility-label">New custody record</p>
              <h2>Declare a provider contract.</h2>
            </div>
            <button onClick={closeCreateForm} type="button">Close</button>
          </header>
          <div className="connector-form-grid">
            <label>
              Connector name
              <input
                onChange={(event) => setCreateName(event.target.value)}
                required
                value={createName}
              />
            </label>
            <label>
              Provider
              <select
                onChange={(event) => changeCreateProvider(event.target.value as ConnectorProvider)}
                value={createProvider}
              >
                {Object.entries(PROVIDER_CONTRACTS).map(([provider, contract]) => (
                  <option key={provider} value={provider}>{contract.label}</option>
                ))}
              </select>
            </label>
            {createContract.configuration.map((field) => (
              <ContractInput
                field={field}
                key={field.key}
                onChange={(value) => setCreateConfiguration((current) => ({ ...current, [field.key]: value }))}
                value={createConfiguration[field.key] ?? ""}
              />
            ))}
          </div>
          <fieldset className="secret-envelope">
            <legend>Sealed credential envelope</legend>
            <p>Write-only fields are blank by design and are cleared after the record is sealed.</p>
            <div>
              {createContract.credentials.map((field) => (
                <SecretInput
                  field={field}
                  key={field.key}
                  onChange={(value) => setCreateCredentials((current) => ({ ...current, [field.key]: value }))}
                  value={createCredentials[field.key] ?? ""}
                />
              ))}
            </div>
          </fieldset>
          <footer>
            <span>{createContract.label} · {createContract.noun}</span>
            <button disabled={acting !== null} type="submit">
              {acting === "create" ? "Sealing record…" : "Seal custody record"}
            </button>
          </footer>
        </form>
      ) : null}

      <div className="connector-ledger">
        <aside className="connector-index" aria-label="Connector records">
          <header>
            <div>
              <span>Organization ledger</span>
              <strong>{connectors.length.toString().padStart(2, "0")} records</strong>
            </div>
            {canManage ? (
              <button onClick={() => setCreating(true)} type="button">New contract</button>
            ) : null}
          </header>
          {loading ? <p className="connector-placeholder">Reading custody ledger…</p> : null}
          {!loading && connectors.length === 0 ? (
            <p className="connector-placeholder">
              No provider contract is sealed yet.{canManage ? " Declare the first one above." : " An administrator must create one."}
            </p>
          ) : null}
          <ul>
            {connectors.map((connector, index) => (
              <li key={connector.id}>
                <button
                  aria-current={selectedId === connector.id ? "true" : undefined}
                  className={selectedId === connector.id ? "selected" : undefined}
                  onClick={() => selectConnector(connector.id)}
                  type="button"
                >
                  <span>{String(index + 1).padStart(2, "0")}</span>
                  <span>
                    <strong>{connector.name}</strong>
                    <small>{PROVIDER_CONTRACTS[connector.provider].label}</small>
                  </span>
                  <i className={`connector-status status-${connector.status}`}>{statusLabel(connector)}</i>
                </button>
              </li>
            ))}
          </ul>
        </aside>

        <div className="custody-receipt">
          {detailLoading && !detail ? <p className="connector-placeholder">Opening custody receipt…</p> : null}
          {!detailLoading && !detail ? (
            <div className="connector-empty-receipt">
              <span aria-hidden="true">⌁</span>
              <h2>No custody receipt selected.</h2>
              <p>Select a connector record or declare a new provider contract.</p>
            </div>
          ) : null}
          {detail ? (
            <>
              <header className="custody-receipt-header">
                <div>
                  <p>{PROVIDER_CONTRACTS[detail.provider].label} / custody receipt</p>
                  <h2>{detail.name}</h2>
                </div>
                <div>
                  <span className={`connector-status status-${detail.status}`}>{statusLabel(detail)}</span>
                  <code>record v{detail.version}</code>
                </div>
              </header>

              <div className="custody-proof-strip">
                <div>
                  <span>Provider input</span>
                  <strong>{activeContract?.configuration.length ?? 0} declared fields</strong>
                  <small>{activeContract?.noun}</small>
                </div>
                <div className="sealed-proof">
                  <span>Sealed vault</span>
                  <strong>{declaredCredentialFields.length} credential field{declaredCredentialFields.length === 1 ? "" : "s"}</strong>
                  <small>envelope v{detail.credential_version}</small>
                </div>
                <div>
                  <span>Authorized use</span>
                  <strong>{detail.enabled ? "Enabled" : "Disabled"}</strong>
                  <small>{detail.last_validated_at ? `checked ${formatTimestamp(detail.last_validated_at)}` : "not yet validated"}</small>
                </div>
              </div>

              {!detail.enabled ? (
                <p className="custody-next-step" role="status">
                  <strong>Sealed, not active.</strong>
                  Run the custody check, review its receipt, then explicitly enable this connector.
                </p>
              ) : null}

              <section className="declared-contract" aria-labelledby="declared-contract-title">
                <header>
                  <div>
                    <p className="utility-label">Inspectable contract</p>
                    <h3 id="declared-contract-title">Configuration and secret field names</h3>
                  </div>
                  <span>Secret values are never returned</span>
                </header>
                <dl>
                  {activeContract?.configuration.map((field) => (
                    <div key={field.key}>
                      <dt><code>{field.key}</code></dt>
                      <dd>{String(detail.configuration[field.key] ?? "Not declared")}</dd>
                    </div>
                  ))}
                  {declaredCredentialFields.map((field) => (
                    <div className="secret-field-row" key={field}>
                      <dt><code>{field}</code></dt>
                      <dd><span aria-label="Sealed credential">Sealed · write-only</span></dd>
                    </div>
                  ))}
                </dl>
              </section>

              {canManage && activeContract ? (
                <div className="custody-controls">
                  <form onSubmit={handleSaveConfiguration}>
                    <header>
                      <p className="utility-label">Provider contract</p>
                      <h3>Update inspectable fields</h3>
                    </header>
                    <label>
                      Connector name
                      <input onChange={(event) => setEditName(event.target.value)} required value={editName} />
                    </label>
                    {activeContract.configuration.map((field) => (
                      <ContractInput
                        field={field}
                        key={field.key}
                        onChange={(value) => setEditConfiguration((current) => ({ ...current, [field.key]: value }))}
                        value={editConfiguration[field.key] ?? ""}
                      />
                    ))}
                    <div className="custody-button-row">
                      <button disabled={acting !== null} type="submit">
                        {acting === "configuration" ? "Saving…" : "Save contract"}
                      </button>
                      <button
                        disabled={
                          acting !== null ||
                          (!detail.enabled && detail.last_validation_ok !== true)
                        }
                        onClick={() => void handleToggleEnabled()}
                        type="button"
                      >
                        {acting === "enabled" ? "Updating…" : detail.enabled ? "Disable connector" : "Enable connector"}
                      </button>
                    </div>
                  </form>

                  <form className="credential-rotation" onSubmit={handleRotateCredentials}>
                    <header>
                      <p className="utility-label">Credential envelope</p>
                      <h3>Rotate sealed values</h3>
                    </header>
                    <p>Existing values cannot be viewed. Every field starts and returns blank.</p>
                    {rotationCredentialFields.map((fieldKey) => {
                      const field = activeContract.credentials.find((candidate) => candidate.key === fieldKey) ?? {
                        key: fieldKey,
                        label: titleCase(fieldKey),
                        hint: "Replacement secret",
                      };
                      return (
                        <SecretInput
                          field={field}
                          key={fieldKey}
                          onChange={(value) => setRotationCredentials((current) => ({ ...current, [fieldKey]: value }))}
                          value={rotationCredentials[fieldKey] ?? ""}
                        />
                      );
                    })}
                    <button disabled={acting !== null} type="submit">
                      {acting === "credentials" ? "Rotating…" : "Rotate credentials"}
                    </button>
                  </form>
                </div>
              ) : (
                <AuthorityNote
                  allowed={canManage}
                  message="Incident commanders can inspect custody, but only administrators can change provider contracts or secret envelopes."
                  permission="connectors.manage"
                />
              )}

              <section className="custody-validation" aria-labelledby="validation-title">
                <div>
                  <p className="utility-label">Custody check</p>
                  <h3 id="validation-title">Validate without revealing.</h3>
                  <p>
                    {!detail.last_validated_at
                      ? "No validation receipt has been recorded."
                      : detail.last_validation_ok === false || detail.status === "invalid"
                        ? "Validation failed. Rotate the credentials or correct the contract before enabling."
                        : detail.last_validation_ok !== true
                          ? "This legacy receipt does not attest to a successful validation. Validate again before enabling."
                          : detail.provider === "github"
                            ? "GitHub App provider handshake succeeded."
                            : "PagerAgent verified the local contract and sealed-envelope integrity."}
                    {detail.last_validation_message ? (
                      <span className="validation-server-message">
                        Validation receipt: {detail.last_validation_message}
                      </span>
                    ) : null}
                  </p>
                </div>
                <div>
                  <button
                    disabled={!canValidate || acting !== null}
                    onClick={() => void handleValidate()}
                    type="button"
                  >
                    {acting === "validate" ? "Validating…" : "Validate custody"}
                  </button>
                  <AuthorityNote
                    allowed={canValidate}
                    message={detail.provider === "github"
                      ? "Only administrators can run the GitHub App handshake and vault-integrity check."
                      : "Only administrators can run the local contract and vault-integrity check."}
                    permission="connectors.validate"
                  />
                </div>
              </section>

              <section className="custody-audit" aria-labelledby="audit-title">
                <header>
                  <div>
                    <p className="utility-label">Append-only custody audit</p>
                    <h3 id="audit-title">Who changed the boundary.</h3>
                  </div>
                  <span>{events.length} events</span>
                </header>
                {events.length === 0 ? <p>No custody changes have been recorded.</p> : null}
                <ol>
                  {events.map((event) => (
                    <li key={event.id}>
                      <span>{formatTimestamp(event.created_at)}</span>
                      <div>
                        <strong>{titleCase(event.event_type.replaceAll(".", " "))}</strong>
                        <small>{event.actor} · record v{event.connector_version}</small>
                      </div>
                      <code>{Object.keys(event.payload).sort().join(" · ") || "receipt only"}</code>
                    </li>
                  ))}
                </ol>
              </section>
            </>
          ) : null}
        </div>
      </div>
    </section>
  );
}

interface FieldInputProps {
  field: ContractField;
  onChange: (value: string) => void;
  value: string;
}

function ContractInput({ field, onChange, value }: FieldInputProps) {
  return (
    <label>
      <span>{field.label} <code>{field.key}</code></span>
      <input
        inputMode={field.inputMode}
        onChange={(event) => onChange(event.target.value)}
        placeholder={field.hint}
        required
        type={field.inputMode === "url" ? "url" : "text"}
        value={value}
      />
    </label>
  );
}

function SecretInput({ field, onChange, value }: FieldInputProps) {
  const accessibilityLabel = `${field.label} (${field.key}, write only)`;
  if (field.rendering === "multiline") {
    return (
      <label>
        <span>{field.label} <code>{field.key}</code></span>
        <textarea
          aria-label={accessibilityLabel}
          autoComplete="new-password"
          autoCapitalize="none"
          onChange={(event) => onChange(event.target.value)}
          onPaste={(event) => {
            const pasted = event.clipboardData.getData("text/plain");
            event.preventDefault();
            const start = rawIndexForTextareaIndex(value, event.currentTarget.selectionStart ?? 0);
            const end = rawIndexForTextareaIndex(value, event.currentTarget.selectionEnd ?? 0);
            onChange(`${value.slice(0, start)}${pasted}${value.slice(end)}`);
          }}
          placeholder={field.hint}
          required
          rows={7}
          spellCheck={false}
          value={value}
        />
      </label>
    );
  }
  return (
    <label>
      <span>{field.label} <code>{field.key}</code></span>
      <input
        aria-label={accessibilityLabel}
        autoComplete="new-password"
        autoCapitalize="none"
        onChange={(event) => onChange(event.target.value)}
        placeholder={field.hint}
        required
        spellCheck={false}
        type="password"
        value={value}
      />
    </label>
  );
}

function rawIndexForTextareaIndex(value: string, textareaIndex: number): number {
  let rawIndex = 0;
  let displayedIndex = 0;
  while (rawIndex < value.length && displayedIndex < textareaIndex) {
    rawIndex += value[rawIndex] === "\r" && value[rawIndex + 1] === "\n" ? 2 : 1;
    displayedIndex += 1;
  }
  return rawIndex;
}
