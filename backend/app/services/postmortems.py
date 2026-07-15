from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.copilot.postmortems import (
    DeterministicPostmortemGenerator,
    OpenAIPostmortemGenerator,
    PostmortemGenerator,
    PostmortemGroundingValidator,
)
from app.core.config import settings
from app.db.models import (
    IncidentEventRecord,
    IncidentRecord,
    InvestigationRunRecord,
    MitigationProposalRecord,
    PostmortemRecord,
    PostmortemRevisionRecord,
)
from app.domain.incidents import AlertPayload, IncidentStatus
from app.domain.postmortems import (
    GroundedObservation,
    GroundedSection,
    PostmortemContent,
    PostmortemDetail,
    PostmortemFinalizeRequest,
    PostmortemRevisionDetail,
    PostmortemStatus,
    PostmortemUpdateRequest,
    PreventionItem,
    TimelineEntry,
)
from app.domain.proposals import ProposalStatus
from app.investigation.text import canonical_hash
from app.services.incidents import IncidentNotFoundError
from app.services.investigations import InvestigationService
from app.services.proposals import ProposalService


class PostmortemNotFoundError(Exception):
    pass


class PostmortemGenerationError(Exception):
    pass


class PostmortemConflictError(Exception):
    pass


class PostmortemVersionConflictError(Exception):
    def __init__(self, current_version: int) -> None:
        self.current_version = current_version
        super().__init__(f"Postmortem changed; current version is {current_version}")


class PostmortemService:
    def __init__(
        self,
        session: Session,
        generator: PostmortemGenerator,
        validator: PostmortemGroundingValidator,
    ) -> None:
        self.session = session
        self.generator = generator
        self.validator = validator

    def generate(self, incident_id: UUID) -> PostmortemDetail:
        incident = self._load_incident(incident_id)
        if incident.status != IncidentStatus.RESOLVED.value:
            raise PostmortemGenerationError(
                "Incident must be resolved before postmortem generation"
            )
        if incident.postmortem is not None:
            return self.get(incident.postmortem.id)

        proposal = next(
            (
                item
                for item in reversed(incident.proposals)
                if item.status == ProposalStatus.VERIFICATION_PASSED.value
            ),
            None,
        )
        if proposal is None or proposal.execution is None:
            raise PostmortemGenerationError("Verified mitigation evidence is required")
        investigation = next(
            (
                item
                for item in incident.investigations
                if item.id == proposal.investigation_id
            ),
            None,
        )
        if investigation is None:
            raise PostmortemGenerationError("Proposal investigation evidence is unavailable")

        context = self._generation_context(incident, investigation, proposal)
        allowed_ids = self._allowed_evidence_ids(incident, investigation, proposal)
        try:
            narrative = self.generator.generate(context)
            self.validator.validate(narrative, allowed_ids)
        except Exception as error:
            raise PostmortemGenerationError(f"Postmortem generation rejected: {error}") from error

        timeline = [
            TimelineEntry(
                occurred_at=event.created_at,
                event_type=event.event_type,
                actor=event.actor,
                description=event.note or "Incident record updated.",
                evidence_ids=[str(event.id)],
            )
            for event in incident.events
        ]
        content = PostmortemContent(
            **narrative.model_dump(),
            timeline=timeline,
        )
        prompt_version = str(
            getattr(self.generator, "prompt_version", "blameless-postmortem-v1")
        )
        input_hash = canonical_hash(
            {
                "context": context,
                "generator_version": self.generator.version,
                "model_name": self.generator.model_name,
                "prompt_version": prompt_version,
                "validator_version": self.validator.version,
            }
        )
        record = PostmortemRecord(
            incident_id=incident.id,
            status=PostmortemStatus.DRAFT.value,
            version=1,
            generator_version=self.generator.version,
            model_name=self.generator.model_name,
            prompt_version=prompt_version,
            input_hash=input_hash,
            content=content.model_dump(mode="json"),
        )
        self.session.add(record)
        self.session.flush()
        self._add_revision(
            record,
            source="generated",
            editor="pageragent-postmortem",
            change_note="Initial grounded postmortem generated from the resolved incident.",
        )
        self.session.add(
            IncidentEventRecord(
                incident_id=incident.id,
                event_type="postmortem.generated",
                actor="pageragent-postmortem",
                from_status=None,
                to_status=incident.status,
                note="Grounded postmortem draft generated from the immutable incident record.",
                payload={
                    "postmortem_id": str(record.id),
                    "version": record.version,
                    "generator": self.generator.version,
                    "model": self.generator.model_name,
                },
            )
        )
        self.session.commit()
        return self.get(record.id)

    def get_for_incident(self, incident_id: UUID) -> PostmortemDetail:
        if self.session.get(IncidentRecord, incident_id) is None:
            raise IncidentNotFoundError
        record = self.session.scalar(
            select(PostmortemRecord).where(PostmortemRecord.incident_id == incident_id)
        )
        if record is None:
            raise PostmortemNotFoundError
        return self.get(record.id)

    def get(self, postmortem_id: UUID) -> PostmortemDetail:
        record = self.session.scalar(
            select(PostmortemRecord)
            .where(PostmortemRecord.id == postmortem_id)
            .options(selectinload(PostmortemRecord.revisions))
            .execution_options(populate_existing=True)
        )
        if record is None:
            raise PostmortemNotFoundError
        return self._to_detail(record)

    def update(
        self, postmortem_id: UUID, request: PostmortemUpdateRequest
    ) -> PostmortemDetail:
        record = self._load_for_update(postmortem_id)
        if record.status != PostmortemStatus.DRAFT.value:
            raise PostmortemConflictError("Finalized postmortems cannot be edited")
        if record.version != request.expected_version:
            raise PostmortemVersionConflictError(record.version)

        current = PostmortemContent.model_validate(record.content)
        if len(request.what_went_well) != len(current.what_went_well):
            raise PostmortemConflictError("What-went-well item count cannot change")
        if len(request.what_went_poorly) != len(current.what_went_poorly):
            raise PostmortemConflictError("What-went-poorly item count cannot change")
        if len(request.prevention_items) != len(current.prevention_items):
            raise PostmortemConflictError("Prevention item count cannot change")

        content = PostmortemContent(
            title=request.title,
            summary=GroundedSection(
                text=request.summary,
                evidence_ids=current.summary.evidence_ids,
            ),
            root_cause=GroundedSection(
                text=request.root_cause,
                evidence_ids=current.root_cause.evidence_ids,
            ),
            customer_impact=GroundedSection(
                text=request.customer_impact,
                evidence_ids=current.customer_impact.evidence_ids,
            ),
            detection=GroundedSection(
                text=request.detection,
                evidence_ids=current.detection.evidence_ids,
            ),
            resolution=GroundedSection(
                text=request.resolution,
                evidence_ids=current.resolution.evidence_ids,
            ),
            what_went_well=[
                GroundedObservation(
                    text=text,
                    evidence_ids=current.what_went_well[index].evidence_ids,
                )
                for index, text in enumerate(request.what_went_well)
            ],
            what_went_poorly=[
                GroundedObservation(
                    text=text,
                    evidence_ids=current.what_went_poorly[index].evidence_ids,
                )
                for index, text in enumerate(request.what_went_poorly)
            ],
            prevention_items=[
                PreventionItem(
                    **item.model_dump(),
                    evidence_ids=current.prevention_items[index].evidence_ids,
                )
                for index, item in enumerate(request.prevention_items)
            ],
            timeline=current.timeline,
        )
        record.version += 1
        record.content = content.model_dump(mode="json")
        record.updated_at = datetime.now(UTC)
        self._add_revision(
            record,
            source="operator_edit",
            editor=request.actor,
            change_note=request.change_note,
        )
        self.session.add(
            IncidentEventRecord(
                incident_id=record.incident_id,
                event_type="postmortem.edited",
                actor=request.actor,
                from_status=None,
                to_status=IncidentStatus.RESOLVED.value,
                note=request.change_note,
                payload={"postmortem_id": str(record.id), "version": record.version},
            )
        )
        self.session.commit()
        return self.get(record.id)

    def finalize(
        self, postmortem_id: UUID, request: PostmortemFinalizeRequest
    ) -> PostmortemDetail:
        record = self._load_for_update(postmortem_id)
        if record.status != PostmortemStatus.DRAFT.value:
            raise PostmortemConflictError("Postmortem is already finalized")
        if record.version != request.expected_version:
            raise PostmortemVersionConflictError(record.version)
        now = datetime.now(UTC)
        record.status = PostmortemStatus.FINAL.value
        record.version += 1
        record.updated_at = now
        record.finalized_at = now
        record.finalized_by = request.actor
        note = request.note or "Operator reviewed and finalized the postmortem."
        self._add_revision(
            record,
            source="finalized",
            editor=request.actor,
            change_note=note,
        )
        self.session.add(
            IncidentEventRecord(
                incident_id=record.incident_id,
                event_type="postmortem.finalized",
                actor=request.actor,
                from_status=None,
                to_status=IncidentStatus.RESOLVED.value,
                note=note,
                payload={"postmortem_id": str(record.id), "version": record.version},
            )
        )
        self.session.commit()
        return self.get(record.id)

    def export_markdown(self, postmortem_id: UUID) -> str:
        postmortem = self.get(postmortem_id)
        content = postmortem.content

        def citations(ids: list[str]) -> str:
            return " ".join(f"[E-{value[:6]}]" for value in ids)

        def safe(value: str) -> str:
            return value.replace("|", "\\|").replace("\n", " ")

        lines = [
            f"# {content.title}",
            "",
            f"- Incident: `{postmortem.incident_id}`",
            f"- Status: `{postmortem.status.value}`",
            f"- Document version: `{postmortem.version}`",
            f"- Generated by: `{postmortem.generator_version}` / `{postmortem.model_name}`",
            "",
        ]
        sections = [
            ("Summary", content.summary),
            ("Root cause", content.root_cause),
            ("Customer impact", content.customer_impact),
            ("Detection", content.detection),
            ("Resolution", content.resolution),
        ]
        for heading, section in sections:
            lines.extend(
                [
                    f"## {heading}",
                    "",
                    f"{section.text} {citations(section.evidence_ids)}",
                    "",
                ]
            )

        lines.extend(
            [
                "## Timeline",
                "",
                "| Time | Event | Actor | Description | Evidence |",
                "| --- | --- | --- | --- | --- |",
            ]
        )
        for item in content.timeline:
            lines.append(
                f"| {item.occurred_at.isoformat()} | {safe(item.event_type)} | "
                f"{safe(item.actor)} | {safe(item.description)} | "
                f"{citations(item.evidence_ids)} |"
            )
        lines.extend(["", "## What went well", ""])
        lines.extend(
            f"- {item.text} {citations(item.evidence_ids)}"
            for item in content.what_went_well
        )
        lines.extend(["", "## What went poorly", ""])
        lines.extend(
            f"- {item.text} {citations(item.evidence_ids)}"
            for item in content.what_went_poorly
        )
        lines.extend(
            [
                "",
                "## Prevention items",
                "",
                "| Priority | Owner | Status | Action | Evidence |",
                "| --- | --- | --- | --- | --- |",
            ]
        )
        for item in content.prevention_items:
            lines.append(
                f"| {item.priority.value} | {safe(item.owner)} | {safe(item.status)} | "
                f"**{safe(item.title)}** — {safe(item.description)} | "
                f"{citations(item.evidence_ids)} |"
            )
        evidence_ids = sorted(
            {
                evidence_id
                for item in [
                    *(section for _, section in sections),
                    *content.what_went_well,
                    *content.what_went_poorly,
                    *content.prevention_items,
                    *content.timeline,
                ]
                for evidence_id in item.evidence_ids
            }
        )
        lines.extend(["", "## Evidence index", ""])
        lines.extend(f"- `E-{value[:6]}` → `{value}`" for value in evidence_ids)
        return "\n".join(lines) + "\n"

    def _load_incident(self, incident_id: UUID) -> IncidentRecord:
        record = self.session.scalar(
            select(IncidentRecord)
            .where(IncidentRecord.id == incident_id)
            .options(
                selectinload(IncidentRecord.alerts),
                selectinload(IncidentRecord.events),
                selectinload(IncidentRecord.postmortem),
                selectinload(IncidentRecord.investigations).selectinload(
                    InvestigationRunRecord.evidence
                ),
                selectinload(IncidentRecord.investigations).selectinload(
                    InvestigationRunRecord.error_clusters
                ),
                selectinload(IncidentRecord.investigations).selectinload(
                    InvestigationRunRecord.cause_candidates
                ),
                selectinload(IncidentRecord.investigations).selectinload(
                    InvestigationRunRecord.commit_candidates
                ),
                selectinload(IncidentRecord.investigations).selectinload(
                    InvestigationRunRecord.runbook_matches
                ),
                selectinload(IncidentRecord.proposals).selectinload(
                    MitigationProposalRecord.decisions
                ),
                selectinload(IncidentRecord.proposals).selectinload(
                    MitigationProposalRecord.execution
                ),
            )
            .execution_options(populate_existing=True)
        )
        if record is None:
            raise IncidentNotFoundError
        return record

    def _load_for_update(self, postmortem_id: UUID) -> PostmortemRecord:
        record = self.session.scalar(
            select(PostmortemRecord)
            .where(PostmortemRecord.id == postmortem_id)
            .options(selectinload(PostmortemRecord.revisions))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if record is None:
            raise PostmortemNotFoundError
        return record

    @staticmethod
    def _generation_context(
        incident: IncidentRecord,
        investigation: InvestigationRunRecord,
        proposal: MitigationProposalRecord,
    ) -> dict[str, Any]:
        alert = AlertPayload.model_validate(incident.alerts[0].payload)
        investigation_detail = InvestigationService._to_detail(investigation)
        proposal_detail = ProposalService._to_detail(proposal)
        return {
            "incident": {
                "id": str(incident.id),
                "service": incident.service,
                "severity": incident.severity,
                "status": incident.status,
                "started_at": incident.started_at.isoformat(),
                "detected_at": incident.detected_at.isoformat(),
                "resolved_at": incident.resolved_at.isoformat() if incident.resolved_at else None,
            },
            "alert": alert.model_dump(mode="json"),
            "investigation": {
                "id": str(investigation_detail.id),
                "error_clusters": [
                    item.model_dump(mode="json")
                    for item in investigation_detail.error_clusters
                ],
                "cause_candidates": [
                    item.model_dump(mode="json")
                    for item in investigation_detail.cause_candidates
                ],
                "commit_candidates": [
                    item.model_dump(mode="json")
                    for item in investigation_detail.commit_candidates
                ],
                "runbook_matches": [
                    item.model_dump(mode="json")
                    for item in investigation_detail.runbook_matches
                ],
                "evidence_manifest": [
                    {
                        "id": str(item.id),
                        "kind": item.kind,
                        "content_hash": item.content_hash,
                    }
                    for item in investigation_detail.evidence
                ],
            },
            "proposal": proposal_detail.model_dump(mode="json"),
            "events": [
                {
                    "id": str(event.id),
                    "event_type": event.event_type,
                    "actor": event.actor,
                    "description": event.note or "Incident record updated.",
                    "created_at": event.created_at.isoformat(),
                    "from_status": event.from_status,
                    "to_status": event.to_status,
                }
                for event in incident.events
            ],
        }

    @staticmethod
    def _allowed_evidence_ids(
        incident: IncidentRecord,
        investigation: InvestigationRunRecord,
        proposal: MitigationProposalRecord,
    ) -> set[str]:
        records = [
            *incident.alerts,
            *incident.events,
            *investigation.evidence,
            *investigation.error_clusters,
            *investigation.cause_candidates,
            *investigation.commit_candidates,
            *investigation.runbook_matches,
            proposal,
            *proposal.decisions,
        ]
        if proposal.execution is not None:
            records.append(proposal.execution)
        return {str(record.id) for record in records}

    def _add_revision(
        self,
        record: PostmortemRecord,
        source: str,
        editor: str,
        change_note: str,
    ) -> None:
        self.session.add(
            PostmortemRevisionRecord(
                postmortem_id=record.id,
                version=record.version,
                source=source,
                editor=editor,
                change_note=change_note,
                snapshot={
                    "status": record.status,
                    "version": record.version,
                    "content": record.content,
                },
            )
        )

    @staticmethod
    def _to_detail(record: PostmortemRecord) -> PostmortemDetail:
        return PostmortemDetail(
            id=record.id,
            incident_id=record.incident_id,
            status=PostmortemStatus(record.status),
            version=record.version,
            generator_version=record.generator_version,
            model_name=record.model_name,
            prompt_version=record.prompt_version,
            input_hash=record.input_hash,
            content=PostmortemContent.model_validate(record.content),
            created_at=record.created_at,
            updated_at=record.updated_at,
            finalized_at=record.finalized_at,
            finalized_by=record.finalized_by,
            revisions=[
                PostmortemRevisionDetail(
                    id=revision.id,
                    version=revision.version,
                    source=revision.source,
                    editor=revision.editor,
                    change_note=revision.change_note,
                    created_at=revision.created_at,
                )
                for revision in record.revisions
            ],
        )


def build_postmortem_service(session: Session) -> PostmortemService:
    api_key_value = (
        settings.openai_api_key.get_secret_value().strip()
        if settings.openai_api_key is not None
        else ""
    )
    api_key = api_key_value or None
    provider = settings.synthesis_provider
    if provider == "openai" and not api_key:
        raise PostmortemGenerationError("SYNTHESIS_PROVIDER=openai requires OPENAI_API_KEY")
    generator: PostmortemGenerator
    if provider == "openai" or (provider == "auto" and api_key):
        generator = OpenAIPostmortemGenerator(
            api_key=api_key or "",
            model_name=settings.openai_model,
            base_url=settings.openai_base_url,
            timeout_seconds=settings.synthesis_http_timeout_seconds,
        )
    else:
        generator = DeterministicPostmortemGenerator()
    return PostmortemService(
        session=session,
        generator=generator,
        validator=PostmortemGroundingValidator(),
    )
