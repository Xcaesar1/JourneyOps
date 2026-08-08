"""Unit tests for the typed Phase 3 JourneyGraph skeleton."""

from __future__ import annotations

import pytest
from backend.app.agents.journey_graph import build_journey_graph
from backend.app.agents.journey_graph.nodes import (
    build_placeholder_plan,
    collect,
    enrich_plan,
    make_draft_node,
    normalize_request,
    persist,
    make_plan_intercity_transport_node,
    prepare_research_queries,
    validate_stub,
)
from backend.app.domain.trip_models import (
    TRIP_REQUEST_V2_EXAMPLE,
    TripPlanV2,
    TripRequestV2,
)
from pydantic import ValidationError
from backend.app.services.routing import NoopRouteEstimateProvider


def _initial_state() -> dict:
    return {
        "trip_id": "trip_graph_test",
        "task_id": "task_graph_test",
        "request": TRIP_REQUEST_V2_EXAMPLE,
    }


def _normalized_state() -> dict:
    initial = _initial_state()
    return {**initial, **normalize_request(initial)}


def _enriched_state() -> dict:
    state = _normalized_state()
    state.update(make_plan_intercity_transport_node(NoopRouteEstimateProvider())(state))
    state.update(make_draft_node(build_placeholder_plan)(state))
    state.update(enrich_plan(state))
    return state


def test_normalize_request_node_returns_typed_request() -> None:
    result = normalize_request(_initial_state())

    assert isinstance(result["request"], TripRequestV2)
    assert result["metrics"] == {"normalized": True}


def test_collect_node_returns_structured_provider_boundaries() -> None:
    result = collect(_normalized_state())

    assert result["sources"] == []
    assert result["poi_candidates"] == {"Tokyo": [], "Kyoto": []}
    assert result["weather"] == {"Tokyo": [], "Kyoto": []}
    assert result["metrics"]["collected"] is True


def test_prepare_research_node_returns_five_queries_per_city() -> None:
    result = prepare_research_queries(_normalized_state())

    assert len(result["research_queries"]) == 10
    assert result["metrics"]["research_prepared"] is True


def test_draft_node_returns_typed_plan() -> None:
    state = _normalized_state()
    state.update(make_plan_intercity_transport_node(NoopRouteEstimateProvider())(state))
    result = make_draft_node(build_placeholder_plan)(state)

    assert isinstance(result["draft_plan"], TripPlanV2)
    assert result["draft_plan"].transport_options
    assert result["draft_plan"].route_matrix
    assert result["metrics"]["drafted"] is True


def test_validate_stub_node_returns_typed_report() -> None:
    state = _enriched_state()

    result = validate_stub(state)

    assert result["validation_report"].has_critical is False
    assert result["metrics"]["validated"] is True


def test_persist_node_finalizes_valid_typed_plan() -> None:
    state = _enriched_state()
    state.update(validate_stub(state))

    result = persist(state)

    assert result["final_plan"] is state["draft_plan"]
    assert result["metrics"]["persisted"] is True


def test_journey_graph_nodes_are_independently_executable() -> None:
    state = {**_initial_state(), **normalize_request(_initial_state())}
    state.update(collect(state))
    state.update(make_plan_intercity_transport_node(NoopRouteEstimateProvider())(state))
    state.update(make_draft_node(build_placeholder_plan)(state))
    state.update(enrich_plan(state))
    state.update(validate_stub(state))
    state.update(persist(state))

    assert isinstance(state["request"], TripRequestV2)
    assert isinstance(state["final_plan"], TripPlanV2)
    assert state["final_plan"].cities == ["Tokyo", "Kyoto"]
    assert state["validation_report"].has_critical is False


def test_compiled_journey_graph_runs_all_phase_three_nodes() -> None:
    result = build_journey_graph().invoke(_initial_state())

    assert isinstance(result["final_plan"], TripPlanV2)
    assert result["metrics"]["normalized"] is True
    assert result["metrics"]["research_prepared"] is True
    assert result["metrics"]["researched"] is True
    assert result["metrics"]["research_unknown_count"] == 8
    assert result["metrics"]["collected"] is True
    assert result["metrics"]["transport_planned"] is True
    assert result["metrics"]["transport_leg_count"] == 2
    assert result["metrics"]["drafted"] is True
    assert result["metrics"]["timeline_enriched"] is True
    assert result["metrics"]["budget_recalculated"] is True
    assert result["metrics"]["validated"] is True
    assert result["metrics"]["persisted"] is True


def test_journey_graph_mermaid_contains_ordered_nodes() -> None:
    mermaid = build_journey_graph().get_graph().draw_mermaid()

    expected_nodes = [
        "normalize_request",
        "prepare_research_queries",
        "research_web",
        "collect",
        "plan_intercity_transport",
        "draft",
        "enrich_plan",
        "deterministic_validate",
        "revise_plan",
        "persist",
    ]
    assert all(node in mermaid for node in expected_nodes)
    assert mermaid.index("normalize_request") < mermaid.index("persist")


def test_trip_plan_v2_rejects_non_contiguous_days() -> None:
    normalized = {**_initial_state(), **normalize_request(_initial_state())}
    payload = build_placeholder_plan(normalized).model_dump(mode="json")
    payload["days"][1]["day_index"] = 9

    with pytest.raises(ValidationError, match="day_index values must be contiguous"):
        TripPlanV2.model_validate(payload)
