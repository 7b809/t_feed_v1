# routes/ema_calc_routes.py
"""On-demand EMA-cross calculation routes.

    GET  /ema-calc/calc
        Query-based lookup. Accepts either:
            ?instrument_key=NSE_FO|51327
        or:
            ?underlying=NIFTY&strike=23500&type=CE

    GET  /ema-calc/calc/{underlying}/{strike}/{strike_type}
        Path-based lookup. Mirrors the /live-sr/calc URL pattern:
            /ema-calc/calc/nifty/23500/ce

    POST /ema-calc/calc/batch
        Batch selector resolution — up to 50 instruments in one call.

MODE — intraday by default
--------------------------
``only_today`` defaults to **True**. The endpoint behaves like an
"intraday EMA-cross" API: it returns only the crosses whose bar
timestamp falls on today's IST calendar date.

To include historical crosses, pass ``only_today=false`` explicitly.
The response then carries ``mode = "full"`` instead of ``"intraday"``.

Historical candles are still fetched in intraday mode — EMA(21) needs
~21 bars of warm-up before the first reliable cross, so the merged
window is always the full 11-day historical tail plus today's
intraday series. The filter runs on the OUTPUT, not the INPUT.

Common query parameters (all optional):

    fast_period      int, 1..200          default: config.EMA_FAST_PERIOD (9)
    slow_period      int, 2..400          default: config.EMA_SLOW_PERIOD (21)
    atr_period       int, 1..200          default: config.EMA_ATR_PERIOD (14)
    volume_lookback  int, 2..200          default: config.EMA_VOLUME_LOOKBACK (20)
    lookback_days    int, 1..30           default: config.EMA_CALC_DEFAULT_LOOKBACK_DAYS (11)
    only_today       bool                 default: TRUE
    only_valid       bool                 default: false  (only score >= EMA_MIN_VALIDATION_SCORE)
    include_candles  bool                 default: false  (include merged candle list)
    include_ema_series bool               default: false  (include the full EMA series)
    debug            bool                 default: false  (forces sample_structure on the
                                                          historical source even when candles
                                                          parse successfully)

Every endpoint returns HTTP 200 with a JSON body whose ``status`` field
carries the outcome:

    "ok"                — crosses computed
    "not_found"         — selector does not resolve
    "no_data"           — no candles from either source
    "insufficient_data" — fewer candles than the EMA warm-up
    "invalid_params"    — fast_period >= slow_period, or window > max_candles
    "error"             — unexpected exception during calc

The service (``services.ema_calc_service``) is STATELESS — it never
writes to disk and never reads ``ema_crosses.json`` /
``live_crosses.json``. It always fetches fresh data:

    1. Historical 1-min candles from ``data/<...>/candles.json``
    2. Today's intraday 1-min candles from Upstox HistoryV3Api
    3. Merged by minute (intraday wins)
    4. EMA(9) × EMA(21) crosses detected with the SAME 6 filters as
       the retired ``live_ema_engine``.

Results are deterministic for the same underlying data, so callers
can poll this endpoint safely.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query

from core.logger import get_logger
from services.ema_calc_service import ema_calc_service

logger = get_logger(__file__)

router = APIRouter(prefix="/ema-calc/calc", tags=["ema-calc"])

# --------------------------------------------------------------------------- #
# Shared plumbing                                                             #
# --------------------------------------------------------------------------- #
def _run_calc(
    instrument_key: str | None,
    underlying: str | None,
    strike: str | None,
    strike_type: str | None,
    fast_period: int | None,
    slow_period: int | None,
    atr_period: int | None,
    volume_lookback: int | None,
    lookback_days: int | None,
    only_today: bool,
    only_valid: bool,
    include_candles: bool,
    include_ema_series: bool,
    debug: bool,
) -> dict[str, Any]:
    return ema_calc_service.calculate(
        instrument_key=instrument_key,
        underlying=underlying,
        strike=strike,
        strike_type=strike_type,
        fast_period=fast_period,
        slow_period=slow_period,
        atr_period=atr_period,
        volume_lookback=volume_lookback,
        lookback_days=lookback_days,
        only_today=only_today,
        only_valid=only_valid,
        include_candles=include_candles,
        include_ema_series=include_ema_series,
        debug=debug,
    )

# --------------------------------------------------------------------------- #
# GET /ema-calc/calc  — query-based                                           #
# --------------------------------------------------------------------------- #
@router.get("")
async def calc_by_query(
    instrument_key: str | None = Query(default=None),
    underlying: str | None = Query(default=None),
    strike: str | None = Query(default=None),
    strike_type: str | None = Query(default=None, alias="type"),
    fast_period: int | None = Query(default=None, ge=1, le=200),
    slow_period: int | None = Query(default=None, ge=2, le=400),
    atr_period: int | None = Query(default=None, ge=1, le=200),
    volume_lookback: int | None = Query(default=None, ge=2, le=200),
    lookback_days: int | None = Query(default=None, ge=1, le=30),
    only_today: bool = Query(default=True),
    only_valid: bool = Query(default=False),
    include_candles: bool = Query(default=False),
    include_ema_series: bool = Query(default=False),
    debug: bool = Query(default=False),
) -> dict[str, Any]:
    if not instrument_key and not (underlying and strike and strike_type):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "selector_required",
                "message": (
                    "Pass either `instrument_key` or all three of "
                    "`underlying` + `strike` + `type`."
                ),
            },
        )
    return await asyncio.to_thread(
        _run_calc,
        instrument_key, underlying, strike, strike_type,
        fast_period, slow_period, atr_period, volume_lookback,
        lookback_days, only_today, only_valid,
        include_candles, include_ema_series, debug,
    )

# --------------------------------------------------------------------------- #
# GET /ema-calc/calc/{underlying}/{strike}/{strike_type}  — path-based        #
# --------------------------------------------------------------------------- #
@router.get("/{underlying}/{strike}/{strike_type}")
async def calc_by_path(
    underlying: str,
    strike: str,
    strike_type: str,
    fast_period: int | None = Query(default=None, ge=1, le=200),
    slow_period: int | None = Query(default=None, ge=2, le=400),
    atr_period: int | None = Query(default=None, ge=1, le=200),
    volume_lookback: int | None = Query(default=None, ge=2, le=200),
    lookback_days: int | None = Query(default=None, ge=1, le=30),
    only_today: bool = Query(default=True),
    only_valid: bool = Query(default=False),
    include_candles: bool = Query(default=False),
    include_ema_series: bool = Query(default=False),
    debug: bool = Query(default=False),
) -> dict[str, Any]:
    return await asyncio.to_thread(
        _run_calc,
        None, underlying, strike, strike_type,
        fast_period, slow_period, atr_period, volume_lookback,
        lookback_days, only_today, only_valid,
        include_candles, include_ema_series, debug,
    )

# --------------------------------------------------------------------------- #
# POST /ema-calc/calc/batch                                                   #
# --------------------------------------------------------------------------- #
@router.post("/batch")
async def calc_batch(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    selectors = payload.get("selectors") or []
    if not isinstance(selectors, list) or not selectors:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "selectors_required",
                "message": "`selectors` must be a non-empty list.",
            },
        )
    if len(selectors) > 50:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "too_many_selectors",
                "message": "Maximum 50 selectors per batch.",
            },
        )

    params_global = payload.get("params") or {}

    def _run() -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for sel in selectors:
            if not isinstance(sel, dict):
                results.append({
                    "status": "invalid_input",
                    "error": "each selector must be an object",
                })
                continue

            params_item = sel.get("params") or {}
            merged_params = {**params_global, **params_item}

            try:
                results.append(
                    ema_calc_service.calculate(
                        instrument_key=sel.get("instrument_key"),
                        underlying=sel.get("underlying"),
                        strike=sel.get("strike"),
                        strike_type=sel.get("type"),
                        fast_period=merged_params.get("fast_period"),
                        slow_period=merged_params.get("slow_period"),
                        atr_period=merged_params.get("atr_period"),
                        volume_lookback=merged_params.get("volume_lookback"),
                        lookback_days=merged_params.get("lookback_days"),
                        only_today=merged_params.get("only_today", True),
                        only_valid=merged_params.get("only_valid", False),
                        include_candles=merged_params.get("include_candles", False),
                        include_ema_series=merged_params.get("include_ema_series", False),
                        debug=merged_params.get("debug", False),
                    )
                )
            except Exception as exc:
                logger.exception(
                    "[ema-calc] batch item failed for selector=%s", sel
                )
                results.append({
                    "status": "error",
                    "error": f"{type(exc).__name__}: {exc}",
                    "selector": sel,
                })
        return results

    results = await asyncio.to_thread(_run)

    return {
        "count": len(results),
        "results": results,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }