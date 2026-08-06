"""Shared pytest fixtures for Phase 1 backend tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from backend.app.api.errors import register_api_exception_handlers
from backend.app.api.routes import trip as legacy_trip_routes
from backend.app.api.v2 import trips as v2_trip_routes
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def tasks_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    legacy_trip_routes._tasks.clear()
    monkeypatch.setattr(legacy_trip_routes, "_TASKS_DATA_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def api_app(tasks_dir: Path) -> FastAPI:
    _ = tasks_dir
    app = FastAPI()
    register_api_exception_handlers(app)
    app.include_router(legacy_trip_routes.router, prefix="/api")
    app.include_router(v2_trip_routes.router, prefix="/api/v2")

    @app.get("/api/v2/_test/unhandled", include_in_schema=False)
    async def raise_unhandled_v2_error() -> None:
        raise RuntimeError("test-only failure")

    return app


@pytest.fixture
def client(api_app: FastAPI) -> TestClient:
    with TestClient(api_app, raise_server_exceptions=False) as test_client:
        yield test_client
