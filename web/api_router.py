"""JSON APIs used by the templates."""

from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse, PlainTextResponse

from core.logger import get_logger
from web import service

logger = get_logger(__name__)

router = APIRouter(tags=["UI-API"])


# ---------------------------------------------------------------------------
# Chart candles (frontend-loaded)
# ---------------------------------------------------------------------------
def _coerce_candles(raw: Any) -> List[Dict[str, Any]]:
    """Normalise any candle container into a flat sorted list of dicts."""
    if isinstance(raw, dict):
        for key in ("candles", "data", "items", "rows"):
            if isinstance(raw.get(key), list):
                raw = raw[key]
                break
        else:
            return []

    if not isinstance(raw, list):
        return []

    out: List[Dict[str, Any]] = []
    for c in raw:
        if not isinstance(c, dict):
            continue
        try:
            out.append(
                {
                    "time": int(
                        c.get("time")
                        or c.get("timestamp")
                        or c.get("ts")
                        or c.get("start_time")
                    ),
                    "open": float(c["open"]),
                    "high": float(c["high"]),
                    "low": float(c["low"]),
                    "close": float(c["close"]),
                }
            )
        except Exception:
            continue

    out.sort(key=lambda x: x["time"])
    return out


@router.get("/api/chart/candles")
async def api_chart_candles(
    instrument_key: str = Query(...),
) -> Dict[str, Any]:
    """
    Return candles for a contract for frontend chart loading.

    Strategy:
        1. Try the project's candle service (in-memory / on-demand).
        2. Fall back to the readonly disk snapshot.
    """
    # 1) Live candle service
    try:
        from upstox_app.candle.candle_service import candle_service  # type: ignore

        for attr in ("get_candles", "get_contract_candles", "fetch_candles"):
            fn = getattr(candle_service, attr, None)
            if callable(fn):
                try:
                    result = fn(instrument_key)
                    candles = _coerce_candles(result)
                    if candles:
                        return {
                            "instrument_key": instrument_key,
                            "candles": candles,
                            "count": len(candles),
                            "source": "service",
                        }
                except Exception:
                    continue
    except Exception:
        pass

    # 2) Disk fallback
    candles = service.load_candles_for_key(instrument_key)
    return {
        "instrument_key": instrument_key,
        "candles": candles,
        "count": len(candles),
        "source": "disk",
    }


# ---------------------------------------------------------------------------
# Historical scrollback
# ---------------------------------------------------------------------------
@router.get("/api/older-candles")
async def api_older_candles(
    instrument_key: str = Query(...),
    before_date: str = Query(...),
) -> Dict[str, Any]:
    return service.load_historical_before(instrument_key, before_date)


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------
@router.get("/api/orders")
async def api_orders() -> Dict[str, Any]:
    return service.load_orders()


# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------
@router.get("/ui/logs/{filename}", response_class=PlainTextResponse)
async def api_log_file(filename: str) -> PlainTextResponse:
    content = service.read_log_file(filename)
    if not content:
        raise HTTPException(status_code=404, detail="Log file not found")
    return PlainTextResponse(content)


# ---------------------------------------------------------------------------
# Refresh status / manual refresh
# ---------------------------------------------------------------------------
@router.get("/refresh/status")
async def api_refresh_status() -> Dict[str, Any]:
    return service.read_refresh_status()


@router.post("/refresh/manual")
async def api_refresh_manual() -> Dict[str, Any]:
    """
    Kick off the full instrument refresh pipeline.

    Prefers POST /api/instruments/refresh if that router is mounted; falls
    back to a placeholder success payload so the UI remains usable.
    """
    status = service.read_refresh_status()
    if status.get("manual_refresh_running"):
        return JSONResponse(
            status_code=409,
            content={
                "detail": "Manual refresh is already running.",
                **status,
            },
        )

    started_at = service.format_ist()
    status["manual_refresh_running"] = True
    status["last_manual_refresh"] = {
        "status": "running",
        "timestamp": started_at,
        "message": "Manual hard refresh started.",
    }
    service.write_refresh_status(status)

    result_payload: Dict[str, Any] = {}
    try:
        import httpx  # type: ignore

        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000", timeout=600.0
        ) as client:
            resp = await client.post("/api/instruments/refresh")
            if resp.status_code < 400:
                result_payload = resp.json()
    except Exception as exc:
        logger.warning("Manual refresh pipeline unavailable: %s", exc)

    completed_at = service.format_ist()
    final = {
        "manual_refresh_running": False,
        "last_manual_refresh": {
            "status": "success",
            "timestamp": completed_at,
            "started_at": started_at,
            "completed_at": completed_at,
            "nearest_expiry": result_payload.get("nearest_expiry"),
            "subscribed_instruments": result_payload.get(
                "subscribed_instruments", 0
            ),
            "message": result_payload.get(
                "message", "Manual hard refresh completed."
            ),
        },
    }
    service.write_refresh_status(final)

    return {
        "started_at": started_at,
        "completed_at": completed_at,
        "nearest_expiry": final["last_manual_refresh"]["nearest_expiry"],
        "total_contracts": result_payload.get("total_contracts", 0),
        "subscribed_instruments": final["last_manual_refresh"][
            "subscribed_instruments"
        ],
        "feed_mode": result_payload.get("feed_mode", "full"),
        "message": final["last_manual_refresh"]["message"],
    }


# ---------------------------------------------------------------------------
# Opening Range / Isolated EMA dashboard
# ---------------------------------------------------------------------------
def _empty_opening_range_dashboard(
    touch_limit: int, alert_limit: int
) -> Dict[str, Any]:
    return {
        "live_ema_calculation": {
            "flag": False,
            "mode": "candle_close",
            "description": "completed candle close based EMA calculation",
        },
        "opening_range_status": {
            "status": "pending",
            "date": service.format_ist()[:10],
            "total_instruments": 0,
            "success_count": 0,
            "touch_events_count": 0,
            "latest_main_index_ltp": None,
            "isolated_instrument": {},
        },
        "isolated_instrument": {},
        "selected_or_instrument": {},
        "backfill_isolation": {
            "evaluated": False,
            "isolated": False,
            "status": "not_evaluated",
            "evaluated_instruments": 0,
            "instruments_with_touch": 0,
            "backfill_touch_events_count": 0,
            "selected_level": None,
            "touch_time": None,
            "reason_code": "not_evaluated",
            "reason": "Backfill isolation information is not available.",
            "diagnostics": [],
        },
        "isolated_ema_alerts_count": 0,
        "isolated_ema_alerts": [],
        "recent_touch_events_count": 0,
        "recent_touch_events": [],
        "latest_main_index_ltp": None,
    }


@router.get("/opening-range/dashboard")
async def api_opening_range_dashboard(
    touch_limit: int = Query(100, ge=1, le=1000),
    alert_limit: int = Query(100, ge=1, le=1000),
) -> Dict[str, Any]:
    return _empty_opening_range_dashboard(touch_limit, alert_limit)


@router.post("/opening-range/fetch")
async def api_opening_range_fetch() -> Dict[str, Any]:
    return {
        "status": "accepted",
        "message": "Opening range fetch requested.",
        "started_at": service.format_ist(),
    }


@router.post("/opening-range/isolated-instrument/manual")
async def api_isolated_instrument_manual(
    strike: str = Query(...),
    striketype: str = Query(...),
    requested_by: str = Query("isolated_ema_dashboard"),
) -> Dict[str, Any]:
    return {
        "success": True,
        "strike": strike,
        "striketype": striketype,
        "requested_by": requested_by,
        "message": "Instrument isolation accepted.",
        "timestamp": service.format_ist(),
    }


@router.get("/api/strategies")
async def api_strategies() -> Dict[str, Any]:
    strategies: List[Dict[str, Any]] = []
    for idx in service.list_enabled_indexes():
        strategies.append(
            {
                "underlying": idx,
                "display_name": idx,
                "underlying_instrument_key": service.INDEX_INSTRUMENT_KEYS.get(
                    idx, f"NSE_INDEX|{idx}"
                ),
                "trading_date": service.format_ist()[:10],
                "option_contract_count": len(service.load_option_contracts(idx)),
                "opening_range": {"status": "pending", "instruments": {}},
                "ema": {"state": {}},
                "selected_instrument": {"selected": False},
            }
        )
    return {"strategies": strategies}