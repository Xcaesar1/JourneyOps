"""Typed task models for the Phase 1 v2 API skeleton."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

TRIP_TASK_RECORD_V2_EXAMPLE: dict[str, Any] = {
    "task_id": "task_1234567890ab",
    "trip_id": "trip_1234567890ab",
    "status": "accepted",
    "created_at": "2026-08-06T00:00:00Z",
    "message": "Accepted by the Phase 1 mock endpoint. No planner execution or persistence has started.",
}


class TripTaskRecordV2(BaseModel):
    """Non-persistent task receipt returned by POST /api/v2/trips."""

    model_config = ConfigDict(json_schema_extra={"example": TRIP_TASK_RECORD_V2_EXAMPLE})

    task_id: str = Field(..., description="Mock task identifier.")
    trip_id: str = Field(..., description="Mock trip identifier.")
    status: Literal["accepted"] = Field(default="accepted", description="Submission status.")
    created_at: datetime = Field(..., description="UTC timestamp when the mock task was created.")
    message: str = Field(
        default="Accepted by the Phase 1 mock endpoint. No planner execution or persistence has started.",
        description="Explicit mock-status message.",
    )
