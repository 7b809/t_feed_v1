"""Authenticated external order mutation API.

These routes are intentionally separate from the dashboard API so that
server-to-server credentials never need to be exposed to browser JavaScript.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from api.routes.orders import _raise_http, _service
from core.external_api_auth import require_external_api_auth
from core.logger import get_logger
from models.gtt_models import CancelGTTRequest, CleanupGTTRequest, GTTOrderIDsRequest, PlaceGTTRequest
from models.order_models import ExitAllRequest, OrderIdsRequest, PlaceOrderRequest
from services.gtt_service import GTTServiceError, cancel_gtt_order, cancel_gtt_orders, place_gtt_order
from services.daily_profit_guard_service import DailyProfitGuardError, get_today_pnl_status
from services.orderflow_cleanup_service import OrderflowCleanupError, OrderflowCleanupService
from services.upstox_order_api import UpstoxOrderAPIError, get_order_book

logger = get_logger(__file__)
_cleanup_service = OrderflowCleanupService()
router = APIRouter(
    prefix="/api/external/v1",
    tags=["external-orders"],
    dependencies=[Depends(require_external_api_auth)],
)


@router.post("/portfolio/today-pnl")
def external_today_portfolio_pnl(body: dict[str, Any]):
    # Router-level dependency validates body.api_token when external auth is enabled.
    try:
        return get_today_pnl_status()
    except DailyProfitGuardError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"error": str(exc), "code": exc.code, **exc.details},
        ) from None
    except Exception:
        logger.exception("Could not calculate external today's portfolio P&L")
        raise HTTPException(
            status_code=503,
            detail={"error": "Today's portfolio P&L is temporarily unavailable."},
        ) from None


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
        result = _service.exit_all_positions(tag=body.tag, segment=body.segment)
        if result.get("status") == "exit_all_partial_success":
            return JSONResponse(status_code=207, content=result)
        return result
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
            source_order_id=body.source_order_id,
            isolation_id=body.isolation_id,
        )
    except DailyProfitGuardError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"error": str(exc), "code": exc.code, **exc.details},
        ) from None
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



@router.post("/gtt/cancel")
def external_cancel_gtt(body: CancelGTTRequest):
    try:
        return cancel_gtt_order(body.gtt_order_id)
    except GTTServiceError as exc:
        detail = {"error": str(exc)}
        if exc.payload:
            detail["broker_error"] = exc.payload
        raise HTTPException(status_code=exc.status_code, detail=detail) from None
    except Exception:
        logger.exception("Unexpected external GTT cancellation failure")
        raise HTTPException(status_code=500, detail={"error": "Unexpected GTT cancellation error."}) from None


@router.post("/gtt/cleanup")
def external_cleanup_gtt(body: CleanupGTTRequest):
    try:
        return _cleanup_service.cleanup(
            body.gtt_order_ids,
            isolation_id=body.isolation_id,
            source_order_id=body.source_order_id,
        )
    except OrderflowCleanupError as exc:
        detail = {"error": str(exc), "cleanup": exc.details}
        raise HTTPException(status_code=exc.status_code, detail=detail) from None
    except Exception:
        logger.exception("Unexpected Orderflow broker cleanup failure")
        raise HTTPException(status_code=502, detail={"error": "Unexpected broker cleanup error."}) from None


@router.post("/gtt/cancel/bulk")
def external_cancel_gtt_bulk(body: GTTOrderIDsRequest):
    try:
        return cancel_gtt_orders(body.gtt_order_ids)
    except GTTServiceError as exc:
        detail = {"error": str(exc)}
        if exc.payload:
            detail["broker_error"] = exc.payload
        raise HTTPException(status_code=exc.status_code, detail=detail) from None
    except Exception:
        logger.exception("Unexpected external bulk GTT cancellation failure")
        raise HTTPException(status_code=500, detail={"error": "Unexpected bulk GTT cancellation error."}) from None


@router.post("/orders/order-book")
def external_broker_order_book(body: dict[str, Any]):
    """Fetch current-day Upstox orders; router dependency validates body.api_token."""
    try:
        return get_order_book()
    except UpstoxOrderAPIError as exc:
        detail = {"error": str(exc)}
        if exc.payload:
            detail["broker_error"] = exc.payload
        raise HTTPException(status_code=exc.status_code, detail=detail) from None
    except Exception:
        logger.exception("Could not retrieve the external Upstox broker order book")
        raise HTTPException(
            status_code=503,
            detail={"error": "The Upstox broker order book is temporarily unavailable."},
        ) from None
