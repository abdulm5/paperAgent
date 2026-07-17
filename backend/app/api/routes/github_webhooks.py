import re
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.auth.dependencies import require_permission
from app.connectors.runtime import (
    GithubConnectorCustodyUnavailableError,
    GithubConnectorUnavailableError,
    load_github_connector_runtime,
)
from app.db.session import get_db
from app.domain.auth import Permission, Principal
from app.domain.github_webhooks import (
    GithubEventType,
    GithubWebhookDelivery,
    GithubWebhookReceipt,
)
from app.services.github_webhooks import (
    SIGNATURE_PATTERN,
    SUPPORTED_ACTIONS,
    GithubDeliveryConnectorNotFoundError,
    GithubWebhookConnectorChangedError,
    GithubWebhookIntegrityError,
    GithubWebhookPayloadError,
    GithubWebhookReplayConflictError,
    GithubWebhookService,
    GithubWebhookSignatureError,
    list_github_deliveries,
)

MAX_GITHUB_WEBHOOK_BODY_BYTES = 1024 * 1024
DELIVERY_ID_PATTERN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
EVENT_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,31}\Z")

router = APIRouter(tags=["github-webhooks"])


@router.post(
    "/webhooks/github/{connector_id}",
    response_model=GithubWebhookReceipt,
    status_code=status.HTTP_202_ACCEPTED,
)
async def ingest_github_webhook(
    connector_id: UUID,
    request: Request,
    session: Session = Depends(get_db),
) -> GithubWebhookReceipt:
    delivery_id, event_type, signature = _validated_headers(request)
    _precheck_content_length(request)
    raw_body = await read_limited_request_body(request)
    try:
        runtime = load_github_connector_runtime(session, connector_id)
    except GithubConnectorCustodyUnavailableError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="GitHub webhook credential custody is temporarily unavailable",
            headers={"Retry-After": "5"},
        ) from error
    except GithubConnectorUnavailableError as error:
        raise _webhook_not_found() from error

    try:
        return GithubWebhookService(session, runtime).ingest(
            delivery_id=delivery_id,
            event_type=event_type,
            signature=signature,
            raw_body=raw_body,
        )
    except GithubWebhookSignatureError as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="GitHub webhook signature verification failed",
        ) from error
    except GithubWebhookConnectorChangedError as error:
        raise _webhook_not_found() from error
    except GithubWebhookPayloadError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="GitHub webhook payload rejected",
        ) from error
    except GithubWebhookReplayConflictError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="GitHub webhook delivery conflicts with an existing delivery",
        ) from error
    except GithubWebhookIntegrityError as error:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="GitHub delivery ledger integrity check failed",
        ) from error


@router.get(
    "/connectors/{connector_id}/github-deliveries",
    response_model=list[GithubWebhookDelivery],
)
def get_github_deliveries(
    connector_id: UUID,
    principal: Principal = Depends(require_permission(Permission.CONNECTORS_READ)),
    session: Session = Depends(get_db),
) -> list[GithubWebhookDelivery]:
    try:
        return list_github_deliveries(
            session,
            principal.organization_id,
            connector_id,
        )
    except GithubDeliveryConnectorNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Connector not found",
        ) from error
    except GithubWebhookIntegrityError as error:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="GitHub delivery ledger integrity check failed",
        ) from error


async def read_limited_request_body(
    request: Request,
    *,
    max_bytes: int = MAX_GITHUB_WEBHOOK_BODY_BYTES,
) -> bytes:
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="GitHub webhook body exceeds the allowed size",
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _validated_headers(request: Request) -> tuple[str, GithubEventType, str]:
    delivery_values = request.headers.getlist("X-GitHub-Delivery")
    event_values = request.headers.getlist("X-GitHub-Event")
    signature_values = request.headers.getlist("X-Hub-Signature-256")
    delivery_id = delivery_values[0] if len(delivery_values) == 1 else ""
    event_type = event_values[0] if len(event_values) == 1 else ""
    signature = signature_values[0] if len(signature_values) == 1 else ""
    if (
        not DELIVERY_ID_PATTERN.fullmatch(delivery_id)
        or not EVENT_PATTERN.fullmatch(event_type)
        or event_type not in SUPPORTED_ACTIONS
        or not SIGNATURE_PATTERN.fullmatch(signature)
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Malformed or unsupported GitHub webhook headers",
        )
    return delivery_id, event_type, signature  # type: ignore[return-value]


def _precheck_content_length(request: Request) -> None:
    content_lengths = request.headers.getlist("Content-Length")
    if not content_lengths:
        return
    if len(content_lengths) != 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Malformed Content-Length header",
        )
    content_length = content_lengths[0]
    if not content_length.isascii() or not content_length.isdecimal():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Malformed Content-Length header",
        )
    if int(content_length) > MAX_GITHUB_WEBHOOK_BODY_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="GitHub webhook body exceeds the allowed size",
        )


def _webhook_not_found() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="GitHub webhook endpoint not found",
    )
