"""Typed request models for the Phase 1 v2 API skeleton."""

from __future__ import annotations

from datetime import date, time
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

TRIP_REQUEST_V2_EXAMPLE: dict[str, Any] = {
    "origin": "Shanghai",
    "destinations": [
        {"city": "Tokyo", "days": 3},
        {"city": "Kyoto", "days": 2},
    ],
    "start_date": "2026-10-10",
    "end_date": "2026-10-14",
    "travel_days": 5,
    "budget_total": "12000.00",
    "currency": "CNY",
    "travelers": 2,
    "transport_preferences": ["flight", "train"],
    "accommodation_preference": "midscale hotel",
    "interests": ["food", "museums"],
    "must_visit": ["Senso-ji"],
    "avoid": ["red-eye flights"],
    "pace": "balanced",
    "daily_start_time": "09:00:00",
    "daily_end_time": "21:00:00",
    "max_daily_walking_minutes": 180,
    "accessibility_needs": ["elevator access"],
    "free_text_input": "Keep the first day light after arrival.",
    "language": "en",
    "timezone": "Asia/Tokyo",
}


class CityStayV2(BaseModel):
    """Requested stay duration for a single destination."""

    model_config = ConfigDict(str_strip_whitespace=True)

    city: str = Field(..., min_length=1, max_length=120, description="Destination city name.")
    days: int = Field(..., ge=1, le=30, description="Inclusive number of days spent in the city.")


class TripRequestV2(BaseModel):
    """Validated request contract for POST /api/v2/trips."""

    model_config = ConfigDict(
        str_strip_whitespace=True,
        json_schema_extra={"example": TRIP_REQUEST_V2_EXAMPLE},
    )

    origin: str = Field(..., min_length=1, max_length=120, description="Trip origin.")
    destinations: list[CityStayV2] = Field(
        ...,
        min_length=1,
        description="Ordered destination list with inclusive stay lengths.",
    )
    start_date: date = Field(..., description="Trip start date.")
    end_date: date = Field(..., description="Trip end date.")
    travel_days: int = Field(..., ge=1, le=30, description="Inclusive trip length in days.")
    budget_total: Decimal | None = Field(
        default=None,
        gt=0,
        description="Optional total budget. Must be positive when provided.",
    )
    currency: str = Field(default="CNY", min_length=3, max_length=3, description="Budget currency.")
    travelers: int = Field(default=1, ge=1, le=20, description="Traveler count.")

    transport_preferences: list[str] = Field(default_factory=list, description="Transport preferences.")
    accommodation_preference: str | None = Field(
        default=None,
        max_length=120,
        description="Accommodation preference.",
    )
    interests: list[str] = Field(default_factory=list, description="Interest tags.")
    must_visit: list[str] = Field(default_factory=list, description="Must-visit places.")
    avoid: list[str] = Field(default_factory=list, description="Items to avoid.")

    pace: Literal["relaxed", "balanced", "intensive"] = Field(
        default="balanced",
        description="Daily pacing preference.",
    )
    daily_start_time: time = Field(default=time(9, 0), description="Preferred daily start time.")
    daily_end_time: time = Field(default=time(21, 0), description="Preferred daily end time.")
    max_daily_walking_minutes: int | None = Field(
        default=None,
        ge=0,
        le=1440,
        description="Optional walking limit per day.",
    )
    accessibility_needs: list[str] = Field(default_factory=list, description="Accessibility requirements.")

    free_text_input: str = Field(default="", max_length=2000, description="Extra planning context.")
    language: str = Field(default="zh", min_length=2, max_length=8, description="Response language.")
    timezone: str = Field(default="Asia/Shanghai", min_length=1, max_length=64, description="Trip timezone.")

    @field_validator("currency")
    @classmethod
    def uppercase_currency(cls, value: str) -> str:
        """Normalize the currency code for downstream consumers."""
        return value.upper()

    @model_validator(mode="after")
    def validate_trip_window(self) -> TripRequestV2:
        """Apply cross-field validation for the v2 skeleton contract."""
        if self.end_date < self.start_date:
            raise ValueError("end_date must be on or after start_date")

        inclusive_days = (self.end_date - self.start_date).days + 1
        if inclusive_days != self.travel_days:
            raise ValueError("travel_days must match the inclusive date range")

        destination_days = sum(destination.days for destination in self.destinations)
        if destination_days != self.travel_days:
            raise ValueError("destinations.days must sum to travel_days")

        if self.daily_end_time <= self.daily_start_time:
            raise ValueError("daily_end_time must be after daily_start_time")

        return self
