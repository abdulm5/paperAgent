from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from time import monotonic
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth.dependencies import (
    AuthenticatedRequest,
    require_authenticated_permission,
    require_permission,
)
from app.auth.service import AuthService, PrincipalNotFoundError
from app.db.models import IncidentRecord, WorkflowEventRecord
from app.db.session import SessionLocal, get_db
from app.domain.auth import Permission, Principal
from app.domain.workflows import WorkflowRun
from app.workflows.store import WorkflowNotFoundError, WorkflowStore

router = APIRouter(tags=["workflows"])


@router.get("/incidents/{incident_id}/workflows", response_model=list[WorkflowRun])
def list_incident_workflows(
    incident_id: UUID,
    principal: Principal = Depends(require_permission(Permission.INCIDENTS_READ)),
    session: Session = Depends(get_db),
) -> list[WorkflowRun]:
    if (
        session.scalar(
            select(IncidentRecord.id).where(
                IncidentRecord.id == incident_id,
                IncidentRecord.organization_id == principal.organization_id,
            )
        )
        is None
    ):
        raise HTTPException(status_code=404, detail="Incident not found")
    return WorkflowStore(session, principal.organization_id).list_for_incident(incident_id)


@router.get("/workflows/{workflow_id:uuid}", response_model=WorkflowRun)
def get_workflow(
    workflow_id: UUID,
    principal: Principal = Depends(require_permission(Permission.INCIDENTS_READ)),
    session: Session = Depends(get_db),
) -> WorkflowRun:
    try:
        return WorkflowStore(session, principal.organization_id).get_detail(workflow_id)
    except WorkflowNotFoundError as error:
        raise HTTPException(status_code=404, detail="Workflow not found") from error


def encode_workflow_event(event: WorkflowEventRecord, workflow: WorkflowRun) -> str:
    envelope = {
        "id": event.id,
        "workflow_id": str(event.workflow_run_id),
        "incident_id": str(workflow.incident_id),
        "sequence": event.sequence,
        "event_type": event.event_type,
        "payload": event.payload,
        "created_at": event.created_at.isoformat(),
        "workflow": workflow.model_dump(mode="json"),
    }
    data = json.dumps(envelope, separators=(",", ":"))
    return f"id: {event.id}\nevent: workflow\ndata: {data}\n\n"


async def workflow_event_stream(
    request: Request,
    *,
    principal: Principal,
    session_expires_at: datetime,
    last_event_id: int = 0,
    poll_seconds: float = 1.0,
) -> AsyncIterator[str]:
    cursor = last_event_id
    last_heartbeat = monotonic()
    while not await request.is_disconnected():
        if datetime.now(UTC) >= session_expires_at:
            return
        emitted = False
        with SessionLocal() as session:
            try:
                refreshed = AuthService(session).load_principal(
                    principal.user_id,
                    principal.organization_id,
                )
            except PrincipalNotFoundError:
                return
            if Permission.INCIDENTS_READ not in refreshed.permissions:
                return
            principal = refreshed
            store = WorkflowStore(session, principal.organization_id)
            events = store.events_after(cursor, limit=100)
            for event in events:
                workflow = store.get_detail(event.workflow_run_id)
                yield encode_workflow_event(event, workflow)
                cursor = event.id
                emitted = True
        if not emitted and monotonic() - last_heartbeat >= 15:
            yield ": keepalive\n\n"
            last_heartbeat = monotonic()
        await asyncio.sleep(poll_seconds)


@router.get("/workflows/events")
def stream_workflow_events(
    request: Request,
    authenticated: AuthenticatedRequest = Depends(
        require_authenticated_permission(Permission.INCIDENTS_READ)
    ),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    principal = authenticated.principal
    if last_event_id is None:
        # REST snapshots provide current state. A fresh browser only needs events
        # committed after it connects; reconnects replay from Last-Event-ID.
        with SessionLocal() as session:
            cursor = (
                session.scalar(
                    select(func.max(WorkflowEventRecord.id))
                    .join(WorkflowEventRecord.workflow_run)
                    .join(IncidentRecord)
                    .where(IncidentRecord.organization_id == principal.organization_id)
                )
                or 0
            )
    else:
        try:
            cursor = int(last_event_id)
        except (TypeError, ValueError) as error:
            raise HTTPException(status_code=400, detail="Invalid Last-Event-ID") from error
        if cursor < 0:
            raise HTTPException(status_code=400, detail="Invalid Last-Event-ID")
    return StreamingResponse(
        workflow_event_stream(
            request,
            principal=principal,
            session_expires_at=authenticated.claims.expires_at,
            last_event_id=cursor,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
