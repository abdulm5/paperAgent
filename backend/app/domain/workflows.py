from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class WorkflowType(StrEnum):
    INCIDENT_RESPONSE = "incident_response"
    MITIGATION = "mitigation"
    POSTMORTEM = "postmortem"
    COLLABORATION = "collaboration"


class WorkflowStepType(StrEnum):
    INVESTIGATE = "investigate"
    GENERATE_PROPOSAL = "generate_proposal"
    EXECUTE_MITIGATION = "execute_mitigation"
    GENERATE_POSTMORTEM = "generate_postmortem"
    DELIVER_COLLABORATION_OUTPUT = "deliver_collaboration_output"


class WorkflowStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    RETRY_SCHEDULED = "retry_scheduled"
    COMPLETED = "completed"
    DEAD_LETTERED = "dead_lettered"


class OutboxMessage(BaseModel):
    id: UUID
    workflow_job_id: UUID
    topic: str
    payload: dict[str, Any]
    dispatch_attempt: int
    available_at: datetime
    published_at: datetime | None
    publish_attempts: int
    stream_message_id: str | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime


class WorkflowJob(BaseModel):
    id: UUID
    workflow_run_id: UUID
    step_type: str
    status: WorkflowStatus
    payload: dict[str, Any]
    result: dict[str, Any]
    idempotency_key: str
    attempt_count: int
    max_attempts: int
    available_at: datetime
    lease_owner: str | None
    lease_expires_at: datetime | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    deliveries: list[OutboxMessage] = Field(default_factory=list)


class WorkflowEvent(BaseModel):
    id: int
    workflow_run_id: UUID
    workflow_job_id: UUID | None
    sequence: int
    event_type: str
    payload: dict[str, Any]
    created_at: datetime


class WorkflowRun(BaseModel):
    id: UUID
    incident_id: UUID
    workflow_type: WorkflowType
    status: WorkflowStatus
    current_step: str | None
    dedupe_key: str
    trace_id: str | None
    version: int
    failure_reason: str | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None
    jobs: list[WorkflowJob] = Field(default_factory=list)
    events: list[WorkflowEvent] = Field(default_factory=list)
