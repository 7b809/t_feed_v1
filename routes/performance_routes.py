"""Read-only performance snapshot endpoints for the member workspace."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Response

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


@router.get("/today-orders")
def today_orders(response: Response) -> dict[str, Any]:
    """Return today's saved dummy orders for the orders workspace."""
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return performance_snapshot_service.get_today_orders()


@router.get("/order-history")
def order_history(
    response: Response,
    page: int = Query(default=1, ge=1, le=1000000),
    page_size: int = Query(default=20, ge=10, le=100),
    search: str | None = Query(default=None, max_length=100),
) -> dict[str, Any]:
    """Return a server-paginated order history across daily documents."""
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return performance_snapshot_service.get_order_history(
        page=page,
        page_size=page_size,
        search=search,
    )
