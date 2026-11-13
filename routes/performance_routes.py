"""Read-only performance snapshot endpoints for the member workspace."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Response

from services.performance_snapshot_service import performance_snapshot_service

router = APIRouter(prefix="/live/performance", tags=["performance-viewer"])


@router.get("/today-snapshot")
def today_snapshot(response: Response) -> dict[str, Any]:
    """Return today's upstream daily document, or a not-found status.

    The endpoint is GET-only and the service uses find_one exclusively.
    It never creates, updates, or deletes documents in the source collection.
    """
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return performance_snapshot_service.get_today_snapshot()
