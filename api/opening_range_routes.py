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
    - Live touch monitoring can track R3/S3 touches for all option instruments.
    - No Opening Range instrument is permanently selected.
    - EMA crossover Telegram alerts are disabled.
    - EMA crossover WebSocket payloads include Opening Range levels when available.
    """

    return {
        "status": "success",
        "opening_range_status": get_opening_range_status(),
        "selected_or_instrument": get_selected_or_instrument_state(),
        "new_flow": {
            "description": (
                "Opening Range levels are calculated for all subscribed instruments. "
                "Every live EMA crossover is broadcast through WebSocket with that "
                "instrument's Opening Range levels when available. No selected OR "
                "instrument flow is active."
            ),
            "selected_or_flow": "disabled",
            "selected_or_ema_telegram_alerts": "disabled",
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
    - Calculates R1/S1, R2/S2, R3/S3, thresholds.
    - Scans post-OR candles for already touched R3/S3.
    - Stores results in memory for every subscribed instrument.
    - Saves to data/opening_range_results.json if enabled.

    New flow:
    - No first touched instrument is selected.
    - No selected OR EMA Telegram alert is sent.
    - EMA WebSocket events use this cache to attach OR levels per instrument.
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
                "and R3/S3 backfill touch scan completed for subscribed instruments."
            ),
            "opening_range_results_saved": selected_save_results,
            "selected_or_instrument": get_selected_or_instrument_state(),
            "ema_websocket_opening_range_enrichment": getattr(
                config,
                "EMA_CROSS_INCLUDE_OPENING_RANGE_LEVELS",
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

    Warning:
    - This can be large because it includes results for all instruments.
    - It may also include touch events and per-instrument touch status.
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

    Supports:
        /opening-range/instrument?instrument_key=NSE_FO%7C41012
        /opening-range/instrument?instrument_key=NSE_INDEX%7CNifty%2050
        /opening-range/instrument?strike=24500&striketype=ce
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

    This is useful for testing the new payload enrichment before waiting for
    a live EMA crossover.
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

    This does not update the global opening_range_cache.
    Use this for testing/debugging a single instrument.

    It also returns backfill R3/S3 touch events for that instrument.
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
# Selected OR Instrument Compatibility Routes
# ============================================================


@router.get("/opening-range/selected-instrument")
async def get_selected_opening_range_instrument():
    """
    Backward-compatible route.

    New flow:
    - Selected Opening Range instrument feature is disabled.
    - No instrument is permanently locked.
    - EMA crossovers are broadcast for all instruments through WebSocket.
    """

    return {
        "status": "success",
        "flow": "disabled",
        "message": (
            "Selected Opening Range instrument flow is disabled. "
            "EMA crossovers are broadcast for all instruments with Opening Range "
            "levels when available."
        ),
        "selected_or_instrument": get_selected_or_instrument_state(),
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
    - Selected OR EMA Telegram alerts are disabled.
    - EMA crossover events are sent through WebSocket only.
    """

    return {
        "status": "success",
        "flow": "disabled",
        "message": (
            "Selected OR EMA Telegram alerts are disabled. "
            "Use /ws/ema-crossover or /ws/ema-crossover/instrument for live EMA "
            "crossovers enriched with Opening Range levels."
        ),
        "limit": limit,
        "selected_or_instrument": get_selected_or_instrument_state(),
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
        description="Number of latest opening range R3/S3 touch events to return.",
    ),
):
    """
    Returns recent Opening Range R3/S3 touch events.

    Events can come from:
    - intraday_backfill_scan
    - live_tick

    Touch events are tracked for diagnostics/WebSocket use.
    They do not select a permanent instrument in the new flow.
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

    New flow:
    - Legacy touch Telegram is disabled by default.
    - Pending events are used only if OPENING_RANGE_LEGACY_TOUCH_TELEGRAM_ENABLED=True.
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

    Recommended new behavior keeps this disabled.
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
        },
    }
