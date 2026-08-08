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
    enrich_plan,
    make_draft_node,
    make_human_review_node,
    make_plan_intercity_transport_node,
    make_research_web_node,
    normalize_request,
    persist,
    prepare_research_queries,
    reject_plan,
    revise_plan,
    validate_plan,
)
from .state import TripState


def build_journey_graph(
    *,
    draft_generator: DraftGenerator = build_placeholder_plan,
    research_provider: WebResearchProvider | None = None,
    route_provider: RouteEstimateProvider | None = None,
    checkpointer: Any | None = None,
    interrupt_before: Sequence[str] | None = None,
    require_human_review: bool = False,
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
    builder.add_node("enrich_plan", enrich_plan)
    builder.add_node("deterministic_validate", validate_plan)
    builder.add_node("revise_plan", revise_plan)
    builder.add_node("human_review", make_human_review_node(require_human_review))
    builder.add_node("reject_plan", reject_plan)
    builder.add_node("persist", persist)
    builder.add_edge(START, "normalize_request")
    builder.add_edge("normalize_request", "prepare_research_queries")
    builder.add_edge("prepare_research_queries", "research_web")
    builder.add_edge("research_web", "collect")
    builder.add_edge("collect", "plan_intercity_transport")
    builder.add_edge("plan_intercity_transport", "draft")
    builder.add_edge("draft", "enrich_plan")
    builder.add_edge("enrich_plan", "deterministic_validate")
    builder.add_conditional_edges(
        "deterministic_validate",
        _route_after_validation,
        {"revise_plan": "revise_plan", "human_review": "human_review"},
    )
    builder.add_edge("revise_plan", "enrich_plan")
    builder.add_conditional_edges(
        "human_review",
        _route_after_human_review,
        {"persist": "persist", "reject_plan": "reject_plan"},
    )
    builder.add_edge("reject_plan", END)
    builder.add_edge("persist", END)
    return builder.compile(
        checkpointer=checkpointer,
        interrupt_before=list(interrupt_before) if interrupt_before else None,
    )


def _route_after_validation(state: TripState) -> str:
    report = state["validation_report"]
    if report.has_critical and state.get("revision_count", 0) < 2:
        return "revise_plan"
    return "human_review"


def _route_after_human_review(state: TripState) -> str:
    return "persist" if state.get("approval_status") == "approved" else "reject_plan"
