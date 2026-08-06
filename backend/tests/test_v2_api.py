"""Tests for the Phase 1 v2 trip skeleton."""

from __future__ import annotations

import sys
from typing import Any

from backend.app.domain.trip_models import TRIP_REQUEST_V2_EXAMPLE


def _payload_with(**overrides: Any) -> dict[str, Any]:
    payload = dict(TRIP_REQUEST_V2_EXAMPLE)
    payload.update(overrides)
    return payload


def test_v2_create_trip_returns_202_and_does_not_import_planner(client) -> None:
    planner_module = "backend.app.agents.trip_planner_agent"
    sys.modules.pop(planner_module, None)

    response = client.post("/api/v2/trips", json=TRIP_REQUEST_V2_EXAMPLE)

    body = response.json()
    assert response.status_code == 202
    assert body["status"] == "accepted"
    assert body["task_id"].startswith("task_")
    assert body["trip_id"].startswith("trip_")
    assert body["message"] == (
        "Accepted by the Phase 1 mock endpoint. No planner execution or persistence has started."
    )
    assert planner_module not in sys.modules


def test_v2_invalid_date_returns_v2_error_envelope(client) -> None:
    response = client.post("/api/v2/trips", json=_payload_with(start_date="2026-02-30"))

    body = response.json()
    assert response.status_code == 422
    assert body["error"]["code"] == "validation_error"
    assert body["error"]["message"] == "Request validation failed."
    assert body["error"]["details"][0]["field"] == "start_date"


def test_v2_reversed_dates_return_v2_error_envelope(client) -> None:
    response = client.post(
        "/api/v2/trips",
        json=_payload_with(start_date="2026-10-14", end_date="2026-10-10"),
    )

    body = response.json()
    assert response.status_code == 422
    assert body["error"]["code"] == "validation_error"
    assert any(
        detail["message"] == "Value error, end_date must be on or after start_date"
        for detail in body["error"]["details"]
    )


def test_v2_out_of_range_days_return_v2_error_envelope(client) -> None:
    response = client.post("/api/v2/trips", json=_payload_with(travel_days=31))

    body = response.json()
    assert response.status_code == 422
    assert body["error"]["code"] == "validation_error"
    assert any(detail["field"] == "travel_days" for detail in body["error"]["details"])


def test_v2_inconsistent_days_return_v2_error_envelope(client) -> None:
    response = client.post("/api/v2/trips", json=_payload_with(travel_days=4))

    body = response.json()
    assert response.status_code == 422
    assert body["error"]["code"] == "validation_error"
    assert any(
        detail["message"] == "Value error, travel_days must match the inclusive date range"
        for detail in body["error"]["details"]
    )


def test_v2_non_positive_budget_returns_v2_error_envelope(client) -> None:
    response = client.post("/api/v2/trips", json=_payload_with(budget_total=0))

    body = response.json()
    assert response.status_code == 422
    assert body["error"]["code"] == "validation_error"
    assert any(detail["field"] == "budget_total" for detail in body["error"]["details"])


def test_v2_unhandled_exception_returns_error_envelope(client) -> None:
    response = client.get("/api/v2/_test/unhandled")

    assert response.status_code == 500
    assert response.json() == {
        "error": {
            "code": "internal_server_error",
            "message": "An unexpected server error occurred.",
            "details": [],
        }
    }


def test_unknown_v2_route_returns_error_envelope(client) -> None:
    response = client.get("/api/v2/missing")

    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "code": "not_found",
            "message": "Not Found",
            "details": [],
        }
    }


def test_v2_error_prefix_does_not_capture_future_api_versions(client) -> None:
    response = client.get("/api/v20/missing")

    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}


def test_openapi_includes_v2_examples(client) -> None:
    schema = client.get("/openapi.json").json()
    post_operation = schema["paths"]["/api/v2/trips"]["post"]

    request_examples = post_operation["requestBody"]["content"]["application/json"]["examples"]
    accepted_example = post_operation["responses"]["202"]["content"]["application/json"]["example"]
    validation_example = post_operation["responses"]["422"]["content"]["application/json"]["example"]
    internal_error_example = post_operation["responses"]["500"]["content"]["application/json"]["example"]

    assert "phase_1_mock" in request_examples
    assert accepted_example["status"] == "accepted"
    assert validation_example["error"]["code"] == "validation_error"
    assert internal_error_example["error"]["code"] == "internal_server_error"
