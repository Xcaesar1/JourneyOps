"""Persistent records for v2 trips, task execution, and immutable versions."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base


def utc_now() -> datetime:
    """Return a timezone-aware timestamp for application-side defaults."""
    return datetime.now(timezone.utc)


class Trip(Base):
    """A canonical v2 trip request keyed by an idempotency digest."""

    __tablename__ = "trips"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    request_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )

    tasks: Mapped[list[TripTask]] = relationship(back_populates="trip", cascade="all, delete-orphan")
    versions: Mapped[list[TripVersion]] = relationship(
        back_populates="trip", cascade="all, delete-orphan"
    )


class TripTask(Base):
    """Durable execution state for one trip planning request."""

    __tablename__ = "trip_tasks"
    __table_args__ = (
        Index("ix_trip_tasks_status_updated_at", "status", "updated_at"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    trip_id: Mapped[str] = mapped_column(
        ForeignKey("trips.id", ondelete="CASCADE"), nullable=False, unique=True, index=True
    )
    celery_task_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    status: Mapped[str] = mapped_column(String(32), default="queued", nullable=False, index=True)
    stage: Mapped[str] = mapped_column(String(64), default="queued", nullable=False)
    progress: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    message: Mapped[str] = mapped_column(Text, default="Task queued.", nullable=False)
    result_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )

    trip: Mapped[Trip] = relationship(back_populates="tasks")


class TripVersion(Base):
    """Immutable planner output. Celery redelivery must not duplicate a version."""

    __tablename__ = "trip_versions"
    __table_args__ = (
        UniqueConstraint("trip_id", "version", name="uq_trip_versions_trip_id_version"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trip_id: Mapped[str] = mapped_column(
        ForeignKey("trips.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

    trip: Mapped[Trip] = relationship(back_populates="versions")
