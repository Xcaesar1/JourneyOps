"""JourneyGraph builder for typed, checkpoint-ready planning."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from langgraph.graph import END, START, StateGraph

from ...services.research import NoopWebResearchProvider, WebResearchProvider
from ...services.routing import NoopRouteEstimateProvider, RouteEstimateProvider
from .nodes import (
    DraftGenerator,
    build_placeholder_plan,
    collect,
    make_draft_node,
    make_research_web_node,
    make_plan_intercity_transport_node,
    normalize_request,
    persist,
    prepare_research_queries,
    validate_stub,
)
from .state import TripState


def build_journey_graph(
    *,
    draft_generator: DraftGenerator = build_placeholder_plan,
    research_provider: WebResearchProvider | None = None,
    route_provider: RouteEstimateProvider | None = None,
    checkpointer: Any | None = None,
    interrupt_before: Sequence[str] | None = None,
):
    configured_research_provider = research_provider or NoopWebResearchProvider()
    configured_route_provider = route_provider or NoopRouteEstimateProvider()
    builder = StateGraph(TripState)
    builder.add_node("normalize_request", normalize_request)
    builder.add_node("prepare_research_queries", prepare_research_queries)
    builder.add_node("research_web", make_research_web_node(configured_research_provider))
    builder.add_node("collect", collect)
    builder.add_node(
        "plan_intercity_transport",
        make_plan_intercity_transport_node(configured_route_provider),
    )
    builder.add_node("draft", make_draft_node(draft_generator))
    builder.add_node("validate_stub", validate_stub)
    builder.add_node("persist", persist)
    builder.add_edge(START, "normalize_request")
    builder.add_edge("normalize_request", "prepare_research_queries")
    builder.add_edge("prepare_research_queries", "research_web")
    builder.add_edge("research_web", "collect")
    builder.add_edge("collect", "plan_intercity_transport")
    builder.add_edge("plan_intercity_transport", "draft")
    builder.add_edge("draft", "validate_stub")
    builder.add_edge("validate_stub", "persist")
    builder.add_edge("persist", END)
    return builder.compile(
        checkpointer=checkpointer,
        interrupt_before=list(interrupt_before) if interrupt_before else None,
    )
