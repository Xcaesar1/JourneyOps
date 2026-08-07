"""Unit tests for the typed Phase 3 JourneyGraph skeleton."""

from __future__ import annotations

import pytest
from backend.app.agents.journey_graph import build_journey_graph
from backend.app.agents.journey_graph.nodes import (
    build_placeholder_plan,
    collect,
    normalize_request,
    persist,
    validate_stub,
)
from backend.app.domain.trip_models import (
    TRIP_REQUEST_V2_EXAMPLE,
    TripPlanV2,
    TripRequestV2,
)
from pydantic import ValidationError


def _initial_state() -> dict:
    return {
        "trip_id": "trip_graph_test",
        "task_id": "task_graph_test",
        "request": TRIP_REQUEST_V2_EXAMPLE,
    }


def test_journey_graph_nodes_are_independently_executable() -> None:
    state = {**_initial_state(), **normalize_request(_initial_state())}
    state.update(collect(state))
    plan = build_placeholder_plan(state)
    state["draft_plan"] = plan
    state.update(validate_stub(state))
    state.update(persist(state))

    assert isinstance(state["request"], TripRequestV2)
    assert isinstance(state["final_plan"], TripPlanV2)
    assert state["final_plan"].cities == ["Tokyo", "Kyoto"]
    assert state["validation_report"].has_critical is False


def test_compiled_journey_graph_runs_all_phase_three_nodes() -> None:
    result = build_journey_graph().invoke(_initial_state())

    assert isinstance(result["final_plan"], TripPlanV2)
    assert result["metrics"] == {
        "normalized": True,
        "collected": True,
        "drafted": True,
        "validated": True,
        "persisted": True,
    }


def test_journey_graph_mermaid_contains_ordered_nodes() -> None:
    mermaid = build_journey_graph().get_graph().draw_mermaid()

    expected_nodes = ["normalize_request", "collect", "draft", "validate_stub", "persist"]
    assert all(node in mermaid for node in expected_nodes)
    assert mermaid.index("normalize_request") < mermaid.index("persist")


def test_trip_plan_v2_rejects_non_contiguous_days() -> None:
    normalized = {**_initial_state(), **normalize_request(_initial_state())}
    payload = build_placeholder_plan(normalized).model_dump(mode="json")
    payload["days"][1]["day_index"] = 9

    with pytest.raises(ValidationError, match="day_index values must be contiguous"):
        TripPlanV2.model_validate(payload)
