"""External GTT routes.

Three sub-contracts are exposed:

* ``/gtt/place``         – legacy single-GTT placement (``services.gtt_service``)
* ``/gtt/place-dual``    – NEW dual-leg placement (``place_dual_gtt_service``)
* ``/gtt/modify``        – NEW structured modify (``gtt_modify_service``)
* ``/gtt/cancel`` / ``/gtt/cancel/bulk`` / ``/gtt/cleanup``
                         – legacy cancel + broker-cleanup flows (unchanged)

All NEW endpoints use the structured failure contract from ``_common.py``.
"""

from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from api.external_routes._common import raise_from_service_result
from core.logger import get_logger
from models.gtt_models import CancelGTTRequest, CleanupGTTRequest, GTTOrderIDsRequest, PlaceGTTRequest
from sandbox_services.gtt_modify_service import modify_gtt_order
from sandbox_services.place_dual_gtt_service import place_dual_gtt_orders
from services.daily_profit_guard_service import DailyProfitGuardError
from services.gtt_service import GTTServiceError, cancel_gtt_order, cancel_gtt_orders, place_gtt_order
from services.orderflow_cleanup_service import OrderflowCleanupError, OrderflowCleanupService

logger = get_logger(__file__)

_cleanup_service = OrderflowCleanupService()

router = APIRouter(
    prefix="/gtt",
    tags=["external-gtt"],
    responses={500: {"description": "Internal GTT service error"}},
)


# ---------------------------------------------------------------------------
# Request models (only for the new endpoints; legacy ones live in models/)
# ---------------------------------------------------------------------------
class ModifyGTTRequest(BaseModel):
    """Request schema for modifying SL and/or Target on an existing GTT order."""

    model_config = ConfigDict(extra="allow")  # Allows body.api_token for require_external_api_auth

    gtt_order_id: str = Field(..., min_length=1, description="Existing Upstox GTT Order ID")
    sl_price: Any = Field(default=None, description="New Stop-Loss trigger price or offset")
    target_price: Any = Field(default=None, description="New Target trigger price or offset")
    is_offset: bool = Field(
        default=False,
        description="If True, computes SL/Target relative to existing ENTRY price.",
    )


class DualGTTOrderConfig(BaseModel):
    """Per-leg configuration for one of the two GTT orders in a dual placement."""

    sl: float = Field(..., description="SL offset from live LTP (or absolute if is_offset=False).")
    target: float = Field(..., description="Target offset from live LTP (or absolute if is_offset=False).")
    trail_sl: Any = Field(
        default=None,
        description="Trailing SL gap. Use only on leg #2; omit on leg #1 for a plain order.",
    )
    lot: Any = Field(default=None, ge=1, description="Per-leg lot override (defaults to request lot).")
    entry: Any = Field(
        default=None,
        description="Absolute entry (only when is_offset=False); otherwise live LTP is used.",
    )


class DualGTTRequest(BaseModel):
    """Request schema for placing TWO GTT orders at once (plain + trailing)."""

    model_config = ConfigDict(extra="allow")

    instrument_key: str = Field(..., min_length=1, description="NSE_FO|64473 or URL-encoded form")
    order_configs: list[DualGTTOrderConfig] = Field(
        ...,
        min_length=2,
        max_length=2,
        description="Exactly 2 configs: [leg #1 plain, leg #2 with trail_sl].",
    )
    lot: int = Field(default=1, ge=1)
    transaction_type: str = Field(default="BUY")
    product: str = Field(default="I")
    symbol: Any = Field(default=None, description="NIFTY / SENSEX / ... for lot-size lookup.")
    is_offset: bool = Field(default=True, description="If True, sl/target/trail are offsets from LTP.")
    lot_size_override: Any = Field(default=None, ge=1, description="Force a lot size.")


# ---------------------------------------------------------------------------
# Legacy single GTT placement (unchanged)
# ---------------------------------------------------------------------------
@router.post("/place")
def external_place_gtt(body: PlaceGTTRequest):
    """Legacy single-GTT placement via ``services.gtt_service.place_gtt_order``."""
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
            status_code=500,
            detail={"error": "Unexpected GTT service error."},
        ) from None


# ---------------------------------------------------------------------------
# NEW dual GTT placement (place_dual_gtt_service)
# ---------------------------------------------------------------------------
@router.post("/place-dual")
def external_place_dual_gtt(body: DualGTTRequest):
    """Place TWO GTT orders at once for the same instrument.

    * Leg #1 (order_configs[0]) — live-market ENTRY, SL, TARGET (no trailing).
    * Leg #2 (order_configs[1]) — live-market ENTRY, SL, TARGET, TRAILING SL.

    Success / partial
    -----------------
    * HTTP 200 + ``{status:"SUCCESS"}``   – both legs placed.
    * HTTP 207 + ``{status:"PARTIAL"}``   – one leg placed, one failed.
    * HTTP 4xx/5xx via ``raise_from_service_result`` when both legs fail or
      a group-level step (validation / LTP / client) fails.

    On HTTP 207, callers should inspect ``orders[i].step`` and ``orders[i].error``
    to decide per-leg retry vs. abandon.
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
        # Group-level failure → 4xx/5xx via helper.
        if result.get("status") == "FAILED":
            raise_from_service_result(result, action="place-dual-gtt")
        # Partial success → 207 with full detail.
        if result.get("status") == "PARTIAL":
            return JSONResponse(status_code=207, content=result)
        return result
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unexpected external dual GTT placement failure")
        raise HTTPException(
            status_code=500,
            detail={"error": "Unexpected dual GTT service error."},
        ) from None


# ---------------------------------------------------------------------------
# NEW modify GTT (gtt_modify_service)
# ---------------------------------------------------------------------------
@router.post("/modify")
def external_modify_gtt(body: ModifyGTTRequest):
    """Modify SL, Target, or both on an existing MULTIPLE GTT order.

    Uses ``gtt_modify_service.modify_gtt_order`` which returns the same
    structured ``{status, step, error, context}`` contract as the other
    sandbox services.
    """
    try:
        result = modify_gtt_order(
            gtt_order_id=body.gtt_order_id,
            sl_price=body.sl_price,
            target_price=body.target_price,
            is_offset=body.is_offset,
        )
        raise_from_service_result(result, action="modify-gtt")
        return result
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unexpected external GTT modification failure")
        raise HTTPException(
            status_code=500,
            detail={"error": "Unexpected GTT modification error."},
        ) from None


# ---------------------------------------------------------------------------
# Cancel / cleanup / bulk cancel (unchanged legacy flow)
# ---------------------------------------------------------------------------
@router.post("/cancel")
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
        raise HTTPException(
            status_code=500,
            detail={"error": "Unexpected GTT cancellation error."},
        ) from None


@router.post("/cleanup")
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
        raise HTTPException(
            status_code=502,
            detail={"error": "Unexpected broker cleanup error."},
        ) from None


@router.post("/cancel/bulk")
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
        raise HTTPException(
            status_code=500,
            detail={"error": "Unexpected bulk GTT cancellation error."},
        ) from None