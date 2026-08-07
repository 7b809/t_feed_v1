from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.concurrency import run_in_threadpool

from core import config
from core.logger import get_logger
from services.option_service import options_cache
from services.history_service import (
    fetch_historical_candles_for_all_subscribed,
    fetch_historical_candles_for_instrument,
    get_historical_candles_status,
    historical_candles_cache,
)
from services.live_ema_service import live_ema_service
from services.opening_range_service import get_opening_range_levels_for_ema_event

logger = get_logger(__file__)

router = APIRouter()


# ============================================================
# Instrument Resolution Helper
# ============================================================


def resolve_instrument_key(
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
           strike=24500, striketype=pe

    Uses options_cache["data"] loaded by option_service.
    """

    if instrument_key:
        return instrument_key

    if strike is None or not striketype:
        raise HTTPException(
            status_code=400,
            detail=(
                "Provide either instrument_key or both strike and striketype. "
                "Example: /history/instrument?strike=24500&striketype=pe"
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
# EMA Event Enrichment Helper
# ============================================================


def enrich_ema_event_with_opening_range(event: dict) -> dict:
    """
    Adds compact Opening Range context to a live EMA crossover event.

    This mirrors the WebSocket enrichment done in services/upstox_websocket.py.

    Expected compact output:

    {
        "type": "live_ema_cross",
        "instrument_key": "...",
        "timestamp": "...",
        "cross_type": "...",
        "interval_minutes": 1,
        "close": ...,
        "current_signal": "...",
        "source": "live_feed",
        "created_at": "...",
        "opening_range": {
            "r1": ...,
            "s1": ...,
            "r2": ...,
            "s2": ...,
            "r3": ...,
            "s3": ...,
            "sub_resistance": ...,
            "sub_support": ...
        },
        "touch_status": {...},
        "latest_intraday_close": ...,
        "latest_main_index_ltp": ...,
        "processed_at": "..."
    }
    """

    if not isinstance(event, dict):
        return event

    enriched_event = dict(event)
    instrument_key = enriched_event.get("instrument_key")

    try:
        opening_range_payload = get_opening_range_levels_for_ema_event(instrument_key)

        if isinstance(opening_range_payload, dict):
            enriched_event.update(opening_range_payload)

    except Exception as ex:
        logger.error(
            f"Opening Range enrichment failed for EMA event. "
            f"instrument_key={instrument_key}, "
            f"error={type(ex).__name__}: {ex}"
        )

        enriched_event.update(
            {
                "opening_range": {},
                "touch_status": {},
                "latest_intraday_close": None,
                "latest_main_index_ltp": None,
                "processed_at": None,
            }
        )

    return enriched_event


# ============================================================
# Historical EMA Routes
# ============================================================


@router.get("/history/status")
async def get_history_status():
    """
    Returns latest historical candle and EMA crossover processing status.

    Important:
    - Raw candles are not saved as files.
    - Candles are fetched, used for EMA 9/21 calculation, and discarded.
    - EMA/crossover results are kept in memory.
    - If TEST_FLAG=True, EMA/crossover results are saved in data folder.
    - Live EMA service is initialized from historical EMA output.

    New flow:
    - Live EMA runs for all initialized instruments.
    - EMA crossover Telegram alerts are disabled.
    - EMA crossover WebSocket events are enriched with compact Opening Range levels.
    """

    return {
        "status": "success",
        "historical_candle_status": get_historical_candles_status(),
        "live_ema_status": live_ema_service.get_status(),
        "test_flag": getattr(config, "TEST_FLAG", False),
        "new_flow": {
            "selected_or_filtering": "disabled",
            "telegram_ema_alerts": "disabled",
            "ema_cross_include_opening_range_levels": getattr(
                config,
                "EMA_CROSS_INCLUDE_OPENING_RANGE_LEVELS",
                True,
            ),
            "ema_payload_mode": "compact",
        },
        "ema_config": {
            "fast_period": getattr(config, "EMA_FAST_PERIOD", 9),
            "slow_period": getattr(config, "EMA_SLOW_PERIOD", 21),
            "output_file": getattr(
                config,
                "EMA_CROSS_OUTPUT_FILE",
                "data/ema_cross_results.json",
            ),
        },
        "live_ema_config": {
            "enabled": getattr(config, "LIVE_EMA_ENABLED", True),
            "interval_minutes": getattr(config, "LIVE_EMA_INTERVAL_MINUTES", 1),
            "fast_period": getattr(config, "LIVE_EMA_FAST_PERIOD", 9),
            "slow_period": getattr(config, "LIVE_EMA_SLOW_PERIOD", 21),
            "output_file": getattr(
                config,
                "LIVE_EMA_OUTPUT_FILE",
                "data/live_ema_cross_results.json",
            ),
            "include_opening_range_levels": getattr(
                config,
                "EMA_CROSS_INCLUDE_OPENING_RANGE_LEVELS",
                True,
            ),
            "payload_mode": "compact",
        },
    }


@router.post("/history/fetch")
async def trigger_history_fetch(
    interval: str = Query(
        default=None,
        description="Candle interval, e.g. 1minute, 30minute, day",
    ),
    history_days: int = Query(
        default=None,
        ge=1,
        le=60,
        description="Number of calendar days to fetch. Default comes from config.",
    ),
    save_results: bool = Query(
        default=None,
        description=(
            "Whether to save EMA crossover results. "
            "If not provided, config.TEST_FLAG will be used."
        ),
    ),
    max_workers: int = Query(
        default=None,
        ge=1,
        le=25,
        description=(
            "Parallel worker count for instrument-level historical fetch. "
            "Default comes from config.HISTORICAL_CANDLE_MAX_WORKERS."
        ),
    ),
):
    """
    Manually triggers historical candle fetch for all subscribed instruments.

    Behavior:
    - Fetches historical candles for all subscribed instruments.
    - Does not save raw candle files.
    - Calculates EMA 9 and EMA 21 using all fetched candles.
    - Detects bullish and bearish EMA crossovers.
    - Stores EMA/crossover summary in memory.
    - Saves EMA/crossover results only if save_results=True or TEST_FLAG=True.
    - Initializes live EMA continuation state from historical EMA values.

    New flow:
    - Live EMA continuation is initialized for all valid instruments.
    - No selected Opening Range instrument filtering is used.
    - No EMA Telegram alert is sent.
    """

    selected_interval = interval or getattr(
        config,
        "HISTORICAL_CANDLE_INTERVAL",
        "1minute",
    )

    selected_history_days = history_days or getattr(
        config,
        "HISTORICAL_CANDLE_DAYS",
        10,
    )

    selected_max_workers = max_workers or getattr(
        config,
        "HISTORICAL_CANDLE_MAX_WORKERS",
        5,
    )

    selected_save_results = (
        bool(save_results)
        if save_results is not None
        else bool(getattr(config, "TEST_FLAG", False))
    )

    logger.info(
        f"Manual historical EMA crossover fetch requested. "
        f"interval={selected_interval}, "
        f"history_days={selected_history_days}, "
        f"max_workers={selected_max_workers}, "
        f"save_results={selected_save_results}"
    )

    try:
        summary = await run_in_threadpool(
            fetch_historical_candles_for_all_subscribed,
            interval=selected_interval,
            history_days=selected_history_days,
            save_data=selected_save_results,
            max_workers=selected_max_workers,
        )

        return {
            "status": "success",
            "message": (
                "Historical candles fetched, EMA 9/21 crossover calculation completed, "
                "and live EMA service initialized if enabled."
            ),
            "raw_candles_saved": False,
            "ema_results_saved": selected_save_results,
            "live_ema_initialized": summary.get("live_ema_initialized"),
            "selected_or_filtering": "disabled",
            "telegram_ema_alerts": "disabled",
            "live_ema_payload_mode": "compact",
            "summary": summary,
        }

    except Exception as ex:
        error_message = f"{type(ex).__name__}: {ex}"

        logger.error(f"Manual historical EMA crossover fetch failed: {error_message}")

        raise HTTPException(
            status_code=500,
            detail=f"Historical EMA crossover fetch failed: {error_message}",
        )


@router.get("/history/instrument")
async def fetch_history_for_single_instrument(
    instrument_key: str = Query(
        default=None,
        description="Instrument key, e.g. NSE_INDEX|Nifty 50 or NSE_FO|65860",
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
        description=("Option type: ce or pe. Optional if instrument_key is provided."),
    ),
    interval: str = Query(
        default=None,
        description="Candle interval, e.g. 1minute, 30minute, day",
    ),
    from_date: str = Query(
        default=None,
        description="From date in YYYY-MM-DD format. Optional.",
    ),
    to_date: str = Query(
        default=None,
        description="To date in YYYY-MM-DD format. Optional.",
    ),
):
    """
    Fetches historical candles for one instrument and returns processed EMA result.

    Supports:
        /history/instrument?instrument_key=NSE_FO%7C41012
        /history/instrument?strike=24500&striketype=pe
        /history/instrument?instrument_key=NSE_INDEX%7CNifty%2050
    """

    resolved_instrument_key = resolve_instrument_key(
        instrument_key=instrument_key,
        strike=strike,
        striketype=striketype,
    )

    selected_interval = interval or getattr(
        config,
        "HISTORICAL_CANDLE_INTERVAL",
        "1minute",
    )

    logger.info(
        f"Single instrument historical EMA crossover fetch requested. "
        f"instrument_key={resolved_instrument_key}, "
        f"input_instrument_key={instrument_key}, "
        f"strike={strike}, "
        f"striketype={striketype}, "
        f"interval={selected_interval}, "
        f"from_date={from_date}, "
        f"to_date={to_date}"
    )

    try:
        result = await run_in_threadpool(
            fetch_historical_candles_for_instrument,
            instrument_key=resolved_instrument_key,
            interval=selected_interval,
            from_date=from_date,
            to_date=to_date,
            api_version=getattr(config, "HISTORICAL_CANDLE_API_VERSION", "2.0"),
            max_days_per_request=getattr(
                config,
                "HISTORICAL_CANDLE_MAX_DAYS_PER_REQUEST",
                7,
            ),
            save_data=False,
        )

        return {
            "status": "success",
            "message": (
                "Historical candles fetched for instrument and processed locally. "
                "Raw candles were not saved."
            ),
            "raw_candles_saved": False,
            "resolved_instrument_key": resolved_instrument_key,
            "input": {
                "instrument_key": instrument_key,
                "strike": strike,
                "striketype": striketype,
            },
            "result": result,
        }

    except Exception as ex:
        error_message = f"{type(ex).__name__}: {ex}"

        logger.error(
            f"Single instrument historical EMA crossover fetch failed for "
            f"instrument_key={resolved_instrument_key}: {error_message}"
        )

        raise HTTPException(
            status_code=500,
            detail=f"Instrument historical EMA crossover fetch failed: {error_message}",
        )


@router.get("/history/cache")
async def get_history_cache():
    """
    Returns current historical/EMA processing cache summary.

    Notes:
    - Raw candle arrays are not stored permanently.
    - EMA/crossover result summaries are stored in memory.
    """

    return {
        "status": "success",
        "raw_candles_saved": False,
        "cache": {
            "last_run_at": historical_candles_cache.get("last_run_at"),
            "from_date": historical_candles_cache.get("from_date"),
            "to_date": historical_candles_cache.get("to_date"),
            "intraday_today_used": historical_candles_cache.get("intraday_today_used"),
            "interval": historical_candles_cache.get("interval"),
            "total_instruments": historical_candles_cache.get("total_instruments"),
            "success_count": historical_candles_cache.get("success_count"),
            "failed_count": historical_candles_cache.get("failed_count"),
            "empty_count": historical_candles_cache.get("empty_count"),
            "insufficient_data_count": historical_candles_cache.get(
                "insufficient_data_count"
            ),
            "total_candles": historical_candles_cache.get("total_candles"),
            "ema_fast_period": historical_candles_cache.get("ema_fast_period"),
            "ema_slow_period": historical_candles_cache.get("ema_slow_period"),
            "ema_results_file_path": historical_candles_cache.get(
                "ema_results_file_path"
            ),
            "live_ema_initialized": historical_candles_cache.get(
                "live_ema_initialized"
            ),
            "data": historical_candles_cache.get("data", {}),
            "errors": historical_candles_cache.get("errors", {}),
        },
        "ema_config": {
            "fast_period": getattr(config, "EMA_FAST_PERIOD", 9),
            "slow_period": getattr(config, "EMA_SLOW_PERIOD", 21),
            "output_file": getattr(
                config,
                "EMA_CROSS_OUTPUT_FILE",
                "data/ema_cross_results.json",
            ),
        },
        "live_ema_status": live_ema_service.get_status(),
    }


@router.get("/history/ema-results-file")
async def get_ema_results_file_status():
    """
    Checks whether historical EMA crossover result file exists.

    This file is created only when:
    - TEST_FLAG=True, or
    - /history/fetch?save_results=true is used.

    Default output:
        data/ema_cross_results.json
    """

    output_file = getattr(
        config,
        "EMA_CROSS_OUTPUT_FILE",
        "data/ema_cross_results.json",
    )

    file_path = Path(output_file)

    return {
        "status": "success",
        "test_flag": getattr(config, "TEST_FLAG", False),
        "ema_results_file_exists": file_path.exists(),
        "ema_results_file_path": str(file_path),
    }


@router.get("/history/config")
async def get_history_config():
    """
    Returns historical candle, historical EMA, and live EMA configuration.
    """

    return {
        "status": "success",
        "config": {
            "historical_candle_enabled": getattr(
                config,
                "HISTORICAL_CANDLE_ENABLED",
                True,
            ),
            "historical_candle_days": getattr(
                config,
                "HISTORICAL_CANDLE_DAYS",
                10,
            ),
            "historical_candle_interval": getattr(
                config,
                "HISTORICAL_CANDLE_INTERVAL",
                "1minute",
            ),
            "historical_candle_api_version": getattr(
                config,
                "HISTORICAL_CANDLE_API_VERSION",
                "2.0",
            ),
            "historical_candle_max_days_per_request": getattr(
                config,
                "HISTORICAL_CANDLE_MAX_DAYS_PER_REQUEST",
                7,
            ),
            "historical_candle_max_workers": getattr(
                config,
                "HISTORICAL_CANDLE_MAX_WORKERS",
                5,
            ),
            "historical_candle_request_sleep_seconds": getattr(
                config,
                "HISTORICAL_CANDLE_REQUEST_SLEEP_SECONDS",
                0.15,
            ),
            "market_timezone": getattr(config, "MARKET_TIMEZONE", "Asia/Kolkata"),
            "market_open_hour": getattr(config, "MARKET_OPEN_HOUR", 9),
            "market_open_minute": getattr(config, "MARKET_OPEN_MINUTE", 15),
            "test_flag": getattr(config, "TEST_FLAG", False),
            "ema_fast_period": getattr(config, "EMA_FAST_PERIOD", 9),
            "ema_slow_period": getattr(config, "EMA_SLOW_PERIOD", 21),
            "ema_cross_output_file": getattr(
                config,
                "EMA_CROSS_OUTPUT_FILE",
                "data/ema_cross_results.json",
            ),
            "live_ema_enabled": getattr(config, "LIVE_EMA_ENABLED", True),
            "live_ema_interval_minutes": getattr(
                config,
                "LIVE_EMA_INTERVAL_MINUTES",
                1,
            ),
            "live_ema_fast_period": getattr(config, "LIVE_EMA_FAST_PERIOD", 9),
            "live_ema_slow_period": getattr(config, "LIVE_EMA_SLOW_PERIOD", 21),
            "live_ema_save_test_file": getattr(
                config,
                "LIVE_EMA_SAVE_TEST_FILE",
                True,
            ),
            "live_ema_output_file": getattr(
                config,
                "LIVE_EMA_OUTPUT_FILE",
                "data/live_ema_cross_results.json",
            ),
            "live_ema_max_events_in_memory": getattr(
                config,
                "LIVE_EMA_MAX_EVENTS_IN_MEMORY",
                5000,
            ),
            "live_ema_payload_mode": "compact",
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
            "selected_or_filtering": "disabled",
            "telegram_ema_alerts": "disabled",
        },
    }


# ============================================================
# Live EMA Routes
# ============================================================


@router.get("/history/live-ema/status")
async def get_live_ema_status():
    """
    Returns live EMA service status.
    """

    return {
        "status": "success",
        "live_ema_status": live_ema_service.get_status(),
        "ema_cross_include_opening_range_levels": getattr(
            config,
            "EMA_CROSS_INCLUDE_OPENING_RANGE_LEVELS",
            True,
        ),
        "payload_mode": "compact",
        "selected_or_filtering": "disabled",
        "telegram_ema_alerts": "disabled",
    }


@router.get("/history/live-ema/events")
async def get_live_ema_events(
    limit: int = Query(
        default=100,
        ge=1,
        le=1000,
        description="Number of latest live EMA crossover events to return.",
    ),
    include_opening_range: bool = Query(
        default=True,
        description=(
            "If true, each compact EMA event is enriched with compact Opening Range "
            "levels for the same instrument when available."
        ),
    ),
):
    """
    Returns recent live EMA crossover events.

    Events are generated only when live feed candles produce EMA 9/21 crossovers.

    New flow:
    - Events are compact.
    - Events are not filtered by selected Opening Range instrument.
    - Telegram EMA alerts are disabled.
    - When include_opening_range=true, each event includes compact Opening Range context.
    """

    events = live_ema_service.get_events(limit=limit)

    if include_opening_range:
        events = [enrich_ema_event_with_opening_range(event) for event in events]

    return {
        "status": "success",
        "limit": limit,
        "include_opening_range": include_opening_range,
        "payload_mode": "compact",
        "events": events,
    }


@router.get("/history/live-ema/instrument")
async def get_live_ema_instrument_state(
    instrument_key: str = Query(
        default=None,
        description="Instrument key, e.g. NSE_INDEX|Nifty 50 or NSE_FO|41011",
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
        description=("Option type: ce or pe. Optional if instrument_key is provided."),
    ),
    include_opening_range: bool = Query(
        default=True,
        description="If true, state and recent crossover events include compact Opening Range context.",
    ),
):
    """
    Returns live EMA state for one instrument.

    Supports:
        /history/live-ema/instrument?instrument_key=NSE_FO%7C41012
        /history/live-ema/instrument?strike=24500&striketype=pe
        /history/live-ema/instrument?instrument_key=NSE_INDEX%7CNifty%2050
    """

    resolved_instrument_key = resolve_instrument_key(
        instrument_key=instrument_key,
        strike=strike,
        striketype=striketype,
    )

    state = live_ema_service.get_instrument_state(resolved_instrument_key)

    if not state:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Live EMA state not found for "
                f"instrument_key={resolved_instrument_key}"
            ),
        )

    if include_opening_range:
        try:
            opening_range_payload = get_opening_range_levels_for_ema_event(
                resolved_instrument_key
            )

            if isinstance(opening_range_payload, dict):
                state.update(opening_range_payload)

        except Exception as ex:
            logger.error(
                f"Opening Range enrichment failed for live EMA state. "
                f"instrument_key={resolved_instrument_key}, "
                f"error={type(ex).__name__}: {ex}"
            )

            state.update(
                {
                    "opening_range": {},
                    "touch_status": {},
                    "latest_intraday_close": None,
                    "latest_main_index_ltp": None,
                    "processed_at": None,
                }
            )

        recent_crossovers = state.get("recent_crossovers", [])

        if recent_crossovers:
            state["recent_crossovers"] = [
                enrich_ema_event_with_opening_range(event)
                for event in recent_crossovers
            ]

        last_crossover = state.get("last_crossover")

        if last_crossover:
            state["last_crossover"] = enrich_ema_event_with_opening_range(
                last_crossover
            )

    return {
        "status": "success",
        "instrument_key": resolved_instrument_key,
        "include_opening_range": include_opening_range,
        "payload_mode": "compact",
        "input": {
            "instrument_key": instrument_key,
            "strike": strike,
            "striketype": striketype,
        },
        "state": state,
    }


@router.get("/history/live-ema/instruments")
async def get_all_live_ema_instrument_summaries(
    include_opening_range: bool = Query(
        default=False,
        description=(
            "If true, each instrument summary includes compact Opening Range context. "
            "Can be large if many instruments are tracked."
        ),
    ),
):
    """
    Returns lightweight live EMA summaries for all tracked instruments.

    By default, Opening Range context is not attached to keep this endpoint light.
    """

    data = live_ema_service.get_all_instrument_summaries()

    if include_opening_range:
        for instrument_key, item in data.items():
            try:
                opening_range_payload = get_opening_range_levels_for_ema_event(
                    instrument_key
                )

                if isinstance(opening_range_payload, dict):
                    item.update(opening_range_payload)

            except Exception as ex:
                logger.error(
                    f"Opening Range enrichment failed for instrument summary. "
                    f"instrument_key={instrument_key}, "
                    f"error={type(ex).__name__}: {ex}"
                )

                item.update(
                    {
                        "opening_range": {},
                        "touch_status": {},
                        "latest_intraday_close": None,
                        "latest_main_index_ltp": None,
                        "processed_at": None,
                    }
                )

    return {
        "status": "success",
        "tracked_instruments": live_ema_service.get_status().get(
            "tracked_instruments",
            0,
        ),
        "include_opening_range": include_opening_range,
        "payload_mode": "compact",
        "data": data,
    }


@router.get("/history/live-ema/file")
async def get_live_ema_results_file_status():
    """
    Checks whether live EMA crossover result file exists.

    This file is created only when:
    - TEST_FLAG=True
    - LIVE_EMA_SAVE_TEST_FILE=True
    - At least one live EMA crossover event occurred

    Default output:
        data/live_ema_cross_results.json
    """

    output_file = getattr(
        config,
        "LIVE_EMA_OUTPUT_FILE",
        "data/live_ema_cross_results.json",
    )

    file_path = Path(output_file)

    return {
        "status": "success",
        "test_flag": getattr(config, "TEST_FLAG", False),
        "live_ema_save_test_file": getattr(config, "LIVE_EMA_SAVE_TEST_FILE", True),
        "live_ema_results_file_exists": file_path.exists(),
        "live_ema_results_file_path": str(file_path),
        "payload_mode": "compact",
        "note": (
            "Saved file contains compact live EMA events as generated by "
            "live_ema_service. Compact Opening Range enrichment is applied at "
            "API/WebSocket response time."
        ),
    }
