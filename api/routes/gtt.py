"""GTT order placement endpoints."""
from fastapi import APIRouter, HTTPException

from core.logger import get_logger
from models.gtt_models import PlaceGTTRequest
from core.config import GTT_ENABLED, ORDERFLOW_CLEANUP_ENABLED, UPSTOX_SANDBOX
from services.gtt_service import GTTServiceError, LOT_SIZE_MAP, place_gtt_order
from services.daily_profit_guard_service import DailyProfitGuardError

logger = get_logger(__file__)
router = APIRouter(prefix="/api/v1/gtt", tags=["gtt"])


@router.get("/config")
def gtt_config():
    return {
        "supported": True,
        "enabled": GTT_ENABLED and not UPSTOX_SANDBOX,
        "orderflow_cleanup_enabled": ORDERFLOW_CLEANUP_ENABLED,
        "account_mode": "sandbox" if UPSTOX_SANDBOX else "live",
        "transaction_type": "BUY",
        "default_lot": 1,
        "lot_size_map": LOT_SIZE_MAP,
        "entry_optional": True,
        "target_required": True,
        "stoploss_required": True,
    }


@router.post("/place")
def place_gtt(request: PlaceGTTRequest):
    try:
        return place_gtt_order(
            instrument_key=request.instrument_key,
            entry=request.entry,
            target=request.target,
            stoploss=request.stoploss,
            lot=request.lot,
            lot_size=request.lot_size,
            trading_symbol=request.trading_symbol,
            product=request.product,
            transaction_type=request.transaction_type,
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
        logger.exception("Unexpected GTT placement failure")
        raise HTTPException(status_code=500, detail={"error": "Unexpected GTT service error."}) from None
