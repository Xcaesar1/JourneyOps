"""Process and local-dependency health endpoints."""

import os
from pathlib import Path

from fastapi import APIRouter, HTTPException


router = APIRouter(tags=["health"])
DATA_DIR = Path(__file__).resolve().parents[2] / "data"


def _data_directory_check() -> dict[str, object]:
    """Inspect the persistent data directory without writing to it."""
    exists = DATA_DIR.is_dir()
    readable = exists and os.access(DATA_DIR, os.R_OK | os.X_OK)
    writable = exists and os.access(DATA_DIR, os.W_OK)
    ready = bool(exists and readable and writable)
    return {
        "status": "ready" if ready else "not_ready",
        "exists": exists,
        "readable": readable,
        "writable": writable,
    }


@router.get("/health/live")
async def health_live():
    """Report only whether the API process can serve requests."""
    return {"status": "alive", "service": "tripstar-api"}


@router.get("/health/ready")
async def health_ready():
    """Report readiness using local, side-effect-free dependency checks."""
    data_directory = _data_directory_check()
    payload = {
        "status": "ready" if data_directory["status"] == "ready" else "not_ready",
        "service": "tripstar-api",
        "checks": {"data_directory": data_directory},
    }
    if payload["status"] != "ready":
        raise HTTPException(status_code=503, detail=payload)
    return payload
