# routes/dummy_order_routes.py
"""Admin routes for the live-tracked dummy orders.

    GET  /admin/dummy-orders          → full state (all today's orders)
    GET  /admin/dummy-orders/summary  → aggregates only (lightweight)
    POST /admin/dummy-orders/scan     → force a scan now (returns summary)
    POST /admin/dummy-orders/reset    → clear today's orders (testing)

No credentials, no tokens, no raw candle lists appear in any response.
Every order carries ``dummy: true`` so a consumer cannot confuse a
dummy order with a real Upstox order.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter

from services.dummy_order_service import dummy_order_service

router = APIRouter(prefix="/admin", tags=["dummy-orders"])

@router.get("/dummy-orders")
def get_dummy_orders() -> dict[str, Any]:
    """Return the full state of today's dummy orders."""
    return dummy_order_service.get_state()

@router.get("/dummy-orders/summary")
def get_dummy_orders_summary() -> dict[str, Any]:
    """Return aggregate stats only (for a lightweight poll)."""
    return dummy_order_service.get_summary()

@router.post("/dummy-orders/scan")
async def scan_dummy_orders() -> dict[str, Any]:
    """Force a scan now (create-if-new + update + save)."""
    return await asyncio.to_thread(
        dummy_order_service.run_once, "manual",
    )

@router.post("/dummy-orders/reset")
async def reset_dummy_orders() -> dict[str, Any]:
    """Clear today's orders. Admin-only; used for testing."""
    return await asyncio.to_thread(dummy_order_service.reset)