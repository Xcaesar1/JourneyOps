"""Phase 3 validation stub with a typed report boundary."""

from __future__ import annotations

from typing import Any

from ....domain.validation_models import ValidationIssueV2, ValidationReportV2
from ..state import TripState


def validate_stub(state: TripState) -> dict[str, Any]:
    plan = state["draft_plan"]
    issues: list[ValidationIssueV2] = []
    if not plan.days:
        issues.append(
            ValidationIssueV2(
                code="empty_itinerary",
                severity="critical",
                message="The structured plan contains no itinerary days.",
                suggested_action="Generate at least one day before persistence.",
            )
        )
    return {
        "validation_report": ValidationReportV2(issues=issues),
        "metrics": {**state.get("metrics", {}), "validated": True},
    }
