from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.db.models import AlertRecord, IncidentEventRecord, IncidentRecord
from app.domain.incidents import (
    AlertPayload,
    AlertSeverity,
    IncidentDetail,
    IncidentEvent,
    IncidentStatus,
    IncidentSummary,
    IncidentTransitionRequest,
)

ALLOWED_TRANSITIONS = {
    IncidentStatus.DETECTED: {IncidentStatus.INVESTIGATING},
    IncidentStatus.INVESTIGATING: {IncidentStatus.MITIGATED},
    IncidentStatus.MITIGATED: {IncidentStatus.RESOLVED},
    IncidentStatus.RESOLVED: set(),
}


class IncidentNotFoundError(Exception):
    pass


class InvalidTransitionError(Exception):
    def __init__(self, current: IncidentStatus, requested: IncidentStatus) -> None:
        super().__init__(f"Cannot transition incident from {current} to {requested}")


class IncidentVersionConflictError(Exception):
    def __init__(self, current_version: int) -> None:
        self.current_version = current_version
        super().__init__(f"Incident changed; current version is {current_version}")


class IncidentService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def ingest_alert(self, alert: AlertPayload) -> tuple[IncidentDetail, bool]:
        incident = self._find_active_by_fingerprint(alert.fingerprint)
        deduplicated = incident is not None

        if incident is None:
            incident = IncidentRecord(
                fingerprint=alert.fingerprint,
                active_fingerprint=alert.fingerprint,
                status=IncidentStatus.DETECTED.value,
                service=alert.service,
                severity=alert.severity.value,
                summary=alert.summary,
                started_at=alert.started_at,
                detected_at=alert.detected_at,
                version=1,
            )
            self.session.add(incident)
            try:
                self.session.flush()
            except IntegrityError:
                self.session.rollback()
                incident = self._find_active_by_fingerprint(alert.fingerprint)
                if incident is None:
                    raise
                deduplicated = True

        self.session.add(
            AlertRecord(
                incident_id=incident.id,
                source=alert.source,
                fingerprint=alert.fingerprint,
                deduplicated=deduplicated,
                payload=alert.model_dump(mode="json"),
            )
        )
        self.session.add(
            IncidentEventRecord(
                incident_id=incident.id,
                event_type="alert.deduplicated" if deduplicated else "incident.detected",
                actor=alert.source,
                from_status=None,
                to_status=IncidentStatus.DETECTED.value,
                note=(
                    "Repeated alert attached to the active incident."
                    if deduplicated
                    else "Monitoring threshold created the incident."
                ),
                payload={
                    "metric": alert.metric.model_dump(mode="json"),
                    "release": alert.release.model_dump(mode="json"),
                },
            )
        )
        self.session.commit()
        return self.get_detail(incident.id), deduplicated

    def list_incidents(self) -> list[IncidentSummary]:
        records = self.session.scalars(
            select(IncidentRecord).order_by(IncidentRecord.received_at.desc())
        ).all()
        return [self._to_summary(record) for record in records]

    def get_detail(self, incident_id: UUID) -> IncidentDetail:
        record = self.session.scalar(
            select(IncidentRecord)
            .where(IncidentRecord.id == incident_id)
            .options(
                selectinload(IncidentRecord.alerts),
                selectinload(IncidentRecord.events),
            )
        )
        if record is None:
            raise IncidentNotFoundError
        return self._to_detail(record)

    def transition(
        self,
        incident_id: UUID,
        request: IncidentTransitionRequest,
    ) -> IncidentDetail:
        record = self.session.scalar(
            select(IncidentRecord).where(IncidentRecord.id == incident_id).with_for_update()
        )
        if record is None:
            raise IncidentNotFoundError

        current_status = IncidentStatus(record.status)
        if record.version != request.expected_version:
            raise IncidentVersionConflictError(record.version)
        if request.to_status not in ALLOWED_TRANSITIONS[current_status]:
            raise InvalidTransitionError(current_status, request.to_status)

        now = datetime.now(UTC)
        record.status = request.to_status.value
        record.version += 1
        record.updated_at = now
        if request.to_status is IncidentStatus.RESOLVED:
            record.resolved_at = now
            record.active_fingerprint = None

        self.session.add(
            IncidentEventRecord(
                incident_id=record.id,
                event_type="incident.status_changed",
                actor=request.actor,
                from_status=current_status.value,
                to_status=request.to_status.value,
                note=request.note,
                payload={"version": record.version},
            )
        )
        self.session.commit()
        return self.get_detail(record.id)

    def clear(self) -> int:
        count = len(self.session.scalars(select(IncidentRecord.id)).all())
        self.session.execute(delete(IncidentEventRecord))
        self.session.execute(delete(AlertRecord))
        self.session.execute(delete(IncidentRecord))
        self.session.commit()
        return count

    def _find_active_by_fingerprint(self, fingerprint: str) -> IncidentRecord | None:
        return self.session.scalar(
            select(IncidentRecord).where(IncidentRecord.active_fingerprint == fingerprint)
        )

    @staticmethod
    def _to_summary(record: IncidentRecord) -> IncidentSummary:
        return IncidentSummary(
            id=record.id,
            status=IncidentStatus(record.status),
            service=record.service,
            severity=AlertSeverity(record.severity),
            summary=record.summary,
            started_at=record.started_at,
            detected_at=record.detected_at,
            received_at=record.received_at,
            updated_at=record.updated_at,
            resolved_at=record.resolved_at,
            version=record.version,
        )

    @classmethod
    def _to_detail(cls, record: IncidentRecord) -> IncidentDetail:
        if not record.alerts:
            raise RuntimeError("Persisted incident is missing its source alert")
        summary = cls._to_summary(record)
        return IncidentDetail(
            **summary.model_dump(),
            alert=AlertPayload.model_validate(record.alerts[0].payload),
            alert_count=len(record.alerts),
            events=[
                IncidentEvent(
                    id=event.id,
                    event_type=event.event_type,
                    actor=event.actor,
                    from_status=(
                        IncidentStatus(event.from_status) if event.from_status is not None else None
                    ),
                    to_status=(
                        IncidentStatus(event.to_status) if event.to_status is not None else None
                    ),
                    note=event.note,
                    payload=event.payload,
                    created_at=event.created_at,
                )
                for event in record.events
            ],
        )
