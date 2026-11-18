"""HTTP endpoints for order placement, cancellation and square-off."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from core.logger import get_logger
from models.order_models import ExitAllRequest, OrderIdsRequest, PlaceOrderRequest
from repositories.order_repository import OrderRepository
from services.order_service import (
    OrderService, OrderServiceError, TradingDisabledError,
)

logger = get_logger(__file__)
router = APIRouter(prefix="/api/v1/orders", tags=["orders"])
_service = OrderService()


def _raise_http(exc: Exception):
    if isinstance(exc, TradingDisabledError):
        raise HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, (OrderServiceError, ValueError, KeyError)):
        raise HTTPException(status_code=400, detail=str(exc))
    logger.exception("Order API operation failed")
    raise HTTPException(status_code=502, detail=f"{type(exc).__name__}: {exc}")


@router.post("/place")
def place_order(body: PlaceOrderRequest):
    try:
        return _service.place_order(body)
    except Exception as exc:
        _raise_http(exc)


@router.get("/today")
def get_today_orders():
    try:
        return OrderRepository().list_today()
    except Exception as exc:
        _raise_http(exc)


@router.post("/cancel/bulk")
def cancel_orders(body: OrderIdsRequest):
    try:
        return {"results": _service.cancel_orders(body.order_ids)}
    except Exception as exc:
        _raise_http(exc)


@router.post("/exit-by-ids")
def exit_orders_by_ids(body: OrderIdsRequest):
    try:
        return {"results": _service.exit_orders(body.order_ids)}
    except Exception as exc:
        _raise_http(exc)


@router.post("/exit-all")
def exit_all_positions(body: ExitAllRequest = ExitAllRequest()):
    try:
        return _service.exit_all_positions(tag=body.tag, segment=body.segment)
    except Exception as exc:
        _raise_http(exc)


@router.post("/{order_id}/cancel")
def cancel_single_order(order_id: str):
    try:
        return _service.cancel_order(order_id)
    except Exception as exc:
        _raise_http(exc)


@router.post("/{order_id}/exit")
def exit_single_order(order_id: str):
    try:
        return _service.exit_order(order_id)
    except Exception as exc:
        _raise_http(exc)


@router.get("/{order_id}")
def get_local_order(order_id: str):
    try:
        order = OrderRepository().find_order(order_id)
        if not order:
            raise HTTPException(status_code=404, detail="Order not found in today's local ledger")
        return order
    except HTTPException:
        raise
    except Exception as exc:
        _raise_http(exc)
