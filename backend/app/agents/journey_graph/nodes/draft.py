"""Draft node with an injectable typed generator."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Any

from ....domain.trip_models import BudgetV2, DayPlanV2, TripPlanV2
from ..state import TripState

DraftGenerator = Callable[[TripState], TripPlanV2]


def build_placeholder_plan(state: TripState) -> TripPlanV2:
    """Build a deterministic typed plan until the structured model node is connected."""
    request = state["request"]
    city_by_day = [
        destination.city
        for destination in request.destinations
        for _ in range(destination.days)
    ]
    transport = ", ".join(request.transport_preferences) or "public transit"
    days = [
        DayPlanV2(
            date=request.start_date + timedelta(days=day_index),
            day_index=day_index,
            city=city,
            description=f"Typed planning placeholder for {city}.",
            transportation=transport,
            accommodation=request.accommodation_preference or "midscale hotel",
        )
        for day_index, city in enumerate(city_by_day)
    ]
    return TripPlanV2(
        city=city_by_day[0],
        cities=[destination.city for destination in request.destinations],
        start_date=request.start_date,
        end_date=request.end_date,
        days=days,
        overall_suggestions="Structured JourneyGraph draft awaiting provider enrichment.",
        budget=BudgetV2(),
    )


def make_draft_node(generator: DraftGenerator) -> Callable[[TripState], dict[str, Any]]:
    def draft(state: TripState) -> dict[str, Any]:
        plan = TripPlanV2.model_validate(generator(state))
        return {
            "draft_plan": plan,
            "metrics": {**state.get("metrics", {}), "drafted": True},
        }

    return draft
