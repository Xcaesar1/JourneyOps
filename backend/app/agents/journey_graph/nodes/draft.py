"""Draft node with an injectable typed generator."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Any

from ....domain.attraction_models import AttractionCandidate
from ....domain.trip_models import AttractionV2, BudgetV2, DayPlanV2, LocationV2, TripPlanV2
from ..state import TripState

DraftGenerator = Callable[[TripState], TripPlanV2]


def _place_key(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


def _verified_attractions(state: TripState, plan: TripPlanV2) -> TripPlanV2:
    """Drop model-invented stops and enrich verified AMap candidates."""
    if not state.get("metrics", {}).get("poi_candidate_policy_enforced", False):
        return plan
    candidates_by_city: dict[str, list[AttractionCandidate]] = {
        city: [AttractionCandidate.model_validate(item) for item in items]
        for city, items in state.get("poi_candidates", {}).items()
    }
    must_visit = [_place_key(value) for value in state["request"].must_visit]
    days: list[DayPlanV2] = []
    for day in plan.days:
        city_candidates = candidates_by_city.get(day.city, [])
        by_id = {item.poi_id: item for item in city_candidates}
        by_name = {_place_key(item.name): item for item in city_candidates}
        verified: list[AttractionV2] = []
        for attraction in day.attractions:
            candidate = by_id.get(attraction.poi_id) if attraction.poi_id else None
            candidate = candidate or by_name.get(_place_key(attraction.name))
            attraction_key = _place_key(attraction.name)
            allowed_must_visit = any(
                key and (key in attraction_key or attraction_key in key) for key in must_visit
            )
            if candidate is None and not allowed_must_visit:
                continue
            if candidate is None:
                verified.append(attraction)
                continue
            image = candidate.image
            verified.append(
                attraction.model_copy(
                    update={
                        "name": candidate.name,
                        "address": candidate.address,
                        "location": (
                            LocationV2(
                                longitude=candidate.longitude,
                                latitude=candidate.latitude,
                            )
                            if candidate.longitude is not None and candidate.latitude is not None
                            else attraction.location
                        ),
                        "category": candidate.category,
                        "poi_id": candidate.poi_id,
                        "rating": candidate.rating,
                        "image_url": image.url,
                        "image_source": image.source,
                        "image_author": image.author,
                        "image_license": image.license,
                        "image_source_page": image.source_page,
                        "image_attribution": image.attribution,
                        "recommendation_reason": candidate.recommendation_reason,
                    }
                )
            )
        days.append(day.model_copy(update={"attractions": verified}))
    return plan.model_copy(update={"days": days})


def build_placeholder_plan(state: TripState) -> TripPlanV2:
    """Build a deterministic typed plan until the structured model node is connected."""
    request = state["request"]
    city_by_day = [
        destination.city for destination in request.destinations for _ in range(destination.days)
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
        origin=request.origin,
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
        plan = _verified_attractions(state, TripPlanV2.model_validate(generator(state)))
        generation_metrics = getattr(generator, "last_metrics", {})
        request = state["request"]
        evidence = list(state.get("sources", []))
        sourced_count = sum(item.url is not None for item in evidence)
        if sourced_count == 0:
            research_status = "unavailable"
        elif sourced_count == len(evidence) and not state.get("research_issues"):
            research_status = "complete"
        else:
            research_status = "partial"
        payload = plan.model_dump(mode="python")
        payload.update(
            {
                "origin": request.origin,
                "city": request.destinations[0].city,
                "cities": [destination.city for destination in request.destinations],
                "start_date": request.start_date,
                "end_date": request.end_date,
                "transport_options": list(state.get("transport_options", [])),
                "route_matrix": list(state.get("route_estimates", [])),
                "source_evidence": evidence,
                "research_updated_at": max(
                    (item.fetched_at for item in evidence),
                    default=None,
                ),
                "research_status": research_status,
            }
        )
        plan = TripPlanV2.model_validate(payload)
        return {
            "draft_plan": plan,
            "metrics": {
                **state.get("metrics", {}),
                "drafted": True,
                "model_generation": generation_metrics,
            },
        }

    return draft
