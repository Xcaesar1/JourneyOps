"""Feature-flag and persistence checks for legacy/JourneyGraph execution."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from backend.app.config import settings
from backend.app.db.models import TripVersion
from backend.app.db.repository import create_or_get_task, get_task
from backend.app.domain.trip_models import TRIP_REQUEST_V2_EXAMPLE, TripRequestV2
from backend.app.workers import trip_tasks
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker


def _execution(engine: trip_tasks.PlannerEngine, marker: str) -> trip_tasks.PlannerExecution:
    native_payload = {"schema_version": "2.0", "marker": marker} if engine == "journey_graph" else None
    return trip_tasks.PlannerExecution(
        engine=engine,
        client_payload={"success": True, "plan_id": marker, "data": {"city": marker}},
        schema_version="2.0" if engine == "journey_graph" else "legacy",
        native_payload=native_payload,
    )


def _create_task(factory: sessionmaker[Session], key: str) -> str:
    with factory() as session:
        task, _ = create_or_get_task(
            session,
            request_payload=TRIP_REQUEST_V2_EXAMPLE,
            idempotency_key=key,
        )
        return task.id


def test_planner_engine_selects_only_journey_graph(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "planner_engine", "journey_graph")
    monkeypatch.setattr(settings, "planner_compare_engines", False)
    calls: list[str] = []

    async def fake_run_engine(engine, *_args, **_kwargs):
        calls.append(engine)
        return _execution(engine, engine)

    monkeypatch.setattr(trip_tasks, "_run_engine", fake_run_engine)
    result = asyncio.run(
        trip_tasks._run_configured_planners("task", "trip", {}, lambda *_args: None)
    )

    assert result.primary.engine == "journey_graph"
    assert result.comparison is None
    assert calls == ["journey_graph"]


def test_comparison_flag_runs_both_engines_for_the_same_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "planner_engine", "legacy")
    monkeypatch.setattr(settings, "planner_compare_engines", True)
    calls: list[tuple[str, str, str, dict[str, Any]]] = []
    payload = {"request": "same-payload"}

    async def fake_run_engine(engine, task_id, trip_id, engine_payload, _callback):
        calls.append((engine, task_id, trip_id, engine_payload))
        return _execution(engine, engine)

    monkeypatch.setattr(trip_tasks, "_run_engine", fake_run_engine)
    result = asyncio.run(
        trip_tasks._run_configured_planners(
            "task_same",
            "trip_same",
            payload,
            lambda *_args: None,
        )
    )

    assert result.primary.engine == "legacy"
    assert result.comparison is not None
    assert result.comparison.engine == "journey_graph"
    assert {call[0] for call in calls} == {"legacy", "journey_graph"}
    assert all(call[1:] == ("task_same", "trip_same", payload) for call in calls)


def test_comparison_failure_is_redacted_and_does_not_fail_primary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "planner_engine", "legacy")
    monkeypatch.setattr(settings, "planner_compare_engines", True)
    sensitive_error = "provider failed with sk-sensitive-comparison-value"

    async def fake_run_engine(engine, *_args, **_kwargs):
        if engine == "journey_graph":
            raise RuntimeError(sensitive_error)
        return _execution(engine, engine)

    monkeypatch.setattr(trip_tasks, "_run_engine", fake_run_engine)
    result = asyncio.run(
        trip_tasks._run_configured_planners("task", "trip", {}, lambda *_args: None)
    )

    assert result.primary.engine == "legacy"
    assert result.comparison is not None
    assert result.comparison.client_payload["success"] is False
    assert result.comparison.client_payload["error_code"] == "RuntimeError"
    assert sensitive_error not in str(result.comparison.client_payload)


def test_primary_failure_cancels_comparison_work(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "planner_engine", "legacy")
    monkeypatch.setattr(settings, "planner_compare_engines", True)
    comparison_started = asyncio.Event()
    comparison_cancelled = False

    async def fake_run_engine(engine, *_args, **_kwargs):
        nonlocal comparison_cancelled
        if engine == "legacy":
            await comparison_started.wait()
            raise RuntimeError("primary failed")
        comparison_started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            comparison_cancelled = True
            raise

    monkeypatch.setattr(trip_tasks, "_run_engine", fake_run_engine)

    with pytest.raises(RuntimeError, match="primary failed"):
        asyncio.run(
            trip_tasks._run_configured_planners("task", "trip", {}, lambda *_args: None)
        )

    assert comparison_cancelled is True


def test_worker_persists_primary_and_comparison_versions(
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_id = _create_task(db_session_factory, "planner-comparison-persistence")
    primary = _execution("journey_graph", "graph-primary")
    comparison = _execution("legacy", "legacy-comparison")

    async def fake_run_set(*_args, **_kwargs):
        return trip_tasks.PlannerRunSet(primary=primary, comparison=comparison)

    class FakeLock:
        def extend(self, *_args, **_kwargs):
            return True

    class FakeTask:
        def retry(self, **_kwargs):
            raise AssertionError("Successful dual execution must not retry.")

    monkeypatch.setattr(trip_tasks, "SessionLocal", db_session_factory)
    monkeypatch.setattr(trip_tasks, "_run_configured_planners", fake_run_set)
    monkeypatch.setattr(trip_tasks, "publish_task_event", lambda *_args, **_kwargs: None)

    result = trip_tasks._execute_task(FakeTask(), task_id, FakeLock(), 60)

    with db_session_factory() as session:
        task = get_task(session, task_id)
        versions = list(
            session.scalars(
                select(TripVersion)
                .where(TripVersion.trip_id == task.trip_id)
                .order_by(TripVersion.version)
            )
        )
    assert result["status"] == "completed"
    assert task is not None and task.result_payload == primary.client_payload
    assert [(item.version, item.version_role, item.planner_engine) for item in versions] == [
        (1, "primary", "journey_graph"),
        (2, "comparison", "legacy"),
    ]
    assert versions[0].schema_version == "2.0"
    assert versions[0].native_payload == primary.native_payload
    assert versions[1].native_payload is None


def test_legacy_request_adapts_to_trip_request_v2() -> None:
    request = trip_tasks._to_v2_request(
        {
            "contract": "legacy",
            "request": {
                "city": "Tokyo",
                "cities": [{"city": "Tokyo", "days": 1}],
                "start_date": "2026-10-10",
                "end_date": "2026-10-10",
                "travel_days": 1,
                "transportation": "public transit",
                "accommodation": "midscale hotel",
                "preferences": ["food"],
                "free_text_input": "Light first day",
                "language": "en",
            },
        }
    )

    assert isinstance(request, TripRequestV2)
    assert request.destinations[0].city == "Tokyo"
    assert request.transport_preferences == ["public transit"]
