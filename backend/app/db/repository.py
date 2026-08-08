"""Transactional persistence operations for durable trip tasks."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..domain.research_models import SourceEvidence
from .models import SourceEvidenceRecord, Trip, TripSourceLink, TripTask, TripVersion

FINAL_TASK_STATUSES = frozenset({"completed", "failed", "cancelled"})


class IdempotencyConflictError(ValueError):
    """Raised when one idempotency key is reused for a different request."""


def create_or_get_task(
    session: Session,
    *,
    request_payload: dict[str, Any],
    idempotency_key: str,
    max_attempts: int = 3,
) -> tuple[TripTask, bool]:
    """Create a trip and task atomically, or return the idempotent existing task."""
    existing_trip = session.scalar(select(Trip).where(Trip.idempotency_key == idempotency_key))
    if existing_trip is not None:
        _ensure_same_idempotent_request(existing_trip, request_payload)
        return _task_for_trip(session, existing_trip.id), False

    trip = Trip(
        id=f"trip_{uuid4().hex[:20]}",
        idempotency_key=idempotency_key,
        request_payload=request_payload,
    )
    task = TripTask(
        id=f"task_{uuid4().hex[:20]}",
        trip=trip,
        status="queued",
        stage="queued",
        progress=0,
        message="Task queued for durable execution.",
        max_attempts=max_attempts,
    )
    session.add(task)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        concurrent_trip = session.scalar(select(Trip).where(Trip.idempotency_key == idempotency_key))
        if concurrent_trip is None:
            raise
        _ensure_same_idempotent_request(concurrent_trip, request_payload)
        return _task_for_trip(session, concurrent_trip.id), False
    session.refresh(task)
    return task, True


def get_task(session: Session, task_id: str, *, for_update: bool = False) -> TripTask | None:
    """Load a task, optionally locking it for a state transition."""
    statement = select(TripTask).where(TripTask.id == task_id)
    if for_update:
        statement = statement.with_for_update()
    return session.scalar(statement)


def get_trip(session: Session, trip_id: str) -> Trip | None:
    """Load a trip request by its durable identifier."""
    return session.get(Trip, trip_id)


def attach_celery_task(
    session: Session,
    task_id: str,
    celery_task_id: str,
    *,
    expected_celery_task_id: str | None = None,
) -> TripTask:
    """Persist the broker task identifier after successful dispatch."""
    task = _required_task(session, task_id, for_update=True)
    if expected_celery_task_id is not None:
        if task.celery_task_id != expected_celery_task_id:
            raise RuntimeError(f"Task {task_id} recovery claim changed before broker confirmation.")
        task.celery_task_id = celery_task_id
        session.commit()
        session.refresh(task)
    elif task.celery_task_id is None:
        task.celery_task_id = celery_task_id
        session.commit()
        session.refresh(task)
    return task


def update_task_state(
    session: Session,
    task_id: str,
    *,
    status: str | None = None,
    stage: str | None = None,
    progress: int | None = None,
    message: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    result_payload: dict[str, Any] | None = None,
    increment_attempt: bool = False,
    finished: bool = False,
) -> TripTask:
    """Apply one locked task transition and commit it."""
    task = _required_task(session, task_id, for_update=True)
    if status is not None:
        task.status = status
    if stage is not None:
        task.stage = stage
    if progress is not None:
        task.progress = min(100, max(0, progress))
    if message is not None:
        task.message = message
    if error_code is not None:
        task.error_code = error_code
    if error_message is not None:
        task.error_message = error_message
    if result_payload is not None:
        task.result_payload = result_payload
    if increment_attempt:
        task.attempt_count += 1
        task.started_at = task.started_at or datetime.now(timezone.utc)
    if finished:
        task.finished_at = datetime.now(timezone.utc)
    session.commit()
    session.refresh(task)
    return task


def request_cancellation(session: Session, task_id: str) -> TripTask:
    """Persist a cooperative cancellation request."""
    task = _required_task(session, task_id, for_update=True)
    if task.status not in FINAL_TASK_STATUSES:
        task.cancel_requested = True
        if task.status in {"queued", "retrying"}:
            task.status = "cancelled"
            task.stage = "cancelled"
            task.message = "Task cancelled before execution."
            task.finished_at = datetime.now(timezone.utc)
        else:
            task.status = "cancel_requested"
            task.stage = "cancel_requested"
            task.message = "Cancellation requested."
        session.commit()
        session.refresh(task)
    return task


def prepare_retry(session: Session, task_id: str) -> TripTask:
    """Reset a failed or cancelled task for an explicit retry."""
    task = _required_task(session, task_id, for_update=True)
    if task.status not in {"failed", "cancelled"}:
        raise ValueError("Only failed or cancelled tasks can be retried.")
    task.status = "queued"
    task.stage = "queued"
    task.progress = 0
    task.message = "Task queued for retry."
    task.error_code = None
    task.error_message = None
    task.cancel_requested = False
    task.celery_task_id = None
    task.attempt_count = 0
    task.finished_at = None
    session.commit()
    session.refresh(task)
    return task


def save_trip_version(
    session: Session,
    *,
    trip_id: str,
    version: int,
    payload: dict[str, Any],
    planner_engine: str = "legacy",
    version_role: str = "primary",
    schema_version: str = "legacy",
    native_payload: dict[str, Any] | None = None,
    source_evidence: Sequence[SourceEvidence] = (),
) -> TripVersion:
    """Insert one immutable version, returning the existing row on redelivery."""
    existing = session.scalar(
        select(TripVersion).where(
            TripVersion.trip_id == trip_id,
            TripVersion.version == version,
        )
    )
    if existing is not None:
        _save_source_links(session, existing, source_evidence)
        session.commit()
        return existing

    record = TripVersion(
        trip_id=trip_id,
        version=version,
        planner_engine=planner_engine,
        version_role=version_role,
        schema_version=schema_version,
        payload=payload,
        native_payload=native_payload,
    )
    session.add(record)
    try:
        session.flush()
        _save_source_links(session, record, source_evidence)
        session.commit()
    except IntegrityError:
        session.rollback()
        existing = session.scalar(
            select(TripVersion).where(
                TripVersion.trip_id == trip_id,
                TripVersion.version == version,
            )
        )
        if existing is None:
            raise
        return existing
    session.refresh(record)
    return record


def _save_source_links(
    session: Session,
    version: TripVersion,
    evidence_items: Sequence[SourceEvidence],
) -> None:
    for evidence in evidence_items:
        source_bucket = _source_bucket(evidence)
        evidence_key = _evidence_key(evidence, source_bucket)
        source = session.scalar(
            select(SourceEvidenceRecord).where(
                SourceEvidenceRecord.evidence_key == evidence_key
            )
        )
        if source is None:
            source = SourceEvidenceRecord(
                id=str(evidence.id),
                evidence_key=evidence_key,
                source_bucket=source_bucket,
                title=evidence.title,
                url=str(evidence.url) if evidence.url is not None else None,
                domain=evidence.domain,
                provider=evidence.provider,
                claim_type=evidence.claim_type,
                claim_text=evidence.claim_text,
                published_at=evidence.published_at,
                fetched_at=evidence.fetched_at,
                freshness_status=evidence.freshness_status,
                trust_level=evidence.trust_level,
                confidence=evidence.confidence,
            )
            session.add(source)
            session.flush()

        linked = session.scalar(
            select(TripSourceLink).where(
                TripSourceLink.trip_version_id == version.id,
                TripSourceLink.source_id == source.id,
            )
        )
        if linked is None:
            session.add(TripSourceLink(trip_version_id=version.id, source_id=source.id))


def _source_bucket(evidence: SourceEvidence) -> str:
    if evidence.url is None:
        return hashlib.sha256(f"unknown:{evidence.id}".encode()).hexdigest()
    fetched_at = evidence.fetched_at
    if fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=timezone.utc)
    bucket = fetched_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H")
    return hashlib.sha256(f"{evidence.url}\x1f{bucket}".encode()).hexdigest()


def _evidence_key(evidence: SourceEvidence, source_bucket: str) -> str:
    material = f"{source_bucket}\x1f{evidence.claim_type}\x1f{evidence.claim_text}"
    return hashlib.sha256(material.encode()).hexdigest()


def get_trip_version(session: Session, trip_id: str, version: int) -> TripVersion | None:
    """Load an immutable trip version by its business key."""
    return session.scalar(
        select(TripVersion).where(
            TripVersion.trip_id == trip_id,
            TripVersion.version == version,
        )
    )


def stale_recoverable_tasks(session: Session, stale_after_seconds: int) -> list[TripTask]:
    """Return work whose broker dispatch or execution heartbeat may have been lost."""
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=stale_after_seconds)
    return list(
        session.scalars(
            select(TripTask).where(
                (TripTask.status == "cancel_requested")
                | (
                    TripTask.status.in_(["queued", "retrying"])
                    & (
                        TripTask.celery_task_id.is_(None)
                        | (TripTask.updated_at < cutoff)
                    )
                )
                | ((TripTask.status == "processing") & (TripTask.updated_at < cutoff))
            )
        )
    )


def task_needs_recovery(
    task: TripTask,
    stale_after_seconds: int,
    *,
    now: datetime | None = None,
) -> bool:
    """Revalidate a recovery candidate after acquiring its row lock."""
    if task.status in FINAL_TASK_STATUSES:
        return False
    if task.status == "cancel_requested" or task.cancel_requested:
        return True

    current_time = now or datetime.now(timezone.utc)
    updated_at = task.updated_at
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    stale = updated_at < current_time - timedelta(seconds=stale_after_seconds)
    if task.status in {"queued", "retrying"}:
        return task.celery_task_id is None or stale
    return task.status == "processing" and stale


def _task_for_trip(session: Session, trip_id: str) -> TripTask:
    task = session.scalar(select(TripTask).where(TripTask.trip_id == trip_id))
    if task is None:
        raise RuntimeError(f"Trip {trip_id} has no durable task.")
    return task


def _ensure_same_idempotent_request(trip: Trip, request_payload: dict[str, Any]) -> None:
    if trip.request_payload != request_payload:
        raise IdempotencyConflictError(
            "Idempotency-Key has already been used with a different request payload."
        )


def _required_task(session: Session, task_id: str, *, for_update: bool = False) -> TripTask:
    task = get_task(session, task_id, for_update=for_update)
    if task is None:
        raise LookupError(task_id)
    return task
