import json
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.concurrency import run_in_threadpool

from core import config
from core.logger import get_logger
from services.ema_engine import internal_ema_engine
from services.history_service import (
    fetch_latest_intraday_ema_crosses,
    get_contract_info_by_key,
    get_instrument_crosses_directory,
)
from services.opening_range_service import (
    calculate_opening_range_for_all_subscribed,
    calculate_opening_range_for_instrument,
    flush_pending_touch_alerts,
    get_opening_range_cache,
    get_opening_range_for_instrument_from_cache,
    get_opening_range_levels_for_ema_event,
    get_opening_range_pending_touch_events,
    get_opening_range_status,
    get_opening_range_touch_events,
    get_selected_or_ema_alerts,
    get_selected_or_instrument_state,
)
from services.option_service import options_cache

logger = get_logger(__file__)
router = APIRouter()


def get_live_ema_calculation_mode_text() -> str:
    return (
        "tick_ltp"
        if bool(
            getattr(
                config,
                "LIVE_EMA_CALCULATION_MODE",
                False,
            )
        )
        else "candle_close"
    )


def get_live_ema_calculation_mode_payload() -> dict:
    flag = bool(
        getattr(
            config,
            "LIVE_EMA_CALCULATION_MODE",
            False,
        )
    )

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


def normalize_striketype(
    striketype: str | None,
) -> str | None:
    if not striketype:
        return None

    option_type = str(striketype).strip().upper()

    if option_type == "CALL":
        return "CE"

    if option_type == "PUT":
        return "PE"

    return option_type


def resolve_opening_range_instrument_key(
    instrument_key: str | None = None,
    strike: float | None = None,
    striketype: str | None = None,
) -> str:
    if instrument_key:
        return instrument_key

    if strike is None or not striketype:
        raise HTTPException(
            status_code=400,
            detail=("Provide either instrument_key or both strike and " "striketype."),
        )

    option_type = normalize_striketype(striketype)

    if option_type not in {"CE", "PE"}:
        raise HTTPException(
            status_code=400,
            detail=("Invalid striketype. Allowed values are " "CE, PE, CALL, and PUT."),
        )

    try:
        target_strike = float(strike)
    except Exception as ex:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid strike value: {ex}",
        ) from ex

    cache_data = options_cache.get("data", [])

    for item in cache_data:
        item_strike = item.get("strike_price")
        item_type = normalize_striketype(
            item.get("option_type")
            or item.get("instrument_type")
            or item.get("strike_type")
        )

        try:
            strike_matches = float(item_strike) == target_strike
        except Exception:
            strike_matches = False

        if strike_matches and item_type == option_type:
            resolved_key = item.get("instrument_key")

            if resolved_key:
                return resolved_key

    raise HTTPException(
        status_code=404,
        detail=(
            "No option instrument found for "
            f"strike={target_strike}, "
            f"striketype={option_type}. "
            "Make sure option contracts are loaded."
        ),
    )


def get_isolated_instrument_key(
    isolated_state: dict | None = None,
) -> str | None:
    isolated_state = isolated_state or get_selected_or_instrument_state() or {}

    instrument_key = isolated_state.get("instrument_key")

    if instrument_key:
        return str(instrument_key)

    nested_candidates = (
        isolated_state.get("isolated_instrument"),
        isolated_state.get("selected_instrument"),
        isolated_state.get("instrument"),
    )

    for candidate in nested_candidates:
        if not isinstance(candidate, dict):
            continue

        instrument_key = candidate.get("instrument_key")

        if instrument_key:
            return str(instrument_key)

    return None


def read_json_file_safe(
    file_path: Path,
) -> dict:
    if not file_path.exists():
        return {
            "exists": False,
            "file_path": str(file_path),
            "data": None,
            "error": None,
        }

    try:
        with open(
            file_path,
            "r",
            encoding="utf-8",
        ) as file:
            data = json.load(file)

        return {
            "exists": True,
            "file_path": str(file_path),
            "data": data,
            "error": None,
        }
    except Exception as ex:
        error_message = f"{type(ex).__name__}: {ex}"

        logger.error(
            "Failed reading EMA crossover file %s: %s",
            file_path,
            error_message,
        )

        return {
            "exists": True,
            "file_path": str(file_path),
            "data": None,
            "error": error_message,
        }


def get_saved_ema_crosses(
    instrument_key: str,
    cross_scope: str,
    limit: int,
) -> dict:
    contract_info = get_contract_info_by_key(instrument_key)

    directory = get_instrument_crosses_directory(
        instrument_key,
        contract_info,
    )

    if cross_scope == "historical":
        file_name = "historical_crosses.json"
    elif cross_scope == "intraday":
        file_name = "intraday_crosses.json"
    else:
        raise HTTPException(
            status_code=400,
            detail=(
                "Invalid cross_scope. Allowed values are " "historical and intraday."
            ),
        )

    file_result = read_json_file_safe(directory / file_name)

    payload = file_result.get("data")

    if isinstance(payload, dict):
        crosses = payload.get("crosses", [])
    elif isinstance(payload, list):
        crosses = payload
    else:
        crosses = []

    if not isinstance(crosses, list):
        crosses = []

    limited_crosses = crosses[-limit:] if limit else crosses

    return {
        "instrument_key": instrument_key,
        "contract_info": contract_info,
        "cross_scope": cross_scope,
        "directory": str(directory),
        "file_exists": file_result.get("exists"),
        "file_path": file_result.get("file_path"),
        "file_error": file_result.get("error"),
        "total_crosses": len(crosses),
        "returned_crosses": len(limited_crosses),
        "last_cross": (crosses[-1] if crosses else None),
        "crosses": limited_crosses,
    }


async def calculate_latest_intraday_crosses(
    instrument_key: str,
    interval: str,
    history_days: int,
    save_crosses: bool,
) -> dict:
    try:
        return await run_in_threadpool(
            fetch_latest_intraday_ema_crosses,
            instrument_key=instrument_key,
            interval=interval,
            history_days=history_days,
            save_crosses=save_crosses,
        )
    except Exception as ex:
        error_message = f"{type(ex).__name__}: {ex}"

        logger.error(
            "Latest intraday EMA crossover calculation "
            "failed. instrument_key=%s, error=%s",
            instrument_key,
            error_message,
        )

        raise HTTPException(
            status_code=500,
            detail=(
                "Latest intraday EMA crossover " f"calculation failed: {error_message}"
            ),
        ) from ex


@router.get("/history/live-ema/state")
async def get_internal_live_ema_state(
    instrument_key: str | None = Query(default=None),
    limit: int = Query(
        default=100,
        ge=1,
        le=1000,
    ),
):
    states = internal_ema_engine.get_states_snapshot()

    if instrument_key:
        states = (
            {instrument_key: states[instrument_key]} if instrument_key in states else {}
        )

    events = internal_ema_engine.get_events_snapshot(
        instrument_key,
        limit,
    )

    return {
        "status": "success",
        "source": "internal_completed_candle",
        "instrument_key": instrument_key,
        "limit": limit,
        "states": states,
        "latest_crossover": (events[-1] if events else None),
        "crossover_history": events,
    }


@router.get("/history/ema-crosses")
async def get_ema_crosses_from_file(
    instrument_key: str | None = Query(
        default=None,
        description="Upstox instrument key.",
    ),
    strike: float | None = Query(
        default=None,
        description="Option strike price.",
    ),
    striketype: str | None = Query(
        default=None,
        description="Option type: CE, PE, CALL, or PUT.",
    ),
    cross_scope: str = Query(
        default="intraday",
        description=("Cross file to return: intraday or historical."),
    ),
    limit: int = Query(
        default=100,
        ge=1,
        le=1000,
    ),
):
    resolved_instrument_key = resolve_opening_range_instrument_key(
        instrument_key=instrument_key,
        strike=strike,
        striketype=striketype,
    )

    normalized_scope = str(cross_scope).strip().lower()

    result = await run_in_threadpool(
        get_saved_ema_crosses,
        resolved_instrument_key,
        normalized_scope,
        limit,
    )

    return {
        "status": "success",
        "source": "saved_ema_cross_file",
        "live_ema_calculation": (get_live_ema_calculation_mode_payload()),
        "input": {
            "instrument_key": instrument_key,
            "strike": strike,
            "striketype": striketype,
            "cross_scope": normalized_scope,
            "limit": limit,
        },
        **result,
    }


@router.get("/history/ema-crosses/files")
async def get_ema_cross_files(
    instrument_key: str | None = Query(
        default=None,
        description="Upstox instrument key.",
    ),
    strike: float | None = Query(
        default=None,
        description="Option strike price.",
    ),
    striketype: str | None = Query(
        default=None,
        description="Option type: CE, PE, CALL, or PUT.",
    ),
    limit: int = Query(
        default=100,
        ge=1,
        le=1000,
    ),
):
    resolved_instrument_key = resolve_opening_range_instrument_key(
        instrument_key=instrument_key,
        strike=strike,
        striketype=striketype,
    )

    historical_result = await run_in_threadpool(
        get_saved_ema_crosses,
        resolved_instrument_key,
        "historical",
        limit,
    )

    intraday_result = await run_in_threadpool(
        get_saved_ema_crosses,
        resolved_instrument_key,
        "intraday",
        limit,
    )

    return {
        "status": "success",
        "source": "saved_ema_cross_files",
        "instrument_key": resolved_instrument_key,
        "live_ema_calculation": (get_live_ema_calculation_mode_payload()),
        "historical": historical_result,
        "intraday": intraday_result,
    }


@router.get("/history/ema-crosses/intraday/latest")
async def get_latest_intraday_ema_crosses(
    instrument_key: str | None = Query(
        default=None,
        description="Upstox instrument key.",
    ),
    strike: float | None = Query(
        default=None,
        description="Option strike price.",
    ),
    striketype: str | None = Query(
        default=None,
        description="Option type: CE, PE, CALL, or PUT.",
    ),
    interval: str = Query(
        default=None,
        description=("EMA candle interval. Default comes from config."),
    ),
    history_days: int = Query(
        default=None,
        ge=1,
        le=365,
        description=("Historical context days. " "Default comes from config."),
    ),
    save_crosses: bool = Query(
        default=True,
        description=(
            "Save calculated current-day crosses to " "intraday_crosses.json."
        ),
    ),
):
    resolved_instrument_key = resolve_opening_range_instrument_key(
        instrument_key=instrument_key,
        strike=strike,
        striketype=striketype,
    )

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

    result = await calculate_latest_intraday_crosses(
        instrument_key=resolved_instrument_key,
        interval=selected_interval,
        history_days=selected_history_days,
        save_crosses=save_crosses,
    )

    return {
        "status": result.get(
            "status",
            "success",
        ),
        "source": ("latest_intraday_candles_with_" "historical_ema_context"),
        "live_ema_calculation": (get_live_ema_calculation_mode_payload()),
        "input": {
            "instrument_key": instrument_key,
            "strike": strike,
            "striketype": striketype,
            "interval": selected_interval,
            "history_days": selected_history_days,
            "save_crosses": save_crosses,
        },
        "result": result,
    }


@router.get("/opening-range/status")
async def get_opening_range_latest_status():
    isolated_state = get_selected_or_instrument_state()

    return {
        "status": "success",
        "opening_range_status": (get_opening_range_status()),
        "isolated_instrument": isolated_state,
        "selected_or_instrument": isolated_state,
        "live_ema_calculation": (get_live_ema_calculation_mode_payload()),
        "new_flow": {
            "description": (
                "Opening Range levels are calculated for all "
                "subscribed instruments. The system monitors "
                "R2/R3/S2/S3 touches. After an eligible touch, "
                "one instrument is isolated using level priority "
                "and nearest strike to Opening Range average. "
                "Live EMA continues for all instruments, but "
                "Telegram EMA alerts are sent only for the "
                "isolated instrument."
            ),
            "selected_or_flow": ("mapped_to_isolated_instrument_flow"),
            "isolated_instrument_flow": "enabled",
            "live_ema_calculation": (get_live_ema_calculation_mode_payload()),
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
            "ema_cross_file_directory": getattr(
                config,
                "EMA_CROSSES_DIRECTORY",
                "data/ema_crosses",
            ),
        },
        "config": {
            "enabled": getattr(
                config,
                "OPENING_RANGE_ENABLED",
                True,
            ),
            "interval": getattr(
                config,
                "OPENING_RANGE_INTERVAL",
                "1minute",
            ),
            "candle_count": getattr(
                config,
                "OPENING_RANGE_CANDLE_COUNT",
                1,
            ),
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
            "fetch_hour": getattr(
                config,
                "OPENING_RANGE_FETCH_HOUR",
                9,
            ),
            "fetch_minute": getattr(
                config,
                "OPENING_RANGE_FETCH_MINUTE",
                18,
            ),
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
            "ema_crosses_directory": getattr(
                config,
                "EMA_CROSSES_DIRECTORY",
                "data/ema_crosses",
            ),
            "live_ema_calculation_mode_flag": getattr(
                config,
                "LIVE_EMA_CALCULATION_MODE",
                False,
            ),
            "live_ema_calculation_mode": (get_live_ema_calculation_mode_text()),
            "output_file": getattr(
                config,
                "OPENING_RANGE_OUTPUT_FILE",
                "data/opening_range_results.json",
            ),
        },
    }


@router.post("/opening-range/fetch")
async def trigger_opening_range_fetch(
    candle_count: int | None = Query(
        default=None,
        ge=1,
        le=60,
    ),
    save_results: bool | None = Query(default=None),
    max_workers: int | None = Query(
        default=None,
        ge=1,
        le=25,
    ),
):
    selected_candle_count = candle_count or getattr(
        config,
        "OPENING_RANGE_CANDLE_COUNT",
        1,
    )

    selected_save_results = (
        bool(save_results)
        if save_results is not None
        else bool(
            getattr(
                config,
                "OPENING_RANGE_SAVE_FILE",
                True,
            )
        )
    )

    selected_max_workers = max_workers or getattr(
        config,
        "OPENING_RANGE_MAX_WORKERS",
        5,
    )

    logger.info(
        "Manual opening range fetch requested. "
        "candle_count=%s, save_results=%s, "
        "max_workers=%s, live_ema_calculation_mode=%s",
        selected_candle_count,
        selected_save_results,
        selected_max_workers,
        get_live_ema_calculation_mode_text(),
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
                "Opening range intraday candles fetched, "
                "levels calculated, touch scan completed, "
                "and isolated instrument selection evaluated."
            ),
            "opening_range_results_saved": (selected_save_results),
            "isolated_instrument": isolated_state,
            "selected_or_instrument": isolated_state,
            "live_ema_calculation": (get_live_ema_calculation_mode_payload()),
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

        logger.error(
            "Manual opening range fetch failed: %s",
            error_message,
        )

        raise HTTPException(
            status_code=500,
            detail=("Opening range fetch failed: " f"{error_message}"),
        ) from ex


@router.get("/opening-range/cache")
async def get_opening_range_full_cache():
    return {
        "status": "success",
        "live_ema_calculation": (get_live_ema_calculation_mode_payload()),
        "cache": get_opening_range_cache(),
    }


@router.get("/opening-range/instrument")
async def get_opening_range_instrument(
    instrument_key: str | None = Query(default=None),
    strike: float | None = Query(default=None),
    striketype: str | None = Query(default=None),
):
    resolved_instrument_key = resolve_opening_range_instrument_key(
        instrument_key=instrument_key,
        strike=strike,
        striketype=striketype,
    )

    result = get_opening_range_for_instrument_from_cache(resolved_instrument_key)

    if not result:
        raise HTTPException(
            status_code=404,
            detail=(
                "Opening range result not found for "
                f"instrument_key={resolved_instrument_key}."
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
        "live_ema_calculation": (get_live_ema_calculation_mode_payload()),
        "result": result,
    }


@router.get("/opening-range/ema-context")
async def get_opening_range_ema_context(
    instrument_key: str | None = Query(default=None),
    strike: float | None = Query(default=None),
    striketype: str | None = Query(default=None),
):
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
        "live_ema_calculation": (get_live_ema_calculation_mode_payload()),
        "opening_range": context,
    }


@router.post("/opening-range/instrument/fetch")
async def fetch_opening_range_for_single_instrument(
    instrument_key: str | None = Query(default=None),
    strike: float | None = Query(default=None),
    striketype: str | None = Query(default=None),
    candle_count: int | None = Query(
        default=None,
        ge=1,
        le=60,
    ),
):
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

    try:
        result = await run_in_threadpool(
            calculate_opening_range_for_instrument,
            instrument_key=resolved_instrument_key,
            candle_count=selected_candle_count,
        )

        return {
            "status": "success",
            "message": ("Opening range calculated for " "single instrument."),
            "instrument_key": resolved_instrument_key,
            "input": {
                "instrument_key": instrument_key,
                "strike": strike,
                "striketype": striketype,
                "candle_count": candle_count,
            },
            "live_ema_calculation": (get_live_ema_calculation_mode_payload()),
            "result": result,
        }
    except Exception as ex:
        error_message = f"{type(ex).__name__}: {ex}"

        logger.error(
            "Single instrument opening range fetch " "failed for %s: %s",
            resolved_instrument_key,
            error_message,
        )

        raise HTTPException(
            status_code=500,
            detail=(
                "Opening range fetch failed for "
                f"{resolved_instrument_key}: "
                f"{error_message}"
            ),
        ) from ex


@router.get("/opening-range/selected-instrument")
async def get_selected_opening_range_instrument():
    isolated_state = get_selected_or_instrument_state()

    return {
        "status": "success",
        "flow": "isolated_instrument",
        "message": (
            "Selected OR compatibility route returns "
            "the isolated Opening Range instrument."
        ),
        "live_ema_calculation": (get_live_ema_calculation_mode_payload()),
        "selected_or_instrument": isolated_state,
        "isolated_instrument": isolated_state,
    }


@router.get("/opening-range/selected-instrument/ema-alerts")
async def get_selected_opening_range_ema_alerts(
    limit: int = Query(
        default=100,
        ge=1,
        le=1000,
    ),
    include_calculated_crosses: bool = Query(
        default=True,
    ),
    refresh_intraday: bool = Query(
        default=False,
    ),
    interval: str = Query(
        default=None,
    ),
    history_days: int = Query(
        default=None,
        ge=1,
        le=365,
    ),
):
    return await get_isolated_opening_range_ema_alerts(
        limit=limit,
        include_calculated_crosses=(include_calculated_crosses),
        refresh_intraday=refresh_intraday,
        interval=interval,
        history_days=history_days,
    )


@router.get("/opening-range/isolated-instrument")
async def get_isolated_opening_range_instrument():
    isolated_state = get_selected_or_instrument_state()

    isolated_key = get_isolated_instrument_key(isolated_state)

    return {
        "status": "success",
        "flow": "isolated_instrument",
        "live_ema_calculation": (get_live_ema_calculation_mode_payload()),
        "isolated_instrument_key": isolated_key,
        "isolated_instrument": isolated_state,
    }


@router.get("/opening-range/isolated-instrument/ema-alerts")
async def get_isolated_opening_range_ema_alerts(
    limit: int = Query(
        default=100,
        ge=1,
        le=1000,
    ),
    include_calculated_crosses: bool = Query(
        default=True,
        description=("Include saved calculated intraday crosses."),
    ),
    refresh_intraday: bool = Query(
        default=False,
        description=(
            "Fetch latest intraday candles, recalculate "
            "today's crosses, and update the JSON file."
        ),
    ),
    interval: str | None = Query(
        default=None,
    ),
    history_days: int | None = Query(
        default=None,
        ge=1,
        le=365,
    ),
):
    isolated_state = get_selected_or_instrument_state()

    isolated_key = get_isolated_instrument_key(isolated_state)

    telegram_alerts = get_selected_or_ema_alerts(limit=limit)

    if not isolated_key:
        return {
            "status": "success",
            "flow": "isolated_instrument",
            "limit": limit,
            "live_ema_calculation": (get_live_ema_calculation_mode_payload()),
            "isolated_instrument": isolated_state,
            "isolated_instrument_key": None,
            "telegram_alerts_count": len(telegram_alerts or []),
            "alerts": telegram_alerts or [],
            "calculated_intraday_ema_crosses": [],
            "calculated_intraday_ema_crosses_count": 0,
            "last_calculated_intraday_ema_cross": None,
            "message": ("No isolated instrument is currently selected."),
        }

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

    refresh_result = None

    if refresh_intraday:
        refresh_result = await calculate_latest_intraday_crosses(
            instrument_key=isolated_key,
            interval=selected_interval,
            history_days=selected_history_days,
            save_crosses=True,
        )

    saved_cross_result = None
    calculated_crosses = []

    if include_calculated_crosses:
        saved_cross_result = await run_in_threadpool(
            get_saved_ema_crosses,
            isolated_key,
            "intraday",
            limit,
        )

        calculated_crosses = saved_cross_result.get(
            "crosses",
            [],
        )

    return {
        "status": "success",
        "flow": "isolated_instrument",
        "limit": limit,
        "live_ema_calculation": (get_live_ema_calculation_mode_payload()),
        "isolated_instrument_key": isolated_key,
        "isolated_instrument": isolated_state,
        "telegram_alerts_count": len(telegram_alerts or []),
        "alerts": telegram_alerts or [],
        "include_calculated_crosses": (include_calculated_crosses),
        "refresh_intraday": refresh_intraday,
        "calculated_intraday_ema_crosses_count": len(calculated_crosses),
        "last_calculated_intraday_ema_cross": (
            calculated_crosses[-1] if calculated_crosses else None
        ),
        "calculated_intraday_ema_crosses": (calculated_crosses),
        "saved_cross_file": saved_cross_result,
        "latest_intraday_calculation": refresh_result,
    }


@router.get("/opening-range/isolated-instrument/" "ema-crosses/intraday/latest")
async def get_isolated_instrument_latest_intraday_crosses(
    interval: str | None = Query(default=None),
    history_days: int | None = Query(
        default=None,
        ge=1,
        le=365,
    ),
    save_crosses: bool = Query(default=True),
):
    isolated_state = get_selected_or_instrument_state()

    isolated_key = get_isolated_instrument_key(isolated_state)

    if not isolated_key:
        raise HTTPException(
            status_code=404,
            detail=("No isolated Opening Range instrument " "is currently selected."),
        )

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

    result = await calculate_latest_intraday_crosses(
        instrument_key=isolated_key,
        interval=selected_interval,
        history_days=selected_history_days,
        save_crosses=save_crosses,
    )

    return {
        "status": result.get(
            "status",
            "success",
        ),
        "flow": "isolated_instrument",
        "source": ("latest_intraday_candles_with_" "historical_ema_context"),
        "isolated_instrument_key": isolated_key,
        "isolated_instrument": isolated_state,
        "live_ema_calculation": (get_live_ema_calculation_mode_payload()),
        "result": result,
    }


@router.get("/opening-range/touch-events")
async def get_opening_range_touch_events_route(
    limit: int = Query(
        default=100,
        ge=1,
        le=1000,
    ),
):
    return {
        "status": "success",
        "limit": limit,
        "live_ema_calculation": (get_live_ema_calculation_mode_payload()),
        "events": get_opening_range_touch_events(limit=limit),
        "isolated_instrument": (get_selected_or_instrument_state()),
    }


@router.get("/opening-range/touch-events/pending")
async def get_opening_range_pending_touch_events_route():
    return {
        "status": "success",
        "legacy_touch_telegram_enabled": getattr(
            config,
            "OPENING_RANGE_LEGACY_TOUCH_TELEGRAM_ENABLED",
            False,
        ),
        "live_ema_calculation": (get_live_ema_calculation_mode_payload()),
        "pending_events": (get_opening_range_pending_touch_events()),
    }


@router.post("/opening-range/touch-events/flush")
async def flush_opening_range_pending_touch_events_route(
    force: bool = Query(default=True),
):
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
            "legacy_touch_telegram_enabled": (legacy_enabled),
            "live_ema_calculation": (get_live_ema_calculation_mode_payload()),
            "message": (
                "Pending touch events flushed to Telegram."
                if sent
                else (
                    "No Telegram message sent. There may "
                    "be no pending events, or legacy touch "
                    "Telegram is disabled."
                )
            ),
        }
    except Exception as ex:
        error_message = f"{type(ex).__name__}: {ex}"

        logger.error(
            "Manual opening range touch alert " "flush failed: %s",
            error_message,
        )

        raise HTTPException(
            status_code=500,
            detail=("Touch alert flush failed: " f"{error_message}"),
        ) from ex


@router.get("/opening-range/file")
async def get_opening_range_file_status():
    opening_range_file = Path(
        getattr(
            config,
            "OPENING_RANGE_OUTPUT_FILE",
            "data/opening_range_results.json",
        )
    )

    touch_events_file = Path(
        getattr(
            config,
            "OPENING_RANGE_TOUCH_EVENTS_OUTPUT_FILE",
            "data/opening_range_touch_events.json",
        )
    )

    isolated_file = Path(
        getattr(
            config,
            "OPENING_RANGE_ISOLATED_INSTRUMENT_OUTPUT_FILE",
            "data/isolated_opening_range_instrument.json",
        )
    )

    ema_crosses_directory = Path(
        getattr(
            config,
            "EMA_CROSSES_DIRECTORY",
            "data/ema_crosses",
        )
    )

    return {
        "status": "success",
        "live_ema_calculation": (get_live_ema_calculation_mode_payload()),
        "opening_range_file_exists": (opening_range_file.exists()),
        "opening_range_file_path": str(opening_range_file),
        "save_file_enabled": getattr(
            config,
            "OPENING_RANGE_SAVE_FILE",
            True,
        ),
        "touch_events_file_exists": (touch_events_file.exists()),
        "touch_events_file_path": str(touch_events_file),
        "touch_events_save_test_file": getattr(
            config,
            "OPENING_RANGE_TOUCH_EVENTS_SAVE_TEST_FILE",
            True,
        ),
        "isolated_instrument_file_exists": (isolated_file.exists()),
        "isolated_instrument_file_path": str(isolated_file),
        "ema_crosses_directory_exists": (ema_crosses_directory.exists()),
        "ema_crosses_directory": str(ema_crosses_directory),
    }


@router.get("/opening-range/config")
async def get_opening_range_config():
    return {
        "status": "success",
        "live_ema_calculation": (get_live_ema_calculation_mode_payload()),
        "config": {
            "enabled": getattr(
                config,
                "OPENING_RANGE_ENABLED",
                True,
            ),
            "interval": getattr(
                config,
                "OPENING_RANGE_INTERVAL",
                "1minute",
            ),
            "candle_count": getattr(
                config,
                "OPENING_RANGE_CANDLE_COUNT",
                1,
            ),
            "market_timezone": getattr(
                config,
                "MARKET_TIMEZONE",
                "Asia/Kolkata",
            ),
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
            "fetch_hour": getattr(
                config,
                "OPENING_RANGE_FETCH_HOUR",
                9,
            ),
            "fetch_minute": getattr(
                config,
                "OPENING_RANGE_FETCH_MINUTE",
                18,
            ),
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
            "max_workers": getattr(
                config,
                "OPENING_RANGE_MAX_WORKERS",
                5,
            ),
            "request_sleep_seconds": getattr(
                config,
                "OPENING_RANGE_REQUEST_SLEEP_SECONDS",
                0.15,
            ),
            "save_file": getattr(
                config,
                "OPENING_RANGE_SAVE_FILE",
                True,
            ),
            "output_file": getattr(
                config,
                "OPENING_RANGE_OUTPUT_FILE",
                "data/opening_range_results.json",
            ),
            "ema_crosses_directory": getattr(
                config,
                "EMA_CROSSES_DIRECTORY",
                "data/ema_crosses",
            ),
            "historical_ema_interval": getattr(
                config,
                "HISTORICAL_CANDLE_INTERVAL",
                "1minute",
            ),
            "historical_ema_days": getattr(
                config,
                "HISTORICAL_CANDLE_DAYS",
                10,
            ),
            "historical_ema_fast_period": getattr(
                config,
                "EMA_FAST_PERIOD",
                9,
            ),
            "historical_ema_slow_period": getattr(
                config,
                "EMA_SLOW_PERIOD",
                21,
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
            "ema_alert_bullish_option_type": getattr(
                config,
                "EMA_ALERT_BULLISH_OPTION_TYPE",
                "CE",
            ),
            "ema_alert_bearish_option_type": getattr(
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
            "live_ema_calculation_mode": (get_live_ema_calculation_mode_text()),
            "live_ema_calculation_mode_description": (
                "live tick/LTP based EMA calculation"
                if bool(
                    getattr(
                        config,
                        "LIVE_EMA_CALCULATION_MODE",
                        False,
                    )
                )
                else ("completed candle close based " "EMA calculation")
            ),
            "live_ema_interval_minutes": getattr(
                config,
                "LIVE_EMA_INTERVAL_MINUTES",
                1,
            ),
            "live_ema_fast_period": getattr(
                config,
                "LIVE_EMA_FAST_PERIOD",
                getattr(
                    config,
                    "EMA_FAST_PERIOD",
                    9,
                ),
            ),
            "live_ema_slow_period": getattr(
                config,
                "LIVE_EMA_SLOW_PERIOD",
                getattr(
                    config,
                    "EMA_SLOW_PERIOD",
                    21,
                ),
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
