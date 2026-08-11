from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.concurrency import run_in_threadpool

from core import config
from core.logger import get_logger
from services.option_service import options_cache
from services.opening_range_service import (
    calculate_opening_range_for_all_subscribed,
    calculate_opening_range_for_instrument,
    flush_pending_touch_alerts,
    get_opening_range_status,
    get_opening_range_cache,
    get_opening_range_for_instrument_from_cache,
    get_opening_range_touch_events,
    get_opening_range_pending_touch_events,
    get_selected_or_instrument_state,
    get_selected_or_ema_alerts,
    get_opening_range_levels_for_ema_event,
    get_or_ema_strategy_status,
    get_or_ema_strategy_cache,
)

logger = get_logger(__file__)

router = APIRouter()


# ============================================================
# Instrument Resolution Helper
# ============================================================


def resolve_opening_range_instrument_key(
    instrument_key: str | None = None,
    strike: float | None = None,
    striketype: str | None = None,
) -> str:
    """
    Resolves instrument key from either:

    1. Direct instrument_key
       Example:
           NSE_FO|41012
           NSE_INDEX|Nifty 50

    2. strike + striketype
       Example:
           strike=24500, striketype=ce

    Uses options_cache["data"] loaded by option_service.
    """

    if instrument_key:
        return instrument_key

    if strike is None or not striketype:
        raise HTTPException(
            status_code=400,
            detail=(
                "Provide either instrument_key or both strike and striketype. "
                "Example: /opening-range/instrument?strike=24500&striketype=ce"
            ),
        )

    option_type = str(striketype).upper()

    if option_type not in ["CE", "PE"]:
        raise HTTPException(
            status_code=400,
            detail="Invalid striketype. Allowed values: ce, pe",
        )

    try:
        target_strike = float(strike)
    except Exception:
        raise HTTPException(
            status_code=400,
            detail="Invalid strike value.",
        )

    cache_data = options_cache.get("data", [])

    for item in cache_data:
        item_strike = item.get("strike_price")
        item_type = str(item.get("instrument_type", "")).upper()

        try:
            if float(item_strike) == target_strike and item_type == option_type:
                resolved_key = item.get("instrument_key")

                if resolved_key:
                    return resolved_key

        except Exception:
            continue

    raise HTTPException(
        status_code=404,
        detail=(
            f"No option instrument found for strike={target_strike}, "
            f"striketype={option_type}. Make sure option contracts are loaded."
        ),
    )


# ============================================================
# Opening Range Routes
# ============================================================


@router.get("/opening-range/status")
async def get_opening_range_latest_status():
    """
    Returns latest opening range calculation status.

    Notes:
    - Opening Range is calculated from Upstox intraday candles.
    - Default scheduled fetch time is 09:18 AM Asia/Kolkata.
    - Default opening range candle is 09:15 to 09:16.
    - Backfill touch scan checks candles after OR completion.
    - Live touch monitoring can track S2/S3/R2/R3 touches for option instruments.
    - Old selected OR instrument flow is disabled.
    - New OR + EMA strategy selection rule:
        1. First touched instrument is selected.
        2. If multiple instruments touch at the same timestamp/candle,
           nearest strike to current NIFTY spot is selected.
        3. Only selected touched instrument waits for EMA confirmation.
        4. Non-selected touched instruments are retained for debug only.
    """

    strategy_status = get_or_ema_strategy_status()

    return {
        "status": "success",
        "opening_range_status": get_opening_range_status(),
        "selected_or_instrument": get_selected_or_instrument_state(),
        "or_ema_strategy_status": strategy_status,
        "strategy_selection": {
            "mode": "first_touch_same_time_nearest_to_nifty",
            "description": (
                "First touched eligible instrument is selected. If multiple eligible "
                "instruments touch configured levels at the same timestamp/candle, "
                "the instrument whose strike is nearest to current NIFTY spot is selected. "
                "Only the selected instrument can trigger the OR + EMA Telegram alert."
            ),
            "selected_touch": strategy_status.get("selected_touch"),
            "selected_touch_key": strategy_status.get("selected_touch_key"),
            "selected_instrument_key": strategy_status.get("selected_instrument_key"),
            "selected_level": strategy_status.get("selected_level"),
            "selected_reason": strategy_status.get("selected_reason"),
            "selected_at": strategy_status.get("selected_at"),
        },
        "new_flow": {
            "description": (
                "Opening Range levels are calculated for all subscribed instruments. "
                "EMA crossover events are broadcast through WebSocket with that "
                "instrument's Opening Range levels when available. The legacy selected "
                "OR flow is disabled. The new OR + EMA strategy uses a selected touch "
                "model where first touch wins, and same-time multiple touches are resolved "
                "by nearest strike to NIFTY spot."
            ),
            "selected_or_flow": "disabled",
            "selected_or_ema_telegram_alerts": "disabled",
            "or_ema_strategy_selection": "first_touch_same_time_nearest_to_nifty",
            "ema_websocket_opening_range_enrichment": getattr(
                config,
                "EMA_CROSS_INCLUDE_OPENING_RANGE_LEVELS",
                True,
            ),
            "or_ema_strategy_alert_enabled": getattr(
                config,
                "OR_EMA_STRATEGY_ALERT_ENABLED",
                True,
            ),
        },
        "config": {
            "enabled": getattr(config, "OPENING_RANGE_ENABLED", True),
            "interval": getattr(config, "OPENING_RANGE_INTERVAL", "1minute"),
            "candle_count": getattr(config, "OPENING_RANGE_CANDLE_COUNT", 1),
            "market_open_hour": getattr(
                config,
                "OPENING_RANGE_MARKET_OPEN_HOUR",
                9,
            ),
            "market_open_minute": getattr(
                config,
                "OPENING_RANGE_MARKET_OPEN_MINUTE",
                15,
            ),
            "fetch_hour": getattr(config, "OPENING_RANGE_FETCH_HOUR", 9),
            "fetch_minute": getattr(config, "OPENING_RANGE_FETCH_MINUTE", 18),
            "backfill_touch_scan_enabled": getattr(
                config,
                "OPENING_RANGE_BACKFILL_TOUCH_SCAN_ENABLED",
                True,
            ),
            "touch_alert_enabled": getattr(
                config,
                "OPENING_RANGE_TOUCH_ALERT_ENABLED",
                True,
            ),
            "live_touch_alert_enabled": getattr(
                config,
                "OPENING_RANGE_LIVE_TOUCH_ALERT_ENABLED",
                True,
            ),
            "first_touch_selection_enabled": getattr(
                config,
                "OPENING_RANGE_FIRST_TOUCH_SELECTION_ENABLED",
                False,
            ),
            "first_touch_selection_source": getattr(
                config,
                "OPENING_RANGE_FIRST_TOUCH_SELECTION_SOURCE",
                "disabled",
            ),
            "selected_or_touch_notify_enabled": getattr(
                config,
                "OPENING_RANGE_SELECTED_OR_TOUCH_NOTIFY_ENABLED",
                False,
            ),
            "selected_or_ema_alert_enabled": getattr(
                config,
                "OPENING_RANGE_SELECTED_OR_EMA_ALERT_ENABLED",
                False,
            ),
            "legacy_touch_telegram_enabled": getattr(
                config,
                "OPENING_RANGE_LEGACY_TOUCH_TELEGRAM_ENABLED",
                False,
            ),
            "ema_cross_include_opening_range_levels": getattr(
                config,
                "EMA_CROSS_INCLUDE_OPENING_RANGE_LEVELS",
                True,
            ),
            "ema_cross_broadcast_without_opening_range": getattr(
                config,
                "EMA_CROSS_BROADCAST_WITHOUT_OPENING_RANGE",
                True,
            ),
            "or_ema_strategy_alert_enabled": getattr(
                config,
                "OR_EMA_STRATEGY_ALERT_ENABLED",
                True,
            ),
            "or_ema_strategy_strike_window_points": getattr(
                config,
                "OR_EMA_STRATEGY_STRIKE_WINDOW_POINTS",
                500,
            ),
            "or_ema_strategy_touch_levels": getattr(
                config,
                "OR_EMA_STRATEGY_TOUCH_LEVELS",
                ["S2", "S3", "R2", "R3"],
            ),
            "or_ema_strategy_touch_check_mode": getattr(
                config,
                "OR_EMA_STRATEGY_TOUCH_CHECK_MODE",
                "high_low",
            ),
            "or_ema_strategy_selection_mode": getattr(
                config,
                "OR_EMA_STRATEGY_SELECTION_MODE",
                "first_touch_same_time_nearest",
            ),
            "or_ema_strategy_lock_after_first_selection": getattr(
                config,
                "OR_EMA_STRATEGY_LOCK_AFTER_FIRST_SELECTION",
                True,
            ),
            "or_ema_strategy_nearest_strike_count": getattr(
                config,
                "OR_EMA_STRATEGY_NEAREST_STRIKE_COUNT",
                3,
            ),
            "or_ema_strategy_bullish_option_type": getattr(
                config,
                "OR_EMA_STRATEGY_BULLISH_OPTION_TYPE",
                "CE",
            ),
            "or_ema_strategy_bullish_strike_mode": getattr(
                config,
                "OR_EMA_STRATEGY_BULLISH_STRIKE_MODE",
                "equal_or_below",
            ),
            "or_ema_strategy_bearish_option_type": getattr(
                config,
                "OR_EMA_STRATEGY_BEARISH_OPTION_TYPE",
                "PE",
            ),
            "or_ema_strategy_bearish_strike_mode": getattr(
                config,
                "OR_EMA_STRATEGY_BEARISH_STRIKE_MODE",
                "equal_or_above",
            ),
            "output_file": getattr(
                config,
                "OPENING_RANGE_OUTPUT_FILE",
                "data/opening_range_results.json",
            ),
        },
    }


@router.post("/opening-range/fetch")
async def trigger_opening_range_fetch(
    candle_count: int = Query(
        default=None,
        ge=1,
        le=60,
        description=(
            "Number of candles from market open to use. "
            "Default comes from config.OPENING_RANGE_CANDLE_COUNT."
        ),
    ),
    save_results: bool = Query(
        default=None,
        description=(
            "Whether to save opening range results to file. "
            "Default comes from config.OPENING_RANGE_SAVE_FILE."
        ),
    ),
    max_workers: int = Query(
        default=None,
        ge=1,
        le=25,
        description=(
            "Parallel worker count for instrument-level opening range fetch. "
            "Default comes from config.OPENING_RANGE_MAX_WORKERS."
        ),
    ),
):
    """
    Manually triggers opening range calculation for all subscribed instruments.

    Behavior:
    - Fetches today's intraday candles using Upstox HistoryV3Api.
    - Selects first N candles from market open.
    - Calculates open, high, low, close, average.
    - Calculates R1/S1, R2/S2, R3/S3.
    - Scans post-OR candles for already touched S2/S3/R2/R3.
    - Stores results in memory for every subscribed instrument.
    - Builds OR + EMA strategy universe using NIFTY OR average +/- configured points.
    - Applies strategy selection rule:
        first touch wins; same-time touches use nearest strike to NIFTY.
    - Saves to data/opening_range_results.json if enabled.
    """

    selected_candle_count = candle_count or getattr(
        config,
        "OPENING_RANGE_CANDLE_COUNT",
        1,
    )

    selected_save_results = (
        bool(save_results)
        if save_results is not None
        else bool(getattr(config, "OPENING_RANGE_SAVE_FILE", True))
    )

    selected_max_workers = max_workers or getattr(
        config,
        "OPENING_RANGE_MAX_WORKERS",
        5,
    )

    logger.info(
        f"Manual opening range fetch requested. "
        f"candle_count={selected_candle_count}, "
        f"save_results={selected_save_results}, "
        f"max_workers={selected_max_workers}"
    )

    try:
        summary = await run_in_threadpool(
            calculate_opening_range_for_all_subscribed,
            candle_count=selected_candle_count,
            save_data=selected_save_results,
            max_workers=selected_max_workers,
        )

        return {
            "status": "success",
            "message": (
                "Opening range intraday candles fetched, levels calculated, "
                "touch scan completed, strategy universe updated, and selected "
                "touch rule applied."
            ),
            "opening_range_results_saved": selected_save_results,
            "selected_or_instrument": get_selected_or_instrument_state(),
            "ema_websocket_opening_range_enrichment": getattr(
                config,
                "EMA_CROSS_INCLUDE_OPENING_RANGE_LEVELS",
                True,
            ),
            "or_ema_strategy_status": get_or_ema_strategy_status(),
            "summary": summary,
        }

    except Exception as ex:
        error_message = f"{type(ex).__name__}: {ex}"

        logger.error(f"Manual opening range fetch failed: {error_message}")

        raise HTTPException(
            status_code=500,
            detail=f"Opening range fetch failed: {error_message}",
        )


@router.get("/opening-range/cache")
async def get_opening_range_full_cache():
    """
    Returns full opening range cache.

    Warning:
    - This can be large because it includes results for all instruments.
    - It may include touch events and per-instrument touch status.
    - It may include OR + EMA strategy cache snapshots.
    - Selected OR instrument state is retained only as disabled compatibility data.
    """

    return {
        "status": "success",
        "cache": get_opening_range_cache(),
    }


@router.get("/opening-range/instrument")
async def get_opening_range_instrument(
    instrument_key: str = Query(
        default=None,
        description="Instrument key, e.g. NSE_INDEX|Nifty 50 or NSE_FO|41012",
    ),
    strike: float = Query(
        default=None,
        description=(
            "Option strike price, e.g. 24500. "
            "Optional if instrument_key is provided."
        ),
    ),
    striketype: str = Query(
        default=None,
        description="Option type: ce or pe. Optional if instrument_key is provided.",
    ),
):
    """
    Returns opening range result for one instrument from memory cache.
    """

    resolved_instrument_key = resolve_opening_range_instrument_key(
        instrument_key=instrument_key,
        strike=strike,
        striketype=striketype,
    )

    result = get_opening_range_for_instrument_from_cache(
        resolved_instrument_key,
    )

    if not result:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Opening range result not found for "
                f"instrument_key={resolved_instrument_key}. "
                "Run /opening-range/fetch first or wait for scheduled 09:18 job."
            ),
        )

    return {
        "status": "success",
        "instrument_key": resolved_instrument_key,
        "input": {
            "instrument_key": instrument_key,
            "strike": strike,
            "striketype": striketype,
        },
        "result": result,
    }


@router.get("/opening-range/ema-context")
async def get_opening_range_ema_context(
    instrument_key: str = Query(
        default=None,
        description="Instrument key, e.g. NSE_FO|41012 or NSE_INDEX|Nifty 50",
    ),
    strike: float = Query(
        default=None,
        description="Option strike price, e.g. 24500. Optional if instrument_key is provided.",
    ),
    striketype: str = Query(
        default=None,
        description="Option type: ce or pe. Optional if instrument_key is provided.",
    ),
):
    """
    Returns the Opening Range context that will be attached to an EMA crossover
    WebSocket event for the given instrument.
    """

    resolved_instrument_key = resolve_opening_range_instrument_key(
        instrument_key=instrument_key,
        strike=strike,
        striketype=striketype,
    )

    context = get_opening_range_levels_for_ema_event(resolved_instrument_key)

    return {
        "status": "success",
        "instrument_key": resolved_instrument_key,
        "input": {
            "instrument_key": instrument_key,
            "strike": strike,
            "striketype": striketype,
        },
        "opening_range": context,
    }


@router.post("/opening-range/instrument/fetch")
async def fetch_opening_range_for_single_instrument(
    instrument_key: str = Query(
        default=None,
        description="Instrument key, e.g. NSE_INDEX|Nifty 50 or NSE_FO|41012",
    ),
    strike: float = Query(
        default=None,
        description=(
            "Option strike price, e.g. 24500. "
            "Optional if instrument_key is provided."
        ),
    ),
    striketype: str = Query(
        default=None,
        description="Option type: ce or pe. Optional if instrument_key is provided.",
    ),
    candle_count: int = Query(
        default=None,
        ge=1,
        le=60,
        description=(
            "Number of candles from market open to use. "
            "Default comes from config.OPENING_RANGE_CANDLE_COUNT."
        ),
    ),
):
    """
    Fetches intraday candles and calculates opening range for one instrument.
    """

    resolved_instrument_key = resolve_opening_range_instrument_key(
        instrument_key=instrument_key,
        strike=strike,
        striketype=striketype,
    )

    selected_candle_count = candle_count or getattr(
        config,
        "OPENING_RANGE_CANDLE_COUNT",
        1,
    )

    logger.info(
        f"Single instrument opening range fetch requested. "
        f"resolved_instrument_key={resolved_instrument_key}, "
        f"candle_count={selected_candle_count}"
    )

    try:
        result = await run_in_threadpool(
            calculate_opening_range_for_instrument,
            instrument_key=resolved_instrument_key,
            candle_count=selected_candle_count,
        )

        return {
            "status": "success",
            "message": "Opening range calculated for single instrument.",
            "instrument_key": resolved_instrument_key,
            "input": {
                "instrument_key": instrument_key,
                "strike": strike,
                "striketype": striketype,
                "candle_count": candle_count,
            },
            "result": result,
        }

    except Exception as ex:
        error_message = f"{type(ex).__name__}: {ex}"

        logger.error(
            f"Single instrument opening range fetch failed for "
            f"{resolved_instrument_key}: {error_message}"
        )

        raise HTTPException(
            status_code=500,
            detail=(
                f"Opening range fetch failed for "
                f"{resolved_instrument_key}: {error_message}"
            ),
        )


# ============================================================
# OR + EMA Strategy Routes
# ============================================================


@router.get("/opening-range/strategy/status")
async def get_opening_range_strategy_status():
    """
    Returns compact OR touch + EMA strategy status.

    Shows:
    - NIFTY OR average
    - eligible strike range
    - eligible instrument count
    - all touched candidate count
    - selected touched instrument
    - selected reason
    - sent alert count
    - nearest strike selection rules
    """

    strategy_status = get_or_ema_strategy_status()

    return {
        "status": "success",
        "selection_mode": "first_touch_same_time_nearest_to_nifty",
        "strategy_status": strategy_status,
        "selected": {
            "selected_touch": strategy_status.get("selected_touch"),
            "selected_touch_key": strategy_status.get("selected_touch_key"),
            "selected_instrument_key": strategy_status.get("selected_instrument_key"),
            "selected_level": strategy_status.get("selected_level"),
            "selected_reason": strategy_status.get("selected_reason"),
            "selected_at": strategy_status.get("selected_at"),
        },
    }


@router.get("/opening-range/strategy/selected")
async def get_opening_range_strategy_selected():
    """
    Returns the currently selected OR + EMA strategy touch.

    Selection rule:
    - First eligible touch is selected.
    - If multiple eligible touches happen at the same timestamp/candle,
      nearest strike to current NIFTY spot is selected.
    - Only selected touch can trigger Telegram alert after EMA confirmation.
    """

    strategy_cache = get_or_ema_strategy_cache()

    return {
        "status": "success",
        "selection_mode": "first_touch_same_time_nearest_to_nifty",
        "selected_touch": strategy_cache.get("selected_touch"),
        "selected_touch_key": strategy_cache.get("selected_touch_key"),
        "selected_instrument_key": strategy_cache.get("selected_instrument_key"),
        "selected_level": strategy_cache.get("selected_level"),
        "selected_reason": strategy_cache.get("selected_reason"),
        "selected_at": strategy_cache.get("selected_at"),
    }


@router.get("/opening-range/strategy/cache")
async def get_opening_range_strategy_cache(
    include_latest_ticks: bool = Query(
        default=False,
        description="If true, include latest live tick cache. Can be large.",
    ),
):
    """
    Returns full OR touch + EMA strategy cache.

    Warning:
    - This can be large if include_latest_ticks=true.
    """

    strategy_cache = get_or_ema_strategy_cache()

    if not include_latest_ticks:
        strategy_cache.pop("latest_ticks", None)

    return {
        "status": "success",
        "include_latest_ticks": include_latest_ticks,
        "strategy_cache": strategy_cache,
    }


@router.get("/opening-range/strategy/eligible-instruments")
async def get_opening_range_strategy_eligible_instruments(
    option_type: str = Query(
        default=None,
        description="Optional filter: ce or pe.",
    ),
    limit: int = Query(
        default=500,
        ge=1,
        le=5000,
        description="Maximum number of eligible instruments to return.",
    ),
):
    """
    Returns eligible instruments built from:

        NIFTY Opening Range average +/- OR_EMA_STRATEGY_STRIKE_WINDOW_POINTS

    The range is clamped by STRIKE_FROM and STRIKE_TO.
    """

    strategy_cache = get_or_ema_strategy_cache()
    eligible = strategy_cache.get("eligible_instruments", {})

    selected_option_type = str(option_type or "").upper()

    instruments = list(eligible.values())

    if selected_option_type in ["CE", "PE"]:
        instruments = [
            item
            for item in instruments
            if str(item.get("instrument_type", "")).upper() == selected_option_type
        ]

    instruments = instruments[:limit]

    return {
        "status": "success",
        "option_type": selected_option_type if selected_option_type else None,
        "limit": limit,
        "or_average": strategy_cache.get("or_average"),
        "strike_from": strategy_cache.get("strike_from"),
        "strike_to": strategy_cache.get("strike_to"),
        "eligible_count": len(eligible),
        "returned_count": len(instruments),
        "eligible_instruments": instruments,
    }


@router.get("/opening-range/strategy/touched-instruments")
async def get_opening_range_strategy_touched_instruments(
    instrument_key: str = Query(
        default=None,
        description="Optional instrument key filter.",
    ),
    level: str = Query(
        default=None,
        description="Optional level filter: S2, S3, R2, R3.",
    ),
    selected_only: bool = Query(
        default=False,
        description="If true, return only the selected touch record.",
    ),
    limit: int = Query(
        default=500,
        ge=1,
        le=5000,
        description="Maximum number of touched records to return.",
    ),
):
    """
    Returns instruments that touched/crossed strategy levels.

    Important:
    - All touched instruments are kept for debug.
    - Only selected touch is used for EMA confirmation.
    - Non-selected touches do not trigger Telegram strategy alert.
    """

    strategy_cache = get_or_ema_strategy_cache()

    if selected_only:
        selected_touch = strategy_cache.get("selected_touch")

        return {
            "status": "success",
            "selected_only": True,
            "selection_mode": "first_touch_same_time_nearest_to_nifty",
            "selected_touch": selected_touch,
            "records": [selected_touch] if selected_touch else [],
        }

    touched = strategy_cache.get("touched_instruments", {})
    records = list(touched.values())

    if instrument_key:
        records = [
            item for item in records if item.get("instrument_key") == instrument_key
        ]

    selected_level = str(level or "").upper()

    if selected_level in ["S2", "S3", "R2", "R3"]:
        records = [
            item
            for item in records
            if str(item.get("level", "")).upper() == selected_level
        ]

    records = records[-limit:]

    return {
        "status": "success",
        "selected_only": False,
        "selection_mode": "first_touch_same_time_nearest_to_nifty",
        "instrument_key": instrument_key,
        "level": selected_level if selected_level else None,
        "limit": limit,
        "touched_count": len(touched),
        "returned_count": len(records),
        "selected_touch_key": strategy_cache.get("selected_touch_key"),
        "selected_instrument_key": strategy_cache.get("selected_instrument_key"),
        "selected_level": strategy_cache.get("selected_level"),
        "selected_reason": strategy_cache.get("selected_reason"),
        "touched_instruments": records,
    }


@router.get("/opening-range/strategy/alerts")
async def get_opening_range_strategy_alerts(
    instrument_key: str = Query(
        default=None,
        description="Optional instrument key filter.",
    ),
    cross_type: str = Query(
        default=None,
        description="Optional cross type filter: bullish_cross or bearish_cross.",
    ),
    limit: int = Query(
        default=500,
        ge=1,
        le=5000,
        description="Maximum number of alert records to return.",
    ),
):
    """
    Returns OR touch + EMA strategy alert records.

    Alerts are created only when:
    - one eligible touch is selected
    - selected instrument later produces live EMA crossover
    - duplicate prevention allows the alert
    """

    strategy_cache = get_or_ema_strategy_cache()
    alerts = strategy_cache.get("alerts_sent", {})

    records = list(alerts.values())

    if instrument_key:
        records = [
            item for item in records if item.get("instrument_key") == instrument_key
        ]

    selected_cross_type = str(cross_type or "").lower()

    if selected_cross_type:
        records = [
            item
            for item in records
            if str(item.get("cross_type", "")).lower() == selected_cross_type
        ]

    records = records[-limit:]

    return {
        "status": "success",
        "instrument_key": instrument_key,
        "cross_type": selected_cross_type if selected_cross_type else None,
        "limit": limit,
        "alerts_count": len(alerts),
        "returned_count": len(records),
        "selected_instrument_key": strategy_cache.get("selected_instrument_key"),
        "selected_level": strategy_cache.get("selected_level"),
        "alerts": records,
    }


@router.get("/opening-range/strategy/latest-ticks")
async def get_opening_range_strategy_latest_ticks(
    instrument_key: str = Query(
        default=None,
        description="Optional instrument key filter.",
    ),
    limit: int = Query(
        default=500,
        ge=1,
        le=5000,
        description="Maximum number of latest ticks to return.",
    ),
):
    """
    Returns latest live tick snapshots stored by the strategy engine.

    Useful for checking nearest strike live data availability.
    """

    strategy_cache = get_or_ema_strategy_cache()
    latest_ticks = strategy_cache.get("latest_ticks", {})

    if instrument_key:
        tick = latest_ticks.get(instrument_key)

        return {
            "status": "success",
            "instrument_key": instrument_key,
            "found": tick is not None,
            "latest_tick": tick,
        }

    records = list(latest_ticks.values())[-limit:]

    return {
        "status": "success",
        "limit": limit,
        "latest_ticks_count": len(latest_ticks),
        "returned_count": len(records),
        "latest_ticks": records,
    }


# ============================================================
# Selected OR Instrument Compatibility Routes
# ============================================================


@router.get("/opening-range/selected-instrument")
async def get_selected_opening_range_instrument():
    """
    Backward-compatible route.

    New flow:
    - Legacy selected Opening Range instrument feature is disabled.
    - New OR + EMA strategy selected touch is available at:
      /opening-range/strategy/selected
    """

    return {
        "status": "success",
        "flow": "disabled",
        "message": (
            "Legacy selected Opening Range instrument flow is disabled. "
            "Use /opening-range/strategy/selected for the new OR + EMA strategy "
            "selected touched instrument."
        ),
        "selected_or_instrument": get_selected_or_instrument_state(),
        "or_ema_strategy_selected": get_or_ema_strategy_cache().get("selected_touch"),
    }


@router.get("/opening-range/selected-instrument/ema-alerts")
async def get_selected_opening_range_ema_alerts(
    limit: int = Query(
        default=100,
        ge=1,
        le=1000,
        description="Number of latest selected OR instrument EMA alert records.",
    ),
):
    """
    Backward-compatible route.

    New flow:
    - Legacy selected OR EMA Telegram alerts are disabled.
    - New strategy alerts are available at:
      /opening-range/strategy/alerts
    """

    return {
        "status": "success",
        "flow": "disabled",
        "message": (
            "Legacy selected OR EMA Telegram alerts are disabled. "
            "Use /opening-range/strategy/alerts for OR touch + EMA strategy alerts."
        ),
        "limit": limit,
        "selected_or_instrument": get_selected_or_instrument_state(),
        "alerts": get_selected_or_ema_alerts(limit=limit),
        "or_ema_strategy_alerts": list(
            get_or_ema_strategy_cache().get("alerts_sent", {}).values()
        )[-limit:],
    }


# ============================================================
# Touch Event Routes
# ============================================================


@router.get("/opening-range/touch-events")
async def get_opening_range_touch_events_route(
    limit: int = Query(
        default=100,
        ge=1,
        le=1000,
        description="Number of latest opening range touch events to return.",
    ),
):
    """
    Returns recent Opening Range touch events.

    Events can come from:
    - intraday_backfill_scan
    - live_tick

    Touch events are tracked for diagnostics/WebSocket use.
    They do not use the legacy selected OR flow.
    """

    return {
        "status": "success",
        "limit": limit,
        "events": get_opening_range_touch_events(limit=limit),
    }


@router.get("/opening-range/touch-events/pending")
async def get_opening_range_pending_touch_events_route():
    """
    Returns pending Opening Range touch events waiting for legacy Telegram batch flush.

    Legacy touch Telegram is disabled by default.
    """

    return {
        "status": "success",
        "legacy_touch_telegram_enabled": getattr(
            config,
            "OPENING_RANGE_LEGACY_TOUCH_TELEGRAM_ENABLED",
            False,
        ),
        "pending_events": get_opening_range_pending_touch_events(),
    }


@router.post("/opening-range/touch-events/flush")
async def flush_opening_range_pending_touch_events_route(
    force: bool = Query(
        default=True,
        description="If true, flush pending touch events immediately.",
    ),
):
    """
    Manually flushes pending Opening Range touch events to Telegram.

    This sends only if:
        OPENING_RANGE_LEGACY_TOUCH_TELEGRAM_ENABLED=True
    """

    try:
        sent = await run_in_threadpool(
            flush_pending_touch_alerts,
            force=force,
            source="manual_api_flush",
        )

        legacy_enabled = getattr(
            config,
            "OPENING_RANGE_LEGACY_TOUCH_TELEGRAM_ENABLED",
            False,
        )

        return {
            "status": "success",
            "telegram_sent": sent,
            "legacy_touch_telegram_enabled": legacy_enabled,
            "message": (
                "Pending touch events flushed to Telegram."
                if sent
                else (
                    "No Telegram message sent. There may be no pending events, "
                    "or legacy touch Telegram is disabled."
                )
            ),
        }

    except Exception as ex:
        error_message = f"{type(ex).__name__}: {ex}"

        logger.error(f"Manual opening range touch alert flush failed: {error_message}")

        raise HTTPException(
            status_code=500,
            detail=f"Touch alert flush failed: {error_message}",
        )


# ============================================================
# File / Config Routes
# ============================================================


@router.get("/opening-range/file")
async def get_opening_range_file_status():
    """
    Checks whether opening range output file exists.

    Default output:
        data/opening_range_results.json
    """

    output_file = getattr(
        config,
        "OPENING_RANGE_OUTPUT_FILE",
        "data/opening_range_results.json",
    )

    file_path = Path(output_file)

    touch_events_file = getattr(
        config,
        "OPENING_RANGE_TOUCH_EVENTS_OUTPUT_FILE",
        "data/opening_range_touch_events.json",
    )

    touch_events_file_path = Path(touch_events_file)

    return {
        "status": "success",
        "opening_range_file_exists": file_path.exists(),
        "opening_range_file_path": str(file_path),
        "save_file_enabled": getattr(config, "OPENING_RANGE_SAVE_FILE", True),
        "touch_events_file_exists": touch_events_file_path.exists(),
        "touch_events_file_path": str(touch_events_file_path),
        "touch_events_save_test_file": getattr(
            config,
            "OPENING_RANGE_TOUCH_EVENTS_SAVE_TEST_FILE",
            True,
        ),
    }


@router.get("/opening-range/config")
async def get_opening_range_config():
    """
    Returns opening range configuration.
    """

    return {
        "status": "success",
        "config": {
            "enabled": getattr(config, "OPENING_RANGE_ENABLED", True),
            "interval": getattr(config, "OPENING_RANGE_INTERVAL", "1minute"),
            "candle_count": getattr(config, "OPENING_RANGE_CANDLE_COUNT", 1),
            "market_timezone": getattr(config, "MARKET_TIMEZONE", "Asia/Kolkata"),
            "market_open_hour": getattr(
                config,
                "OPENING_RANGE_MARKET_OPEN_HOUR",
                9,
            ),
            "market_open_minute": getattr(
                config,
                "OPENING_RANGE_MARKET_OPEN_MINUTE",
                15,
            ),
            "fetch_hour": getattr(config, "OPENING_RANGE_FETCH_HOUR", 9),
            "fetch_minute": getattr(config, "OPENING_RANGE_FETCH_MINUTE", 18),
            "intraday_unit": getattr(
                config,
                "OPENING_RANGE_INTRADAY_UNIT",
                "minutes",
            ),
            "intraday_interval": getattr(
                config,
                "OPENING_RANGE_INTRADAY_INTERVAL",
                "1",
            ),
            "max_workers": getattr(config, "OPENING_RANGE_MAX_WORKERS", 5),
            "request_sleep_seconds": getattr(
                config,
                "OPENING_RANGE_REQUEST_SLEEP_SECONDS",
                0.15,
            ),
            "save_file": getattr(config, "OPENING_RANGE_SAVE_FILE", True),
            "output_file": getattr(
                config,
                "OPENING_RANGE_OUTPUT_FILE",
                "data/opening_range_results.json",
            ),
            "max_events_in_memory": getattr(
                config,
                "OPENING_RANGE_MAX_EVENTS_IN_MEMORY",
                5000,
            ),
            "backfill_touch_scan_enabled": getattr(
                config,
                "OPENING_RANGE_BACKFILL_TOUCH_SCAN_ENABLED",
                True,
            ),
            "backfill_touch_scan_source": getattr(
                config,
                "OPENING_RANGE_BACKFILL_TOUCH_SCAN_SOURCE",
                "intraday_api",
            ),
            "touch_alert_enabled": getattr(
                config,
                "OPENING_RANGE_TOUCH_ALERT_ENABLED",
                True,
            ),
            "touch_alert_max_instruments": getattr(
                config,
                "OPENING_RANGE_TOUCH_ALERT_MAX_INSTRUMENTS",
                5,
            ),
            "touch_alert_batch_seconds": getattr(
                config,
                "OPENING_RANGE_TOUCH_ALERT_BATCH_SECONDS",
                10,
            ),
            "touch_alert_once_per_level": getattr(
                config,
                "OPENING_RANGE_TOUCH_ALERT_ONCE_PER_LEVEL",
                True,
            ),
            "touch_alert_options_only": getattr(
                config,
                "OPENING_RANGE_TOUCH_ALERT_OPTIONS_ONLY",
                True,
            ),
            "touch_alert_sort_by_nearest_index": getattr(
                config,
                "OPENING_RANGE_TOUCH_ALERT_SORT_BY_NEAREST_INDEX",
                True,
            ),
            "touch_alert_main_index_key": getattr(
                config,
                "OPENING_RANGE_TOUCH_ALERT_MAIN_INDEX_KEY",
                getattr(config, "MAIN_NIFTY_SECURITY", "NSE_INDEX|Nifty 50"),
            ),
            "backfill_touch_alert_enabled": getattr(
                config,
                "OPENING_RANGE_BACKFILL_TOUCH_ALERT_ENABLED",
                True,
            ),
            "live_touch_alert_enabled": getattr(
                config,
                "OPENING_RANGE_LIVE_TOUCH_ALERT_ENABLED",
                True,
            ),
            "touch_check_mode": getattr(
                config,
                "OPENING_RANGE_TOUCH_CHECK_MODE",
                "high_low",
            ),
            "store_touch_status": getattr(
                config,
                "OPENING_RANGE_STORE_TOUCH_STATUS",
                True,
            ),
            "touch_events_output_file": getattr(
                config,
                "OPENING_RANGE_TOUCH_EVENTS_OUTPUT_FILE",
                "data/opening_range_touch_events.json",
            ),
            "touch_events_save_test_file": getattr(
                config,
                "OPENING_RANGE_TOUCH_EVENTS_SAVE_TEST_FILE",
                True,
            ),
            "first_touch_selection_enabled": getattr(
                config,
                "OPENING_RANGE_FIRST_TOUCH_SELECTION_ENABLED",
                False,
            ),
            "first_touch_selection_source": getattr(
                config,
                "OPENING_RANGE_FIRST_TOUCH_SELECTION_SOURCE",
                "disabled",
            ),
            "selected_or_touch_notify_enabled": getattr(
                config,
                "OPENING_RANGE_SELECTED_OR_TOUCH_NOTIFY_ENABLED",
                False,
            ),
            "selected_or_ema_alert_enabled": getattr(
                config,
                "OPENING_RANGE_SELECTED_OR_EMA_ALERT_ENABLED",
                False,
            ),
            "legacy_touch_telegram_enabled": getattr(
                config,
                "OPENING_RANGE_LEGACY_TOUCH_TELEGRAM_ENABLED",
                False,
            ),
            "selected_or_ema_alert_once_per_cross": getattr(
                config,
                "OPENING_RANGE_SELECTED_OR_EMA_ALERT_ONCE_PER_CROSS",
                False,
            ),
            "ema_cross_include_opening_range_levels": getattr(
                config,
                "EMA_CROSS_INCLUDE_OPENING_RANGE_LEVELS",
                True,
            ),
            "ema_cross_broadcast_without_opening_range": getattr(
                config,
                "EMA_CROSS_BROADCAST_WITHOUT_OPENING_RANGE",
                True,
            ),
            "or_ema_strategy": {
                "alert_enabled": getattr(
                    config,
                    "OR_EMA_STRATEGY_ALERT_ENABLED",
                    True,
                ),
                "strike_window_points": getattr(
                    config,
                    "OR_EMA_STRATEGY_STRIKE_WINDOW_POINTS",
                    500,
                ),
                "touch_levels": getattr(
                    config,
                    "OR_EMA_STRATEGY_TOUCH_LEVELS",
                    ["S2", "S3", "R2", "R3"],
                ),
                "touch_check_mode": getattr(
                    config,
                    "OR_EMA_STRATEGY_TOUCH_CHECK_MODE",
                    "high_low",
                ),
                "selection_mode": getattr(
                    config,
                    "OR_EMA_STRATEGY_SELECTION_MODE",
                    "first_touch_same_time_nearest",
                ),
                "lock_after_first_selection": getattr(
                    config,
                    "OR_EMA_STRATEGY_LOCK_AFTER_FIRST_SELECTION",
                    True,
                ),
                "store_non_selected_touches": getattr(
                    config,
                    "OR_EMA_STRATEGY_STORE_NON_SELECTED_TOUCHES",
                    True,
                ),
                "options_only": getattr(
                    config,
                    "OR_EMA_STRATEGY_OPTIONS_ONLY",
                    True,
                ),
                "store_live_tick_cache": getattr(
                    config,
                    "OR_EMA_STRATEGY_STORE_LIVE_TICK_CACHE",
                    True,
                ),
                "nearest_strike_count": getattr(
                    config,
                    "OR_EMA_STRATEGY_NEAREST_STRIKE_COUNT",
                    3,
                ),
                "bullish_option_type": getattr(
                    config,
                    "OR_EMA_STRATEGY_BULLISH_OPTION_TYPE",
                    "CE",
                ),
                "bearish_option_type": getattr(
                    config,
                    "OR_EMA_STRATEGY_BEARISH_OPTION_TYPE",
                    "PE",
                ),
                "bullish_strike_mode": getattr(
                    config,
                    "OR_EMA_STRATEGY_BULLISH_STRIKE_MODE",
                    "equal_or_below",
                ),
                "bearish_strike_mode": getattr(
                    config,
                    "OR_EMA_STRATEGY_BEARISH_STRIKE_MODE",
                    "equal_or_above",
                ),
                "confirm_same_instrument": getattr(
                    config,
                    "OR_EMA_STRATEGY_CONFIRM_SAME_INSTRUMENT",
                    True,
                ),
                "alert_once_per_touch_and_cross": getattr(
                    config,
                    "OR_EMA_STRATEGY_ALERT_ONCE_PER_TOUCH_AND_CROSS",
                    True,
                ),
                "max_touched_instruments": getattr(
                    config,
                    "OR_EMA_STRATEGY_MAX_TOUCHED_INSTRUMENTS",
                    2000,
                ),
                "max_alerts_in_memory": getattr(
                    config,
                    "OR_EMA_STRATEGY_MAX_ALERTS_IN_MEMORY",
                    2000,
                ),
                "include_nearest_live_data": getattr(
                    config,
                    "OR_EMA_STRATEGY_INCLUDE_NEAREST_LIVE_DATA",
                    True,
                ),
                "include_touched_instrument_live_data": getattr(
                    config,
                    "OR_EMA_STRATEGY_INCLUDE_TOUCHED_INSTRUMENT_LIVE_DATA",
                    True,
                ),
                "telegram_title": getattr(
                    config,
                    "OR_EMA_STRATEGY_TELEGRAM_TITLE",
                    "OR Touch + EMA Cross Strategy Alert",
                ),
                "telegram_level": getattr(
                    config,
                    "OR_EMA_STRATEGY_TELEGRAM_LEVEL",
                    "OPENING_RANGE",
                ),
            },
        },
    }
