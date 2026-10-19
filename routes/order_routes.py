"""Admin order routes.

    POST /admin/orders/buy             -> place a BUY order (lot-based)
    POST /admin/orders/sell            -> place a SELL order (lot-based)
    POST /admin/orders/exit-all        -> exit every open position
    GET  /admin/orders/runtime-status  -> current toggle state
    POST /admin/orders/runtime-enable  -> enable orders
    POST /admin/orders/runtime-disable -> disable orders

Request body (buy / sell):
    {
      "instrument_key": "NSE_FO|43919",   # required
      "lots": 1,                          # default 1
      "product": "D",                     # default D (intraday)
      "order_type": "MARKET",             # default MARKET
      "price": 0.0,
      "trigger_price": 0.0,
      "validity": "DAY",
      "tag": ""
    }

All endpoints return HTTP 200 with a JSON body whose ``status`` field
carries the outcome (``ok``, ``disabled``, ``no_token``,
``invalid_input``, ``error``). A missing ``instrument_key`` on buy/sell
returns HTTP 400 so callers can distinguish bad input from a disabled
toggle.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Body, HTTPException

from services.order_runtime_service import order_runtime_service
from upstox_services.exit_all_positions import exit_all_positions
from upstox_services.place_order import place_order

router = APIRouter(prefix="/admin/orders", tags=["orders"])


def _require_instrument_key(payload: dict[str, Any]) -> str:
    key = str(payload.get("instrument_key") or "").strip()
    if not key:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "instrument_key_required",
                "message": "instrument_key is mandatory.",
            },
        )
    return key


@router.post("/buy")
async def buy_order(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    key = _require_instrument_key(payload)
    return await asyncio.to_thread(
        place_order,
        key,
        lots=int(payload.get("lots") or 1),
        transaction_type="BUY",
        product=str(payload.get("product") or "D"),
        order_type=str(payload.get("order_type") or "MARKET"),
        price=float(payload.get("price") or 0.0),
        trigger_price=float(payload.get("trigger_price") or 0.0),
        validity=str(payload.get("validity") or "DAY"),
        tag=str(payload.get("tag") or ""),
    )


@router.post("/sell")
async def sell_order(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    key = _require_instrument_key(payload)
    return await asyncio.to_thread(
        place_order,
        key,
        lots=int(payload.get("lots") or 1),
        transaction_type="SELL",
        product=str(payload.get("product") or "D"),
        order_type=str(payload.get("order_type") or "MARKET"),
        price=float(payload.get("price") or 0.0),
        trigger_price=float(payload.get("trigger_price") or 0.0),
        validity=str(payload.get("validity") or "DAY"),
        tag=str(payload.get("tag") or ""),
    )


@router.post("/exit-all")
async def exit_all(dry_run: bool = False) -> dict[str, Any]:
    return await asyncio.to_thread(exit_all_positions, dry_run)


@router.get("/runtime-status")
def runtime_status() -> dict[str, Any]:
    return order_runtime_service.status()


@router.post("/runtime-enable")
def runtime_enable() -> dict[str, Any]:
    return order_runtime_service.enable(source="api")


@router.post("/runtime-disable")
def runtime_disable() -> dict[str, Any]:
    return order_runtime_service.disable(source="api")