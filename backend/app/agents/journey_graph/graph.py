"""JourneyGraph builder for typed, checkpoint-ready planning."""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from .nodes import (
    DraftGenerator,
    build_placeholder_plan,
    collect,
    make_draft_node,
    normalize_request,
    persist,
    validate_stub,
)
from .state import TripState


def build_journey_graph(
    *,
    draft_generator: DraftGenerator = build_placeholder_plan,
    checkpointer: Any | None = None,
):
    builder = StateGraph(TripState)
    builder.add_node("normalize_request", normalize_request)
    builder.add_node("collect", collect)
    builder.add_node("draft", make_draft_node(draft_generator))
    builder.add_node("validate_stub", validate_stub)
    builder.add_node("persist", persist)
    builder.add_edge(START, "normalize_request")
    builder.add_edge("normalize_request", "collect")
    builder.add_edge("collect", "draft")
    builder.add_edge("draft", "validate_stub")
    builder.add_edge("validate_stub", "persist")
    builder.add_edge("persist", END)
    return builder.compile(checkpointer=checkpointer)
