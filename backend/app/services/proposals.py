from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.copilot.actions import derive_action, required_runbook
from app.copilot.citations import CitationValidator
from app.copilot.execution import MitigationExecutor, SimulatorMitigationExecutor
from app.copilot.synthesis import (
    BriefSynthesizer,
    DeterministicBriefSynthesizer,
    OpenAIBriefSynthesizer,
)
from app.core.config import settings
from app.db.models import (
    IncidentEventRecord,
    IncidentRecord,
    InvestigationRunRecord,
    MitigationExecutionRecord,
    MitigationProposalRecord,
    ProposalDecisionRecord,
)
from app.domain.incidents import AlertPayload, IncidentStatus
from app.domain.investigations import InvestigationStatus
from app.domain.proposals import (
    ActionEnvelope,
    GroundedClaim,
    MitigationExecutionDetail,
    MitigationProposalDetail,
    ProposalDecision,
    ProposalDecisionDetail,
    ProposalDecisionRequest,
    ProposalStatus,
)
from app.investigation.text import canonical_hash
from app.services.incidents import IncidentNotFoundError
from app.services.investigations import InvestigationService


class ProposalNotFoundError(Exception):
    pass


class ProposalGenerationError(Exception):
    pass


class ProposalConflictError(Exception):
    pass


class ApprovalPolicyError(Exception):
    pass


class ProposalService:
    def __init__(
        self,
        session: Session,
        synthesizer: BriefSynthesizer,
        citation_validator: CitationValidator,
        executor: MitigationExecutor,
    ) -> None:
        self.session = session
        self.synthesizer = synthesizer
        self.citation_validator = citation_validator
        self.executor = executor

    def generate(
        self, incident_id: UUID, investigation_id: UUID | None = None
    ) -> MitigationProposalDetail:
        incident = self._load_incident(incident_id)
        investigation = self._load_investigation(incident_id, investigation_id)
        if investigation.status != InvestigationStatus.COMPLETED.value:
            raise ProposalGenerationError("A completed investigation is required")

        existing = self.session.scalar(
            select(MitigationProposalRecord)
            .where(
                MitigationProposalRecord.investigation_id == investigation.id,
                MitigationProposalRecord.status.in_(
                    [
                        ProposalStatus.PENDING_APPROVAL.value,
                        ProposalStatus.ADVISORY.value,
                    ]
                ),
            )
            .order_by(MitigationProposalRecord.created_at.desc())
            .limit(1)
        )
        if existing is not None:
            return self.get(existing.id)

        detail = InvestigationService._to_detail(investigation)
        if (
            not detail.error_clusters
            or not detail.cause_candidates
            or not detail.runbook_matches
        ):
            raise ProposalGenerationError("Investigation is missing ranked evidence")
        context = self._synthesis_context(incident, detail)
        try:
            brief = self.synthesizer.generate(context)
            self.citation_validator.validate(brief, self._allowed_evidence_ids(detail))
        except Exception as error:
            raise ProposalGenerationError(f"Grounded brief rejected: {error}") from error

        top_cause = detail.cause_candidates[0]
        top_runbook = detail.runbook_matches[0]
        action = derive_action(top_cause)
        expected_runbook = required_runbook(action)
        if top_runbook.runbook_id != expected_runbook:
            raise ProposalGenerationError(
                f"Expected safety runbook {expected_runbook} was not ranked first"
            )
        prompt_version = str(
            getattr(self.synthesizer, "prompt_version", "grounded-incident-brief-v1")
        )
        input_hash = canonical_hash(
            {
                "context": context,
                "synthesizer_version": self.synthesizer.version,
                "model_name": self.synthesizer.model_name,
                "prompt_version": prompt_version,
                "citation_policy": self.citation_validator.version,
            }
        )
        proposal = MitigationProposalRecord(
            incident_id=incident.id,
            investigation_id=investigation.id,
            status=(
                ProposalStatus.PENDING_APPROVAL.value
                if action.automation_allowed
                else ProposalStatus.ADVISORY.value
            ),
            synthesizer_version=self.synthesizer.version,
            model_name=self.synthesizer.model_name,
            prompt_version=prompt_version,
            input_hash=input_hash,
            root_cause_summary=brief.root_cause_summary,
            confidence=brief.confidence,
            impact_summary=brief.impact_summary,
            recommended_action=brief.recommended_action,
            risk_summary=brief.risk_summary,
            verification_steps=brief.verification_steps,
            slack_update=brief.slack_update,
            claims=[claim.model_dump(mode="json") for claim in brief.claims],
            action_type=action.action_type,
            action_target=action.target_service,
            action_parameters=action.model_dump(
                mode="json", exclude={"action_type", "target_service"}
            ),
        )
        self.session.add(proposal)
        self.session.flush()
        self.session.add(
            IncidentEventRecord(
                incident_id=incident.id,
                event_type="proposal.generated",
                actor="pageragent-copilot",
                from_status=None,
                to_status=incident.status,
                note=(
                    "Grounded incident brief produced an advisory-only response."
                    if not action.automation_allowed
                    else "Grounded incident brief and approval-gated mitigation proposed."
                ),
                payload={
                    "proposal_id": str(proposal.id),
                    "investigation_id": str(investigation.id),
                    "synthesizer": self.synthesizer.version,
                    "model": self.synthesizer.model_name,
                    "confidence": brief.confidence,
                },
            )
        )
        self.session.commit()
        return self.get(proposal.id)

    def get_latest(self, incident_id: UUID) -> MitigationProposalDetail:
        if self.session.get(IncidentRecord, incident_id) is None:
            raise IncidentNotFoundError
        proposal = self.session.scalar(
            select(MitigationProposalRecord)
            .where(MitigationProposalRecord.incident_id == incident_id)
            .order_by(MitigationProposalRecord.created_at.desc())
            .limit(1)
        )
        if proposal is None:
            raise ProposalNotFoundError
        return self.get(proposal.id)

    def get(self, proposal_id: UUID) -> MitigationProposalDetail:
        proposal = self.session.scalar(
            select(MitigationProposalRecord)
            .where(MitigationProposalRecord.id == proposal_id)
            .options(
                selectinload(MitigationProposalRecord.decisions),
                selectinload(MitigationProposalRecord.execution),
            )
            .execution_options(populate_existing=True)
        )
        if proposal is None:
            raise ProposalNotFoundError
        return self._to_detail(proposal)

    def decide(
        self, proposal_id: UUID, request: ProposalDecisionRequest
    ) -> MitigationProposalDetail:
        proposal = self.session.scalar(
            select(MitigationProposalRecord)
            .where(MitigationProposalRecord.id == proposal_id)
            .options(selectinload(MitigationProposalRecord.decisions))
            .with_for_update()
        )
        if proposal is None:
            raise ProposalNotFoundError
        if proposal.status != ProposalStatus.PENDING_APPROVAL.value:
            raise ProposalConflictError(f"Proposal is already {proposal.status}")

        incident = self.session.scalar(
            select(IncidentRecord)
            .where(IncidentRecord.id == proposal.incident_id)
            .with_for_update()
        )
        if incident is None:
            raise IncidentNotFoundError
        if (
            request.decision is ProposalDecision.APPROVE
            and incident.status != IncidentStatus.INVESTIGATING.value
        ):
            raise ApprovalPolicyError(
                "Incident must be investigating before a mitigation can be approved"
            )

        now = datetime.now(UTC)
        proposal.status = (
            ProposalStatus.APPROVED.value
            if request.decision is ProposalDecision.APPROVE
            else ProposalStatus.REJECTED.value
        )
        proposal.decided_at = now
        self.session.add(
            ProposalDecisionRecord(
                proposal_id=proposal.id,
                incident_id=incident.id,
                decision=request.decision.value,
                actor=request.actor,
                note=request.note,
            )
        )
        self.session.add(
            IncidentEventRecord(
                incident_id=incident.id,
                event_type=(
                    "proposal.approved"
                    if request.decision is ProposalDecision.APPROVE
                    else "proposal.rejected"
                ),
                actor=request.actor,
                from_status=None,
                to_status=incident.status,
                note=request.note,
                payload={"proposal_id": str(proposal.id)},
            )
        )
        self.session.commit()
        if request.decision is ProposalDecision.REJECT:
            return self.get(proposal.id)
        return self._execute(proposal.id)

    def _execute(self, proposal_id: UUID) -> MitigationProposalDetail:
        proposal = self.session.get(MitigationProposalRecord, proposal_id)
        if proposal is None:
            raise ProposalNotFoundError
        if proposal.status != ProposalStatus.APPROVED.value:
            raise ApprovalPolicyError("Only an approved proposal can execute")
        if proposal.execution is not None:
            return self.get(proposal.id)

        action = self._action(proposal)
        self._enforce_action_policy(action)
        idempotency_key = f"proposal-{proposal.id}"
        proposal.status = ProposalStatus.EXECUTING.value
        execution = MitigationExecutionRecord(
            proposal_id=proposal.id,
            status="running",
            executor_version=self.executor.version,
            idempotency_key=idempotency_key,
            request_payload=action.model_dump(mode="json"),
            response_payload={},
            before_telemetry={},
            after_telemetry={},
            recovery_verified=False,
        )
        self.session.add(execution)
        self.session.commit()

        try:
            result = self.executor.execute(action, idempotency_key)
            now = datetime.now(UTC)
            execution.status = "completed" if result.recovery_verified else "failed"
            execution.response_payload = result.response_payload
            execution.before_telemetry = result.before_telemetry
            execution.after_telemetry = result.after_telemetry
            execution.recovery_verified = result.recovery_verified
            execution.completed_at = now
            proposal.status = (
                ProposalStatus.VERIFICATION_PASSED.value
                if result.recovery_verified
                else ProposalStatus.EXECUTION_FAILED.value
            )
            if not result.recovery_verified:
                proposal.failure_reason = (
                    "Mitigation executed, but recovery verification failed"
                )
                execution.failure_reason = proposal.failure_reason
            self._record_execution_outcome(proposal, result.recovery_verified, now)
            self.session.commit()
        except Exception as error:
            now = datetime.now(UTC)
            execution.status = "failed"
            execution.failure_reason = str(error)[:2_000]
            execution.completed_at = now
            proposal.status = ProposalStatus.EXECUTION_FAILED.value
            proposal.failure_reason = execution.failure_reason
            self._record_execution_outcome(proposal, False, now)
            self.session.commit()
        return self.get(proposal.id)

    def _record_execution_outcome(
        self, proposal: MitigationProposalRecord, verified: bool, now: datetime
    ) -> None:
        incident = self.session.get(IncidentRecord, proposal.incident_id)
        if incident is None:
            raise IncidentNotFoundError
        from_status = incident.status
        if verified and incident.status == IncidentStatus.INVESTIGATING.value:
            incident.status = IncidentStatus.MITIGATED.value
            incident.version += 1
            incident.updated_at = now
        self.session.add(
            IncidentEventRecord(
                incident_id=incident.id,
                event_type=(
                    "mitigation.recovery_verified" if verified else "mitigation.execution_failed"
                ),
                actor="pageragent-executor",
                from_status=from_status if verified else None,
                to_status=incident.status,
                note=(
                    "Mitigation canaries passed; incident automatically marked mitigated."
                    if verified
                    else "Approved mitigation did not pass recovery verification."
                ),
                payload={
                    "proposal_id": str(proposal.id),
                    "action_type": proposal.action_type,
                    "recovery_verified": verified,
                    "version": incident.version,
                },
            )
        )

    def _load_incident(self, incident_id: UUID) -> IncidentRecord:
        incident = self.session.scalar(
            select(IncidentRecord)
            .where(IncidentRecord.id == incident_id)
            .options(selectinload(IncidentRecord.alerts))
        )
        if incident is None:
            raise IncidentNotFoundError
        return incident

    def _load_investigation(
        self, incident_id: UUID, investigation_id: UUID | None
    ) -> InvestigationRunRecord:
        statement = (
            select(InvestigationRunRecord)
            .where(InvestigationRunRecord.incident_id == incident_id)
            .options(
                selectinload(InvestigationRunRecord.evidence),
                selectinload(InvestigationRunRecord.error_clusters),
                selectinload(InvestigationRunRecord.cause_candidates),
                selectinload(InvestigationRunRecord.commit_candidates),
                selectinload(InvestigationRunRecord.runbook_matches),
            )
        )
        if investigation_id is not None:
            statement = statement.where(InvestigationRunRecord.id == investigation_id)
        investigation = self.session.scalar(
            statement.order_by(InvestigationRunRecord.started_at.desc()).limit(1)
        )
        if investigation is None:
            raise ProposalGenerationError("No investigation exists for this incident")
        return investigation

    @staticmethod
    def _synthesis_context(incident: IncidentRecord, investigation: Any) -> dict[str, Any]:
        alert = AlertPayload.model_validate(incident.alerts[0].payload)
        return {
            "incident": {
                "id": str(incident.id),
                "service": incident.service,
                "severity": incident.severity,
                "summary": incident.summary,
                "status": incident.status,
            },
            "alert": alert.model_dump(mode="json"),
            "evidence_manifest": [
                {
                    "id": str(item.id),
                    "kind": item.kind,
                    "content_hash": item.content_hash,
                    "source_uri": item.source_uri,
                }
                for item in investigation.evidence
            ],
            "error_clusters": [
                item.model_dump(mode="json") for item in investigation.error_clusters
            ],
            "cause_candidates": [
                item.model_dump(mode="json") for item in investigation.cause_candidates
            ],
            "commit_candidates": [
                item.model_dump(mode="json") for item in investigation.commit_candidates
            ],
            "runbook_matches": [
                item.model_dump(mode="json") for item in investigation.runbook_matches
            ],
        }

    @staticmethod
    def _allowed_evidence_ids(investigation: Any) -> set[str]:
        records = [
            *investigation.evidence,
            *investigation.error_clusters,
            *investigation.cause_candidates,
            *investigation.commit_candidates,
            *investigation.runbook_matches,
        ]
        return {str(record.id) for record in records}

    @staticmethod
    def _action(proposal: MitigationProposalRecord) -> ActionEnvelope:
        parameters = proposal.action_parameters
        return ActionEnvelope(
            action_type=proposal.action_type,
            target_service=proposal.action_target,
            target_release=parameters.get("target_release"),
            expected_faulty_commit=parameters.get("expected_faulty_commit"),
            feature_flag=parameters.get("feature_flag"),
            automation_allowed=bool(parameters.get("automation_allowed")),
        )

    @staticmethod
    def _enforce_action_policy(action: ActionEnvelope) -> None:
        if not action.automation_allowed or action.action_type == "escalate_only":
            raise ApprovalPolicyError("Advisory-only responses cannot execute")
        if action.target_service != "checkout-api":
            raise ApprovalPolicyError("Action target is outside the simulator allow-list")
        if action.action_type == "rollback_service" and action.target_release != "stable-v1":
            raise ApprovalPolicyError("Rollback target is outside the simulator allow-list")
        if (
            action.action_type == "disable_feature_flag"
            and action.feature_flag != "wallet_validation_v2"
        ):
            raise ApprovalPolicyError("Feature flag is outside the simulator allow-list")

    @staticmethod
    def _to_detail(proposal: MitigationProposalRecord) -> MitigationProposalDetail:
        execution = proposal.execution
        return MitigationProposalDetail(
            id=proposal.id,
            incident_id=proposal.incident_id,
            investigation_id=proposal.investigation_id,
            status=ProposalStatus(proposal.status),
            synthesizer_version=proposal.synthesizer_version,
            model_name=proposal.model_name,
            prompt_version=proposal.prompt_version,
            input_hash=proposal.input_hash,
            root_cause_summary=proposal.root_cause_summary,
            confidence=proposal.confidence,
            impact_summary=proposal.impact_summary,
            recommended_action=proposal.recommended_action,
            risk_summary=proposal.risk_summary,
            verification_steps=proposal.verification_steps,
            slack_update=proposal.slack_update,
            claims=[GroundedClaim.model_validate(claim) for claim in proposal.claims],
            action=ProposalService._action(proposal),
            failure_reason=proposal.failure_reason,
            created_at=proposal.created_at,
            decided_at=proposal.decided_at,
            decisions=[
                ProposalDecisionDetail(
                    id=decision.id,
                    decision=ProposalDecision(decision.decision),
                    actor=decision.actor,
                    note=decision.note,
                    created_at=decision.created_at,
                )
                for decision in proposal.decisions
            ],
            execution=(
                MitigationExecutionDetail(
                    id=execution.id,
                    status=execution.status,
                    executor_version=execution.executor_version,
                    idempotency_key=execution.idempotency_key,
                    request_payload=execution.request_payload,
                    response_payload=execution.response_payload,
                    before_telemetry=execution.before_telemetry,
                    after_telemetry=execution.after_telemetry,
                    recovery_verified=execution.recovery_verified,
                    failure_reason=execution.failure_reason,
                    started_at=execution.started_at,
                    completed_at=execution.completed_at,
                )
                if execution is not None
                else None
            ),
        )


def build_proposal_service(session: Session) -> ProposalService:
    provider = settings.synthesis_provider.lower()
    api_key_value = (
        settings.openai_api_key.get_secret_value().strip()
        if settings.openai_api_key is not None
        else ""
    )
    api_key = api_key_value or None
    if provider == "openai" and not api_key:
        raise ProposalGenerationError("SYNTHESIS_PROVIDER=openai requires OPENAI_API_KEY")
    synthesizer: BriefSynthesizer
    if provider == "openai" or (provider == "auto" and api_key):
        synthesizer = OpenAIBriefSynthesizer(
            api_key=api_key or "",
            model_name=settings.openai_model,
            base_url=settings.openai_base_url,
            timeout_seconds=settings.synthesis_http_timeout_seconds,
        )
    else:
        synthesizer = DeterministicBriefSynthesizer()
    return ProposalService(
        session=session,
        synthesizer=synthesizer,
        citation_validator=CitationValidator(),
        executor=SimulatorMitigationExecutor(
            base_url=settings.checkout_control_url,
            canary_requests=settings.recovery_canary_requests,
        ),
    )
