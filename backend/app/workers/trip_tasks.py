"""Durable Celery execution for the unchanged legacy trip planner."""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from typing import Any

from celery import signals
from celery.exceptions import Retry
from redis import Redis

from ..db.repository import (
    FINAL_TASK_STATUSES,
    attach_celery_task,
    get_task,
    get_trip,
    get_trip_version,
    save_trip_version,
    stale_recoverable_tasks,
    update_task_state,
)
from ..db.session import SessionLocal
from ..models.schemas import CityStay, TripPlanResponse, TripRequest
from ..services.task_events import publish_task_event, redis_url, task_snapshot
from .celery_app import celery_app

LOGGER = logging.getLogger(__name__)
TASK_NAME = "journeyops.plan_trip"


class TaskCancelled(Exception):
    """Raised when a persisted cooperative cancellation is observed."""


def enqueue_trip_task(task_id: str) -> str:
    """Dispatch one durable task and return the broker task identifier."""
    result = celery_app.send_task(TASK_NAME, args=[task_id])
    return result.id


def recover_incomplete_tasks(stale_after_seconds: int | None = None) -> dict[str, int]:
    """Recover undispatched and stale work when a worker becomes ready."""
    stale_after = (
        stale_after_seconds
        if stale_after_seconds is not None
        else int(os.getenv("TRIP_TASK_STALE_AFTER", "0"))
    )
    summary = {"dispatched": 0, "failed": 0, "cancelled": 0}
    with SessionLocal() as session:
        candidates = stale_recoverable_tasks(session, stale_after)

    for candidate in candidates:
        with SessionLocal() as session:
            task = get_task(session, candidate.id, for_update=True)
            if task is None or task.status in FINAL_TASK_STATUSES:
                continue
            if task.status == "cancel_requested" or task.cancel_requested:
                task = update_task_state(
                    session,
                    task.id,
                    status="cancelled",
                    stage="cancelled",
                    message="Task cancellation recovered after worker interruption.",
                    finished=True,
                )
                publish_task_event(task)
                summary["cancelled"] += 1
                continue
            if task.status == "processing" and task.attempt_count >= task.max_attempts:
                task.status = "failed"
                task.stage = "failed"
                task.message = "Worker recovery policy exhausted."
                task.error_code = "worker_lost"
                task.error_message = "Task exceeded its maximum recovery attempts."
                session.commit()
                session.refresh(task)
                publish_task_event(task)
                summary["failed"] += 1
                continue
            task.status = "retrying" if task.status == "processing" else "queued"
            task.stage = "worker_recovery" if task.status == "retrying" else "queued"
            task.message = "Task recovered after worker interruption."
            task.celery_task_id = None
            session.commit()
            session.refresh(task)
            task_id = task.id

        broker_task_id = enqueue_trip_task(task_id)
        with SessionLocal() as session:
            task = attach_celery_task(session, task_id, broker_task_id)
            publish_task_event(task)
        summary["dispatched"] += 1

    return summary


@signals.worker_ready.connect
def _recover_when_worker_is_ready(**_: Any) -> None:
    try:
        summary = recover_incomplete_tasks()
        LOGGER.info("Worker recovery complete: %s", summary)
    except Exception as exc:
        LOGGER.error("Worker recovery failed: %s", type(exc).__name__, exc_info=True)


@celery_app.task(bind=True, name=TASK_NAME)
def run_trip_planning(self: Any, task_id: str) -> dict[str, Any]:
    """Execute one persisted task with late acknowledgement and idempotent completion."""
    redis_client = Redis.from_url(redis_url(), decode_responses=True)
    lock_timeout = int(os.getenv("TRIP_TASK_LOCK_TIMEOUT", "90"))
    lock = redis_client.lock(
        f"journeyops:task-lock:{task_id}", timeout=lock_timeout, blocking_timeout=1
    )
    acquired = lock.acquire(blocking=True)
    if not acquired:
        redis_client.close()
        raise self.retry(countdown=5, max_retries=24)

    renewal_stop = threading.Event()
    renewal_thread = threading.Thread(
        target=_renew_lock,
        args=(lock, lock_timeout, renewal_stop, task_id),
        daemon=True,
    )
    renewal_thread.start()
    try:
        return _execute_task(self, task_id, lock, lock_timeout)
    finally:
        renewal_stop.set()
        renewal_thread.join(timeout=2)
        try:
            lock.release()
        except Exception:
            pass
        redis_client.close()


def _execute_task(self: Any, task_id: str, lock: Any, lock_timeout: int) -> dict[str, Any]:
    with SessionLocal() as session:
        task = get_task(session, task_id, for_update=True)
        if task is None:
            return {"task_id": task_id, "status": "missing"}
        if task.status in FINAL_TASK_STATUSES:
            return task_snapshot(task)
        if task.cancel_requested:
            task = update_task_state(
                session,
                task_id,
                status="cancelled",
                stage="cancelled",
                message="Task cancelled.",
                finished=True,
            )
            publish_task_event(task)
            return task_snapshot(task)
        existing_version = get_trip_version(session, task.trip_id, 1)
        if existing_version is not None:
            task = update_task_state(
                session,
                task_id,
                status="completed",
                stage="completed",
                progress=100,
                message="Existing trip version recovered after redelivery.",
                result_payload=existing_version.payload,
                finished=True,
            )
            publish_task_event(task)
            return task_snapshot(task)
        if task.attempt_count >= task.max_attempts:
            task = update_task_state(
                session,
                task_id,
                status="failed",
                stage="failed",
                message="Task attempt policy exhausted.",
                error_code="attempts_exhausted",
                error_message="Task reached its maximum execution attempts.",
                finished=True,
            )
            publish_task_event(task)
            return task_snapshot(task)

        trip = get_trip(session, task.trip_id)
        if trip is None:
            raise RuntimeError(f"Task {task_id} references a missing trip.")
        request_payload = trip.request_payload
        task = update_task_state(
            session,
            task_id,
            status="processing",
            stage="initializing",
            progress=5,
            message="Planner worker started.",
            increment_attempt=True,
        )
        publish_task_event(task)
        attempt_count = task.attempt_count
        max_attempts = task.max_attempts
        trip_id = task.trip_id

    async def progress_callback(stage: str, message: str, progress: int) -> None:
        with SessionLocal() as progress_session:
            current = get_task(progress_session, task_id)
            if current is None or current.cancel_requested:
                raise TaskCancelled(task_id)
            updated = update_task_state(
                progress_session,
                task_id,
                status="processing",
                stage=stage,
                progress=progress,
                message=message,
            )
            publish_task_event(updated)
        try:
            lock.extend(lock_timeout, replace_ttl=True)
        except Exception:
            LOGGER.warning("Unable to extend task lock for %s", task_id)

    try:
        result_payload = asyncio.run(
            _run_legacy_planner(task_id, request_payload, progress_callback)
        )
        with SessionLocal() as session:
            current = get_task(session, task_id)
            if current is None or current.cancel_requested:
                raise TaskCancelled(task_id)
            save_trip_version(
                session,
                trip_id=trip_id,
                version=1,
                payload=result_payload,
            )
            completed = update_task_state(
                session,
                task_id,
                status="completed",
                stage="completed",
                progress=100,
                message="Trip plan generated successfully.",
                result_payload=result_payload,
                finished=True,
            )
            publish_task_event(completed)
            return task_snapshot(completed)
    except TaskCancelled:
        with SessionLocal() as session:
            cancelled = update_task_state(
                session,
                task_id,
                status="cancelled",
                stage="cancelled",
                message="Task cancelled.",
                finished=True,
            )
            publish_task_event(cancelled)
            return task_snapshot(cancelled)
    except Retry:
        raise
    except Exception as exc:
        retryable = not isinstance(exc, (ValueError, TypeError))
        if retryable and attempt_count < max_attempts:
            with SessionLocal() as session:
                retrying = update_task_state(
                    session,
                    task_id,
                    status="retrying",
                    stage="retrying",
                    message=f"Planner attempt {attempt_count} failed; retry scheduled.",
                    error_code="planner_retry",
                    error_message=_safe_error_message(exc),
                )
                publish_task_event(retrying)
            raise self.retry(
                exc=exc,
                countdown=min(30, 2**attempt_count),
                max_retries=max_attempts - 1,
            )

        with SessionLocal() as session:
            failed = update_task_state(
                session,
                task_id,
                status="failed",
                stage="failed",
                message="Trip planner failed.",
                error_code="planner_failed",
                error_message=_safe_error_message(exc),
                finished=True,
            )
            publish_task_event(failed)
            return task_snapshot(failed)


async def _run_legacy_planner(
    task_id: str,
    payload: dict[str, Any],
    progress_callback: Any,
) -> dict[str, Any]:
    """Adapt the v2 contract and invoke the existing planner without modifying it."""
    from ..agents.trip_planner_agent import get_trip_planner_agent
    from ..services.knowledge_graph_service import build_knowledge_graph

    request = (
        TripRequest.model_validate(payload["request"])
        if payload.get("contract") == "legacy"
        else _to_legacy_request(payload)
    )
    agent = get_trip_planner_agent()
    trip_plan = await agent.plan_trip(request, progress_callback=progress_callback)
    await progress_callback("graph_building", "Building knowledge graph.", 95)
    graph_data = build_knowledge_graph(trip_plan, language=request.language or "zh")
    result = TripPlanResponse(
        success=True,
        message="Trip plan generated successfully.",
        plan_id=task_id,
        data=trip_plan,
        graph_data=graph_data,
    )
    return result.model_dump(mode="json")


def _to_legacy_request(payload: dict[str, Any]) -> TripRequest:
    destinations = payload["destinations"]
    context = list(payload.get("avoid", [])) + list(payload.get("accessibility_needs", []))
    if payload.get("budget_total"):
        context.append(f"Budget: {payload['budget_total']} {payload.get('currency', 'CNY')}")
    free_text = payload.get("free_text_input", "")
    if context:
        free_text = "\n".join(filter(None, [free_text, "Constraints: " + "; ".join(context)]))
    return TripRequest(
        city=destinations[0]["city"],
        cities=[CityStay(city=item["city"], days=item["days"]) for item in destinations],
        start_date=payload["start_date"],
        end_date=payload["end_date"],
        travel_days=payload["travel_days"],
        transportation=", ".join(payload.get("transport_preferences", [])) or "public transit",
        accommodation=payload.get("accommodation_preference") or "midscale hotel",
        preferences=list(payload.get("interests", [])) + list(payload.get("must_visit", [])),
        free_text_input=free_text,
        language=payload.get("language", "zh"),
    )


def _safe_error_message(exc: Exception) -> str:
    text = str(exc).strip()
    return (text or type(exc).__name__)[:1000]


def _renew_lock(lock: Any, lock_timeout: int, stop: threading.Event, task_id: str) -> None:
    """Keep a live worker lock fresh while allowing fast expiry after process death."""
    interval = max(1.0, lock_timeout / 3)
    while not stop.wait(interval):
        try:
            lock.extend(lock_timeout, replace_ttl=True)
        except Exception:
            LOGGER.warning("Unable to renew task lock for %s", task_id)
