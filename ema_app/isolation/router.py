"""HTTP endpoints for the isolation layer."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from core.logger import get_logger
from ema_app.isolation.alert_storage import isolated_alert_storage
from ema_app.isolation.config import isolation_config
from ema_app.isolation.order_storage import isolated_order_storage
from ema_app.isolation.service import isolation_service
from ema_app.isolation.state import GLOBAL_SCOPE_KEY, isolation_store

logger = get_logger(__name__)

router = APIRouter(prefix="/ema-app/isolation", tags=["ema-app-isolation"])


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
def _today_ist_str() -> str:
    try:
        tz = ZoneInfo("Asia/Kolkata")
    except Exception:
        tz = None
    return (
        datetime.now(tz=tz) if tz else datetime.now()
    ).strftime("%Y-%m-%d")


def _jsonify_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(doc, dict):
        return {}
    out = dict(doc)
    if "_id" in out:
        out["_id"] = str(out["_id"])
    return out


def _scope_name() -> str:
    return "global" if isolation_config.scope_global else "per_index"


# ---------------------------------------------------------------------------
# Request bodies for manual operations
# ---------------------------------------------------------------------------
class ManualSelectRequest(BaseModel):
    """
    Target specification. Provide either `instrument_key` (direct) OR
    the triple (`underlying`, `strike`, `option_type`).
    """
    instrument_key: Optional[str] = Field(
        default=None,
        description="Full Upstox key, e.g. 'NSE_FO|40809'.",
    )
    underlying: Optional[str] = Field(
        default=None,
        description="Underlying index, e.g. 'NIFTY' or 'SENSEX'.",
    )
    strike: Optional[float] = Field(
        default=None,
        description="Strike value, e.g. 23500.",
    )
    option_type: Optional[str] = Field(
        default=None,
        description="'CE' or 'PE' (case-insensitive).",
    )

    reason: str = Field(
        default="manual_override",
        description="Free-text label stored on the winner and in history.",
    )


class ManualClearRequest(BaseModel):
    bucket_key: str = Field(
        ...,
        description=(
            "Bucket to clear: an index name (per-index scope), "
            "'__GLOBAL__' (global scope), or '*' to clear every bucket."
        ),
    )


class ManualLookupRequest(BaseModel):
    """Same resolution inputs as ManualSelectRequest, but no mutation."""
    instrument_key: Optional[str] = None
    underlying: Optional[str] = None
    strike: Optional[float] = None
    option_type: Optional[str] = None


# ===========================================================================
# Live isolation state (in-memory + disk)
# ===========================================================================
@router.get("/status")
async def status() -> Dict[str, Any]:
    all_keys = list(isolation_store.all_states().keys())
    indexes = [k for k in all_keys if k != GLOBAL_SCOPE_KEY]
    return {
        "running": isolation_service.is_running(),
        "session": isolation_store._session_date,
        "scope": _scope_name(),
        "state_keys": all_keys,
        "indexes": indexes,
    }


@router.get("/today")
async def today_all() -> Dict[str, Any]:
    states = isolation_store.all_states()
    return {
        "session_date": isolation_store._session_date,
        "scope": _scope_name(),
        "indexes": {
            name: state.to_dict() for name, state in states.items()
        },
    }


@router.get("/today/{index_name}")
async def today_one(index_name: str) -> Dict[str, Any]:
    state = isolation_store.get_state(index_name)
    if not state.index_name:
        raise HTTPException(status_code=404, detail="Index not tracked")
    return state.to_dict()


@router.get("/candidates/{index_name}")
async def candidates(index_name: str) -> Dict[str, Any]:
    state = isolation_store.get_state(index_name)
    if not state.index_name:
        raise HTTPException(status_code=404, detail="Index not tracked")
    return {
        "index_name": state.index_name,
        "count": len(state.candidates),
        "candidates": [c.to_dict() for c in state.candidates],
    }


# ===========================================================================
# Manual operations
# ===========================================================================
@router.post("/manual/select")
async def manual_select(payload: ManualSelectRequest) -> Dict[str, Any]:
    """
    Force a specific contract to be the isolated instrument.

    Accepted target forms (mutually exclusive):

        { "instrument_key": "NSE_FO|40809" }
        { "underlying": "NIFTY", "strike": 23500, "option_type": "CE" }

    The target bucket depends on the active scope:
        per-index -> the instrument's own underlying
        global    -> the global bucket

    A manual selection is sticky — the algorithm will not overwrite it
    until `POST /manual/clear` is called.

    Returns:
        {
          "success": bool,
          "bucket_key": str | None,
          "scope": "per_index" | "global",
          "isolated": {...} | null,
          "previous_instrument_key": str | None,
          "summary": str,
          "error": str | None,
          "error_code": str | None,
        }
    """
    result = isolation_service.manual_select(
        instrument_key=payload.instrument_key,
        underlying=payload.underlying,
        strike=payload.strike,
        option_type=payload.option_type,
        reason=payload.reason or "manual_override",
        actor="http",
    )
    if not result.get("success"):
        status_code = 400 if result.get("error_code") == "invalid" else 404
        raise HTTPException(
            status_code=status_code,
            detail=result.get("error") or "Manual select failed.",
        )
    return result


@router.post("/manual/clear")
async def manual_clear(payload: ManualClearRequest) -> Dict[str, Any]:
    """
    Clear the winner for `bucket_key`. Use `*` to clear every bucket.
    """
    result = isolation_service.manual_clear(payload.bucket_key)
    if not result.get("success"):
        raise HTTPException(
            status_code=400,
            detail=result.get("error") or "Manual clear failed.",
        )
    return result


@router.get("/manual/lookup")
async def manual_lookup(
    instrument_key: Optional[str] = Query(None),
    underlying: Optional[str] = Query(None),
    strike: Optional[float] = Query(None),
    option_type: Optional[str] = Query(None),
) -> Dict[str, Any]:
    """
    Resolve a target against the tracked metadata cache without mutating
    state. Useful for interactive UIs that need to validate inputs
    before calling /manual/select.
    """
    return isolation_service.manual_lookup(
        instrument_key=instrument_key,
        underlying=underlying,
        strike=strike,
        option_type=option_type,
    )


# ===========================================================================
# Persisted alerts (MongoDB collection `ema_isolated_alerts`)
# ===========================================================================
@router.get("/alerts/today")
async def alerts_today() -> Dict[str, Any]:
    today = _today_ist_str()
    collection = isolated_alert_storage._get_collection()
    if collection is None:
        return {"success": False, "error": "Alert storage unavailable."}

    try:
        doc = collection.find_one({"_id": today})
    except Exception as exc:
        logger.exception("alerts_today failed | err=%s", exc)
        return {"success": False, "error": f"{type(exc).__name__}: {exc}"}

    if not doc:
        return {"success": True, "document_id": today, "alerts": {}, "counts": {}}

    return {"success": True, **_jsonify_doc(doc)}


@router.get("/alerts/range")
async def alerts_range(
    from_date: str = Query(..., description="YYYY-MM-DD inclusive"),
    to_date: str = Query(..., description="YYYY-MM-DD inclusive"),
    include_alerts: bool = Query(False),
) -> Dict[str, Any]:
    collection = isolated_alert_storage._get_collection()
    if collection is None:
        return {"success": False, "error": "Alert storage unavailable."}

    projection = None if include_alerts else {"alerts": 0}

    try:
        cursor = collection.find(
            {"_id": {"$gte": from_date, "$lte": to_date}},
            projection,
        ).sort("_id", -1)
        days = [_jsonify_doc(doc) for doc in cursor]
    except Exception as exc:
        logger.exception("alerts_range failed | err=%s", exc)
        return {"success": False, "error": f"{type(exc).__name__}: {exc}"}

    return {
        "success": True,
        "from_date": from_date,
        "to_date": to_date,
        "count": len(days),
        "days": days,
    }


@router.get("/alerts/{date}")
async def alert_by_date(date: str) -> Dict[str, Any]:
    collection = isolated_alert_storage._get_collection()
    if collection is None:
        return {"success": False, "error": "Alert storage unavailable."}

    try:
        doc = collection.find_one({"_id": date})
    except Exception as exc:
        logger.exception("alert_by_date failed | err=%s", exc)
        return {"success": False, "error": f"{type(exc).__name__}: {exc}"}

    if not doc:
        raise HTTPException(status_code=404, detail=f"No alert document for {date}")

    return {"success": True, **_jsonify_doc(doc)}


# ===========================================================================
# Persisted orders (MongoDB collection `ema_isolated_orders`)
# ===========================================================================
@router.get("/orders/today")
async def orders_today() -> Dict[str, Any]:
    today = _today_ist_str()
    collection = isolated_order_storage._get_collection()
    if collection is None:
        return {"success": False, "error": "Order storage unavailable."}

    try:
        doc = collection.find_one({"_id": today})
    except Exception as exc:
        logger.exception("orders_today failed | err=%s", exc)
        return {"success": False, "error": f"{type(exc).__name__}: {exc}"}

    if not doc:
        return {"success": True, "document_id": today, "orders": {}, "counts": {}}

    return {"success": True, **_jsonify_doc(doc)}


@router.get("/orders/range")
async def orders_range(
    from_date: str = Query(..., description="YYYY-MM-DD inclusive"),
    to_date: str = Query(..., description="YYYY-MM-DD inclusive"),
    include_orders: bool = Query(False),
) -> Dict[str, Any]:
    collection = isolated_order_storage._get_collection()
    if collection is None:
        return {"success": False, "error": "Order storage unavailable."}

    projection = None if include_orders else {"orders": 0}

    try:
        cursor = collection.find(
            {"_id": {"$gte": from_date, "$lte": to_date}},
            projection,
        ).sort("_id", -1)
        days = [_jsonify_doc(doc) for doc in cursor]
    except Exception as exc:
        logger.exception("orders_range failed | err=%s", exc)
        return {"success": False, "error": f"{type(exc).__name__}: {exc}"}

    return {
        "success": True,
        "from_date": from_date,
        "to_date": to_date,
        "count": len(days),
        "days": days,
    }


@router.get("/orders/{date}")
async def order_by_date(date: str) -> Dict[str, Any]:
    collection = isolated_order_storage._get_collection()
    if collection is None:
        return {"success": False, "error": "Order storage unavailable."}

    try:
        doc = collection.find_one({"_id": date})
    except Exception as exc:
        logger.exception("order_by_date failed | err=%s", exc)
        return {"success": False, "error": f"{type(exc).__name__}: {exc}"}

    if not doc:
        raise HTTPException(status_code=404, detail=f"No order document for {date}")

    return {"success": True, **_jsonify_doc(doc)}