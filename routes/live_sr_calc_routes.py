# routes/live_sr_calc_routes.py
"""On-demand SR calculation routes.

    GET  /live-sr/calc
        Query-based lookup. Accepts either:
            ?instrument_key=NSE_FO|51327
        or:
            ?underlying=NIFTY&strike=23500&type=CE

    GET  /live-sr/calc/{underlying}/{strike}/{strike_type}
        Path-based lookup. Mirrors the requirement's URL pattern:
            /live-sr/calc/nifty/23500/ce

    POST /live-sr/calc/batch
        Batch selector resolution — up to 50 instruments in one call.

MODE — intraday by default
--------------------------
``only_today`` defaults to **True**. The endpoint behaves like an
"intraday SR" API: it returns only the levels whose ``confirmed_time``
falls on today's IST calendar date. Levels from prior sessions are
**not** included.

To include historical levels, pass ``only_today=false`` explicitly.
The response then carries ``mode = "full"`` instead of ``"intraday"``.

Historical candles are still fetched in intraday mode — a pivot that
forms at 09:15 needs 15 bars of context before it, and those come from
yesterday's session. The filter runs on the OUTPUT, not the INPUT.

OPT-IN live_sr block
--------------------
``include_live_sr`` defaults to **False**. When set to ``true``, the
endpoint ALSO reads the persisted ``data/<...>/live_sr_levels.json``
(maintained by ``live_sr_levels_service``) and attaches it to the
response under a new top-level ``live_sr`` key.

The persisted levels are **NOT merged into ``levels``**:

    * ``levels`` stays pure — always recomputed from fresh historical
      + intraday data on every call.
    * ``live_sr`` is the running engine's actual persisted state for
      today.

Both ``levels`` and ``live_sr.levels`` are returned **sorted by price
ascending** (supports and resistances intermixed, matching TradingView).
Neither carries any "broken" / "intact" fields.

Common query parameters (all optional):

    left_bars        int, 1..100          default: config.LIVE_SR_LEFT_BARS
    right_bars       int, 1..100          default: config.LIVE_SR_RIGHT_BARS
    lookback_days    int, 1..30           default: config.LIVE_SR_CALC_DEFAULT_LOOKBACK_DAYS
    only_today       bool                 default: TRUE   <-- intraday by default
    include_candles  bool                 default: false  (include merged candle list)
    include_live_sr  bool                 default: FALSE  <-- opt-in live_sr block
    debug            bool                 default: false
                                          forces sample_structure on the
                                          historical source even when
                                          candles parse successfully.

Every endpoint returns HTTP 200 with a JSON body whose ``status`` field
carries the outcome:

    "ok"                — levels computed
    "not_found"         — selector does not resolve
    "no_data"           — no candles from either source
    "insufficient_data" — fewer candles than left + right + 1
    "invalid_params"    — left/right exceed LIVE_SR_MAX_CANDLES
    "error"             — unexpected exception during calc

The ``mode`` field in every "ok"/"no_data"/"insufficient_data" response
tells the caller which filter was applied: ``"intraday"`` (default) or
``"full"``.

The ``live_sr`` field is ``null`` when ``include_live_sr`` is false. When
true, it carries:

    path             — resolved live_sr_levels.json path (or null)
    available        — true when the file exists and parsed
    today_iso        — today's IST date (for comparison)
    date             — the file's own "date" field
    is_today         — true when the file's date == today_iso
    candle_count     — the file's own "candle_count" field
    level_count      — number of levels persisted
    counts           — {resistance, support}
    first_level_time — earliest pivot_time
    last_level_time  — latest pivot_time
    last_candle      — the file's own "last_candle" object
    updated_at       — the file's own "updated_at" field
    levels           — the raw persisted level records (sorted by price)
    error            — "file_missing" / "unreadable: ..." / null

The underlying service (``services.live_sr_calc_service``) is
STATELESS — it never reads ``data/<...>/live_sr_levels.json`` unless
``include_live_sr=true`` and never writes to disk. It always fetches
fresh data:

    1. Historical 1-min candles from ``data/<...>/candles.json``
    2. Today's intraday 1-min candles from Upstox HistoryV3Api
    3. (OPTIONAL) ``data/<...>/live_sr_levels.json`` when
       ``include_live_sr=true`` — attached under ``live_sr``.

Results are deterministic for the same underlying data, so callers can
poll this endpoint safely.

Diagnostics
-----------
Every response includes ``sources.historical`` with:

    path                       — resolved file path (or null)
    candles                    — entries that survived normalisation
    candles_raw                — entries in the file
    candles_rejected_format    — entries the parser couldn't normalise
    candles_rejected_cutoff    — entries filtered by lookback_days
    declared_count             — the file's own "count" field, if present
    count_mismatch             — true when declared_count != candles_raw
    file_updated_at            — the file's own "updated_at" field
    relative_path              — the file's own "relative_path" field
    sample_structure           — shapes of the first 3 raw entries
                                 (present when parsing failed OR debug=true)
    error                      — human-readable reason on failure

The top-level ``warnings`` array flags intraday-mode filtering and any
``live_sr`` anomalies (file missing, stale date, engine disabled).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query

from core.logger import get_logger
from services.live_sr_calc_service import live_sr_calc_service

logger = get_logger(__file__)

router = APIRouter(prefix="/live-sr/calc", tags=["live-sr-calc"])

# --------------------------------------------------------------------------- #
# Shared plumbing                                                             #
# --------------------------------------------------------------------------- #
def _run_calc(
    instrument_key: str | None,
    underlying: str | None,
    strike: str | None,
    strike_type: str | None,
    left_bars: int | None,
    right_bars: int | None,
    lookback_days: int | None,
    only_today: bool,
    include_candles: bool,
    include_live_sr: bool,
    debug: bool,
) -> dict[str, Any]:
    return live_sr_calc_service.calculate(
        instrument_key=instrument_key,
        underlying=underlying,
        strike=strike,
        strike_type=strike_type,
        left_bars=left_bars,
        right_bars=right_bars,
        lookback_days=lookback_days,
        only_today=only_today,
        include_candles=include_candles,
        include_live_sr=include_live_sr,
        debug=debug,
    )

# --------------------------------------------------------------------------- #
# GET /live-sr/calc  — query-based                                            #
# --------------------------------------------------------------------------- #
@router.get("")
async def calc_by_query(
    instrument_key: str | None = Query(default=None),
    underlying: str | None = Query(default=None),
    strike: str | None = Query(default=None),
    strike_type: str | None = Query(default=None, alias="type"),
    left_bars: int | None = Query(default=None, ge=1, le=100),
    right_bars: int | None = Query(default=None, ge=1, le=100),
    lookback_days: int | None = Query(default=None, ge=1, le=30),
    only_today: bool = Query(default=True),        # <-- intraday by default
    include_candles: bool = Query(default=False),
    include_live_sr: bool = Query(default=False),  # <-- opt-in live_sr block
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
        instrument_key,
        underlying,
        strike,
        strike_type,
        left_bars,
        right_bars,
        lookback_days,
        only_today,
        include_candles,
        include_live_sr,
        debug,
    )

# --------------------------------------------------------------------------- #
# GET /live-sr/calc/{underlying}/{strike}/{strike_type}  — path-based         #
# --------------------------------------------------------------------------- #
@router.get("/{underlying}/{strike}/{strike_type}")
async def calc_by_path(
    underlying: str,
    strike: str,
    strike_type: str,
    left_bars: int | None = Query(default=None, ge=1, le=100),
    right_bars: int | None = Query(default=None, ge=1, le=100),
    lookback_days: int | None = Query(default=None, ge=1, le=30),
    only_today: bool = Query(default=True),        # <-- intraday by default
    include_candles: bool = Query(default=False),
    include_live_sr: bool = Query(default=False),  # <-- opt-in live_sr block
    debug: bool = Query(default=False),
) -> dict[str, Any]:
    return await asyncio.to_thread(
        _run_calc,
        None,
        underlying,
        strike,
        strike_type,
        left_bars,
        right_bars,
        lookback_days,
        only_today,
        include_candles,
        include_live_sr,
        debug,
    )

# --------------------------------------------------------------------------- #
# POST /live-sr/calc/batch                                                    #
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
                    live_sr_calc_service.calculate(
                        instrument_key=sel.get("instrument_key"),
                        underlying=sel.get("underlying"),
                        strike=sel.get("strike"),
                        strike_type=sel.get("type"),
                        left_bars=merged_params.get("left_bars"),
                        right_bars=merged_params.get("right_bars"),
                        lookback_days=merged_params.get("lookback_days"),
                        only_today=merged_params.get("only_today", True),
                        include_candles=merged_params.get("include_candles", False),
                        include_live_sr=merged_params.get("include_live_sr", False),
                        debug=merged_params.get("debug", False),
                    )
                )
            except Exception as exc:
                logger.exception(
                    "[live-sr-calc] batch item failed for selector=%s", sel
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