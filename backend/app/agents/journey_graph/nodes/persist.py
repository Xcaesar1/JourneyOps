"""Phase 3 persistence boundary; database persistence is wired by the worker later."""

from __future__ import annotations

from typing import Any

from ..state import TripState


def persist(state: TripState) -> dict[str, Any]:
    report = state["validation_report"]
    if report.has_critical:
        raise ValueError("A plan with critical validation issues cannot be finalized.")
    return {
        "final_plan": state["draft_plan"],
        "metrics": {**state.get("metrics", {}), "persisted": True},
    }
