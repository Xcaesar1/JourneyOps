"""Persistence and worker-policy tests for Phase 2 durable tasks."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from backend.app.db.models import TripTask, TripVersion
from backend.app.db.repository import create_or_get_task, get_task, save_trip_version
from backend.app.domain.trip_models import TRIP_REQUEST_V2_EXAMPLE
from backend.app.workers import trip_tasks
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker


def _create_task(factory: sessionmaker[Session]) -> str:
    with factory() as session:
        task, _ = create_or_get_task(
            session,
            request_payload=TRIP_REQUEST_V2_EXAMPLE,
            idempotency_key="test-durable-task",
        )
        return task.id


def test_trip_version_insert_is_idempotent(db_session_factory: sessionmaker[Session]) -> None:
    task_id = _create_task(db_session_factory)
    with db_session_factory() as session:
        task = get_task(session, task_id)
        assert task is not None
        first = save_trip_version(
            session, trip_id=task.trip_id, version=1, payload={"success": True}
        )
        second = save_trip_version(
            session, trip_id=task.trip_id, version=1, payload={"success": True}
        )
        count = session.scalar(select(func.count()).select_from(TripVersion))

    assert first.id == second.id
    assert count == 1


def test_new_session_reads_running_task_after_api_session_closes(
    db_session_factory: sessionmaker[Session],
) -> None:
    task_id = _create_task(db_session_factory)
    with db_session_factory() as first_session:
        task = get_task(first_session, task_id, for_update=True)
        assert task is not None
        task.status = "processing"
        first_session.commit()

    with db_session_factory() as restarted_api_session:
        recovered = get_task(restarted_api_session, task_id)
        assert recovered is not None
        assert recovered.status == "processing"


def test_worker_invokes_legacy_adapter_and_persists_one_version(
    db_session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id = _create_task(db_session_factory)
    calls: list[str] = []

    async def fake_planner(task_id_arg, payload, progress_callback):
        calls.append(task_id_arg)
        await progress_callback("planning", "Synthetic planner progress.", 50)
        return {"success": True, "plan_id": task_id_arg, "data": {"city": "Tokyo"}}

    class FakeLock:
        def extend(self, *_args, **_kwargs):
            return True

    class FakeTask:
        def retry(self, **_kwargs):
            raise AssertionError("Successful planner must not retry.")

    monkeypatch.setattr(trip_tasks, "SessionLocal", db_session_factory)
    monkeypatch.setattr(trip_tasks, "_run_legacy_planner", fake_planner)
    monkeypatch.setattr(trip_tasks, "publish_task_event", lambda *_args, **_kwargs: None)

    result = trip_tasks._execute_task(FakeTask(), task_id, FakeLock(), 60)

    with db_session_factory() as session:
        task = get_task(session, task_id)
        version_count = session.scalar(select(func.count()).select_from(TripVersion))
    assert result["status"] == "completed"
    assert task is not None and task.status == "completed"
    assert calls == [task_id]
    assert version_count == 1


def test_worker_startup_recovers_stale_processing_task(
    db_session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id = _create_task(db_session_factory)
    with db_session_factory() as session:
        task = get_task(session, task_id, for_update=True)
        assert task is not None
        task.status = "processing"
        task.attempt_count = 1
        task.updated_at = datetime.now(timezone.utc) - timedelta(minutes=10)
        task.celery_task_id = "lost-worker-message"
        session.commit()

    dispatched: list[str] = []
    monkeypatch.setattr(trip_tasks, "SessionLocal", db_session_factory)
    monkeypatch.setattr(
        trip_tasks,
        "enqueue_trip_task",
        lambda recovered_task_id: dispatched.append(recovered_task_id) or "recovered-message",
    )
    monkeypatch.setattr(trip_tasks, "publish_task_event", lambda *_args, **_kwargs: None)

    summary = trip_tasks.recover_incomplete_tasks(stale_after_seconds=60)

    with db_session_factory() as session:
        recovered = get_task(session, task_id)
    assert summary == {"dispatched": 1, "failed": 0, "cancelled": 0}
    assert dispatched == [task_id]
    assert recovered is not None and recovered.status == "retrying"
    assert recovered.celery_task_id == "recovered-message"


def test_worker_startup_explicitly_fails_exhausted_task(
    db_session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id = _create_task(db_session_factory)
    with db_session_factory() as session:
        task = session.get(TripTask, task_id)
        assert task is not None
        task.status = "processing"
        task.attempt_count = task.max_attempts
        task.updated_at = datetime.now(timezone.utc) - timedelta(minutes=10)
        session.commit()

    monkeypatch.setattr(trip_tasks, "SessionLocal", db_session_factory)
    monkeypatch.setattr(trip_tasks, "publish_task_event", lambda *_args, **_kwargs: None)

    summary = trip_tasks.recover_incomplete_tasks(stale_after_seconds=60)

    with db_session_factory() as session:
        failed = get_task(session, task_id)
    assert summary == {"dispatched": 0, "failed": 1, "cancelled": 0}
    assert failed is not None and failed.status == "failed"
    assert failed.error_code == "worker_lost"


def test_worker_startup_finishes_interrupted_cancellation(
    db_session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id = _create_task(db_session_factory)
    with db_session_factory() as session:
        task = session.get(TripTask, task_id)
        assert task is not None
        task.status = "cancel_requested"
        task.cancel_requested = True
        session.commit()

    dispatched: list[str] = []
    monkeypatch.setattr(trip_tasks, "SessionLocal", db_session_factory)
    monkeypatch.setattr(
        trip_tasks,
        "enqueue_trip_task",
        lambda recovered_task_id: dispatched.append(recovered_task_id) or "unexpected-message",
    )
    monkeypatch.setattr(trip_tasks, "publish_task_event", lambda *_args, **_kwargs: None)

    summary = trip_tasks.recover_incomplete_tasks(stale_after_seconds=60)

    with db_session_factory() as session:
        cancelled = get_task(session, task_id)
    assert summary == {"dispatched": 0, "failed": 0, "cancelled": 1}
    assert dispatched == []
    assert cancelled is not None and cancelled.status == "cancelled"
    assert cancelled.finished_at is not None
