"""Authenticated external order mutation API.

These routes are intentionally separate from the dashboard API so that
server-to-server credentials never need to be exposed to browser JavaScript.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from api.routes.orders import _raise_http, _service
from core.external_api_auth import require_external_api_auth
from core.logger import get_logger
from models.gtt_models import PlaceGTTRequest
from models.order_models import ExitAllRequest, OrderIdsRequest, PlaceOrderRequest
from services.gtt_service import GTTServiceError, place_gtt_order

logger = get_logger(__file__)
router = APIRouter(
    prefix="/api/external/v1",
    tags=["external-orders"],
    dependencies=[Depends(require_external_api_auth)],
)


@router.post("/orders/place")
def external_place_order(body: PlaceOrderRequest):
    try:
        return _service.place_order(body)
    except Exception as exc:
        _raise_http(exc, action="order")


@router.post("/orders/cancel/bulk")
def external_cancel_orders(body: OrderIdsRequest):
    try:
        return {"results": _service.cancel_orders(body.order_ids)}
    except Exception as exc:
        _raise_http(exc, action="cancel")


@router.post("/orders/exit-by-ids")
def external_exit_orders(body: OrderIdsRequest):
    try:
        return {"results": _service.exit_orders(body.order_ids)}
    except Exception as exc:
        _raise_http(exc, action="exit")


@router.post("/orders/exit-all")
def external_exit_all(body: ExitAllRequest = ExitAllRequest()):
    try:
        return _service.exit_all_positions(tag=body.tag, segment=body.segment)
    except Exception as exc:
        _raise_http(exc, action="exit")


@router.post("/orders/{order_id}/cancel")
def external_cancel_single_order(order_id: str):
    try:
        return _service.cancel_order(order_id)
    except Exception as exc:
        _raise_http(exc, action="cancel")


@router.post("/orders/{order_id}/exit")
def external_exit_single_order(order_id: str):
    try:
        return _service.exit_order(order_id)
    except Exception as exc:
        _raise_http(exc, action="exit")


@router.post("/gtt/place")
def external_place_gtt(body: PlaceGTTRequest):
    try:
        return place_gtt_order(
            instrument_key=body.instrument_key,
            entry=body.entry,
            target=body.target,
            stoploss=body.stoploss,
            lot=body.lot,
            lot_size=body.lot_size,
            trading_symbol=body.trading_symbol,
            product=body.product,
            transaction_type=body.transaction_type,
        )
    except GTTServiceError as exc:
        detail = {"error": str(exc)}
        if exc.payload:
            detail["broker_error"] = exc.payload
        raise HTTPException(status_code=exc.status_code, detail=detail) from None
    except Exception:
        logger.exception("Unexpected external GTT placement failure")
        raise HTTPException(
            status_code=500, detail={"error": "Unexpected GTT service error."}
        ) from None
