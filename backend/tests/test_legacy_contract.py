"""Contract tests that freeze existing legacy response shapes."""

from __future__ import annotations

import json
from pathlib import Path

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "legacy"


def _load_fixture(name: str) -> dict:
    with open(FIXTURES_DIR / name, encoding="utf-8") as fixture_file:
        return json.load(fixture_file)


def test_completed_task_status_matches_legacy_fixture(client, tasks_dir: Path) -> None:
    expected = _load_fixture("trip_status_completed.json")
    target = tasks_dir / f"{expected['task_id']}.json"
    with open(target, "w", encoding="utf-8") as fixture_file:
        json.dump(expected, fixture_file, ensure_ascii=False, indent=2)

    response = client.get(f"/api/trip/status/{expected['task_id']}")

    assert response.status_code == 200
    assert response.json() == expected


def test_missing_task_status_preserves_legacy_404_shape(client) -> None:
    expected = _load_fixture("trip_status_not_found.json")

    response = client.get("/api/trip/status/missing-task")

    assert response.status_code == 404
    assert response.json() == expected
