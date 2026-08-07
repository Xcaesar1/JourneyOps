"""Durable v2 trip submission, status, cancellation, retry, and event endpoints."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Any

from fastapi import (
    APIRouter,
    Body,
    Depends,
    Header,
    HTTPException,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from redis.asyncio import Redis as AsyncRedis
from sqlalchemy.orm import Session

from ...db.models import TripTask
from ...db.repository import (
    IdempotencyConflictError,
    attach_celery_task,
    create_or_get_task,
    get_task,
    prepare_retry,
    request_cancellation,
    update_task_state,
)
from ...db.session import SessionLocal, get_db_session
from ...domain.error_models import (
    V2_CONFLICT_ERROR_EXAMPLE,
    V2_INTERNAL_ERROR_EXAMPLE,
    V2_NOT_FOUND_ERROR_EXAMPLE,
    V2_VALIDATION_ERROR_EXAMPLE,
    ErrorEnvelopeV2,
)
from ...domain.task_models import TRIP_TASK_RECORD_V2_EXAMPLE, TripTaskRecordV2
from ...domain.trip_models import TRIP_REQUEST_V2_EXAMPLE, TripRequestV2
from ...services.task_events import (
    FINAL_TASK_STATUSES,
    publish_task_event,
    redis_url,
    task_channel,
    task_snapshot,
)
from ...workers.trip_tasks import enqueue_trip_task

router = APIRouter(prefix="/trips", tags=["API v2"])
DbSession = Annotated[Session, Depends(get_db_session)]


@router.post(
    "",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=TripTaskRecordV2,
    summary="Submit a durable v2 trip request",
    description=(
        "Persist the canonical request and task in PostgreSQL before dispatching the configured "
        "planner through Celery. Repeated requests are deduplicated by Idempotency-Key or payload."
    ),
    responses={
        202: {
            "description": "Durable task accepted.",
            "content": {"application/json": {"example": TRIP_TASK_RECORD_V2_EXAMPLE}},
        },
        404: {
            "model": ErrorEnvelopeV2,
            "content": {"application/json": {"example": V2_NOT_FOUND_ERROR_EXAMPLE}},
        },
        409: {
            "model": ErrorEnvelopeV2,
            "content": {"application/json": {"example": V2_CONFLICT_ERROR_EXAMPLE}},
        },
        422: {
            "model": ErrorEnvelopeV2,
            "content": {"application/json": {"example": V2_VALIDATION_ERROR_EXAMPLE}},
        },
        500: {
            "model": ErrorEnvelopeV2,
            "content": {"application/json": {"example": V2_INTERNAL_ERROR_EXAMPLE}},
        },
    },
)
def create_trip(
    session: DbSession,
    request: TripRequestV2 = Body(
        ...,
        openapi_examples={
            "durable_submission": {
                "summary": "Valid durable trip submission",
                "value": TRIP_REQUEST_V2_EXAMPLE,
            }
        },
    ),
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> TripTaskRecordV2:
    """Persist before dispatch so an API restart cannot lose accepted work."""
    payload = request.model_dump(mode="json")
    try:
        task, created = create_or_get_task(
            session,
            request_payload=payload,
            idempotency_key=_digest_idempotency_key(payload, idempotency_key),
        )
    except IdempotencyConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if created:
        task = _dispatch_or_fail(session, task)
    publish_task_event(task)
    return _response(task)


@router.get(
    "/tasks/{task_id}",
    response_model=TripTaskRecordV2,
    summary="Read durable task status",
)
def read_task(task_id: str, session: DbSession) -> TripTaskRecordV2:
    """Read current state from PostgreSQL, never from process memory."""
    return _response(_required_task(session, task_id))


@router.post(
    "/tasks/{task_id}/cancel",
    response_model=TripTaskRecordV2,
    summary="Cancel a queued or running task",
)
def cancel_task(task_id: str, session: DbSession) -> TripTaskRecordV2:
    """Cancel queued work immediately or request cooperative running cancellation."""
    _required_task(session, task_id)
    task = request_cancellation(session, task_id)
    publish_task_event(task)
    return _response(task)


@router.post(
    "/tasks/{task_id}/retry",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=TripTaskRecordV2,
    summary="Retry a failed or cancelled task",
)
def retry_task(task_id: str, session: DbSession) -> TripTaskRecordV2:
    """Reset the persisted attempt policy and dispatch a new Celery message."""
    _required_task(session, task_id)
    try:
        task = prepare_retry(session, task_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    task = _dispatch_or_fail(session, task)
    publish_task_event(task)
    return _response(task)


@router.websocket("/tasks/{task_id}/ws")
async def task_events(websocket: WebSocket, task_id: str) -> None:
    """Stream Redis Pub/Sub events while periodically reconciling from PostgreSQL."""
    await websocket.accept()
    redis_client = AsyncRedis.from_url(redis_url(), decode_responses=True)
    pubsub = redis_client.pubsub()
    try:
        # Subscribe before reading the snapshot so no transition can fall into a read/subscribe gap.
        await pubsub.subscribe(task_channel(task_id))
        with SessionLocal() as session:
            task = get_task(session, task_id)
            if task is None:
                await websocket.send_json({"error": {"code": "not_found", "message": "Task not found."}})
                await websocket.close(code=4404)
                return
            initial = task_snapshot(task)
        await websocket.send_json(initial)
        if initial["status"] in FINAL_TASK_STATUSES:
            await websocket.close()
            return

        while True:
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if message is not None:
                event = json.loads(message["data"])
                await websocket.send_json(event)
                if event["status"] in FINAL_TASK_STATUSES:
                    break
            else:
                with SessionLocal() as session:
                    current = get_task(session, task_id)
                    if current is None:
                        break
                    snapshot = task_snapshot(current)
                if snapshot["status"] in FINAL_TASK_STATUSES:
                    await websocket.send_json(snapshot)
                    break
    except WebSocketDisconnect:
        return
    finally:
        await pubsub.aclose()
        await redis_client.aclose()


def _dispatch_or_fail(session: Session, task: TripTask) -> TripTask:
    try:
        broker_task_id = enqueue_trip_task(task.id)
    except Exception as exc:
        failed = update_task_state(
            session,
            task.id,
            status="failed",
            stage="dispatch_failed",
            message="Task could not be dispatched.",
            error_code="dispatch_failed",
            error_message=type(exc).__name__,
            finished=True,
        )
        publish_task_event(failed)
        raise HTTPException(status_code=503, detail="Task broker is unavailable.") from exc

    try:
        return attach_celery_task(session, task.id, broker_task_id)
    except Exception as exc:
        session.rollback()
        raise HTTPException(
            status_code=503,
            detail="Task dispatch was sent but could not be confirmed; recovery is pending.",
        ) from exc


def _required_task(session: Session, task_id: str) -> TripTask:
    task = get_task(session, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found.")
    return task


def _response(task: TripTask) -> TripTaskRecordV2:
    return TripTaskRecordV2.model_validate(task_snapshot(task))


def _digest_idempotency_key(payload: dict[str, Any], supplied_key: str | None) -> str:
    if supplied_key is not None and len(supplied_key) > 256:
        raise HTTPException(status_code=400, detail="Idempotency-Key is too long.")
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    namespace = f"header:{supplied_key}" if supplied_key else f"payload:{canonical}"
    return hashlib.sha256(namespace.encode("utf-8")).hexdigest()
