from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from app.domain.evaluations import CauseCandidate


class InvestigationStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class EvidenceArtifact(BaseModel):
    id: UUID
    kind: str
    source_uri: str
    content_hash: str
    payload: dict[str, Any]
    collected_at: datetime


class ErrorCluster(BaseModel):
    id: UUID
    signature: str
    error_type: str
    endpoint: str
    affected_attributes: dict[str, Any]
    failure_count: int
    first_seen_at: datetime
    last_seen_at: datetime
    sample_request_ids: list[str]
    evidence_ids: list[str]


class CommitCandidate(BaseModel):
    id: UUID
    commit_sha: str
    rank: int
    total_score: float
    title: str
    author: str
    committed_at: datetime
    files_changed: list[str]
    diff_summary: str
    feature_scores: dict[str, float]
    explanation: list[str]
    evidence_ids: list[str]


class RunbookSection(BaseModel):
    heading: str
    excerpt: str


class RunbookMatch(BaseModel):
    id: UUID
    runbook_id: str
    rank: int
    title: str
    service: str
    failure_mode: str
    total_score: float
    score_breakdown: dict[str, float]
    matched_sections: list[RunbookSection]
    content_hash: str
    evidence_ids: list[str]


class InvestigationDetail(BaseModel):
    id: UUID
    incident_id: UUID
    status: InvestigationStatus
    collector_version: str
    clusterer_version: str
    ranker_version: str
    retrieval_version: str
    input_hash: str
    failure_reason: str | None
    started_at: datetime
    completed_at: datetime | None
    evidence: list[EvidenceArtifact]
    error_clusters: list[ErrorCluster]
    cause_candidates: list[CauseCandidate]
    commit_candidates: list[CommitCandidate]
    runbook_matches: list[RunbookMatch]
