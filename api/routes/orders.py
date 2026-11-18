"""HTTP endpoints for order execution, instrument search and order history."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

from core.config import (
    DEFAULT_ORDER_QUANTITY, DEFAULT_PRODUCT, DEFAULT_ORDER_TYPE, DEFAULT_VALIDITY,
    ORDERS_DATA_DIR,
)
from core.logger import get_logger
from core.token_service import fetch_access_token
from models.order_models import ExitAllRequest, OrderIdsRequest, PlaceOrderRequest
from repositories.order_repository import OrderRepository
from services.order_service import OrderService, OrderServiceError, TradingDisabledError
from services.instrument_search_service import InstrumentSearchError, search_upstox_instruments

logger = get_logger(__file__)
router = APIRouter(prefix="/api/v1", tags=["orders"])
_service = OrderService()
_repository = OrderRepository()


def _raise_http(exc: Exception):
    if isinstance(exc, TradingDisabledError):
        raise HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, (OrderServiceError, ValueError, KeyError)):
        raise HTTPException(status_code=400, detail=str(exc))
    logger.exception("Order API operation failed")
    raise HTTPException(status_code=502, detail=f"{type(exc).__name__}: {exc}")


@router.get("/config/order-defaults")
def order_defaults():
    from core.config import TRADING_ENABLED, UPSTOX_SANDBOX
    return {
        "quantity": DEFAULT_ORDER_QUANTITY, "transaction_type": "BUY",
        "product": DEFAULT_PRODUCT, "order_type": DEFAULT_ORDER_TYPE,
        "validity": DEFAULT_VALIDITY, "trading_enabled": TRADING_ENABLED,
        "upstox_sandbox": UPSTOX_SANDBOX,
    }


@router.get("/instruments/search")
def search_instruments(
    query: str = Query(min_length=1, max_length=50),
    exchanges: str = "ALL",
    segments: str = "ALL",
    instrument_types: str | None = None,
    expiry: str | None = None,
    atm_offset: int | None = None,
    page_number: int = Query(default=1, ge=1),
    records: int = Query(default=15, ge=1, le=30),
):
    """Search Upstox and fall back to a bounded-age local cache on upstream failures."""
    params = {
        "query": query.strip(),
        "exchanges": exchanges,
        "segments": segments,
        "page_number": page_number,
        "records": records,
    }
    if instrument_types:
        params["instrument_types"] = instrument_types
    if expiry:
        params["expiry"] = expiry
    if atm_offset is not None:
        params["atm_offset"] = atm_offset
    try:
        return search_upstox_instruments(params)
    except InstrumentSearchError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"message": str(exc), **exc.details},
        ) from exc
    except RuntimeError as exc:
        logger.exception("Instrument search could not load the Upstox access token")
        raise HTTPException(status_code=503, detail="Upstox access token is unavailable") from exc


@router.get("/orders/dates")
def order_dates():
    """Return available daily ledgers from local JSON and MongoDB."""
    dates: set[str] = set()
    folder = Path(ORDERS_DATA_DIR)
    if folder.exists():
        dates.update(path.stem for path in folder.glob("????-??-??.json"))
    try:
        rows = _repository.collection.find({}, {"_id": 1, "date": 1}).sort("_id", -1).limit(500)
        for row in rows:
            value = row.get("date") or row.get("_id")
            if value:
                dates.add(str(value))
    except Exception:
        logger.exception("Could not enumerate MongoDB order dates; using local ledgers")
    return {"dates": sorted(dates, reverse=True)}


@router.get("/orders/history")
def order_history(date_key: str = Query(alias="date")):
    try:
        date.fromisoformat(date_key)
    except ValueError:
        raise HTTPException(status_code=400, detail="date must use YYYY-MM-DD")
    try:
        return _repository.load_daily_document(date_key)
    except Exception as exc:
        _raise_http(exc)


@router.post("/orders/place")
def place_order(body: PlaceOrderRequest):
    try:
        return _service.place_order(body)
    except Exception as exc:
        _raise_http(exc)


@router.get("/orders/today")
def get_today_orders():
    try:
        return _repository.list_today()
    except Exception as exc:
        _raise_http(exc)


@router.post("/orders/cancel/bulk")
def cancel_orders(body: OrderIdsRequest):
    try:
        return {"results": _service.cancel_orders(body.order_ids)}
    except Exception as exc:
        _raise_http(exc)


@router.post("/orders/exit-by-ids")
def exit_orders_by_ids(body: OrderIdsRequest):
    try:
        return {"results": _service.exit_orders(body.order_ids)}
    except Exception as exc:
        _raise_http(exc)


@router.post("/orders/exit-all")
def exit_all_positions(body: ExitAllRequest = ExitAllRequest()):
    try:
        return _service.exit_all_positions(tag=body.tag, segment=body.segment)
    except Exception as exc:
        _raise_http(exc)


@router.post("/orders/{order_id}/cancel")
def cancel_single_order(order_id: str):
    try:
        return _service.cancel_order(order_id)
    except Exception as exc:
        _raise_http(exc)


@router.post("/orders/{order_id}/exit")
def exit_single_order(order_id: str):
    try:
        return _service.exit_order(order_id)
    except Exception as exc:
        _raise_http(exc)


@router.get("/orders/{order_id}")
def get_local_order(order_id: str):
    try:
        order = _repository.find_order(order_id)
        if not order:
            raise HTTPException(status_code=404, detail="Order not found in today's local ledger")
        return order
    except HTTPException:
        raise
    except Exception as exc:
        _raise_http(exc)
