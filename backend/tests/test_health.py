import tempfile
import unittest
from pathlib import Path

from backend.app.api import health
from fastapi import FastAPI
from fastapi.testclient import TestClient


class HealthEndpointsTest(unittest.TestCase):
    def setUp(self):
        self.original_data_dir = health.DATA_DIR
        self.temp_dir = tempfile.TemporaryDirectory()
        health.DATA_DIR = Path(self.temp_dir.name)
        app = FastAPI()
        app.include_router(health.router)
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        health.DATA_DIR = self.original_data_dir
        self.temp_dir.cleanup()

    def test_live_reports_process_alive(self):
        response = self.client.get("/health/live")

        self.assertEqual(200, response.status_code)
        self.assertEqual("alive", response.json()["status"])

    def test_ready_reports_accessible_data_directory(self):
        response = self.client.get("/health/ready")

        self.assertEqual(200, response.status_code)
        self.assertEqual("ready", response.json()["status"])
        self.assertEqual(
            "ready",
            response.json()["checks"]["data_directory"]["status"],
        )

    def test_ready_returns_503_when_data_directory_is_missing(self):
        health.DATA_DIR = Path(self.temp_dir.name) / "missing"

        response = self.client.get("/health/ready")

        self.assertEqual(503, response.status_code)
        self.assertEqual("not_ready", response.json()["detail"]["status"])


if __name__ == "__main__":
    unittest.main()
