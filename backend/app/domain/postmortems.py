from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, Field


class PostmortemStatus(StrEnum):
    DRAFT = "draft"
    FINAL = "final"


class PreventionPriority(StrEnum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"


class GroundedSection(BaseModel):
    text: str = Field(min_length=1, max_length=5_000)
    evidence_ids: list[str] = Field(min_length=1)


class GroundedObservation(BaseModel):
    text: str = Field(min_length=1, max_length=2_000)
    evidence_ids: list[str] = Field(min_length=1)


class PreventionItem(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    description: str = Field(min_length=1, max_length=2_000)
    owner: str = Field(min_length=1, max_length=100)
    priority: PreventionPriority
    status: str = "open"
    evidence_ids: list[str] = Field(min_length=1)


class TimelineEntry(BaseModel):
    occurred_at: datetime
    event_type: str
    actor: str
    description: str
    evidence_ids: list[str] = Field(min_length=1)


class PostmortemNarrativeDraft(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    summary: GroundedSection
    root_cause: GroundedSection
    customer_impact: GroundedSection
    detection: GroundedSection
    resolution: GroundedSection
    what_went_well: list[GroundedObservation] = Field(min_length=1, max_length=8)
    what_went_poorly: list[GroundedObservation] = Field(min_length=1, max_length=8)
    prevention_items: list[PreventionItem] = Field(min_length=1, max_length=12)


class PostmortemContent(PostmortemNarrativeDraft):
    timeline: list[TimelineEntry] = Field(min_length=1)


class EditablePreventionItem(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    description: str = Field(min_length=1, max_length=2_000)
    owner: str = Field(min_length=1, max_length=100)
    priority: PreventionPriority
    status: str = Field(min_length=1, max_length=50)


class PostmortemUpdateRequest(BaseModel):
    expected_version: int = Field(gt=0)
    actor: str = Field(min_length=1, max_length=100)
    change_note: str = Field(min_length=1, max_length=500)
    title: str = Field(min_length=1, max_length=300)
    summary: str = Field(min_length=1, max_length=5_000)
    root_cause: str = Field(min_length=1, max_length=5_000)
    customer_impact: str = Field(min_length=1, max_length=5_000)
    detection: str = Field(min_length=1, max_length=5_000)
    resolution: str = Field(min_length=1, max_length=5_000)
    what_went_well: list[str] = Field(min_length=1, max_length=8)
    what_went_poorly: list[str] = Field(min_length=1, max_length=8)
    prevention_items: list[EditablePreventionItem] = Field(min_length=1, max_length=12)


class PostmortemFinalizeRequest(BaseModel):
    expected_version: int = Field(gt=0)
    actor: str = Field(min_length=1, max_length=100)
    note: str | None = Field(default=None, max_length=500)


class PostmortemRevisionDetail(BaseModel):
    id: UUID
    version: int
    source: str
    editor: str
    change_note: str
    created_at: datetime


class PostmortemDetail(BaseModel):
    id: UUID
    incident_id: UUID
    status: PostmortemStatus
    version: int
    generator_version: str
    model_name: str
    prompt_version: str
    input_hash: str
    content: PostmortemContent
    created_at: datetime
    updated_at: datetime
    finalized_at: datetime | None
    finalized_by: str | None
    revisions: list[PostmortemRevisionDetail]
