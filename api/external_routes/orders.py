"""External order-mutation routes.

Groups every ``/api/external/v1/orders/*`` endpoint under one module so the
routing surface for regular orders is easy to audit.

Two order-placement endpoints coexist:

* ``POST /orders/place``         — legacy route using ``services.orders_service``
                                   (raises on error; wrapped by ``handle_api_exception``)
* ``POST /orders/place-service`` — NEW route using ``sandbox_services.place_order_service``
                                   (returns structured ``{status, step, error}`` payload;
                                    wrapped by ``raise_from_service_result``)

Both place MARKET / LIMIT / SL / SL-M orders – the new one adds first-class
exception tagging plus opt-in offset support.
"""

from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from api.external_routes._common import (
    handle_api_exception,
    raise_from_service_result,
)
from api.routes.orders import _raise_http, _service
from core.logger import get_logger
from models.order_models import ExitAllRequest, OrderIdsRequest, PlaceOrderRequest
from sandbox_services.place_order_service import place_order as place_order_service
from services.upstox_order_api import UpstoxOrderAPIError, get_order_book

logger = get_logger(__file__)

router = APIRouter(
    prefix="/orders",
    tags=["external-orders"],
    responses={500: {"description": "Internal order service error"}},
)


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------
class PlaceOrderServiceRequest(BaseModel):
    """Request schema for the new ``place_order_service``-backed endpoint.

    Every field maps 1:1 to ``sandbox_services.place_order_service.place_order``.
    Extra fields are allowed so the router-level ``require_external_api_auth``
    dependency can read ``body.api_token`` from the same payload.
    """

    model_config = ConfigDict(extra="allow")

    instrument_token: str = Field(..., min_length=1, description="NSE_FO|64473 or URL-encoded form")
    order_type: Optional[str] = Field(
        default=None,
        description="MARKET | LIMIT | SL | SL-M. Auto-detected from sl/entry when omitted.",
    )
    transaction_type: str = Field(default="BUY", description="BUY or SELL")
    product: str = Field(default="D", description="'D' delivery (default) or 'I' intraday")
    lot: int = Field(default=1, ge=1, description="Number of lots (default 1)")
    entry: Optional[float] = Field(
        default=None,
        description="Limit price, or offset from LTP when is_offset=True. Omit for market.",
    )
    sl: Optional[float] = Field(
        default=None,
        description="Stop-loss trigger, or offset from LTP when is_offset=True. Promotes order to SL / SL-M.",
    )
    sl_market: bool = Field(default=False, description="If sl is set, use SL-M instead of SL.")
    sl_limit: Optional[float] = Field(default=None, description="Explicit SL limit price.")
    symbol: Optional[str] = Field(default=None, description="NIFTY / SENSEX / ... for lot-size lookup.")
    is_offset: bool = Field(default=False, description="Treat entry/sl as offsets from live LTP.")
    lot_size_override: Optional[int] = Field(default=None, ge=1, description="Bypass the lot-size map.")
    validity: str = Field(default="DAY", description="DAY or IOC.")
    is_amo: bool = Field(default=False, description="After-market order.")
    tag: Optional[str] = Field(default=None, description="Upstox order tag (max 20 chars).")
    disclosed_quantity: int = Field(default=0, ge=0)
    slice_order: bool = Field(default=True, description="Enable Upstox order slicing (v3 default).")
    algo_name: Optional[str] = Field(default=None, description="Optional algo identifier forwarded to the broker.")


# ---------------------------------------------------------------------------
# Legacy route (services.orders_service) — kept for URL compatibility
# ---------------------------------------------------------------------------
@router.post("/place")
def external_place_order(body: PlaceOrderRequest):
    """Legacy single-order placement.

    Kept exactly as before – delegates to ``services.orders_service`` and
    surfaces its ``ApiException`` via ``_raise_http``.
    """
    try:
        return _service.place_order(body)
    except Exception as exc:
        _raise_http(exc, action="order")


# ---------------------------------------------------------------------------
# NEW route (sandbox_services.place_order_service)
# ---------------------------------------------------------------------------
@router.post("/place-service")
def external_place_order_service(body: PlaceOrderServiceRequest):
    """Place one MARKET / LIMIT / SL / SL-M order via ``place_order_service``.

    Behaviour highlights
    --------------------
    * If ``entry`` is omitted → live LTP is fetched as the reference.
    * If ``sl`` is supplied → the order is auto-promoted to SL (or SL-M if
      ``sl_market=True``).
    * With ``is_offset=True``, ``entry`` and ``sl`` are treated as offsets
      from live LTP (BUY: entry below, sl above; SELL: mirror).
    * Quantity = ``lot × resolved_lot_size``. Lot size resolves from
      ``symbol`` → exchange prefix (NSE_FO / BSE_FO / ...) → fallback 1.

    Failure contract
    ----------------
    The service always returns a dict. On failure:
    ``{"status": "FAILED", "step": "<step>", "error": {...}, "context": {...}}``
    ``raise_from_service_result`` converts that into the correct HTTP error
    (400 for validation, 502 for broker/network, 500 for internal).
    """
    try:
        result = place_order_service(
            instrument_token=body.instrument_token,
            order_type=body.order_type,
            transaction_type=body.transaction_type,
            product=body.product,
            lot=body.lot,
            entry=body.entry,
            sl=body.sl,
            sl_market=body.sl_market,
            sl_limit=body.sl_limit,
            symbol=body.symbol,
            is_offset=body.is_offset,
            lot_size_override=body.lot_size_override,
            validity=body.validity,
            is_amo=body.is_amo,
            tag=body.tag,
            disclosed_quantity=body.disclosed_quantity,
            slice_order=body.slice_order,
            algo_name=body.algo_name,
        )
        raise_from_service_result(result, action="place-order")
        return result
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unexpected external place-order-service failure")
        raise HTTPException(
            status_code=500,
            detail={"error": "Unexpected place-order-service error."},
        ) from None


# ---------------------------------------------------------------------------
# Cancel / exit / bulk / single routes (unchanged behaviour)
# ---------------------------------------------------------------------------
@router.post("/cancel/bulk")
def external_cancel_orders(body: OrderIdsRequest):
    try:
        return {"results": _service.cancel_orders(body.order_ids)}
    except Exception as exc:
        _raise_http(exc, action="cancel")


@router.post("/exit-by-ids")
def external_exit_orders(body: OrderIdsRequest):
    try:
        return {"results": _service.exit_orders(body.order_ids)}
    except Exception as exc:
        _raise_http(exc, action="exit")


@router.post("/exit-all")
def external_exit_all(body: ExitAllRequest = ExitAllRequest()):
    try:
        result = _service.exit_all_positions(tag=body.tag, segment=body.segment)
        if result.get("status") == "exit_all_partial_success":
            return JSONResponse(status_code=207, content=result)
        return result
    except Exception as exc:
        _raise_http(exc, action="exit")


@router.post("/{order_id}/cancel")
def external_cancel_single_order(order_id: str):
    try:
        return _service.cancel_order(order_id)
    except Exception as exc:
        _raise_http(exc, action="cancel")


@router.post("/{order_id}/exit")
def external_exit_single_order(order_id: str):
    try:
        return _service.exit_order(order_id)
    except Exception as exc:
        _raise_http(exc, action="exit")


# ---------------------------------------------------------------------------
# Order book
# ---------------------------------------------------------------------------
@router.post("/order-book")
def external_broker_order_book(body: dict):
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