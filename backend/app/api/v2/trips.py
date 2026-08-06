"""Phase 1 mock v2 trip endpoints."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, Body, status

from ...domain.error_models import (
    V2_INTERNAL_ERROR_EXAMPLE,
    V2_NOT_FOUND_ERROR_EXAMPLE,
    V2_VALIDATION_ERROR_EXAMPLE,
    ErrorEnvelopeV2,
)
from ...domain.task_models import TRIP_TASK_RECORD_V2_EXAMPLE, TripTaskRecordV2
from ...domain.trip_models import TRIP_REQUEST_V2_EXAMPLE, TripRequestV2

router = APIRouter(prefix="/trips", tags=["API v2"])


@router.post(
    "",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=TripTaskRecordV2,
    summary="Submit a v2 trip request",
    description=(
        "Phase 1 mock skeleton for the future v2 trip workflow. "
        "This endpoint validates input and returns a non-persistent accepted task record. "
        "It does not invoke the legacy planner, start a background task, or persist anything."
    ),
    responses={
        202: {
            "description": "Mock task accepted.",
            "content": {"application/json": {"example": TRIP_TASK_RECORD_V2_EXAMPLE}},
        },
        404: {
            "model": ErrorEnvelopeV2,
            "description": "Consistent v2 not-found envelope example.",
            "content": {"application/json": {"example": V2_NOT_FOUND_ERROR_EXAMPLE}},
        },
        422: {
            "model": ErrorEnvelopeV2,
            "description": "Consistent v2 validation error envelope.",
            "content": {"application/json": {"example": V2_VALIDATION_ERROR_EXAMPLE}},
        },
        500: {
            "model": ErrorEnvelopeV2,
            "description": "Consistent v2 internal error envelope.",
            "content": {"application/json": {"example": V2_INTERNAL_ERROR_EXAMPLE}},
        },
    },
)
async def create_trip(
    request: TripRequestV2 = Body(
        ...,
        openapi_examples={
            "phase_1_mock": {
                "summary": "Valid Phase 1 mock submission",
                "value": TRIP_REQUEST_V2_EXAMPLE,
            }
        },
    )
) -> TripTaskRecordV2:
    """Return a typed mock task receipt without touching the planner."""
    _ = request
    return TripTaskRecordV2(
        task_id=f"task_{uuid4().hex[:12]}",
        trip_id=f"trip_{uuid4().hex[:12]}",
        created_at=datetime.now(timezone.utc),
    )
