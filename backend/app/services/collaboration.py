from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from secrets import token_hex
from typing import Any, Protocol
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.auth.constants import DEFAULT_ORGANIZATION_ID
from app.connectors.contracts import ConnectorContractError, validate_configuration
from app.connectors.runtime import (
    GithubConnectorCustodyUnavailableError,
    GithubConnectorUnavailableError,
    SlackConnectorCustodyUnavailableError,
    SlackConnectorUnavailableError,
    load_github_connector_runtime,
    load_slack_connector_runtime,
)
from app.core.config import settings
from app.core.telemetry import current_trace_id
from app.db.models import (
    CollaborationDecisionRecord,
    CollaborationDeliveryRecord,
    CollaborationOutputRecord,
    ConnectorRecord,
    IncidentEventRecord,
    IncidentRecord,
    MitigationProposalRecord,
)
from app.domain.collaboration import (
    CollaborationDecision,
    CollaborationDecisionDetail,
    CollaborationDecisionRequest,
    CollaborationDeliveryReceipt,
    CollaborationOutputCreateInput,
    CollaborationOutputDetail,
    CollaborationOutputKind,
    CollaborationOutputStatus,
    CollaborationProvider,
)
from app.domain.connectors import ConnectorProvider, ConnectorStatus
from app.domain.workflows import WorkflowType
from app.investigation.text import canonical_hash
from app.workflows.errors import PermanentWorkflowError, RetryableWorkflowError
from app.workflows.fencing import WorkflowFence, commit_with_fence
from app.workflows.store import WorkflowStore


class Publisher(Protocol):
    def publish(self, *args: Any, **kwargs: Any) -> Any: ...

    def close(self) -> None: ...


PublisherFactory = Callable[[Any, Any], Publisher]


class CollaborationOutputNotFoundError(LookupError):
    pass


class CollaborationProposalConflictError(RuntimeError):
    pass


class CollaborationOutputConflictError(RuntimeError):
    pass


class CollaborationConnectorUnavailableError(RuntimeError):
    pass


class CollaborationDraftBuilder:
    """Build immutable outbound content only from already-grounded proposal fields."""

    version = "grounded-collaboration-v1"

    @staticmethod
    def _bounded(value: str, maximum: int) -> str:
        normalized = " ".join(value.split())
        if len(normalized) <= maximum:
            return normalized
        return normalized[: maximum - 1].rstrip() + "…"

    @staticmethod
    def _evidence_ids(proposal: MitigationProposalRecord) -> list[str]:
        identifiers = {
            str(identifier)
            for claim in proposal.claims
            if isinstance(claim, dict)
            for identifier in claim.get("evidence_ids", [])
            if isinstance(identifier, str)
        }
        return sorted(identifiers)[:24]

    def slack_payload(
        self,
        incident: IncidentRecord,
        proposal: MitigationProposalRecord,
    ) -> dict[str, str]:
        evidence = ", ".join(item[:8] for item in self._evidence_ids(proposal)) or "none"
        text = "\n".join(
            (
                f":rotating_light: *PagerAgent incident update — {incident.service}*",
                f"*Status:* {incident.status} · *Severity:* {incident.severity}",
                f"*Impact:* {self._bounded(proposal.impact_summary, 600)}",
                (
                    f"*Likely cause ({proposal.confidence:.0%}):* "
                    f"{self._bounded(proposal.root_cause_summary, 600)}"
                ),
                f"*Recommended action:* {self._bounded(proposal.recommended_action, 600)}",
                f"*Risk:* {self._bounded(proposal.risk_summary, 500)}",
                f"*Evidence receipts:* {evidence}",
                f"*PagerAgent incident:* `{incident.id}`",
            )
        )
        return {"text": text}

    def github_payload(
        self,
        output_id: UUID,
        incident: IncidentRecord,
        proposal: MitigationProposalRecord,
    ) -> dict[str, str]:
        evidence = self._evidence_ids(proposal)
        verification = "\n".join(
            f"- [ ] {self._bounded(step, 500)}"
            for step in proposal.verification_steps[:20]
        ) or "- [ ] Re-run the PagerAgent incident verification plan."
        evidence_list = "\n".join(f"- `{item}`" for item in evidence) or "- None recorded"
        title = self._bounded(
            f"[PagerAgent][{incident.severity.upper()}] {incident.service} incident",
            240,
        )
        body = "\n\n".join(
            (
                "## Incident",
                (
                    f"PagerAgent incident `{incident.id}` is **{incident.status}**. "
                    f"This issue was prepared from grounded proposal `{proposal.id}` and "
                    "required a separate human approval before delivery."
                ),
                "## Customer impact",
                self._bounded(proposal.impact_summary, 2_000),
                f"## Likely cause ({proposal.confidence:.0%} confidence)",
                self._bounded(proposal.root_cause_summary, 2_000),
                "## Recommended action",
                self._bounded(proposal.recommended_action, 2_000),
                "## Risk",
                self._bounded(proposal.risk_summary, 2_000),
                "## Verification",
                verification,
                "## Evidence receipts",
                evidence_list,
                f"<!-- pageragent-delivery:{output_id} -->",
            )
        )
        return {"title": title, "body": body}


class CollaborationService:
    def __init__(
        self,
        session: Session,
        *,
        organization_id: UUID = DEFAULT_ORGANIZATION_ID,
        draft_builder: CollaborationDraftBuilder | None = None,
        slack_publisher_factory: PublisherFactory | None = None,
        github_publisher_factory: PublisherFactory | None = None,
    ) -> None:
        self.session = session
        self.organization_id = organization_id
        self.draft_builder = draft_builder or CollaborationDraftBuilder()
        self.slack_publisher_factory = slack_publisher_factory
        self.github_publisher_factory = github_publisher_factory

    def prepare(
        self,
        incident_id: UUID,
        request: CollaborationOutputCreateInput,
        *,
        actor: str,
    ) -> list[CollaborationOutputDetail]:
        incident = self._load_incident(incident_id)
        proposal = self.session.scalar(
            select(MitigationProposalRecord).where(
                MitigationProposalRecord.id == request.proposal_id,
                MitigationProposalRecord.incident_id == incident.id,
            )
        )
        if proposal is None:
            raise CollaborationOutputNotFoundError
        if proposal.input_hash != request.expected_proposal_hash:
            raise CollaborationProposalConflictError(
                "The grounded proposal changed before collaboration content was prepared"
            )

        results: list[CollaborationOutputRecord] = []
        for kind in request.kinds:
            existing = self.session.scalar(
                select(CollaborationOutputRecord).where(
                    CollaborationOutputRecord.proposal_id == proposal.id,
                    CollaborationOutputRecord.kind == kind.value,
                )
            )
            if existing is not None:
                results.append(existing)
                continue

            provider = self._provider_for_kind(kind)
            connector, configuration = self._select_connector(
                incident.service,
                provider,
            )
            if connector.credential is None:
                raise CollaborationConnectorUnavailableError(
                    "The selected collaboration connector has no credential envelope"
                )
            output_id = uuid4()
            if kind is CollaborationOutputKind.SLACK_UPDATE:
                destination = str(configuration["channel"])
                payload = self.draft_builder.slack_payload(incident, proposal)
            else:
                destination = str(configuration["repository"])
                payload = self.draft_builder.github_payload(output_id, incident, proposal)
            content_sha256 = self._content_hash(
                proposal_hash=proposal.input_hash,
                kind=kind.value,
                provider=provider.value,
                destination=destination,
                payload=payload,
                connector_id=connector.id,
                connector_version=connector.version,
                credential_version=connector.credential.credential_version,
            )
            output = CollaborationOutputRecord(
                id=output_id,
                organization_id=self.organization_id,
                incident_id=incident.id,
                proposal_id=proposal.id,
                connector_id=connector.id,
                kind=kind.value,
                provider=provider.value,
                status=CollaborationOutputStatus.PENDING_APPROVAL.value,
                version=1,
                destination=destination,
                payload=payload,
                content_sha256=content_sha256,
                connector_version=connector.version,
                credential_version=connector.credential.credential_version,
                requested_by=actor,
            )
            self.session.add(output)
            self.session.add(
                IncidentEventRecord(
                    incident_id=incident.id,
                    event_type="collaboration.output_prepared",
                    actor=actor,
                    from_status=None,
                    to_status=incident.status,
                    note="External collaboration content prepared for a separate approval.",
                    payload={
                        "output_id": str(output.id),
                        "kind": kind.value,
                        "content_sha256": content_sha256,
                    },
                )
            )
            results.append(output)
        try:
            self.session.commit()
        except IntegrityError:
            self.session.rollback()
            # A concurrent preparation can win the unique proposal/kind key.
            results = list(
                self.session.scalars(
                    select(CollaborationOutputRecord).where(
                        CollaborationOutputRecord.proposal_id == proposal.id,
                        CollaborationOutputRecord.kind.in_([kind.value for kind in request.kinds]),
                    )
                ).all()
            )
            if len(results) != len(request.kinds):
                raise
        return [self.get(output.id) for output in results]

    def list_for_incident(self, incident_id: UUID) -> list[CollaborationOutputDetail]:
        self._load_incident(incident_id)
        records = self.session.scalars(
            self._detail_query().where(
                CollaborationOutputRecord.incident_id == incident_id,
                CollaborationOutputRecord.organization_id == self.organization_id,
            ).order_by(CollaborationOutputRecord.requested_at, CollaborationOutputRecord.id)
        ).all()
        return [self._to_detail(record) for record in records]

    def get(self, output_id: UUID) -> CollaborationOutputDetail:
        record = self.session.scalar(
            self._detail_query()
            .where(
                CollaborationOutputRecord.id == output_id,
                CollaborationOutputRecord.organization_id == self.organization_id,
            )
            .execution_options(populate_existing=True)
        )
        if record is None:
            raise CollaborationOutputNotFoundError
        return self._to_detail(record)

    def decide(
        self,
        output_id: UUID,
        request: CollaborationDecisionRequest,
    ) -> CollaborationOutputDetail:
        output = self.session.scalar(
            select(CollaborationOutputRecord)
            .where(
                CollaborationOutputRecord.id == output_id,
                CollaborationOutputRecord.organization_id == self.organization_id,
            )
            .options(selectinload(CollaborationOutputRecord.delivery))
            .with_for_update()
        )
        if output is None:
            raise CollaborationOutputNotFoundError
        if output.version != request.expected_version:
            raise CollaborationOutputConflictError(
                f"Collaboration output changed; current version is {output.version}"
            )
        if output.content_sha256 != request.expected_content_sha256:
            raise CollaborationOutputConflictError(
                "Collaboration content changed before the decision was recorded"
            )
        if output.status != CollaborationOutputStatus.PENDING_APPROVAL.value:
            raise CollaborationOutputConflictError(
                f"Collaboration output is already {output.status}"
            )
        incident = self.session.scalar(
            select(IncidentRecord)
            .where(
                IncidentRecord.id == output.incident_id,
                IncidentRecord.organization_id == self.organization_id,
            )
            .with_for_update()
        )
        connector = self.session.scalar(
            select(ConnectorRecord)
            .where(
                ConnectorRecord.id == output.connector_id,
                ConnectorRecord.organization_id == self.organization_id,
            )
            .options(selectinload(ConnectorRecord.credential))
            .with_for_update()
        )
        if incident is None or connector is None or connector.credential is None:
            raise CollaborationConnectorUnavailableError(
                "The approved collaboration destination is unavailable"
            )
        if request.decision is CollaborationDecision.APPROVE and (
            not connector.enabled
            or connector.status != ConnectorStatus.CONFIGURED.value
            or connector.version != output.connector_version
            or connector.credential.credential_version != output.credential_version
        ):
            raise CollaborationConnectorUnavailableError(
                "The connector changed after preview; prepare and approve a new output"
            )

        now = datetime.now(UTC)
        output.decided_at = now
        output.version += 1
        self.session.add(
            CollaborationDecisionRecord(
                output_id=output.id,
                decision=request.decision.value,
                actor=request.actor,
                note=request.note,
            )
        )
        if request.decision is CollaborationDecision.REJECT:
            output.status = CollaborationOutputStatus.REJECTED.value
            event_type = "collaboration.output_rejected"
            note = "External collaboration output rejected; nothing was queued."
        else:
            output.status = CollaborationOutputStatus.QUEUED.value
            output.delivery = CollaborationDeliveryRecord(
                output_id=output.id,
                idempotency_key=f"collaboration:{output.id}",
                status="prepared",
                attempt_count=0,
                provider_receipt={},
            )
            run = WorkflowStore(self.session, self.organization_id).enqueue(
                incident.id,
                WorkflowType.COLLABORATION,
                "deliver_collaboration_output",
                f"collaboration-output:{output.id}",
                max_attempts=settings.workflow_max_attempts,
                payload={"collaboration_output_id": str(output.id)},
                trace_id=current_trace_id() or token_hex(16),
            )
            output.workflow_run_id = run.id
            event_type = "collaboration.output_approved"
            note = "External collaboration output approved and durably queued."
        self.session.add(
            IncidentEventRecord(
                incident_id=incident.id,
                event_type=event_type,
                actor=request.actor,
                from_status=None,
                to_status=incident.status,
                note=request.note or note,
                payload={
                    "output_id": str(output.id),
                    "kind": output.kind,
                    "content_sha256": output.content_sha256,
                    "version": output.version,
                },
            )
        )
        self.session.commit()
        return self.get(output.id)

    def deliver(
        self,
        output_id: UUID,
        *,
        fence: WorkflowFence | None = None,
    ) -> CollaborationOutputDetail:
        output = self.session.scalar(
            select(CollaborationOutputRecord)
            .where(
                CollaborationOutputRecord.id == output_id,
                CollaborationOutputRecord.organization_id == self.organization_id,
            )
            .options(selectinload(CollaborationOutputRecord.delivery))
        )
        if output is None or output.delivery is None:
            raise PermanentWorkflowError(
                "Approved collaboration output is unavailable",
                code="collaboration_output_unavailable",
            )
        if output.status == CollaborationOutputStatus.DELIVERED.value:
            return self.get(output.id)
        if output.status not in {
            CollaborationOutputStatus.QUEUED.value,
            CollaborationOutputStatus.DELIVERING.value,
            CollaborationOutputStatus.RETRY_SCHEDULED.value,
        }:
            raise PermanentWorkflowError(
                "Collaboration output is not approved for delivery",
                code="collaboration_output_not_approved",
            )

        try:
            if output.provider == CollaborationProvider.SLACK.value:
                runtime = load_slack_connector_runtime(self.session, output.connector_id)
            else:
                runtime = load_github_connector_runtime(self.session, output.connector_id)
        except (
            SlackConnectorCustodyUnavailableError,
            GithubConnectorCustodyUnavailableError,
        ) as error:
            self.session.rollback()
            raise RetryableWorkflowError(
                "Approved collaboration connector custody is temporarily unavailable",
                code="collaboration_connector_custody_unavailable",
            ) from error
        except (SlackConnectorUnavailableError, GithubConnectorUnavailableError) as error:
            self.session.rollback()
            raise PermanentWorkflowError(
                "Approved collaboration connector is unavailable",
                code="collaboration_connector_unavailable",
            ) from error
        if runtime.organization_id != self.organization_id:
            self.session.rollback()
            raise PermanentWorkflowError(
                "Approved collaboration connector ownership changed",
                code="collaboration_connector_unavailable",
            )
        self.session.rollback()

        output = self.session.scalar(
            select(CollaborationOutputRecord)
            .where(
                CollaborationOutputRecord.id == output_id,
                CollaborationOutputRecord.organization_id == self.organization_id,
            )
            .options(selectinload(CollaborationOutputRecord.delivery))
            .with_for_update()
        )
        if output is None or output.delivery is None:
            raise PermanentWorkflowError(
                "Approved collaboration output disappeared",
                code="collaboration_output_unavailable",
            )
        if output.status == CollaborationOutputStatus.DELIVERED.value:
            self.session.rollback()
            return self.get(output.id)
        connector = self.session.scalar(
            select(ConnectorRecord)
            .where(
                ConnectorRecord.id == output.connector_id,
                ConnectorRecord.organization_id == self.organization_id,
            )
            .options(selectinload(ConnectorRecord.credential))
            .with_for_update()
        )
        if connector is None or connector.credential is None:
            self.session.rollback()
            raise PermanentWorkflowError(
                "Approved collaboration connector is unavailable",
                code="collaboration_connector_unavailable",
            )
        if (
            not connector.enabled
            or connector.status != ConnectorStatus.CONFIGURED.value
            or connector.version != output.connector_version
            or connector.credential.credential_version != output.credential_version
            or runtime.connector_version != output.connector_version
            or runtime.credential_version != output.credential_version
        ):
            self.session.rollback()
            raise PermanentWorkflowError(
                "Approved collaboration connector revision changed",
                code="collaboration_connector_revision_changed",
            )
        incident_service = self.session.scalar(
            select(IncidentRecord.service).where(
                IncidentRecord.id == output.incident_id,
                IncidentRecord.organization_id == self.organization_id,
            )
        )
        proposal_hash = self.session.scalar(
            select(MitigationProposalRecord.input_hash).where(
                MitigationProposalRecord.id == output.proposal_id,
                MitigationProposalRecord.incident_id == output.incident_id,
            )
        )
        if incident_service is None or proposal_hash is None:
            self.session.rollback()
            raise PermanentWorkflowError(
                "Approved collaboration grounding record is unavailable",
                code="collaboration_grounding_unavailable",
            )
        live_destination = (
            runtime.configuration.channel
            if output.provider == CollaborationProvider.SLACK.value
            else runtime.configuration.repository
        )
        if runtime.configuration.service != incident_service:
            self.session.rollback()
            raise PermanentWorkflowError(
                "Approved collaboration service binding changed",
                code="collaboration_service_binding_changed",
            )
        if live_destination != output.destination:
            self.session.rollback()
            raise PermanentWorkflowError(
                "Approved collaboration destination changed",
                code="collaboration_destination_changed",
            )
        payload = dict(output.payload)
        expected_content_sha256 = self._content_hash(
            proposal_hash=proposal_hash,
            kind=output.kind,
            provider=output.provider,
            destination=output.destination,
            payload=payload,
            connector_id=output.connector_id,
            connector_version=output.connector_version,
            credential_version=output.credential_version,
        )
        if expected_content_sha256 != output.content_sha256:
            self.session.rollback()
            raise PermanentWorkflowError(
                "Approved collaboration content failed its integrity check",
                code="collaboration_content_integrity_failed",
            )
        now = datetime.now(UTC)
        output.status = CollaborationOutputStatus.DELIVERING.value
        output.failure_reason = None
        output.version += 1
        output.delivery.status = "delivering"
        output.delivery.attempt_count += 1
        output.delivery.last_error_code = None
        output.delivery.started_at = output.delivery.started_at or now
        output.delivery.updated_at = now
        attempt = output.delivery.attempt_count
        provider = output.provider
        commit_with_fence(self.session, fence)

        publisher: Publisher | None = None
        try:
            if provider == CollaborationProvider.SLACK.value:
                publisher = self._slack_factory()(runtime.configuration, runtime.credentials)
                receipt = publisher.publish(
                    text=str(payload["text"]),
                    delivery_id=output_id,
                )
            else:
                publisher = self._github_factory()(runtime.configuration, runtime.credentials)
                receipt = publisher.publish(
                    title=str(payload["title"]),
                    body=str(payload["body"]),
                    delivery_id=output_id,
                )
        except Exception as error:
            permanent = bool(getattr(error, "permanent", False))
            retryable = bool(getattr(error, "retryable", False))
            ambiguous = bool(getattr(error, "ambiguous", False))
            code = (
                "provider_reconciliation_ambiguous"
                if permanent and ambiguous
                else "provider_delivery_ambiguous"
                if ambiguous
                else "provider_delivery_retryable"
                if retryable
                else "provider_delivery_rejected"
            )
            workflow_error = (
                PermanentWorkflowError
                if permanent
                else RetryableWorkflowError
                if retryable or ambiguous
                else PermanentWorkflowError
            )
            raise workflow_error(
                "Collaboration provider did not confirm delivery",
                code=code,
                retry_after_seconds=self._retry_after(error),
            ) from error
        finally:
            if publisher is not None:
                close = getattr(publisher, "close", None)
                if callable(close):
                    close()

        normalized_receipt = self._normalize_receipt(receipt)
        output = self.session.scalar(
            select(CollaborationOutputRecord)
            .where(
                CollaborationOutputRecord.id == output_id,
                CollaborationOutputRecord.organization_id == self.organization_id,
            )
            .options(selectinload(CollaborationOutputRecord.delivery))
            .with_for_update()
        )
        if output is None or output.delivery is None:
            raise PermanentWorkflowError(
                "Collaboration receipt target disappeared",
                code="collaboration_output_unavailable",
            )
        if output.status == CollaborationOutputStatus.DELIVERED.value:
            self.session.rollback()
            return self.get(output.id)
        if output.delivery.attempt_count != attempt:
            self.session.rollback()
            raise RetryableWorkflowError(
                "Collaboration attempt was superseded before receipt commit",
                code="collaboration_attempt_superseded",
            )
        delivered_at = datetime.now(UTC)
        output.status = CollaborationOutputStatus.DELIVERED.value
        output.delivered_at = delivered_at
        output.failure_reason = None
        output.version += 1
        output.delivery.status = "delivered"
        output.delivery.provider_receipt = normalized_receipt
        output.delivery.last_error_code = None
        output.delivery.updated_at = delivered_at
        output.delivery.delivered_at = delivered_at
        self.session.add(
            IncidentEventRecord(
                incident_id=output.incident_id,
                event_type="collaboration.output_delivered",
                actor="pageragent-collaboration-worker",
                from_status=None,
                to_status=output.incident.status,
                note="Approved collaboration output received a provider delivery receipt.",
                payload={
                    "output_id": str(output.id),
                    "provider": output.provider,
                    "attempt": attempt,
                    "connector_version": output.connector_version,
                    "credential_version": output.credential_version,
                },
            )
        )
        commit_with_fence(self.session, fence)
        return self.get(output.id)

    def _load_incident(self, incident_id: UUID) -> IncidentRecord:
        incident = self.session.scalar(
            select(IncidentRecord).where(
                IncidentRecord.id == incident_id,
                IncidentRecord.organization_id == self.organization_id,
            )
        )
        if incident is None:
            raise CollaborationOutputNotFoundError
        return incident

    def _select_connector(
        self,
        service: str,
        provider: CollaborationProvider,
    ) -> tuple[ConnectorRecord, dict[str, Any]]:
        records = self.session.scalars(
            select(ConnectorRecord)
            .where(
                ConnectorRecord.organization_id == self.organization_id,
                ConnectorRecord.provider == provider.value,
                ConnectorRecord.enabled.is_(True),
                ConnectorRecord.status == ConnectorStatus.CONFIGURED.value,
            )
            .options(selectinload(ConnectorRecord.credential))
        ).all()
        matches: list[tuple[ConnectorRecord, dict[str, Any]]] = []
        for record in records:
            try:
                configuration = validate_configuration(
                    ConnectorProvider(record.provider),
                    record.configuration,
                )
            except ConnectorContractError:
                continue
            if configuration.get("service") != service:
                continue
            if (
                provider is CollaborationProvider.GITHUB
                and configuration.get("issue_creation_enabled") is not True
            ):
                continue
            matches.append((record, configuration))
        if len(matches) != 1:
            raise CollaborationConnectorUnavailableError(
                "Exactly one enabled collaboration connector must own this service"
            )
        return matches[0]

    @staticmethod
    def _provider_for_kind(kind: CollaborationOutputKind) -> CollaborationProvider:
        return (
            CollaborationProvider.SLACK
            if kind is CollaborationOutputKind.SLACK_UPDATE
            else CollaborationProvider.GITHUB
        )

    def _content_hash(
        self,
        *,
        proposal_hash: str,
        kind: str,
        provider: str,
        destination: str,
        payload: dict[str, Any],
        connector_id: UUID,
        connector_version: int,
        credential_version: int,
    ) -> str:
        return canonical_hash(
            {
                "builder_version": self.draft_builder.version,
                "proposal_hash": proposal_hash,
                "kind": kind,
                "provider": provider,
                "destination": destination,
                "payload": payload,
                "connector_id": str(connector_id),
                "connector_version": connector_version,
                "credential_version": credential_version,
            }
        )

    @staticmethod
    def _detail_query():
        return select(CollaborationOutputRecord).options(
            selectinload(CollaborationOutputRecord.decisions),
            selectinload(CollaborationOutputRecord.delivery),
        )

    @staticmethod
    def _to_detail(record: CollaborationOutputRecord) -> CollaborationOutputDetail:
        delivery = record.delivery
        return CollaborationOutputDetail(
            id=record.id,
            incident_id=record.incident_id,
            proposal_id=record.proposal_id,
            connector_id=record.connector_id,
            workflow_run_id=record.workflow_run_id,
            kind=CollaborationOutputKind(record.kind),
            provider=CollaborationProvider(record.provider),
            status=CollaborationOutputStatus(record.status),
            version=record.version,
            destination=record.destination,
            payload=dict(record.payload),
            content_sha256=record.content_sha256,
            connector_version=record.connector_version,
            credential_version=record.credential_version,
            requested_by=record.requested_by,
            requested_at=record.requested_at,
            decided_at=record.decided_at,
            delivered_at=record.delivered_at,
            failure_reason=record.failure_reason,
            decisions=[
                CollaborationDecisionDetail(
                    id=decision.id,
                    decision=CollaborationDecision(decision.decision),
                    actor=decision.actor,
                    note=decision.note,
                    created_at=decision.created_at,
                )
                for decision in record.decisions
            ],
            delivery=(
                CollaborationDeliveryReceipt(
                    idempotency_key=delivery.idempotency_key,
                    status=delivery.status,
                    attempt_count=delivery.attempt_count,
                    provider_receipt=dict(delivery.provider_receipt),
                    last_error_code=delivery.last_error_code,
                    started_at=delivery.started_at,
                    updated_at=delivery.updated_at,
                    delivered_at=delivery.delivered_at,
                )
                if delivery is not None
                else None
            ),
        )

    def _slack_factory(self) -> PublisherFactory:
        if self.slack_publisher_factory is not None:
            return self.slack_publisher_factory
        from app.connectors.slack import SlackIncidentPublisher

        return SlackIncidentPublisher

    def _github_factory(self) -> PublisherFactory:
        if self.github_publisher_factory is not None:
            return self.github_publisher_factory
        from app.connectors.github_issues import GitHubIssuePublisher

        return GitHubIssuePublisher

    @staticmethod
    def _normalize_receipt(receipt: Any) -> dict[str, Any]:
        if is_dataclass(receipt):
            payload = asdict(receipt)
        elif hasattr(receipt, "model_dump"):
            payload = receipt.model_dump(mode="json")
        elif isinstance(receipt, dict):
            payload = dict(receipt)
        else:
            raise PermanentWorkflowError(
                "Collaboration provider returned an invalid receipt",
                code="provider_receipt_invalid",
            )
        allowed: dict[str, str | int | bool] = {}
        for key, value in payload.items():
            if (
                isinstance(key, str)
                and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", key)
                and isinstance(value, str | int | bool)
                and not isinstance(value, float)
            ):
                allowed[key] = value
        if not allowed or len(allowed) > 12:
            raise PermanentWorkflowError(
                "Collaboration provider returned an invalid receipt",
                code="provider_receipt_invalid",
            )
        return allowed

    @staticmethod
    def _retry_after(error: Exception) -> int | None:
        value = getattr(error, "retry_after_seconds", None)
        if isinstance(value, int) and not isinstance(value, bool):
            return max(1, min(value, 900))
        return None


def build_collaboration_service(
    session: Session,
    organization_id: UUID = DEFAULT_ORGANIZATION_ID,
) -> CollaborationService:
    return CollaborationService(session, organization_id=organization_id)
