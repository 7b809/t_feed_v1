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
    get_opening_range_dashboard_summary,
)

logger = get_logger(__file__)

router = APIRouter()


# ============================================================
# Live EMA Mode Helper
# ============================================================


def get_live_ema_calculation_mode_text() -> str:
    """
    Returns configured live EMA calculation mode.

    LIVE_EMA_CALCULATION_MODE = False
        completed candle close based EMA calculation.

    LIVE_EMA_CALCULATION_MODE = True
        live tick/LTP based EMA calculation.
    """

    return (
        "tick_ltp"
        if bool(getattr(config, "LIVE_EMA_CALCULATION_MODE", False))
        else "candle_close"
    )


def get_live_ema_calculation_mode_payload() -> dict:
    """
    Returns live EMA mode payload for API responses.
    """

    flag = bool(getattr(config, "LIVE_EMA_CALCULATION_MODE", False))
    mode = get_live_ema_calculation_mode_text()

    return {
        "flag": flag,
        "mode": mode,
        "description": (
            "live tick/LTP based EMA calculation"
            if flag
            else "completed candle close based EMA calculation"
        ),
    }


def get_ema_order_side_rule_payload() -> dict:
    """
    Returns EMA alert order-side rule payload.

    Current requirement:
        bullish_cross -> same side as isolated instrument
        bearish_cross -> opposite side of isolated instrument

    Fallback behavior:
        If isolated instrument type is unavailable, option_service may use
        EMA_ALERT_BULLISH_OPTION_TYPE and EMA_ALERT_BEARISH_OPTION_TYPE.
    """

    return {
        "mode": "dynamic_isolated_instrument_side",
        "description": (
            "EMA alert suggested order instruments are selected from current "
            "NIFTY spot based strikes. The option side is dynamic: bullish_cross "
            "uses the same side as the isolated instrument, while bearish_cross "
            "uses the opposite side of the isolated instrument."
        ),
        "rules": {
            "bullish_cross": "same_side_as_isolated_instrument",
            "bearish_cross": "opposite_side_of_isolated_instrument",
        },
        "examples": [
            {
                "isolated_instrument_type": "CE",
                "cross_type": "bullish_cross",
                "suggested_order_side": "CE",
            },
            {
                "isolated_instrument_type": "CE",
                "cross_type": "bearish_cross",
                "suggested_order_side": "PE",
            },
            {
                "isolated_instrument_type": "PE",
                "cross_type": "bullish_cross",
                "suggested_order_side": "PE",
            },
            {
                "isolated_instrument_type": "PE",
                "cross_type": "bearish_cross",
                "suggested_order_side": "CE",
            },
        ],
        "strike_selection": {
            "basis": "current_nifty_spot_ltp",
            "strike_step": getattr(config, "EMA_ALERT_STRIKE_STEP", 50),
            "offsets": getattr(
                config,
                "EMA_ALERT_NEAREST_STRIKE_OFFSETS",
                [-50, 0, 50],
            ),
            "max_order_instruments": getattr(
                config,
                "EMA_ALERT_MAX_ORDER_INSTRUMENTS",
                3,
            ),
            "clamp_to_filter_range": getattr(
                config,
                "EMA_ALERT_ORDER_STRIKES_CLAMP_TO_FILTER_RANGE",
                True,
            ),
        },
        "fallback": {
            "used_only_when_isolated_instrument_type_missing": True,
            "bullish_option_type": getattr(
                config,
                "EMA_ALERT_BULLISH_OPTION_TYPE",
                "CE",
            ),
            "bearish_option_type": getattr(
                config,
                "EMA_ALERT_BEARISH_OPTION_TYPE",
                "PE",
            ),
        },
    }


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
    """

    isolated_state = get_selected_or_instrument_state()

    return {
        "status": "success",
        "opening_range_status": get_opening_range_status(),
        "isolated_instrument": isolated_state,
        "selected_or_instrument": isolated_state,
        "live_ema_calculation": get_live_ema_calculation_mode_payload(),
        "ema_order_side_rule": get_ema_order_side_rule_payload(),
        "new_flow": {
            "description": (
                "Opening Range levels are calculated for all subscribed instruments. "
                "The system monitors R2/R3/S2/S3 touches. After an eligible touch, "
                "one instrument is isolated using level priority and nearest strike "
                "to Opening Range average. Live EMA continues for all instruments, "
                "but Telegram EMA alerts are sent only for the isolated instrument. "
                "For Telegram EMA suggested order instruments, bullish_cross uses "
                "the same option side as the isolated instrument and bearish_cross "
                "uses the opposite option side."
            ),
            "selected_or_flow": "mapped_to_isolated_instrument_flow",
            "isolated_instrument_flow": "enabled",
            "live_ema_calculation": get_live_ema_calculation_mode_payload(),
            "ema_order_side_rule": get_ema_order_side_rule_payload(),
            "isolated_ema_telegram_alerts": getattr(
                config,
                "EMA_ISOLATED_INSTRUMENT_TELEGRAM_ENABLED",
                True,
            ),
            "ema_websocket_opening_range_enrichment": getattr(
                config,
                "EMA_CROSS_INCLUDE_OPENING_RANGE_LEVELS",
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
            "isolation_enabled": getattr(
                config,
                "OPENING_RANGE_ISOLATED_INSTRUMENT_ENABLED",
                True,
            ),
            "isolation_average_window_points": getattr(
                config,
                "OPENING_RANGE_ISOLATION_AVERAGE_WINDOW_POINTS",
                500.0,
            ),
            "isolation_touch_levels": getattr(
                config,
                "OPENING_RANGE_ISOLATION_TOUCH_LEVELS",
                ["R2", "R3", "S2", "S3"],
            ),
            "isolation_priority_levels": getattr(
                config,
                "OPENING_RANGE_ISOLATION_PRIORITY_LEVELS",
                ["R3", "S3", "R2", "S2"],
            ),
            "isolation_lock_for_day": getattr(
                config,
                "OPENING_RANGE_ISOLATION_LOCK_FOR_DAY",
                True,
            ),
            "isolation_allow_priority_upgrade": getattr(
                config,
                "OPENING_RANGE_ISOLATION_ALLOW_PRIORITY_UPGRADE",
                True,
            ),
            "isolated_instrument_notify_enabled": getattr(
                config,
                "OPENING_RANGE_ISOLATED_INSTRUMENT_NOTIFY_ENABLED",
                True,
            ),
            "legacy_touch_telegram_enabled": getattr(
                config,
                "OPENING_RANGE_LEGACY_TOUCH_TELEGRAM_ENABLED",
                False,
            ),
            "ema_isolated_instrument_telegram_enabled": getattr(
                config,
                "EMA_ISOLATED_INSTRUMENT_TELEGRAM_ENABLED",
                True,
            ),
            "ema_order_side_rule": get_ema_order_side_rule_payload(),
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
            "live_ema_calculation_mode_flag": getattr(
                config,
                "LIVE_EMA_CALCULATION_MODE",
                False,
            ),
            "live_ema_calculation_mode": get_live_ema_calculation_mode_text(),
            "output_file": getattr(
                config,
                "OPENING_RANGE_OUTPUT_FILE",
                "data/opening_range_results.json",
            ),
        },
    }


@router.get("/opening-range/dashboard")
async def get_opening_range_dashboard(
    touch_limit: int = Query(
        default=100,
        ge=1,
        le=1000,
        description="Number of recent Opening Range touch events.",
    ),
    alert_limit: int = Query(
        default=100,
        ge=1,
        le=1000,
        description="Number of recent isolated EMA alert records.",
    ),
):
    """
    Returns compact dashboard data for isolated EMA dashboard.

    Used by:
        templates/isolated_ema_dashboard.html
    """

    return get_opening_range_dashboard_summary(
        touch_limit=touch_limit,
        alert_limit=alert_limit,
    )


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
        f"max_workers={selected_max_workers}, "
        f"live_ema_calculation_mode={get_live_ema_calculation_mode_text()}"
    )

    try:
        summary = await run_in_threadpool(
            calculate_opening_range_for_all_subscribed,
            candle_count=selected_candle_count,
            save_data=selected_save_results,
            max_workers=selected_max_workers,
        )

        isolated_state = get_selected_or_instrument_state()

        return {
            "status": "success",
            "message": (
                "Opening range intraday candles fetched, levels calculated, "
                "R2/R3/S2/S3 backfill touch scan completed, and isolated "
                "instrument selection evaluated for subscribed instruments."
            ),
            "opening_range_results_saved": selected_save_results,
            "isolated_instrument": isolated_state,
            "selected_or_instrument": isolated_state,
            "live_ema_calculation": get_live_ema_calculation_mode_payload(),
            "ema_order_side_rule": get_ema_order_side_rule_payload(),
            "ema_websocket_opening_range_enrichment": getattr(
                config,
                "EMA_CROSS_INCLUDE_OPENING_RANGE_LEVELS",
                True,
            ),
            "isolated_ema_telegram_alerts_enabled": getattr(
                config,
                "EMA_ISOLATED_INSTRUMENT_TELEGRAM_ENABLED",
                True,
            ),
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
    """

    return {
        "status": "success",
        "live_ema_calculation": get_live_ema_calculation_mode_payload(),
        "ema_order_side_rule": get_ema_order_side_rule_payload(),
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
        "live_ema_calculation": get_live_ema_calculation_mode_payload(),
        "ema_order_side_rule": get_ema_order_side_rule_payload(),
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
        "live_ema_calculation": get_live_ema_calculation_mode_payload(),
        "ema_order_side_rule": get_ema_order_side_rule_payload(),
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
            "live_ema_calculation": get_live_ema_calculation_mode_payload(),
            "ema_order_side_rule": get_ema_order_side_rule_payload(),
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
# Isolated Instrument Compatibility Routes
# ============================================================


@router.get("/opening-range/selected-instrument")
async def get_selected_opening_range_instrument():
    """
    Backward-compatible route.
    """

    isolated_state = get_selected_or_instrument_state()

    return {
        "status": "success",
        "flow": "isolated_instrument",
        "message": (
            "Selected OR compatibility route now returns isolated Opening Range "
            "instrument state. EMA Telegram alerts are sent only for the isolated "
            "instrument, while WebSocket EMA events can still be broadcast for all "
            "instruments. EMA alert suggested order side is dynamic: bullish_cross "
            "uses the same side as the isolated instrument and bearish_cross uses "
            "the opposite side."
        ),
        "live_ema_calculation": get_live_ema_calculation_mode_payload(),
        "ema_order_side_rule": get_ema_order_side_rule_payload(),
        "selected_or_instrument": isolated_state,
        "isolated_instrument": isolated_state,
    }


@router.get("/opening-range/selected-instrument/ema-alerts")
async def get_selected_opening_range_ema_alerts(
    limit: int = Query(
        default=100,
        ge=1,
        le=1000,
        description="Number of latest isolated instrument EMA alert records.",
    ),
):
    """
    Backward-compatible route.
    """

    isolated_state = get_selected_or_instrument_state()

    return {
        "status": "success",
        "flow": "isolated_instrument",
        "message": (
            "Returns EMA Telegram alert records for the isolated Opening Range "
            "instrument. Other instruments may produce EMA WebSocket events, but "
            "do not produce Telegram EMA alerts. Suggested order side follows "
            "the dynamic isolated-side rule."
        ),
        "limit": limit,
        "live_ema_calculation": get_live_ema_calculation_mode_payload(),
        "ema_order_side_rule": get_ema_order_side_rule_payload(),
        "selected_or_instrument": isolated_state,
        "isolated_instrument": isolated_state,
        "alerts": get_selected_or_ema_alerts(limit=limit),
    }


@router.get("/opening-range/isolated-instrument")
async def get_isolated_opening_range_instrument():
    """
    Returns current isolated Opening Range instrument state.
    """

    return {
        "status": "success",
        "flow": "isolated_instrument",
        "live_ema_calculation": get_live_ema_calculation_mode_payload(),
        "ema_order_side_rule": get_ema_order_side_rule_payload(),
        "isolated_instrument": get_selected_or_instrument_state(),
    }


@router.get("/opening-range/isolated-instrument/ema-alerts")
async def get_isolated_opening_range_ema_alerts(
    limit: int = Query(
        default=100,
        ge=1,
        le=1000,
        description="Number of latest isolated instrument EMA alert records.",
    ),
):
    """
    Returns latest EMA Telegram alert records for the isolated instrument.
    """

    return {
        "status": "success",
        "flow": "isolated_instrument",
        "limit": limit,
        "live_ema_calculation": get_live_ema_calculation_mode_payload(),
        "ema_order_side_rule": get_ema_order_side_rule_payload(),
        "isolated_instrument": get_selected_or_instrument_state(),
        "alerts": get_selected_or_ema_alerts(limit=limit),
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
        description="Number of latest opening range R2/R3/S2/S3 touch events to return.",
    ),
):
    """
    Returns recent Opening Range R2/R3/S2/S3 touch events.
    """

    return {
        "status": "success",
        "limit": limit,
        "live_ema_calculation": get_live_ema_calculation_mode_payload(),
        "ema_order_side_rule": get_ema_order_side_rule_payload(),
        "events": get_opening_range_touch_events(limit=limit),
        "isolated_instrument": get_selected_or_instrument_state(),
    }


@router.get("/opening-range/touch-events/pending")
async def get_opening_range_pending_touch_events_route():
    """
    Returns pending Opening Range touch events waiting for legacy Telegram batch flush.
    """

    return {
        "status": "success",
        "legacy_touch_telegram_enabled": getattr(
            config,
            "OPENING_RANGE_LEGACY_TOUCH_TELEGRAM_ENABLED",
            False,
        ),
        "live_ema_calculation": get_live_ema_calculation_mode_payload(),
        "ema_order_side_rule": get_ema_order_side_rule_payload(),
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
            "live_ema_calculation": get_live_ema_calculation_mode_payload(),
            "ema_order_side_rule": get_ema_order_side_rule_payload(),
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

    isolated_file = getattr(
        config,
        "OPENING_RANGE_ISOLATED_INSTRUMENT_OUTPUT_FILE",
        "data/isolated_opening_range_instrument.json",
    )

    isolated_file_path = Path(isolated_file)

    return {
        "status": "success",
        "live_ema_calculation": get_live_ema_calculation_mode_payload(),
        "ema_order_side_rule": get_ema_order_side_rule_payload(),
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
        "isolated_instrument_file_exists": isolated_file_path.exists(),
        "isolated_instrument_file_path": str(isolated_file_path),
    }


@router.get("/opening-range/config")
async def get_opening_range_config():
    """
    Returns opening range configuration.
    """

    return {
        "status": "success",
        "live_ema_calculation": get_live_ema_calculation_mode_payload(),
        "ema_order_side_rule": get_ema_order_side_rule_payload(),
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
            "isolation_enabled": getattr(
                config,
                "OPENING_RANGE_ISOLATED_INSTRUMENT_ENABLED",
                True,
            ),
            "isolation_average_window_points": getattr(
                config,
                "OPENING_RANGE_ISOLATION_AVERAGE_WINDOW_POINTS",
                500.0,
            ),
            "isolation_touch_levels": getattr(
                config,
                "OPENING_RANGE_ISOLATION_TOUCH_LEVELS",
                ["R2", "R3", "S2", "S3"],
            ),
            "isolation_priority_levels": getattr(
                config,
                "OPENING_RANGE_ISOLATION_PRIORITY_LEVELS",
                ["R3", "S3", "R2", "S2"],
            ),
            "isolation_lock_for_day": getattr(
                config,
                "OPENING_RANGE_ISOLATION_LOCK_FOR_DAY",
                True,
            ),
            "isolation_allow_priority_upgrade": getattr(
                config,
                "OPENING_RANGE_ISOLATION_ALLOW_PRIORITY_UPGRADE",
                True,
            ),
            "isolation_allow_backfill_touch": getattr(
                config,
                "OPENING_RANGE_ISOLATION_ALLOW_BACKFILL_TOUCH",
                True,
            ),
            "isolation_allow_live_touch": getattr(
                config,
                "OPENING_RANGE_ISOLATION_ALLOW_LIVE_TOUCH",
                True,
            ),
            "isolation_options_only": getattr(
                config,
                "OPENING_RANGE_ISOLATION_OPTIONS_ONLY",
                True,
            ),
            "isolated_instrument_notify_enabled": getattr(
                config,
                "OPENING_RANGE_ISOLATED_INSTRUMENT_NOTIFY_ENABLED",
                True,
            ),
            "first_touch_selection_enabled": getattr(
                config,
                "OPENING_RANGE_FIRST_TOUCH_SELECTION_ENABLED",
                True,
            ),
            "first_touch_selection_source": getattr(
                config,
                "OPENING_RANGE_FIRST_TOUCH_SELECTION_SOURCE",
                "average_window_level_priority",
            ),
            "selected_or_touch_notify_enabled": getattr(
                config,
                "OPENING_RANGE_SELECTED_OR_TOUCH_NOTIFY_ENABLED",
                True,
            ),
            "selected_or_ema_alert_enabled": getattr(
                config,
                "OPENING_RANGE_SELECTED_OR_EMA_ALERT_ENABLED",
                True,
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
            "ema_isolated_instrument_telegram_enabled": getattr(
                config,
                "EMA_ISOLATED_INSTRUMENT_TELEGRAM_ENABLED",
                True,
            ),
            "ema_isolated_alert_every_cross": getattr(
                config,
                "EMA_ISOLATED_ALERT_EVERY_CROSS",
                True,
            ),
            "ema_order_side_rule": get_ema_order_side_rule_payload(),
            "ema_alert_bullish_option_type_fallback": getattr(
                config,
                "EMA_ALERT_BULLISH_OPTION_TYPE",
                "CE",
            ),
            "ema_alert_bearish_option_type_fallback": getattr(
                config,
                "EMA_ALERT_BEARISH_OPTION_TYPE",
                "PE",
            ),
            "ema_alert_strike_step": getattr(
                config,
                "EMA_ALERT_STRIKE_STEP",
                50,
            ),
            "ema_alert_nearest_strike_count": getattr(
                config,
                "EMA_ALERT_NEAREST_STRIKE_COUNT",
                3,
            ),
            "ema_alert_nearest_strike_offsets": getattr(
                config,
                "EMA_ALERT_NEAREST_STRIKE_OFFSETS",
                [-50, 0, 50],
            ),
            "ema_alert_order_strikes_clamp_to_filter_range": getattr(
                config,
                "EMA_ALERT_ORDER_STRIKES_CLAMP_TO_FILTER_RANGE",
                True,
            ),
            "ema_alert_include_order_instrument_ltp": getattr(
                config,
                "EMA_ALERT_INCLUDE_ORDER_INSTRUMENT_LTP",
                True,
            ),
            "ema_alert_max_order_instruments": getattr(
                config,
                "EMA_ALERT_MAX_ORDER_INSTRUMENTS",
                3,
            ),
            "live_ema_enabled": getattr(
                config,
                "LIVE_EMA_ENABLED",
                True,
            ),
            "live_ema_calculation_mode_flag": getattr(
                config,
                "LIVE_EMA_CALCULATION_MODE",
                False,
            ),
            "live_ema_calculation_mode": get_live_ema_calculation_mode_text(),
            "live_ema_calculation_mode_description": (
                "live tick/LTP based EMA calculation"
                if bool(getattr(config, "LIVE_EMA_CALCULATION_MODE", False))
                else "completed candle close based EMA calculation"
            ),
            "live_ema_interval_minutes": getattr(
                config,
                "LIVE_EMA_INTERVAL_MINUTES",
                1,
            ),
            "live_ema_fast_period": getattr(
                config,
                "LIVE_EMA_FAST_PERIOD",
                getattr(config, "EMA_FAST_PERIOD", 9),
            ),
            "live_ema_slow_period": getattr(
                config,
                "LIVE_EMA_SLOW_PERIOD",
                getattr(config, "EMA_SLOW_PERIOD", 21),
            ),
            "live_ema_tick_alert_once_per_direction": getattr(
                config,
                "LIVE_EMA_TICK_ALERT_ONCE_PER_DIRECTION",
                True,
            ),
            "live_ema_tick_min_price_change": getattr(
                config,
                "LIVE_EMA_TICK_MIN_PRICE_CHANGE",
                0.0,
            ),
        },
    }
