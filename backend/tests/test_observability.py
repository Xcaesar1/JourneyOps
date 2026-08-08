"""Phase 7 trace, telemetry, and runtime manifest tests."""

from __future__ import annotations

from backend.app.db.repository import record_telemetry_event, save_trip_version
from backend.app.domain.trip_models import TRIP_REQUEST_V2_EXAMPLE
from sqlalchemy.orm import Session, sessionmaker


def test_trace_header_propagates_into_durable_task(client) -> None:
    trace_id = "trace_external_12345678"

    response = client.post(
        "/api/v2/trips",
        json=TRIP_REQUEST_V2_EXAMPLE,
        headers={"X-Trace-ID": trace_id},
    )

    assert response.status_code == 202
    assert response.headers["X-Trace-ID"] == trace_id
    assert response.json()["trace_id"] == trace_id


def test_telemetry_endpoint_redacts_sensitive_metadata(
    client,
    db_session_factory: sessionmaker[Session],
) -> None:
    created = client.post("/api/v2/trips", json=TRIP_REQUEST_V2_EXAMPLE).json()
    with db_session_factory() as session:
        record_telemetry_event(
            session,
            trace_id=created["trace_id"],
            task_id=created["task_id"],
            trip_id=created["trip_id"],
            component="tool",
            operation="provider_call",
            status="failed",
            node="research",
            tool="web_search",
            latency_ms=123,
            cache_hit=False,
            metadata={
                "error_code": "rate_limited",
                "api_key": "must-not-leak",
                "nested": {"cookie": "must-not-leak"},
            },
        )

    response = client.get(f"/api/v2/trips/tasks/{created['task_id']}/telemetry")

    assert response.status_code == 200
    event = response.json()[0]
    assert event["trace_id"] == created["trace_id"]
    assert event["node"] == "research"
    assert event["tool"] == "web_search"
    assert event["metadata"]["api_key"] == "[REDACTED]"
    assert event["metadata"]["nested"]["cookie"] == "[REDACTED]"
    assert "must-not-leak" not in response.text


def test_trip_version_persists_runtime_manifest(
    client,
    db_session_factory: sessionmaker[Session],
) -> None:
    created = client.post("/api/v2/trips", json=TRIP_REQUEST_V2_EXAMPLE).json()
    with db_session_factory() as session:
        version = save_trip_version(
            session,
            trip_id=created["trip_id"],
            version=1,
            payload={"success": True},
            planner_engine="journey_graph",
            schema_version="2.0",
            model_id="test-model",
            prompt_version="prompt/1",
            workflow_version="workflow/1",
            tool_versions={"web_research": "2.0"},
            usage_summary={"total_tokens": 321, "model_cost_usd": 0.0123},
            activate=True,
        )

    response = client.get(f"/api/v2/trips/{created['trip_id']}/versions/{version.version}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["model_id"] == "test-model"
    assert payload["prompt_version"] == "prompt/1"
    assert payload["workflow_version"] == "workflow/1"
    assert payload["tool_versions"] == {"web_research": "2.0"}
    assert payload["usage_summary"]["total_tokens"] == 321
