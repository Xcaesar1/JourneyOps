"""Durable Celery execution for feature-flagged JourneyOps planners."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
from dataclasses import dataclass
from typing import Any, Literal
from uuid import uuid4

from celery import signals
from celery.exceptions import Retry
from redis import Redis

from ..config import get_settings
from ..db.repository import (
    FINAL_TASK_STATUSES,
    attach_celery_task,
    get_task,
    get_trip,
    get_trip_version,
    save_trip_version,
    stale_recoverable_tasks,
    task_needs_recovery,
    update_task_state,
)
from ..db.session import SessionLocal
from ..domain.research_models import SourceEvidence
from ..domain.trip_models import TripPlanV2, TripRequestV2
from ..models.schemas import CityStay, TripPlanResponse, TripRequest
from ..services.task_events import publish_task_event, redis_url, task_snapshot
from .celery_app import celery_app

LOGGER = logging.getLogger(__name__)
TASK_NAME = "journeyops.plan_trip"
_RECOVERY_STOP = threading.Event()
_RECOVERY_THREAD: threading.Thread | None = None
_RECOVERY_THREAD_GUARD = threading.Lock()
_REDACTED = "[REDACTED]"
_AUTH_VALUE_PATTERN = re.compile(r"(?i)\b(Bearer|Basic)\s+[^\s,;]+")
_SECRET_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)(?<![\w-])([\"']?(?:api[_-]?key|authorization|auth[_-]?token|"
    r"access[_-]?token|secret|password|passwd|cookie)[\"']?)(\s*[:=]\s*)"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_URL_CREDENTIAL_PATTERN = re.compile(r"(?i)(https?://)[^\s/@:]+:[^\s/@]+@")
_PROVIDER_TOKEN_PATTERN = re.compile(r"(?i)\bsk-[a-z0-9_-]{8,}\b")


class TaskCancelled(Exception):
    """Raised when a persisted cooperative cancellation is observed."""


PlannerEngine = Literal["legacy", "journey_graph"]


@dataclass(frozen=True)
class PlannerExecution:
    """One planner output plus its persistence metadata."""

    engine: PlannerEngine
    client_payload: dict[str, Any]
    schema_version: str
    native_payload: dict[str, Any] | None = None
    source_evidence: tuple[SourceEvidence, ...] = ()


@dataclass(frozen=True)
class PlannerRunSet:
    """Primary output and optional shadow-comparison output."""

    primary: PlannerExecution
    comparison: PlannerExecution | None = None


def enqueue_trip_task(task_id: str) -> str:
    """Dispatch one durable task and return the broker task identifier."""
    result = celery_app.send_task(TASK_NAME, args=[task_id])
    return result.id


def recover_incomplete_tasks(stale_after_seconds: int | None = None) -> dict[str, int]:
    """Recover undispatched and stale work when a worker becomes ready."""
    stale_after = (
        stale_after_seconds
        if stale_after_seconds is not None
        else int(os.getenv("TRIP_TASK_STALE_AFTER", "120"))
    )
    summary = {"dispatched": 0, "failed": 0, "cancelled": 0}
    with SessionLocal() as session:
        candidates = stale_recoverable_tasks(session, stale_after)

    for candidate in candidates:
        with SessionLocal() as session:
            task = get_task(session, candidate.id, for_update=True)
            if task is None or not task_needs_recovery(task, stale_after):
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
            if task.status == "processing" and _execution_lock_active(task.id):
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
            recovery_claim = f"recovery-{uuid4().hex}"
            task.celery_task_id = recovery_claim
            session.commit()
            session.refresh(task)
            task_id = task.id

        broker_task_id = enqueue_trip_task(task_id)
        with SessionLocal() as session:
            task = attach_celery_task(
                session,
                task_id,
                broker_task_id,
                expected_celery_task_id=recovery_claim,
            )
            publish_task_event(task)
        summary["dispatched"] += 1

    return summary


@signals.worker_ready.connect
def _recover_when_worker_is_ready(**_: Any) -> None:
    _run_recovery_safely("startup")
    _start_recovery_thread()


@signals.worker_shutdown.connect
def _stop_recovery_when_worker_shuts_down(**_: Any) -> None:
    _RECOVERY_STOP.set()
    thread = _RECOVERY_THREAD
    if thread is not None:
        thread.join(timeout=2)


def _run_recovery_safely(trigger: str) -> None:
    try:
        summary = recover_incomplete_tasks()
        LOGGER.info("Worker recovery complete (%s): %s", trigger, summary)
    except Exception as exc:
        LOGGER.error("Worker recovery failed (%s): %s", trigger, type(exc).__name__, exc_info=True)


def _start_recovery_thread() -> None:
    global _RECOVERY_THREAD
    with _RECOVERY_THREAD_GUARD:
        if _RECOVERY_THREAD is not None and _RECOVERY_THREAD.is_alive():
            return
        _RECOVERY_STOP.clear()
        _RECOVERY_THREAD = threading.Thread(
            target=_recovery_loop,
            name="journeyops-task-recovery",
            daemon=True,
        )
        _RECOVERY_THREAD.start()


def _recovery_loop() -> None:
    interval = max(5, int(os.getenv("TRIP_TASK_RECOVERY_INTERVAL", "60")))
    while not _RECOVERY_STOP.wait(interval):
        _run_recovery_safely("periodic")


@celery_app.task(bind=True, name=TASK_NAME)
def run_trip_planning(self: Any, task_id: str) -> dict[str, Any]:
    """Execute one persisted task with late acknowledgement and idempotent completion."""
    redis_client = Redis.from_url(redis_url(), decode_responses=True)
    lock_timeout = int(os.getenv("TRIP_TASK_LOCK_TIMEOUT", "90"))
    lock = redis_client.lock(
        f"journeyops:task-lock:{task_id}",
        timeout=lock_timeout,
        blocking_timeout=1,
        thread_local=False,
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
        planner_runs = asyncio.run(
            _run_configured_planners(
                task_id,
                trip_id,
                request_payload,
                progress_callback,
            )
        )
        result_payload = planner_runs.primary.client_payload
        with SessionLocal() as session:
            current = get_task(session, task_id)
            if current is None or current.cancel_requested:
                raise TaskCancelled(task_id)
            if planner_runs.comparison is not None:
                comparison = planner_runs.comparison
                save_trip_version(
                    session,
                    trip_id=trip_id,
                    version=2,
                    payload=comparison.client_payload,
                    planner_engine=comparison.engine,
                    version_role="comparison",
                    schema_version=comparison.schema_version,
                    native_payload=comparison.native_payload,
                    source_evidence=comparison.source_evidence,
                )
            primary = planner_runs.primary
            save_trip_version(
                session,
                trip_id=trip_id,
                version=1,
                payload=result_payload,
                planner_engine=primary.engine,
                version_role="primary",
                schema_version=primary.schema_version,
                native_payload=primary.native_payload,
                source_evidence=primary.source_evidence,
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


async def _run_configured_planners(
    task_id: str,
    trip_id: str,
    payload: dict[str, Any],
    progress_callback: Any,
) -> PlannerRunSet:
    settings = get_settings()
    primary_engine: PlannerEngine = settings.planner_engine
    primary_call = _run_engine(
        primary_engine,
        task_id,
        trip_id,
        payload,
        progress_callback,
    )
    if not settings.planner_compare_engines:
        return PlannerRunSet(primary=await primary_call)

    comparison_engine: PlannerEngine = (
        "journey_graph" if primary_engine == "legacy" else "legacy"
    )

    async def comparison_progress(*_args: Any, **_kwargs: Any) -> None:
        return None

    primary_task = asyncio.create_task(primary_call)
    comparison_task = asyncio.create_task(
        _run_engine(
            comparison_engine,
            task_id,
            trip_id,
            payload,
            comparison_progress,
        )
    )
    try:
        primary_result = await primary_task
    except BaseException:
        comparison_task.cancel()
        await asyncio.gather(comparison_task, return_exceptions=True)
        raise

    try:
        comparison_result = await comparison_task
    except asyncio.CancelledError:
        raise
    except Exception as comparison_error:
        comparison = PlannerExecution(
            engine=comparison_engine,
            client_payload={
                "success": False,
                "message": "Comparison planner failed.",
                "plan_id": task_id,
                "error_code": type(comparison_error).__name__,
            },
            schema_version="error",
        )
    else:
        comparison = comparison_result
    return PlannerRunSet(primary=primary_result, comparison=comparison)


async def _run_engine(
    engine: PlannerEngine,
    task_id: str,
    trip_id: str,
    payload: dict[str, Any],
    progress_callback: Any,
) -> PlannerExecution:
    if engine == "legacy":
        result = await _run_legacy_planner(task_id, payload, progress_callback)
        return PlannerExecution(
            engine="legacy",
            client_payload=result,
            schema_version="legacy",
        )
    return await _run_journey_graph_planner(
        task_id,
        trip_id,
        payload,
        progress_callback,
    )


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


async def _run_journey_graph_planner(
    task_id: str,
    trip_id: str,
    payload: dict[str, Any],
    progress_callback: Any,
) -> PlannerExecution:
    """Run or resume the typed graph and adapt its result for existing clients."""
    from ..adapters import trip_plan_v2_to_legacy
    from ..agents.journey_graph import build_journey_graph, build_structured_plan_generator
    from ..agents.journey_graph.checkpoint import open_postgres_checkpointer
    from ..services.knowledge_graph_service import build_knowledge_graph
    from ..services.research import build_configured_web_research_provider
    from ..services.routing import build_configured_route_estimate_provider

    request = _to_v2_request(payload)
    await progress_callback("planning", "JourneyGraph structured planning started.", 50)

    def invoke_graph() -> TripPlanV2:
        config = {"configurable": {"thread_id": task_id}}
        initial_state = {
            "trip_id": trip_id,
            "task_id": task_id,
            "request": request,
        }
        with open_postgres_checkpointer() as checkpointer:
            graph = build_journey_graph(
                draft_generator=build_structured_plan_generator(),
                research_provider=build_configured_web_research_provider(),
                route_provider=build_configured_route_estimate_provider(),
                checkpointer=checkpointer,
            )
            snapshot = graph.get_state(config)
            if snapshot.values.get("final_plan") is not None and not snapshot.next:
                state = snapshot.values
            else:
                graph_input = None if snapshot.next else initial_state
                state = graph.invoke(graph_input, config)
        return TripPlanV2.model_validate(state["final_plan"])

    native_plan = await asyncio.to_thread(invoke_graph)
    adapted_plan = trip_plan_v2_to_legacy(native_plan)
    await progress_callback("graph_building", "Building knowledge graph.", 95)
    graph_data = build_knowledge_graph(adapted_plan, language=request.language)
    client_response = TripPlanResponse(
        success=True,
        message="Trip plan generated successfully.",
        plan_id=task_id,
        data=adapted_plan,
        graph_data=graph_data,
    ).model_dump(mode="json")
    return PlannerExecution(
        engine="journey_graph",
        client_payload=client_response,
        schema_version=native_plan.schema_version,
        native_payload=native_plan.model_dump(mode="json"),
        source_evidence=tuple(native_plan.source_evidence),
    )


def _to_v2_request(payload: dict[str, Any]) -> TripRequestV2:
    if payload.get("contract") != "legacy":
        return TripRequestV2.model_validate(payload)

    legacy = TripRequest.model_validate(payload["request"])
    destinations = [
        {"city": destination.city, "days": destination.days}
        for destination in legacy.cities
    ]
    return TripRequestV2(
        origin=legacy.origin or legacy.city,
        destinations=destinations,
        start_date=legacy.start_date,
        end_date=legacy.end_date,
        travel_days=legacy.travel_days,
        transport_preferences=[legacy.transportation] if legacy.transportation else [],
        accommodation_preference=legacy.accommodation,
        interests=legacy.preferences,
        free_text_input=legacy.free_text_input or "",
        language=legacy.language or "zh",
        timezone="Asia/Shanghai",
    )


def _to_legacy_request(payload: dict[str, Any]) -> TripRequest:
    destinations = payload["destinations"]
    context = list(payload.get("avoid", [])) + list(payload.get("accessibility_needs", []))
    if payload.get("budget_total"):
        context.append(f"Budget: {payload['budget_total']} {payload.get('currency', 'CNY')}")
    free_text = payload.get("free_text_input", "")
    if context:
        free_text = "\n".join(filter(None, [free_text, "Constraints: " + "; ".join(context)]))
    return TripRequest(
        origin=payload["origin"],
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
    text = str(exc).strip() or type(exc).__name__
    settings = get_settings()
    known_secrets = {
        settings.openai_api_key,
        settings.google_maps_api_key,
        settings.xhs_cookie,
        os.getenv("LLM_API_KEY", ""),
        os.getenv("OPENAI_API_KEY", ""),
    }
    for secret in sorted(known_secrets, key=len, reverse=True):
        if len(secret) >= 8:
            text = text.replace(secret, _REDACTED)

    text = _URL_CREDENTIAL_PATTERN.sub(rf"\1{_REDACTED}@", text)
    text = _AUTH_VALUE_PATTERN.sub(rf"\1 {_REDACTED}", text)
    text = _SECRET_ASSIGNMENT_PATTERN.sub(rf"\1\2{_REDACTED}", text)
    text = _PROVIDER_TOKEN_PATTERN.sub(_REDACTED, text)
    return text[:1000]


def _renew_lock(lock: Any, lock_timeout: int, stop: threading.Event, task_id: str) -> None:
    """Keep a live worker lock fresh while allowing fast expiry after process death."""
    interval = max(1.0, lock_timeout / 3)
    while not stop.wait(interval):
        try:
            lock.extend(lock_timeout, replace_ttl=True)
        except Exception:
            LOGGER.warning("Unable to renew task lock for %s", task_id)


def _execution_lock_active(task_id: str) -> bool:
    """Return whether another worker still owns the renewable execution lock."""
    client = Redis.from_url(
        redis_url(),
        decode_responses=True,
        socket_connect_timeout=1,
        socket_timeout=1,
    )
    try:
        return bool(client.exists(f"journeyops:task-lock:{task_id}"))
    finally:
        client.close()
