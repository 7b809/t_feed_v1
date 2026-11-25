"""Authenticated external order mutation API.

These routes are intentionally separate from the dashboard API so that
server-to-server credentials never need to be exposed to browser JavaScript.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from upstox_client.rest import ApiException

from api.routes.orders import _raise_http, _service
from core.external_api_auth import require_external_api_auth
from core.logger import get_logger
from models.gtt_models import CancelGTTRequest, CleanupGTTRequest, GTTOrderIDsRequest, PlaceGTTRequest
from models.order_models import ExitAllRequest, OrderIdsRequest, PlaceOrderRequest
from sandbox_services.gtt_modify_service import modify_gtt_order
from sandbox_services.place_dual_gtt_service import place_dual_gtt_orders   # NEW
from services.daily_profit_guard_service import DailyProfitGuardError, get_today_pnl_status
from services.gtt_service import GTTServiceError, cancel_gtt_order, cancel_gtt_orders, place_gtt_order
from services.orderflow_cleanup_service import OrderflowCleanupError, OrderflowCleanupService
from services.upstox_order_api import UpstoxOrderAPIError, get_order_book

logger = get_logger(__file__)
_cleanup_service = OrderflowCleanupService()
router = APIRouter(
    prefix="/api/external/v1",
    tags=["external-orders"],
    dependencies=[Depends(require_external_api_auth)],
)


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------
class ModifyGTTRequest(BaseModel):
    """Request schema for modifying SL and/or Target on an existing GTT order."""

    model_config = ConfigDict(extra="allow")  # Allows body.api_token for require_external_api_auth

    gtt_order_id: str = Field(..., min_length=1, description="Existing Upstox GTT Order ID")
    sl_price: Optional[float] = Field(default=None, description="New Stop-Loss trigger price or offset")
    target_price: Optional[float] = Field(default=None, description="New Target trigger price or offset")
    is_offset: bool = Field(
        default=False,
        description="If True, computes SL/Target relative to existing ENTRY price; if False, uses exact prices.",
    )


class DualGTTOrderConfig(BaseModel):
    """Per-leg configuration for one of the two GTT orders in a dual placement."""

    sl: float = Field(..., description="Stop-loss offset from live LTP (or absolute price if is_offset=False).")
    target: float = Field(..., description="Target offset from live LTP (or absolute price if is_offset=False).")
    trail_sl: Optional[float] = Field(
        default=None,
        description="Trailing SL gap. Use only on leg #2; omit on leg #1 for a plain (non-trailing) order.",
    )
    lot: Optional[int] = Field(default=None, ge=1, description="Per-leg lot override (defaults to request `lot`).")
    entry: Optional[float] = Field(
        default=None,
        description="Absolute entry price. Honoured only when is_offset=False; otherwise live LTP is used.",
    )


class DualGTTRequest(BaseModel):
    """Request schema for placing TWO GTT orders (one plain + one trailing) at once."""

    model_config = ConfigDict(extra="allow")  # Allows body.api_token for require_external_api_auth

    instrument_key: str = Field(
        ...,
        min_length=1,
        description="Instrument key/token (e.g. 'NSE_FO|64473' or URL-encoded 'NSE_FO%7C64473').",
    )
    order_configs: list[DualGTTOrderConfig] = Field(
        ...,
        min_length=2,
        max_length=2,
        description="Exactly 2 configs: [leg #1 plain, leg #2 with trail_sl].",
    )
    lot: int = Field(default=1, ge=1, description="Default lot count for both legs (default 1).")
    transaction_type: str = Field(default="BUY", description="BUY or SELL (default BUY).")
    product: str = Field(default="I", description="Product code: 'I' intraday, 'D' delivery (default 'I').")
    symbol: Optional[str] = Field(
        default=None,
        description="Underlying symbol (NIFTY, SENSEX, ...) for lot-size lookup. Falls back to exchange default.",
    )
    is_offset: bool = Field(
        default=True,
        description="If True, sl/target are offsets from live LTP. If False, they are absolute prices.",
    )
    lot_size_override: Optional[int] = Field(
        default=None,
        ge=1,
        description="Force a specific lot size, bypassing symbol/exchange defaults.",
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# NEW: Dual GTT placement (one plain + one trailing) at once
# ---------------------------------------------------------------------------
@router.post("/gtt/place-dual")
def external_place_dual_gtt(body: DualGTTRequest):
    """
    Place TWO GTT orders at once for the same instrument:

      * Leg #1 (order_configs[0]) — live-market ENTRY, given SL, given TARGET (no trailing).
      * Leg #2 (order_configs[1]) — live-market ENTRY, given SL, given TARGET, given TRAILING SL.

    Defaults: product='I' (intraday), lot=1, transaction_type='BUY',
    is_offset=True (sl/target/trail treated as offsets from live LTP),
    lot size resolved from `symbol` → exchange default → 1.
    """
    try:
        result = place_dual_gtt_orders(
            instrument_token=body.instrument_key,
            order_configs=[cfg.model_dump(exclude_none=True) for cfg in body.order_configs],
            lot=body.lot,
            transaction_type=body.transaction_type,
            product=body.product,
            symbol=body.symbol,
            is_offset=body.is_offset,
            lot_size_override=body.lot_size_override,
        )
        # One leg succeeded, one failed → surface as HTTP 207 with full detail.
        if result.get("status") == "PARTIAL":
            return JSONResponse(status_code=207, content=result)
        return result

    except ValueError as exc:
        # Validation / config errors raised by the service layer.
        raise HTTPException(
            status_code=400,
            detail={"error": str(exc)},
        ) from None
    except ApiException as exc:
        status_code = getattr(exc, "status", None) or 502
        detail: dict[str, Any] = {"error": getattr(exc, "reason", str(exc))}
        if getattr(exc, "body", None):
            detail["broker_error"] = exc.body
        raise HTTPException(status_code=status_code, detail=detail) from None
    except Exception:
        logger.exception("Unexpected external dual GTT placement failure")
        raise HTTPException(
            status_code=500,
            detail={"error": "Unexpected dual GTT service error."},
        ) from None


@router.post("/gtt/modify")
def external_modify_gtt(body: ModifyGTTRequest):
    """Modify Stop-Loss, Target, or both on an existing GTT order."""
    try:
        return modify_gtt_order(
            gtt_order_id=body.gtt_order_id,
            sl_price=body.sl_price,
            target_price=body.target_price,
            is_offset=body.is_offset,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": str(exc)},
        ) from None
    except ApiException as exc:
        status_code = getattr(exc, "status", None) or 502
        detail: dict[str, Any] = {"error": getattr(exc, "reason", str(exc))}
        if getattr(exc, "body", None):
            detail["broker_error"] = exc.body
        raise HTTPException(status_code=status_code, detail=detail) from None
    except Exception:
        logger.exception("Unexpected external GTT modification failure")
        raise HTTPException(
            status_code=500,
            detail={"error": "Unexpected GTT modification error."},
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