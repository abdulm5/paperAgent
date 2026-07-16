import hashlib
import hmac
import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.connectors.runtime import GithubConnectorRuntime
from app.db.models import (
    ConnectorCredentialRecord,
    ConnectorRecord,
    GithubWebhookDeliveryRecord,
)
from app.domain.connectors import ConnectorProvider, ConnectorStatus, GithubConfiguration
from app.domain.github import GitWebhookReceipt as GitEvidenceWebhookReceipt
from app.domain.github_webhooks import (
    GithubEventType,
    GithubWebhookDelivery,
    GithubWebhookReceipt,
)

SIGNATURE_PATTERN = re.compile(r"sha256=[0-9a-f]{64}\Z")
SHA_PATTERN = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})\Z")
CONTROL_CHARACTER_PATTERN = re.compile(r"[\x00-\x1f\x7f]+")
SERVICE_PATTERN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,98}[A-Za-z0-9])?\Z")
MAX_COMMITS = 100
MAX_FILENAMES_PER_CHANGE_KIND = 100
MAX_LISTED_DELIVERIES = 100

SUPPORTED_ACTIONS: dict[str, frozenset[str] | None] = {
    "push": None,
    "pull_request": frozenset(
        {
            "closed",
            "converted_to_draft",
            "edited",
            "opened",
            "ready_for_review",
            "reopened",
            "synchronize",
        }
    ),
    "deployment": frozenset({"created"}),
    "deployment_status": frozenset({"created"}),
    "release": frozenset(
        {
            "created",
            "deleted",
            "edited",
            "prereleased",
            "published",
            "released",
            "unpublished",
        }
    ),
}

NORMALIZED_TOP_LEVEL_FIELDS: dict[str, frozenset[str]] = {
    "push": frozenset(
        {
            "service",
            "ref",
            "before",
            "after",
            "created",
            "deleted",
            "forced",
            "commit_count",
            "commits_truncated",
            "commits",
            "head_commit",
        }
    ),
    "pull_request": frozenset(
        {
            "service",
            "number",
            "title",
            "state",
            "draft",
            "merged",
            "head_sha",
            "base_sha",
            "created_at",
            "updated_at",
            "closed_at",
            "merged_at",
        }
    ),
    "deployment": frozenset(
        {
            "service",
            "deployment_id",
            "sha",
            "ref",
            "environment",
            "status_id",
            "status",
            "created_at",
            "updated_at",
        }
    ),
    "deployment_status": frozenset(
        {
            "service",
            "deployment_id",
            "sha",
            "ref",
            "environment",
            "status_id",
            "status",
            "created_at",
            "updated_at",
        }
    ),
    "release": frozenset(
        {
            "service",
            "release_id",
            "tag_name",
            "name",
            "draft",
            "prerelease",
            "created_at",
            "published_at",
        }
    ),
}


class GithubWebhookSignatureError(Exception):
    pass


class GithubWebhookPayloadError(Exception):
    pass


class GithubWebhookReplayConflictError(Exception):
    pass


class GithubWebhookConnectorChangedError(Exception):
    pass


class GithubDeliveryConnectorNotFoundError(Exception):
    pass


class GithubWebhookIntegrityError(Exception):
    pass


def verify_github_signature(secret: str, raw_body: bytes, signature: str) -> bool:
    """Verify GitHub's SHA-256 signature over the exact, unparsed request bytes."""

    if not SIGNATURE_PATTERN.fullmatch(signature):
        return False
    expected = (
        "sha256="
        + hmac.new(
            secret.encode("utf-8"),
            raw_body,
            hashlib.sha256,
        ).hexdigest()
    )
    return hmac.compare_digest(expected, signature)


class GithubWebhookService:
    def __init__(self, session: Session, runtime: GithubConnectorRuntime) -> None:
        self.session = session
        self.runtime = runtime

    def ingest(
        self,
        *,
        delivery_id: str,
        event_type: GithubEventType,
        signature: str,
        raw_body: bytes,
    ) -> GithubWebhookReceipt:
        secret = self.runtime.credentials.webhook_secret.get_secret_value()
        if not verify_github_signature(secret, raw_body, signature):
            raise GithubWebhookSignatureError

        body_sha256 = hashlib.sha256(raw_body).hexdigest()
        self._lock_current_runtime()
        existing = self.session.scalar(
            select(GithubWebhookDeliveryRecord).where(
                GithubWebhookDeliveryRecord.connector_id == self.runtime.connector_id,
                GithubWebhookDeliveryRecord.delivery_id == delivery_id,
            )
        )
        if existing is not None:
            if existing.event_type == event_type and existing.body_sha256 == body_sha256:
                self.session.rollback()
                return GithubWebhookReceipt(
                    delivery_id=delivery_id,
                    event_type=event_type,
                    duplicate=True,
                )
            self.session.rollback()
            raise GithubWebhookReplayConflictError
        try:
            payload = _parse_payload(raw_body)
            repository, installation_id = self._validate_binding(payload)
            action = _validate_action(event_type, payload)
            normalized_payload = self._normalize(event_type, payload)
        except Exception:
            self.session.rollback()
            raise
        record = GithubWebhookDeliveryRecord(
            id=uuid4(),
            organization_id=self.runtime.organization_id,
            connector_id=self.runtime.connector_id,
            delivery_id=delivery_id,
            event_type=event_type,
            action=action,
            repository=repository,
            installation_id=installation_id,
            connector_version=self.runtime.connector_version,
            credential_version=self.runtime.credential_version,
            body_sha256=body_sha256,
            normalized_payload=normalized_payload,
        )

        try:
            with self.session.begin_nested():
                self.session.add(record)
                self.session.flush()
        except IntegrityError as error:
            existing = self.session.scalar(
                select(GithubWebhookDeliveryRecord).where(
                    GithubWebhookDeliveryRecord.connector_id == self.runtime.connector_id,
                    GithubWebhookDeliveryRecord.delivery_id == delivery_id,
                )
            )
            if (
                existing is not None
                and existing.event_type == event_type
                and existing.body_sha256 == body_sha256
            ):
                self.session.commit()
                return GithubWebhookReceipt(
                    delivery_id=delivery_id,
                    event_type=event_type,
                    duplicate=True,
                )
            self.session.rollback()
            if existing is None:
                raise GithubWebhookIntegrityError from error
            raise GithubWebhookReplayConflictError from error

        self.session.commit()
        return GithubWebhookReceipt(
            delivery_id=delivery_id,
            event_type=event_type,
            duplicate=False,
        )

    def _lock_current_runtime(self) -> None:
        current = self.session.execute(
            select(
                ConnectorRecord.version,
                ConnectorRecord.configuration,
                ConnectorCredentialRecord.credential_version,
            )
            .join(
                ConnectorCredentialRecord,
                ConnectorCredentialRecord.connector_id == ConnectorRecord.id,
            )
            .where(
                ConnectorRecord.id == self.runtime.connector_id,
                ConnectorRecord.organization_id == self.runtime.organization_id,
                ConnectorRecord.provider == ConnectorProvider.GITHUB.value,
                ConnectorRecord.enabled.is_(True),
                ConnectorRecord.status == ConnectorStatus.CONFIGURED.value,
            )
            .with_for_update()
        ).one_or_none()
        try:
            current_configuration = (
                GithubConfiguration.model_validate(current.configuration)
                if current is not None
                else None
            )
        except ValidationError:
            current_configuration = None
        if (
            current is None
            or current.version != self.runtime.connector_version
            or current.credential_version != self.runtime.credential_version
            or current_configuration != self.runtime.configuration
        ):
            self.session.rollback()
            raise GithubWebhookConnectorChangedError

    def _validate_binding(self, payload: Mapping[str, Any]) -> tuple[str, int]:
        repository = _mapping(payload.get("repository"))
        installation = _mapping(payload.get("installation"))
        repository_name = _bounded_text(repository.get("full_name"), 201).lower()
        installation_id = _positive_int(installation.get("id"))
        if (
            repository_name != self.runtime.configuration.repository
            or installation_id != self.runtime.configuration.installation_id
        ):
            raise GithubWebhookPayloadError
        return repository_name, installation_id

    def _normalize(
        self,
        event_type: GithubEventType,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        if event_type == "push":
            return _normalize_push(payload, self.runtime.configuration.service)
        if event_type == "pull_request":
            return _normalize_pull_request(payload, self.runtime.configuration.service)
        if event_type in {"deployment", "deployment_status"}:
            return _normalize_deployment(
                payload,
                self.runtime.configuration.service,
                include_status=event_type == "deployment_status",
            )
        if event_type == "release":
            return _normalize_release(payload, self.runtime.configuration.service)
        raise GithubWebhookPayloadError


def list_github_deliveries(
    session: Session,
    organization_id: UUID,
    connector_id: UUID,
) -> list[GithubWebhookDelivery]:
    connector_exists = session.scalar(
        select(ConnectorRecord.id).where(
            ConnectorRecord.id == connector_id,
            ConnectorRecord.organization_id == organization_id,
            ConnectorRecord.provider == ConnectorProvider.GITHUB.value,
        )
    )
    if connector_exists is None:
        raise GithubDeliveryConnectorNotFoundError

    records = session.scalars(
        select(GithubWebhookDeliveryRecord)
        .where(
            GithubWebhookDeliveryRecord.organization_id == organization_id,
            GithubWebhookDeliveryRecord.connector_id == connector_id,
        )
        .order_by(
            GithubWebhookDeliveryRecord.received_at.desc(),
            GithubWebhookDeliveryRecord.id.desc(),
        )
        .limit(MAX_LISTED_DELIVERIES)
    ).all()
    return [_to_delivery(record) for record in records]


def _parse_payload(raw_body: bytes) -> Mapping[str, Any]:
    try:
        decoded = raw_body.decode("utf-8")
        payload = json.loads(decoded)
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        raise GithubWebhookPayloadError from error
    if not isinstance(payload, dict):
        raise GithubWebhookPayloadError
    return payload


def _validate_action(event_type: str, payload: Mapping[str, Any]) -> str | None:
    allowed = SUPPORTED_ACTIONS.get(event_type)
    if event_type not in SUPPORTED_ACTIONS:
        raise GithubWebhookPayloadError
    action = payload.get("action")
    if allowed is None:
        if action is not None:
            raise GithubWebhookPayloadError
        return None
    if not isinstance(action, str) or action not in allowed:
        raise GithubWebhookPayloadError
    return action


def _normalize_push(payload: Mapping[str, Any], service: str) -> dict[str, Any]:
    commits_value = payload.get("commits")
    if not isinstance(commits_value, list):
        raise GithubWebhookPayloadError
    commits = [_normalize_commit(item) for item in commits_value[:MAX_COMMITS]]
    head_commit_value = payload.get("head_commit")
    head_commit = None
    if head_commit_value is not None:
        head_commit = _normalize_commit(head_commit_value)
    return {
        "service": service,
        "ref": _bounded_text(payload.get("ref"), 500),
        "before": _sha(payload.get("before")),
        "after": _sha(payload.get("after")),
        "created": _boolean(payload.get("created")),
        "deleted": _boolean(payload.get("deleted")),
        "forced": _boolean(payload.get("forced")),
        "commit_count": len(commits_value),
        "commits_truncated": len(commits_value) > MAX_COMMITS,
        "commits": commits,
        "head_commit": head_commit,
    }


def _normalize_commit(value: object) -> dict[str, Any]:
    commit = _mapping(value)
    author_value = commit.get("author")
    author = _mapping(author_value) if author_value is not None else {}
    added = _filenames(commit.get("added"))
    removed = _filenames(commit.get("removed"))
    modified = _filenames(commit.get("modified"))
    return {
        "sha": _sha(commit.get("id")),
        "title": _first_line(commit.get("message"), 300),
        "timestamp": _optional_timestamp(commit.get("timestamp")),
        "author": {
            "name": _optional_text(author.get("name"), 200),
            "username": _optional_text(author.get("username"), 100),
        },
        "files": {
            "added": added,
            "removed": removed,
            "modified": modified,
        },
        "file_stats": {
            "added": _list_length(commit.get("added")),
            "removed": _list_length(commit.get("removed")),
            "modified": _list_length(commit.get("modified")),
        },
    }


def _normalize_pull_request(payload: Mapping[str, Any], service: str) -> dict[str, Any]:
    pull_request = _mapping(payload.get("pull_request"))
    head = _mapping(pull_request.get("head"))
    base = _mapping(pull_request.get("base"))
    number = _positive_int(payload.get("number"))
    if _positive_int(pull_request.get("number")) != number:
        raise GithubWebhookPayloadError
    state = _bounded_text(pull_request.get("state"), 16)
    if state not in {"open", "closed"}:
        raise GithubWebhookPayloadError
    return {
        "service": service,
        "number": number,
        "title": _first_line(pull_request.get("title"), 300),
        "state": state,
        "draft": _boolean(pull_request.get("draft")),
        "merged": _boolean(pull_request.get("merged")),
        "head_sha": _sha(head.get("sha")),
        "base_sha": _sha(base.get("sha")),
        "created_at": _required_timestamp(pull_request.get("created_at")),
        "updated_at": _required_timestamp(pull_request.get("updated_at")),
        "closed_at": _optional_timestamp(pull_request.get("closed_at")),
        "merged_at": _optional_timestamp(pull_request.get("merged_at")),
    }


def _normalize_deployment(
    payload: Mapping[str, Any],
    service: str,
    *,
    include_status: bool,
) -> dict[str, Any]:
    deployment = _mapping(payload.get("deployment"))
    status_id = None
    status = None
    created_at = _required_timestamp(deployment.get("created_at"))
    updated_at = _required_timestamp(deployment.get("updated_at"))
    if include_status:
        deployment_status = _mapping(payload.get("deployment_status"))
        status_id = _positive_int(deployment_status.get("id"))
        status = _bounded_text(deployment_status.get("state"), 32)
        if status not in {
            "error",
            "failure",
            "inactive",
            "in_progress",
            "pending",
            "queued",
            "success",
        }:
            raise GithubWebhookPayloadError
        created_at = _required_timestamp(deployment_status.get("created_at"))
        updated_at = _required_timestamp(deployment_status.get("updated_at"))
    return {
        "service": service,
        "deployment_id": _positive_int(deployment.get("id")),
        "sha": _sha(deployment.get("sha")),
        "ref": _bounded_text(deployment.get("ref"), 500),
        "environment": _bounded_text(deployment.get("environment"), 255),
        "status_id": status_id,
        "status": status,
        "created_at": created_at,
        "updated_at": updated_at,
    }


def _normalize_release(payload: Mapping[str, Any], service: str) -> dict[str, Any]:
    release = _mapping(payload.get("release"))
    return {
        "service": service,
        "release_id": _positive_int(release.get("id")),
        "tag_name": _bounded_text(release.get("tag_name"), 255),
        "name": _optional_first_line(release.get("name"), 255),
        "draft": _boolean(release.get("draft")),
        "prerelease": _boolean(release.get("prerelease")),
        "created_at": _required_timestamp(release.get("created_at")),
        "published_at": _optional_timestamp(release.get("published_at")),
    }


def _mapping(value: object) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise GithubWebhookPayloadError
    return value


def _bounded_text(
    value: object,
    max_length: int,
    *,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str):
        raise GithubWebhookPayloadError
    sanitized = CONTROL_CHARACTER_PATTERN.sub(" ", value)
    sanitized = " ".join(sanitized.split())
    if not sanitized and not allow_empty:
        raise GithubWebhookPayloadError
    return sanitized[:max_length]


def _optional_text(value: object, max_length: int) -> str | None:
    if value is None:
        return None
    return _bounded_text(value, max_length, allow_empty=True)


def _optional_timestamp(value: object) -> str | None:
    if value is None:
        return None
    text = _bounded_text(value, 64)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise GithubWebhookPayloadError from error
    if parsed.tzinfo is None:
        raise GithubWebhookPayloadError
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _required_timestamp(value: object) -> str:
    timestamp = _optional_timestamp(value)
    if timestamp is None:
        raise GithubWebhookPayloadError
    return timestamp


def _first_line(value: object, max_length: int) -> str:
    if not isinstance(value, str):
        raise GithubWebhookPayloadError
    lines = value.splitlines()
    if not lines:
        raise GithubWebhookPayloadError
    return _bounded_text(lines[0], max_length)


def _optional_first_line(value: object, max_length: int) -> str | None:
    if value is None:
        return None
    return _first_line(value, max_length)


def _sha(value: object) -> str:
    if not isinstance(value, str) or not SHA_PATTERN.fullmatch(value):
        raise GithubWebhookPayloadError
    return value.lower()


def _positive_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise GithubWebhookPayloadError
    return value


def _boolean(value: object) -> bool:
    if not isinstance(value, bool):
        raise GithubWebhookPayloadError
    return value


def _filenames(value: object) -> list[str]:
    if not isinstance(value, list):
        raise GithubWebhookPayloadError
    return [
        _normalized_file_path(filename)
        for filename in value[:MAX_FILENAMES_PER_CHANGE_KIND]
    ]


def _normalized_file_path(value: object) -> str:
    if not isinstance(value, str) or CONTROL_CHARACTER_PATTERN.search(value) is not None:
        raise GithubWebhookPayloadError
    path = _bounded_text(value, 500)
    if (
        path.startswith("/")
        or "\\" in path
        or any(segment in {"", ".", ".."} for segment in path.split("/"))
    ):
        raise GithubWebhookPayloadError
    return path


def _list_length(value: object) -> int:
    if not isinstance(value, list):
        raise GithubWebhookPayloadError
    return len(value)


def _to_delivery(record: GithubWebhookDeliveryRecord) -> GithubWebhookDelivery:
    expected_fields = NORMALIZED_TOP_LEVEL_FIELDS.get(record.event_type)
    try:
        GitEvidenceWebhookReceipt(
            delivery_id=record.delivery_id,
            event_type=record.event_type,
            action=record.action,
            repository=record.repository,
            installation_id=record.installation_id,
            connector_version=record.connector_version,
            credential_version=record.credential_version,
            body_sha256=record.body_sha256,
            received_at=record.received_at,
        )
        payload_valid = _normalized_payload_is_valid(
            record.event_type,
            record.normalized_payload,
        )
    except (ValidationError, ValueError):
        payload_valid = False
    try:
        delivery_id_valid = str(UUID(record.delivery_id)) == record.delivery_id
    except ValueError:
        delivery_id_valid = False
    repository_parts = record.repository.split("/", 1)
    repository_valid = (
        len(repository_parts) == 2
        and all(part not in {".", ".."} for part in repository_parts)
    )
    allowed_actions = SUPPORTED_ACTIONS.get(record.event_type)
    action_valid = (
        record.action is None
        if allowed_actions is None
        else record.action in allowed_actions
    )
    if (
        expected_fields is None
        or not isinstance(record.normalized_payload, dict)
        or set(record.normalized_payload) != set(expected_fields)
        or not _safe_normalized_value(record.normalized_payload)
        or not payload_valid
        or not action_valid
        or not delivery_id_valid
        or not repository_valid
    ):
        raise GithubWebhookIntegrityError
    return GithubWebhookDelivery(
        connector_id=record.connector_id,
        delivery_id=record.delivery_id,
        event_type=record.event_type,  # type: ignore[arg-type]
        action=record.action,
        repository=record.repository,
        installation_id=record.installation_id,
        connector_version=record.connector_version,
        credential_version=record.credential_version,
        body_sha256=record.body_sha256,
        normalized_payload=record.normalized_payload,
        received_at=record.received_at,
    )


def _safe_normalized_value(value: object, *, depth: int = 0) -> bool:
    if depth > 5:
        return False
    if value is None or isinstance(value, bool | int):
        return True
    if isinstance(value, str):
        return len(value) <= 500 and CONTROL_CHARACTER_PATTERN.search(value) is None
    if isinstance(value, list):
        return len(value) <= MAX_COMMITS and all(
            _safe_normalized_value(item, depth=depth + 1) for item in value
        )
    if isinstance(value, dict):
        forbidden_fields = {"body", "url", "patch", "secret", "token", "private_key"}
        return (
            len(value) <= 16
            and not (set(value) & forbidden_fields)
            and all(
                isinstance(key, str)
                and len(key) <= 64
                and _safe_normalized_value(item, depth=depth + 1)
                for key, item in value.items()
            )
        )
    return False


def _normalized_payload_is_valid(event_type: str, value: object) -> bool:
    if not isinstance(value, dict) or not _valid_service(value.get("service")):
        return False
    if event_type == "push":
        commits = value.get("commits")
        head_commit = value.get("head_commit")
        commit_count = value.get("commit_count")
        truncated = value.get("commits_truncated")
        return (
            _valid_text(value.get("ref"), 500)
            and _valid_sha(value.get("before"))
            and _valid_sha(value.get("after"))
            and all(
                isinstance(value.get(field), bool)
                for field in ("created", "deleted", "forced")
            )
            and _valid_non_negative_int(commit_count)
            and isinstance(truncated, bool)
            and isinstance(commits, list)
            and len(commits) <= MAX_COMMITS
            and all(_valid_normalized_commit(commit) for commit in commits)
            and (head_commit is None or _valid_normalized_commit(head_commit))
            and (
                (not truncated and commit_count == len(commits))
                or (truncated and commit_count > len(commits))
            )
        )
    if event_type == "pull_request":
        return (
            _valid_positive_int(value.get("number"))
            and _valid_text(value.get("title"), 300)
            and value.get("state") in {"open", "closed"}
            and isinstance(value.get("draft"), bool)
            and isinstance(value.get("merged"), bool)
            and _valid_sha(value.get("head_sha"))
            and _valid_sha(value.get("base_sha"))
            and _valid_timestamp(value.get("created_at"))
            and _valid_timestamp(value.get("updated_at"))
            and _valid_optional_timestamp(value.get("closed_at"))
            and _valid_optional_timestamp(value.get("merged_at"))
        )
    if event_type in {"deployment", "deployment_status"}:
        status_id = value.get("status_id")
        deployment_status = value.get("status")
        status_valid = (
            status_id is None and deployment_status is None
            if event_type == "deployment"
            else _valid_positive_int(status_id)
            and deployment_status
            in {"error", "failure", "inactive", "in_progress", "pending", "queued", "success"}
        )
        return (
            _valid_positive_int(value.get("deployment_id"))
            and _valid_sha(value.get("sha"))
            and _valid_text(value.get("ref"), 500)
            and _valid_text(value.get("environment"), 255)
            and status_valid
            and _valid_timestamp(value.get("created_at"))
            and _valid_timestamp(value.get("updated_at"))
        )
    if event_type == "release":
        return (
            _valid_positive_int(value.get("release_id"))
            and _valid_text(value.get("tag_name"), 255)
            and _valid_optional_text(value.get("name"), 255)
            and isinstance(value.get("draft"), bool)
            and isinstance(value.get("prerelease"), bool)
            and _valid_timestamp(value.get("created_at"))
            and _valid_optional_timestamp(value.get("published_at"))
        )
    return False


def _valid_normalized_commit(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "sha",
        "title",
        "timestamp",
        "author",
        "files",
        "file_stats",
    }:
        return False
    author = value.get("author")
    files = value.get("files")
    file_stats = value.get("file_stats")
    if (
        not isinstance(author, dict)
        or set(author) != {"name", "username"}
        or not _valid_optional_text(author.get("name"), 200)
        or not _valid_optional_text(author.get("username"), 100)
        or not isinstance(files, dict)
        or set(files) != {"added", "removed", "modified"}
        or not isinstance(file_stats, dict)
        or set(file_stats) != {"added", "removed", "modified"}
    ):
        return False
    for change_kind in ("added", "removed", "modified"):
        names = files.get(change_kind)
        if (
            not isinstance(names, list)
            or len(names) > MAX_FILENAMES_PER_CHANGE_KIND
            or not all(_valid_file_path(name) for name in names)
            or not _valid_non_negative_int(file_stats.get(change_kind))
            or file_stats.get(change_kind) < len(names)
        ):
            return False
    return (
        _valid_sha(value.get("sha"))
        and _valid_text(value.get("title"), 300)
        and _valid_optional_timestamp(value.get("timestamp"))
    )


def _valid_service(value: object) -> bool:
    return isinstance(value, str) and SERVICE_PATTERN.fullmatch(value) is not None


def _valid_text(value: object, maximum: int) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= maximum
        and CONTROL_CHARACTER_PATTERN.search(value) is None
    )


def _valid_optional_text(value: object, maximum: int) -> bool:
    return value is None or (
        isinstance(value, str)
        and len(value) <= maximum
        and CONTROL_CHARACTER_PATTERN.search(value) is None
    )


def _valid_sha(value: object) -> bool:
    return isinstance(value, str) and SHA_PATTERN.fullmatch(value) is not None


def _valid_positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _valid_non_negative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _valid_timestamp(value: object) -> bool:
    if not isinstance(value, str) or len(value) > 64:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _valid_optional_timestamp(value: object) -> bool:
    return value is None or _valid_timestamp(value)


def _valid_file_path(value: object) -> bool:
    return (
        _valid_text(value, 500)
        and isinstance(value, str)
        and not value.startswith("/")
        and "\\" not in value
        and all(segment not in {"", ".", ".."} for segment in value.split("/"))
    )
