# routers/history_router.py
"""
History / Chart API router.

Exposes endpoints that wrap `services.candle_service` so callers can
request candles either by:

    * instrument_key (direct)
    * strike + striketype (resolved via options_cache)

Default behaviour for /history/candles:
    * last 7 days of historical candles
    * plus today's intraday candles
    * merged, deduplicated, sorted chronologically

Integrate in main.py with:

    from routers.history_router import router as history_router
    app.include_router(history_router)
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query

# Standalone candle service (direct Upstox HistoryV3Api wrapper).
from services import candle_service as cs

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/candles",
    tags=["candles / Charts"],
)

# ------------------------------------------------------------
# DEFAULTS
# ------------------------------------------------------------
DEFAULT_DAYS = 7                 # last 7 days historical
DEFAULT_BATCH_DAYS = 7           # Upstox per-request max
DEFAULT_INTERVAL = "minutes"
DEFAULT_UNIT = "1"


# ============================================================
# INSTRUMENT KEY RESOLUTION (strike + striketype -> key)
# ============================================================
def _resolve_instrument_key(
    instrument_key: Optional[str],
    strike: Optional[float],
    striketype: Optional[str],
) -> str:
    """
    Resolve the final instrument_key.

    Priority:
        1. If instrument_key is provided -> use it.
        2. Else if strike + striketype provided -> look it up
           in the shared options_cache.
        3. Else -> 400 error.
    """
    if instrument_key:
        return instrument_key

    if strike is None or striketype is None:
        raise HTTPException(
            status_code=400,
            detail=(
                "Provide either 'instrument_key' OR "
                "both 'strike' and 'striketype'."
            ),
        )

    # Lazy import so this router does not force a hard dependency
    # on the cache module at import time.
    try:
        from services.option_service import options_cache  # correct module
    except Exception as exc:
        logger.exception("options_cache import failed: %s", exc)
        raise HTTPException(
            status_code=500,
            detail="options_cache is not available on the server.",
        )

    strike_norm = float(strike)
    type_norm = str(striketype).strip().upper()

    # Normalise CE/PE aliases
    if type_norm in ("CALL", "C"):
        type_norm = "CE"
    elif type_norm in ("PUT", "P"):
        type_norm = "PE"

    if type_norm not in ("CE", "PE"):
        raise HTTPException(
            status_code=400,
            detail="striketype must be one of: CE, PE, CALL, PUT.",
        )

    # options_cache may be:
    #   * a dict containing "data" / "instruments" / "options" (list or dict)
    #   * a flat dict keyed by instrument_key
    #   * a flat list of instrument dicts
    instruments: Any = None
    if isinstance(options_cache, dict):
        instruments = (
            options_cache.get("instruments")
            or options_cache.get("options")
            or options_cache.get("data")
            or options_cache
        )
    else:
        instruments = options_cache

    if not isinstance(instruments, (dict, list)):
        raise HTTPException(
            status_code=500,
            detail="options_cache is in an unexpected shape.",
        )

    # Normalise to (key, meta) pairs regardless of shape.
    if isinstance(instruments, dict):
        iterable = instruments.items()
    else:
        iterable = (
            (item.get("instrument_key"), item)
            for item in instruments
            if isinstance(item, dict)
        )

    for key, meta in iterable:
        if not isinstance(meta, dict):
            continue

        try:
            meta_strike = float(
                meta.get("strike")
                or meta.get("strike_price")
            )
        except (TypeError, ValueError):
            continue

        meta_type = str(
            meta.get("striketype")
            or meta.get("option_type")
            or meta.get("instrument_type")
            or meta.get("type")
            or ""
        ).strip().upper()

        if meta_type in ("CALL", "C"):
            meta_type = "CE"
        elif meta_type in ("PUT", "P"):
            meta_type = "PE"

        if meta_strike == strike_norm and meta_type == type_norm:
            return key or meta.get("instrument_key")

    raise HTTPException(
        status_code=404,
        detail=(
            f"No instrument found for strike={strike_norm} "
            f"striketype={type_norm}."
        ),
    )


# ============================================================
# 1) FULL CHART DATA  (historical + intraday)
# ============================================================
@router.get(
    "/candles",
    summary="Get candles for an instrument (history + intraday)",
    description=(
        "Returns merged candles for the given instrument.\n\n"
        "Default: last 7 days of historical candles PLUS today's "
        "intraday candles, deduplicated and sorted.\n\n"
        "You may pass either:\n"
        "  * instrument_key\n"
        "  * strike + striketype"
    ),
)
def get_candles(
    instrument_key: Optional[str] = Query(
        None,
        description="Upstox instrument key, e.g. NSE_FO|73905",
    ),
    strike: Optional[float] = Query(
        None, description="Option strike price, e.g. 23000"
    ),
    striketype: Optional[str] = Query(
        None, description="Option type: CE, PE, CALL, PUT"
    ),
    days: int = Query(
        DEFAULT_DAYS,
        ge=1,
        le=90,
        description="Historical days to fetch (default 7).",
    ),
    batch_days: int = Query(
        DEFAULT_BATCH_DAYS,
        ge=1,
        le=30,
        description="Batch size in days for historical fetching.",
    ),
    interval: str = Query(
        DEFAULT_INTERVAL,
        description="Upstox interval (minutes, hours, days...).",
    ),
    unit: str = Query(DEFAULT_UNIT, description="Interval unit, e.g. 1, 5, 15"),
    parallel: bool = Query(True, description="Fetch batches in parallel."),
    max_workers: Optional[int] = Query(
        None, ge=1, le=32, description="Override parallel worker count."
    ),
):
    key = _resolve_instrument_key(instrument_key, strike, striketype)

    try:
        historical = cs.fetch_historical_candles(
            instrument_key=key,
            days=days,
            interval=interval,
            unit=unit,
            batch_days=batch_days,
            parallel=parallel,
            max_workers=max_workers,
        )
        intraday = cs.fetch_intraday_candles(
            instrument_key=key,
            interval=interval,
            unit=unit,
        )
    except Exception as exc:
        logger.exception("candle fetch failed for %s: %s", key, exc)
        raise HTTPException(status_code=500, detail=str(exc))

    merged = cs.merge_candles(historical, intraday)
    candles = cs.convert_to_chart_data(merged)

    return {
        "instrument_key": key,
        "resolved_instrument_key": key,
        "strike": strike,
        "striketype": striketype,
        "candles": candles,
        "total_candles": len(candles),
        "history_days": days,
        "batch_days": batch_days,
    }


# ============================================================
# 2) HISTORICAL ONLY
# ============================================================
@router.get(
    "/historical",
    summary="Get historical candles only (no intraday)",
)
def get_historical(
    instrument_key: Optional[str] = Query(None),
    strike: Optional[float] = Query(None),
    striketype: Optional[str] = Query(None),
    days: int = Query(DEFAULT_DAYS, ge=1, le=365),
    batch_days: int = Query(DEFAULT_BATCH_DAYS, ge=1, le=30),
    interval: str = Query(DEFAULT_INTERVAL),
    unit: str = Query(DEFAULT_UNIT),
    end_date: Optional[date] = Query(
        None,
        description="Optional ISO date (YYYY-MM-DD) to end the historical range.",
    ),
    parallel: bool = Query(True),
    max_workers: Optional[int] = Query(None, ge=1, le=32),
):
    key = _resolve_instrument_key(instrument_key, strike, striketype)

    try:
        raw = cs.fetch_historical_candles(
            instrument_key=key,
            days=days,
            interval=interval,
            unit=unit,
            batch_days=batch_days,
            to_date=end_date,
            parallel=parallel,
            max_workers=max_workers,
        )
    except Exception as exc:
        logger.exception("fetch_historical_candles failed for %s: %s", key, exc)
        raise HTTPException(status_code=500, detail=str(exc))

    candles = cs.convert_to_chart_data(cs.merge_candles(raw, []))
    return {
        "instrument_key": key,
        "candles": candles,
        "total_candles": len(candles),
        "history_days": days,
        "batch_days": batch_days,
        "end_date": end_date.isoformat() if end_date else None,
    }


# ============================================================
# 3) INTRADAY ONLY
# ============================================================
@router.get(
    "/intraday",
    summary="Get today's intraday candles only",
)
def get_intraday(
    instrument_key: Optional[str] = Query(None),
    strike: Optional[float] = Query(None),
    striketype: Optional[str] = Query(None),
    interval: str = Query(DEFAULT_INTERVAL),
    unit: str = Query(DEFAULT_UNIT),
):
    key = _resolve_instrument_key(instrument_key, strike, striketype)

    try:
        raw = cs.fetch_intraday_candles(
            instrument_key=key,
            interval=interval,
            unit=unit,
        )
    except Exception as exc:
        logger.exception("fetch_intraday_candles failed for %s: %s", key, exc)
        raise HTTPException(status_code=500, detail=str(exc))

    candles = cs.convert_to_chart_data(raw)
    return {
        "instrument_key": key,
        "candles": candles,
        "total_candles": len(candles),
    }


# ============================================================
# 4) OLDER CHART DATA  (scroll-back)
# ============================================================
@router.get(
    "/older",
    summary="Load older historical candles (for scroll-back)",
    description=(
        "Returns the `days` chunk of historical candles immediately "
        "before `before_date`.\n\n"
        "Example: chart currently ends at 2026-09-03, frontend sends "
        "before_date=2026-09-03 and gets the previous 7-day window."
    ),
)
def get_older(
    before_date: date = Query(
        ...,
        description="ISO date (YYYY-MM-DD). Older candles end the day before this.",
    ),
    instrument_key: Optional[str] = Query(None),
    strike: Optional[float] = Query(None),
    striketype: Optional[str] = Query(None),
    days: int = Query(DEFAULT_DAYS, ge=1, le=90),
    batch_days: int = Query(DEFAULT_BATCH_DAYS, ge=1, le=30),
    interval: str = Query(DEFAULT_INTERVAL),
    unit: str = Query(DEFAULT_UNIT),
    parallel: bool = Query(True),
    max_workers: Optional[int] = Query(None, ge=1, le=32),
):
    key = _resolve_instrument_key(instrument_key, strike, striketype)

    # candle_service has no get_older_chart_data helper, so we
    # implement the scroll-back window inline using the primitives.
    try:
        end_date = before_date - cs.timedelta(days=1)
        raw = cs.fetch_historical_candles(
            instrument_key=key,
            days=days,
            interval=interval,
            unit=unit,
            batch_days=batch_days,
            to_date=end_date,
            parallel=parallel,
            max_workers=max_workers,
        )
    except Exception as exc:
        logger.exception("get_older_chart_data failed for %s: %s", key, exc)
        raise HTTPException(status_code=500, detail=str(exc))

    candles = cs.convert_to_chart_data(cs.merge_candles(raw, []))
    return {
        "instrument_key": key,
        "resolved_instrument_key": key,
        "before_date": before_date.isoformat(),
        "candles": candles,
        "total_candles": len(candles),
        "history_days": days,
        "batch_days": batch_days,
        "has_more": bool(candles),
    }


# ============================================================
# 5) CONVENIENCE: resolve strike -> instrument_key
# ============================================================
@router.get(
    "/resolve",
    summary="Resolve strike + striketype into an instrument_key",
)
def resolve(
    strike: float = Query(..., gt=0),
    striketype: str = Query(..., min_length=1),
):
    key = _resolve_instrument_key(None, strike, striketype)
    return {
        "strike": strike,
        "striketype": striketype,
        "instrument_key": key,
    }