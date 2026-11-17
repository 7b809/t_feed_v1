from __future__ import annotations
import asyncio
from fastapi import APIRouter, HTTPException, Request

router = APIRouter()

@router.get("/dummy-orders/status")
async def dummy_orders_status(request: Request) -> dict:
    return await request.app.state.dummy_order_engine.status()

@router.get("/dummy-orders")
async def list_dummy_orders(request: Request) -> list:
    return await request.app.state.dummy_order_engine.get_orders()


@router.get("/dummy-orders/history/dates")
async def list_dummy_order_history_dates(request: Request) -> dict:
    """Return archived IST trading dates, newest first."""
    mongo_store = getattr(request.app.state, "dummy_order_mongo_store", None)
    if mongo_store is None:
        return {"dates": [], "count": 0}
    dates = await asyncio.to_thread(mongo_store.list_date_keys, 365)
    return {"dates": dates, "count": len(dates)}


@router.get("/dummy-orders/history")
async def list_dummy_order_history(
    request: Request,
    trade_date: str,
    page: int = 1,
    page_size: int = 24,
    strategy: str | None = None,
    status: str | None = None,
    search: str | None = None,
) -> dict:
    """Read one IST day's archived orders using stable page-number pagination."""
    from datetime import date
    from ..services.dummy_order_history import (
        filter_orders, flatten_daily_orders, summarize_orders,
    )

    try:
        parsed_date = date.fromisoformat(trade_date)
        if parsed_date.isoformat() != trade_date:
            raise ValueError
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="trade_date must be YYYY-MM-DD")

    if page < 1:
        raise HTTPException(status_code=400, detail="page must be >= 1")
    if page_size < 1 or page_size > 100:
        raise HTTPException(status_code=400, detail="page_size must be between 1 and 100")
    if strategy and strategy.strip().upper() not in {"SR", "SUPERTREND"}:
        raise HTTPException(status_code=400, detail="strategy must be SR or SUPERTREND")
    allowed_statuses = {
        "DUMMY_PLACED", "COMPLETED_TARGET", "COMPLETED_STOP_LOSS",
        "COMPLETED_SIGNAL_REVERSAL",
    }
    if status and status.strip().upper() not in allowed_statuses:
        raise HTTPException(status_code=400, detail="unsupported order status")

    mongo_store = getattr(request.app.state, "dummy_order_mongo_store", None)
    if mongo_store is None:
        raise HTTPException(status_code=503, detail="order history storage is unavailable")

    document = await asyncio.to_thread(mongo_store.get_day, trade_date)
    archived_orders = flatten_daily_orders(document)
    filtered_orders = filter_orders(
        archived_orders,
        strategy=strategy,
        status=status,
        search=search,
    )
    total = len(filtered_orders)
    start = (page - 1) * page_size
    items = filtered_orders[start:start + page_size]
    total_pages = (total + page_size - 1) // page_size

    return {
        "trade_date": trade_date,
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": total_pages,
        "has_previous": page > 1,
        "has_next": page < total_pages,
        "summary": summarize_orders(filtered_orders),
        "orders": items,
    }

@router.get("/dummy-orders/{isolation_id}")
async def get_dummy_orders(isolation_id: str, request: Request) -> dict:
    state = await request.app.state.dummy_order_engine.get_orders(isolation_id)
    if state is None:
        raise HTTPException(status_code=404, detail="no dummy-order state for isolation")
    return state
