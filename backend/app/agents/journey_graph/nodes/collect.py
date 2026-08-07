"""Phase 3 collection boundary returning only structured objects."""

from __future__ import annotations

from typing import Any

from ..state import TripState


def collect(state: TripState) -> dict[str, Any]:
    request = state["request"]
    cities = [destination.city for destination in request.destinations]
    return {
        "sources": list(state.get("sources", [])),
        "poi_candidates": {city: [] for city in cities},
        "weather": {city: [] for city in cities},
        "transport_options": list(state.get("transport_options", [])),
        "metrics": {**state.get("metrics", {}), "collected": True},
    }
