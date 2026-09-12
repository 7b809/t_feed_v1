"""Opening Range calculation orchestration service."""

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from typing import Any

from core.logger import get_logger

from . import state as runtime_state
from .candle_utils import (
    get_contract_info_by_key,
    get_live_ema_calculation_mode_text,
    get_now_market_time,
    get_opening_range_end_datetime,
    get_subscribed_instrument_keys,
    is_opening_range_enabled,
    safe_float,
    safe_int,
    select_opening_range_candles,
    select_post_opening_range_candles,
    serialize_candle,
)
from .constants import (
    DEFAULT_BACKFILL_SCAN_ENABLED,
    DEFAULT_BACKFILL_TOUCH_ALERT_ENABLED,
    DEFAULT_FETCH_HOUR,
    DEFAULT_FETCH_MINUTE,
    DEFAULT_INTRADAY_INTERVAL,
    DEFAULT_INTRADAY_UNIT,
    DEFAULT_LEGACY_TOUCH_TELEGRAM_ENABLED,
    DEFAULT_LIVE_EMA_CALCULATION_MODE,
    DEFAULT_MAIN_INDEX_KEY,
    DEFAULT_MARKET_OPEN_HOUR,
    DEFAULT_MARKET_OPEN_MINUTE,
    DEFAULT_MAX_WORKERS,
    DEFAULT_OPENING_RANGE_CANDLE_COUNT,
    DEFAULT_OPENING_RANGE_INTERVAL,
    DEFAULT_OUTPUT_FILE,
    DEFAULT_SAVE_FILE,
    DEFAULT_SLEEP_SECONDS,
    DEFAULT_TOUCH_ALERT_ENABLED,
)
from .intraday import fetch_intraday_candles_for_instrument
from .isolation import try_isolate_from_touch_events
from .live_touch import (
    build_touch_status_from_events,
    get_default_touch_status,
    scan_backfill_touches,
    update_latest_main_index_ltp,
)
from .range_calculator import calculate_opening_range_levels
from .storage import (
    save_opening_range_results_to_file,
    save_touch_events_to_file_if_enabled,
)
from .touch_events import send_touch_events_telegram_alert

logger = get_logger(__file__)


def _normalize_candle_count(candle_count: Any) -> int:
    """Return a valid positive Opening Range candle count."""
    return max(
        1,
        safe_int(
            candle_count,
            default=DEFAULT_OPENING_RANGE_CANDLE_COUNT,
        ),
    )


def _normalize_max_workers(
    max_workers: Any,
    total_instruments: int | None = None,
) -> int:
    """Return a valid worker count."""
    normalized_workers = max(
        1,
        safe_int(
            max_workers,
            default=DEFAULT_MAX_WORKERS,
        ),
    )

    if total_instruments is not None:
        normalized_total = max(
            1,
            safe_int(
                total_instruments,
                default=1,
            ),
        )
        normalized_workers = min(
            normalized_workers,
            normalized_total,
        )

    return normalized_workers


def _build_base_instrument_payload(
    *,
    instrument_key: str,
    candle_count: int,
    contract_info: dict,
    processed_at: str,
    processing_date: str,
) -> dict:
    """Build common per-instrument Opening Range fields."""
    return {
        "instrument_key": instrument_key,
        "source": "intraday_api",
        "date": processing_date,
        "interval": DEFAULT_OPENING_RANGE_INTERVAL,
        "unit": DEFAULT_INTRADAY_UNIT,
        "intraday_interval": DEFAULT_INTRADAY_INTERVAL,
        "opening_range_candle_count": candle_count,
        "contract_info": (
            deepcopy(contract_info) if isinstance(contract_info, dict) else {}
        ),
        "processed_at": processed_at,
    }


def _build_instrument_failure_result(
    *,
    instrument_key: str,
    candle_count: int,
    processing_date: str,
    message: str,
    error: str,
) -> dict:
    """Build a result when an instrument worker raises an exception."""
    return {
        "instrument_key": instrument_key,
        "status": "failed",
        "message": message,
        "source": "intraday_api",
        "date": processing_date,
        "interval": DEFAULT_OPENING_RANGE_INTERVAL,
        "unit": DEFAULT_INTRADAY_UNIT,
        "intraday_interval": DEFAULT_INTRADAY_INTERVAL,
        "opening_range_candle_count": candle_count,
        "candles_count": 0,
        "selected_candles_count": 0,
        "latest_intraday_close": None,
        "range": None,
        "levels": None,
        "selected_candles": [],
        "post_or_candles_count": 0,
        "touch_status": get_default_touch_status(),
        "backfill_touch_events": [],
        "error": error,
        "contract_info": get_contract_info_by_key(instrument_key),
        "processed_at": get_now_market_time().isoformat(),
    }


def _build_short_summary(
    *,
    status: str,
    message: str,
) -> dict:
    """Build a disabled or skipped calculation result."""
    return {
        "status": status,
        "message": message,
        "total_instruments": 0,
        "success_count": 0,
        "failed_count": 0,
        "empty_count": 0,
        "insufficient_data_count": 0,
        "backfill_touch_events_count": 0,
        "backfill_isolation": {
            "evaluated": False,
            "isolated": False,
            "status": "not_evaluated",
            "reason_code": status,
            "reason": message,
            "source": None,
            "evaluated_instruments": 0,
            "successful_instruments": 0,
            "instruments_with_touch": 0,
            "backfill_touch_events_count": 0,
            "selected_instrument_key": None,
            "selected_level": None,
            "selected_at": None,
            "touch_time": None,
            "selection_reason": None,
            "reference_average": None,
            "average_window": {},
            "selected_diagnostic": None,
            "diagnostics": [],
            "error": None,
        },
        "results": {},
        "errors": {},
    }


def _get_overall_status(
    *,
    success_count: int,
    failed_count: int,
    empty_count: int,
    insufficient_data_count: int,
) -> str:
    """Return the overall multi-instrument calculation status."""
    if failed_count == 0:
        return "success"

    if success_count > 0 or empty_count > 0 or insufficient_data_count > 0:
        return "partial_success"

    return "failed"


def _get_overall_message(overall_status: str) -> str:
    """Return a readable overall calculation message."""
    if overall_status == "success":
        return "Opening range calculation completed successfully."

    if overall_status == "partial_success":
        return "Opening range calculation completed with some " "instrument failures."

    return "Opening range calculation failed."


def _get_shared_runtime_snapshots() -> tuple[dict, dict, list]:
    """Return touch, isolation, and isolated-alert snapshots."""
    touch_snapshot = runtime_state.get_touch_state_snapshot()
    isolated_state = runtime_state.get_selected_or_state_snapshot()
    isolated_ema_alerts = runtime_state.get_selected_or_ema_alerts_snapshot()

    return (
        touch_snapshot,
        isolated_state,
        isolated_ema_alerts,
    )


def _get_selected_instrument_key(isolated_state: dict) -> str:
    """Extract the selected instrument key from isolation state."""
    if not isinstance(isolated_state, dict):
        return ""

    selected_event = isolated_state.get("selected_event")

    if not isinstance(selected_event, dict):
        selected_event = {}

    contract_info = isolated_state.get("contract_info")

    if not isinstance(contract_info, dict):
        contract_info = {}

    return str(
        isolated_state.get("instrument_key")
        or isolated_state.get("selected_instrument_key")
        or isolated_state.get("instrument_token")
        or selected_event.get("instrument_key")
        or contract_info.get("instrument_key")
        or ""
    ).strip()


def _get_selected_isolation_reason(isolated_state: dict) -> str:
    """Extract the selected instrument reason from isolation state."""
    if not isinstance(isolated_state, dict):
        return "Isolation state is unavailable"

    selected_event = isolated_state.get("selected_event")

    if not isinstance(selected_event, dict):
        selected_event = {}

    return str(
        isolated_state.get("selection_reason")
        or isolated_state.get("reason")
        or selected_event.get("selection_reason")
        or selected_event.get("reason")
        or "Selected by global backfill isolation evaluation"
    ).strip()


def _get_backfill_touch_levels(events: list) -> list:
    """Return unique touched levels from backfill events."""
    if not isinstance(events, list):
        return []

    return sorted(
        {
            str(event.get("level") or "").strip().upper()
            for event in events
            if isinstance(event, dict) and event.get("level")
        }
    )


def _get_backfill_touch_times(events: list) -> list:
    """Return touch times from backfill events."""
    if not isinstance(events, list):
        return []

    return [
        str(event.get("touch_time") or "").strip()
        for event in events
        if isinstance(event, dict) and event.get("touch_time")
    ]


def _get_backfill_diagnostic_reason(
    *,
    result: dict,
    instrument_events: list,
    instrument_isolated: bool,
    isolated_selected: bool,
    selected_instrument_key: str,
    selected_reason: str,
    isolation_error: str | None,
) -> str:
    """Build the backfill isolation diagnostic reason."""
    result_status = str(result.get("status") or "unknown").strip().lower()

    if isolation_error:
        return "Global backfill isolation evaluation failed: " f"{isolation_error}"

    if result_status == "failed":
        return str(
            result.get("error")
            or result.get("message")
            or "Opening Range calculation failed"
        )

    if result_status == "empty":
        return "No intraday candles returned"

    if result_status == "insufficient_data":
        return str(result.get("message") or "Insufficient Opening Range candles")

    if result_status != "success":
        return str(
            result.get("message") or "Opening Range calculation was not successful"
        )

    if not DEFAULT_BACKFILL_SCAN_ENABLED:
        return "Backfill touch scan is disabled"

    if not DEFAULT_BACKFILL_TOUCH_ALERT_ENABLED:
        return "Backfill touch processing is disabled"

    if not instrument_events:
        return "No eligible backfill touch events"

    if instrument_isolated:
        return selected_reason

    if isolated_selected:
        return (
            "Backfill touch event found, but another instrument "
            "was selected by the global isolation evaluation: "
            f"{selected_instrument_key}"
        )

    return (
        "Backfill touch event found, but no instrument satisfied "
        "the global isolation condition"
    )


def _get_backfill_diagnostic_reason_code(
    *,
    result: dict,
    instrument_events: list,
    instrument_isolated: bool,
    isolated_selected: bool,
    isolation_error: str | None,
) -> str:
    """Return a stable backfill diagnostic reason code."""
    result_status = str(result.get("status") or "unknown").strip().lower()

    if isolation_error:
        return "isolation_evaluation_failed"

    if result_status == "failed":
        return "instrument_calculation_failed"

    if result_status == "empty":
        return "no_intraday_candles"

    if result_status == "insufficient_data":
        return "insufficient_opening_range_data"

    if result_status != "success":
        return "instrument_calculation_not_successful"

    if not DEFAULT_BACKFILL_SCAN_ENABLED:
        return "backfill_scan_disabled"

    if not DEFAULT_BACKFILL_TOUCH_ALERT_ENABLED:
        return "backfill_processing_disabled"

    if not instrument_events:
        return "no_backfill_touch_events"

    if instrument_isolated:
        return "instrument_selected"

    if isolated_selected:
        return "another_instrument_selected"

    return "no_eligible_isolation_candidate"


def _log_backfill_isolation_diagnostics(
    *,
    subscribed_keys: list[str],
    results: dict,
    isolated_state: dict,
    isolation_error: str | None = None,
) -> list:
    """Build and log backfill isolation results for all instruments."""
    normalized_isolated_state = (
        isolated_state if isinstance(isolated_state, dict) else {}
    )

    isolated_selected = bool(normalized_isolated_state.get("selected"))

    selected_instrument_key = _get_selected_instrument_key(normalized_isolated_state)

    selected_reason = _get_selected_isolation_reason(normalized_isolated_state)

    total_instruments = len(subscribed_keys)
    diagnostics = []

    logger.info(
        "Backfill isolation diagnostics started. "
        "total_instruments=%s, isolated_selected=%s, "
        "selected_instrument_key=%s, isolation_error=%s",
        total_instruments,
        isolated_selected,
        selected_instrument_key or "not_selected",
        isolation_error or "none",
    )

    for index, instrument_key in enumerate(
        subscribed_keys,
        start=1,
    ):
        result = results.get(instrument_key)

        if not isinstance(result, dict):
            result = {}

        result_status = str(result.get("status") or "unknown").strip().lower()

        instrument_events = result.get(
            "backfill_touch_events",
            [],
        )

        if not isinstance(instrument_events, list):
            instrument_events = []

        touch_levels = _get_backfill_touch_levels(instrument_events)

        touch_times = _get_backfill_touch_times(instrument_events)

        instrument_isolated = bool(
            isolated_selected
            and selected_instrument_key
            and str(instrument_key).strip() == selected_instrument_key
        )

        reason = _get_backfill_diagnostic_reason(
            result=result,
            instrument_events=instrument_events,
            instrument_isolated=instrument_isolated,
            isolated_selected=isolated_selected,
            selected_instrument_key=(selected_instrument_key),
            selected_reason=selected_reason,
            isolation_error=isolation_error,
        )

        reason_code = _get_backfill_diagnostic_reason_code(
            result=result,
            instrument_events=instrument_events,
            instrument_isolated=instrument_isolated,
            isolated_selected=isolated_selected,
            isolation_error=isolation_error,
        )

        contract_info = result.get("contract_info")

        if not isinstance(contract_info, dict):
            contract_info = get_contract_info_by_key(instrument_key)

        if not isinstance(contract_info, dict):
            contract_info = {}

        diagnostic = {
            "instrument_key": instrument_key,
            "contract_info": deepcopy(contract_info),
            "status": result_status,
            "backfill_evaluated": True,
            "backfill_touch_found": bool(instrument_events),
            "backfill_touch_events_count": len(instrument_events),
            "touch_levels": touch_levels,
            "touch_times": touch_times,
            "isolated": instrument_isolated,
            "reason_code": reason_code,
            "reason": reason,
        }

        diagnostics.append(diagnostic)

        logger.info(
            "[BACKFILL ISOLATION CHECK] "
            "progress=%s/%s, instrument_key=%s, "
            "status=%s, touch_events=%s, "
            "touch_levels=%s, touch_times=%s, "
            "isolated=%s, reason_code=%s, reason=%s",
            index,
            total_instruments,
            instrument_key,
            result_status,
            len(instrument_events),
            touch_levels or ["none"],
            touch_times or ["none"],
            instrument_isolated,
            reason_code,
            reason,
        )

    logger.info(
        "Backfill isolation diagnostics completed. "
        "total_instruments=%s, isolated_selected=%s, "
        "selected_instrument_key=%s",
        total_instruments,
        isolated_selected,
        selected_instrument_key or "not_selected",
    )

    return diagnostics


def _build_backfill_isolation_payload(
    *,
    diagnostics: list,
    isolated_state: dict,
    backfill_touch_events_count: int,
    isolation_error: str | None = None,
) -> dict:
    """Build the dashboard-level backfill isolation result."""
    normalized_diagnostics = diagnostics if isinstance(diagnostics, list) else []

    normalized_isolated_state = (
        isolated_state if isinstance(isolated_state, dict) else {}
    )

    isolated_selected = bool(normalized_isolated_state.get("selected"))

    selected_instrument_key = _get_selected_instrument_key(normalized_isolated_state)

    selected_diagnostic = next(
        (
            item
            for item in normalized_diagnostics
            if isinstance(item, dict) and item.get("isolated")
        ),
        None,
    )

    instruments_with_touch = sum(
        1
        for item in normalized_diagnostics
        if isinstance(item, dict) and item.get("backfill_touch_found")
    )

    successful_instruments = sum(
        1
        for item in normalized_diagnostics
        if isinstance(item, dict) and item.get("status") == "success"
    )

    if isolation_error:
        status = "failed"
        reason_code = "isolation_evaluation_failed"
        reason = "Global backfill isolation evaluation failed: " f"{isolation_error}"
    elif isolated_selected:
        status = "selected"
        reason_code = "instrument_selected"
        reason = _get_selected_isolation_reason(normalized_isolated_state)
    elif backfill_touch_events_count <= 0:
        status = "not_selected"
        reason_code = "no_backfill_touch_events"
        reason = (
            "No instrument was isolated because no backfill "
            "touch events were detected."
        )
    else:
        status = "not_selected"
        reason_code = "no_eligible_isolation_candidate"
        reason = (
            "Backfill touch events were detected, but no "
            "instrument satisfied the global isolation condition."
        )

    return {
        "evaluated": True,
        "isolated": isolated_selected,
        "status": status,
        "reason_code": reason_code,
        "reason": reason,
        "source": "intraday_backfill_scan",
        "evaluated_instruments": len(normalized_diagnostics),
        "successful_instruments": successful_instruments,
        "instruments_with_touch": instruments_with_touch,
        "backfill_touch_events_count": (backfill_touch_events_count),
        "selected_instrument_key": (
            selected_instrument_key if isolated_selected else None
        ),
        "selected_level": (
            normalized_isolated_state.get("selected_level")
            if isolated_selected
            else None
        ),
        "selected_at": (
            normalized_isolated_state.get("selected_at") if isolated_selected else None
        ),
        "touch_time": (
            normalized_isolated_state.get("touch_time") if isolated_selected else None
        ),
        "selection_reason": (
            normalized_isolated_state.get("selection_reason")
            if isolated_selected
            else None
        ),
        "reference_average": (
            normalized_isolated_state.get("reference_average")
            if isolated_selected
            else None
        ),
        "average_window": deepcopy(
            normalized_isolated_state.get("average_window") or {}
        ),
        "selected_diagnostic": deepcopy(selected_diagnostic),
        "diagnostics": deepcopy(normalized_diagnostics),
        "error": isolation_error,
    }


def _update_main_cache_after_calculation(
    *,
    completed_at: str,
    current_date: str,
    overall_status: str,
    overall_message: str,
    candle_count: int,
    total_instruments: int,
    success_count: int,
    failed_count: int,
    empty_count: int,
    insufficient_data_count: int,
    results: dict,
    errors: dict,
) -> None:
    """Update the centralized Opening Range cache."""
    (
        touch_snapshot,
        isolated_state,
        isolated_ema_alerts,
    ) = _get_shared_runtime_snapshots()

    with runtime_state.opening_range_cache_lock:
        cache = runtime_state.opening_range_cache

        cache["last_run_at"] = completed_at
        cache["date"] = current_date
        cache["status"] = overall_status
        cache["message"] = overall_message
        cache["source"] = "intraday_api"
        cache["interval"] = DEFAULT_OPENING_RANGE_INTERVAL
        cache["opening_range_candle_count"] = candle_count
        cache["market_open_time"] = (
            f"{DEFAULT_MARKET_OPEN_HOUR:02d}:" f"{DEFAULT_MARKET_OPEN_MINUTE:02d}"
        )
        cache["fetch_time"] = f"{DEFAULT_FETCH_HOUR:02d}:" f"{DEFAULT_FETCH_MINUTE:02d}"
        cache["total_instruments"] = total_instruments
        cache["success_count"] = success_count
        cache["failed_count"] = failed_count
        cache["empty_count"] = empty_count
        cache["insufficient_data_count"] = insufficient_data_count
        cache["latest_main_index_ltp"] = touch_snapshot.get("latest_main_index_ltp")
        cache["latest_main_index_ltp_source"] = touch_snapshot.get(
            "latest_main_index_ltp_source"
        )
        cache["latest_main_index_ltp_updated_at"] = touch_snapshot.get(
            "latest_main_index_ltp_updated_at"
        )
        cache["touch_events_count"] = touch_snapshot.get(
            "events_count",
            0,
        )
        cache["pending_touch_events_count"] = touch_snapshot.get(
            "pending_events_count",
            0,
        )
        cache["alert_sent_keys_count"] = touch_snapshot.get(
            "alert_sent_keys_count",
            0,
        )
        cache["data"] = deepcopy(results)
        cache["touch_events"] = deepcopy(touch_snapshot.get("events", []))
        cache["errors"] = deepcopy(errors)
        cache["isolated_instrument"] = isolated_state
        cache["isolated_instrument_selected"] = bool(isolated_state.get("selected"))
        cache["isolated_instrument_selected_at"] = isolated_state.get("selected_at")
        cache["isolated_instrument_selection_reason"] = isolated_state.get(
            "selection_reason"
        )
        cache["isolated_ema_alerts_count"] = len(isolated_ema_alerts)


def _update_cache_isolation_and_output(
    output_file_path: str | None,
    backfill_isolation: dict | None = None,
) -> None:
    """Synchronize output and isolation state into cache."""
    isolated_state = runtime_state.get_selected_or_state_snapshot()

    isolated_ema_alerts_count = len(runtime_state.get_selected_or_ema_alerts_snapshot())

    normalized_backfill_isolation = (
        deepcopy(backfill_isolation) if isinstance(backfill_isolation, dict) else {}
    )

    with runtime_state.opening_range_cache_lock:
        cache = runtime_state.opening_range_cache

        cache["output_file_path"] = output_file_path
        cache["isolated_instrument"] = isolated_state

        cache["isolated_instrument_selected"] = bool(isolated_state.get("selected"))

        cache["isolated_instrument_selected_at"] = isolated_state.get("selected_at")

        cache["isolated_instrument_selection_reason"] = isolated_state.get(
            "selection_reason"
        )

        cache["isolated_instrument_locked_for_market_day"] = bool(
            isolated_state.get("locked_for_market_day")
        )

        cache["isolated_ema_alerts_count"] = isolated_ema_alerts_count

        cache["backfill_isolation"] = normalized_backfill_isolation


def calculate_opening_range_for_instrument(
    instrument_key: str,
    candle_count: int = DEFAULT_OPENING_RANGE_CANDLE_COUNT,
) -> dict:
    """Calculate Opening Range and backfill touches for one instrument."""
    normalized_instrument_key = str(instrument_key or "").strip()

    normalized_candle_count = _normalize_candle_count(candle_count)

    now_market = get_now_market_time()
    processed_at = now_market.isoformat()
    processing_date = now_market.date().isoformat()

    if not normalized_instrument_key:
        return {
            "instrument_key": normalized_instrument_key,
            "status": "failed",
            "message": "Instrument key is required.",
            "source": "intraday_api",
            "date": processing_date,
            "interval": DEFAULT_OPENING_RANGE_INTERVAL,
            "unit": DEFAULT_INTRADAY_UNIT,
            "intraday_interval": DEFAULT_INTRADAY_INTERVAL,
            "opening_range_candle_count": (normalized_candle_count),
            "candles_count": 0,
            "selected_candles_count": 0,
            "latest_intraday_close": None,
            "range": None,
            "levels": None,
            "selected_candles": [],
            "post_or_candles_count": 0,
            "touch_status": get_default_touch_status(),
            "backfill_touch_events": [],
            "error": "instrument_key is required",
            "contract_info": {},
            "processed_at": processed_at,
        }

    contract_info = get_contract_info_by_key(normalized_instrument_key)

    base_payload = _build_base_instrument_payload(
        instrument_key=normalized_instrument_key,
        candle_count=normalized_candle_count,
        contract_info=contract_info,
        processed_at=processed_at,
        processing_date=processing_date,
    )

    intraday_result = fetch_intraday_candles_for_instrument(
        instrument_key=normalized_instrument_key,
        unit=DEFAULT_INTRADAY_UNIT,
        interval=DEFAULT_INTRADAY_INTERVAL,
    )

    intraday_status = intraday_result.get("status")

    if intraday_status == "failed":
        return {
            **base_payload,
            "status": "failed",
            "message": "Intraday candle fetch failed.",
            "candles_count": 0,
            "selected_candles_count": 0,
            "latest_intraday_close": None,
            "range": None,
            "levels": None,
            "selected_candles": [],
            "post_or_candles_count": 0,
            "touch_status": get_default_touch_status(),
            "backfill_touch_events": [],
            "error": intraday_result.get("error"),
        }

    candles = intraday_result.get("candles", [])

    if not isinstance(candles, list):
        candles = []

    if not candles:
        return {
            **base_payload,
            "status": "empty",
            "message": "No intraday candles returned.",
            "candles_count": 0,
            "selected_candles_count": 0,
            "latest_intraday_close": None,
            "range": None,
            "levels": None,
            "selected_candles": [],
            "post_or_candles_count": 0,
            "touch_status": get_default_touch_status(),
            "backfill_touch_events": [],
            "error": None,
        }

    latest_candle = candles[-1]

    latest_intraday_close = safe_float(
        latest_candle.get("close"),
        default=0.0,
    )

    is_main_index = normalized_instrument_key == DEFAULT_MAIN_INDEX_KEY

    if is_main_index and latest_intraday_close > 0:
        update_latest_main_index_ltp(
            ltp=latest_intraday_close,
            source="intraday_api",
            updated_at=latest_candle.get("timestamp"),
        )

    selected_candles = select_opening_range_candles(
        candles=candles,
        candle_count=normalized_candle_count,
    )

    serialized_selected_candles = [
        serialize_candle(candle) for candle in selected_candles
    ]

    if len(selected_candles) < normalized_candle_count:
        return {
            **base_payload,
            "status": "insufficient_data",
            "message": (
                f"Need {normalized_candle_count} Opening Range "
                f"candles, but only {len(selected_candles)} "
                "are available."
            ),
            "candles_count": len(candles),
            "selected_candles_count": len(selected_candles),
            "latest_intraday_close": (
                latest_intraday_close if latest_intraday_close > 0 else None
            ),
            "range": None,
            "levels": None,
            "selected_candles": (serialized_selected_candles),
            "post_or_candles_count": 0,
            "touch_status": get_default_touch_status(),
            "backfill_touch_events": [],
            "error": None,
        }

    calculation = calculate_opening_range_levels(selected_candles)

    calculation_status = calculation.get("status")
    levels = calculation.get("levels") or {}

    post_or_candles = select_post_opening_range_candles(
        candles=candles,
        candle_count=normalized_candle_count,
    )

    backfill_touch_events = []

    if calculation_status == "success" and DEFAULT_BACKFILL_SCAN_ENABLED:
        backfill_touch_events = scan_backfill_touches(
            instrument_key=normalized_instrument_key,
            candles=candles,
            levels=levels,
            contract_info=contract_info,
            candle_count=normalized_candle_count,
        )

    touch_status = build_touch_status_from_events(backfill_touch_events)

    return {
        **base_payload,
        "status": calculation_status,
        "message": calculation.get("message"),
        "candles_count": len(candles),
        "selected_candles_count": len(selected_candles),
        "latest_intraday_close": (
            latest_intraday_close if latest_intraday_close > 0 else None
        ),
        "range": calculation.get("range"),
        "levels": levels,
        "selected_candles": serialized_selected_candles,
        "post_or_candles_count": len(post_or_candles),
        "touch_status": touch_status,
        "backfill_touch_events": backfill_touch_events,
        "error": (
            None if calculation_status == "success" else calculation.get("message")
        ),
    }


def calculate_opening_range_for_all_subscribed(
    candle_count: int = DEFAULT_OPENING_RANGE_CANDLE_COUNT,
    save_data: bool = DEFAULT_SAVE_FILE,
    max_workers: int = DEFAULT_MAX_WORKERS,
) -> dict:
    """Calculate Opening Range for all subscribed instruments."""
    runtime_state.ensure_current_market_day()

    if not is_opening_range_enabled():
        logger.info("Opening Range calculation skipped because it is disabled.")

        result = _build_short_summary(
            status="disabled",
            message="Opening Range calculation is disabled.",
        )

        with runtime_state.opening_range_cache_lock:
            runtime_state.opening_range_cache["status"] = "disabled"
            runtime_state.opening_range_cache["message"] = result["message"]

        return result

    subscribed_keys = get_subscribed_instrument_keys()

    if not subscribed_keys:
        logger.warning(
            "Opening Range calculation skipped because no "
            "subscribed instruments were found."
        )

        result = _build_short_summary(
            status="skipped",
            message="No subscribed instruments found.",
        )

        with runtime_state.opening_range_cache_lock:
            runtime_state.opening_range_cache["status"] = "skipped"
            runtime_state.opening_range_cache["message"] = result["message"]
            runtime_state.opening_range_cache["total_instruments"] = 0

        return result

    normalized_candle_count = _normalize_candle_count(candle_count)

    total_instruments = len(subscribed_keys)

    normalized_max_workers = _normalize_max_workers(
        max_workers,
        total_instruments=total_instruments,
    )

    now_market = get_now_market_time()
    started_at = now_market.isoformat()
    current_date = now_market.date().isoformat()

    logger.info(
        "================ OPENING RANGE INTRADAY FETCH " "STARTED ================"
    )

    logger.info(
        "Calculating Opening Range for %s instruments. "
        "date=%s, candle_count=%s, interval=%s, unit=%s, "
        "intraday_interval=%s, market_open=%02d:%02d, "
        "max_workers=%s",
        total_instruments,
        current_date,
        normalized_candle_count,
        DEFAULT_OPENING_RANGE_INTERVAL,
        DEFAULT_INTRADAY_UNIT,
        DEFAULT_INTRADAY_INTERVAL,
        DEFAULT_MARKET_OPEN_HOUR,
        DEFAULT_MARKET_OPEN_MINUTE,
        normalized_max_workers,
    )

    results = {}
    errors = {}
    success_count = 0
    failed_count = 0
    empty_count = 0
    insufficient_data_count = 0
    total_backfill_touch_events = 0
    backfill_touch_events = []
    completed_count = 0

    def worker(worker_instrument_key: str) -> dict:
        result = calculate_opening_range_for_instrument(
            instrument_key=worker_instrument_key,
            candle_count=normalized_candle_count,
        )

        if DEFAULT_SLEEP_SECONDS > 0:
            time.sleep(DEFAULT_SLEEP_SECONDS)

        return result

    with ThreadPoolExecutor(max_workers=normalized_max_workers) as executor:
        future_to_instrument = {
            executor.submit(
                worker,
                instrument_key,
            ): instrument_key
            for instrument_key in subscribed_keys
        }

        for future in as_completed(future_to_instrument):
            instrument_key = future_to_instrument[future]
            completed_count += 1

            logger.info(
                "Opening Range progress: %s/%s. " "instrument_key=%s",
                completed_count,
                total_instruments,
                instrument_key,
            )

            try:
                result = future.result()

                if not isinstance(result, dict):
                    raise TypeError(
                        "Opening Range worker returned a " "non-dictionary result."
                    )

                result_status = result.get("status")
                results[instrument_key] = result

                instrument_backfill_events = result.get(
                    "backfill_touch_events",
                    [],
                )

                if (
                    isinstance(
                        instrument_backfill_events,
                        list,
                    )
                    and instrument_backfill_events
                ):
                    total_backfill_touch_events += len(instrument_backfill_events)

                    backfill_touch_events.extend(instrument_backfill_events)

                if result_status == "success":
                    success_count += 1
                elif result_status == "empty":
                    empty_count += 1
                elif result_status == "insufficient_data":
                    insufficient_data_count += 1
                else:
                    failed_count += 1

                    errors[instrument_key] = (
                        result.get("error")
                        or result.get("message")
                        or "Unknown calculation failure."
                    )

            except Exception as ex:
                error_message = f"{type(ex).__name__}: {ex}"

                logger.exception(
                    "Opening Range calculation failed. " "instrument_key=%s, error=%s",
                    instrument_key,
                    error_message,
                )

                failed_count += 1
                errors[instrument_key] = error_message

                results[instrument_key] = _build_instrument_failure_result(
                    instrument_key=instrument_key,
                    candle_count=(normalized_candle_count),
                    processing_date=current_date,
                    message=("Opening Range worker failed."),
                    error=error_message,
                )

    completed_at = get_now_market_time().isoformat()

    overall_status = _get_overall_status(
        success_count=success_count,
        failed_count=failed_count,
        empty_count=empty_count,
        insufficient_data_count=(insufficient_data_count),
    )

    overall_message = _get_overall_message(overall_status)

    _update_main_cache_after_calculation(
        completed_at=completed_at,
        current_date=current_date,
        overall_status=overall_status,
        overall_message=overall_message,
        candle_count=normalized_candle_count,
        total_instruments=total_instruments,
        success_count=success_count,
        failed_count=failed_count,
        empty_count=empty_count,
        insufficient_data_count=(insufficient_data_count),
        results=results,
        errors=errors,
    )

    isolation_error = None

    if backfill_touch_events:
        try:
            try_isolate_from_touch_events(backfill_touch_events)
        except Exception as ex:
            isolation_error = f"{type(ex).__name__}: {ex}"

            logger.exception(
                "Opening Range backfill isolation failed. " "error=%s",
                isolation_error,
            )

            errors["isolated_instrument_selection"] = isolation_error
    else:
        logger.info(
            "Opening Range global backfill isolation skipped. "
            "No eligible backfill touch events were collected."
        )

    touch_snapshot = runtime_state.get_touch_state_snapshot()

    isolated_state = runtime_state.get_selected_or_state_snapshot()

    backfill_isolation_diagnostics = _log_backfill_isolation_diagnostics(
        subscribed_keys=subscribed_keys,
        results=results,
        isolated_state=isolated_state,
        isolation_error=isolation_error,
    )

    backfill_isolation = _build_backfill_isolation_payload(
        diagnostics=backfill_isolation_diagnostics,
        isolated_state=isolated_state,
        backfill_touch_events_count=(total_backfill_touch_events),
        isolation_error=isolation_error,
    )

    isolated_ema_alerts_count = len(runtime_state.get_selected_or_ema_alerts_snapshot())

    opening_range_end_time = get_opening_range_end_datetime(
        candle_count=normalized_candle_count,
        target_date=now_market.date(),
    ).strftime("%H:%M")

    summary = {
        "status": overall_status,
        "message": overall_message,
        "source": "intraday_api",
        "date": current_date,
        "started_at": started_at,
        "completed_at": completed_at,
        "interval": DEFAULT_OPENING_RANGE_INTERVAL,
        "unit": DEFAULT_INTRADAY_UNIT,
        "intraday_interval": DEFAULT_INTRADAY_INTERVAL,
        "opening_range_candle_count": (normalized_candle_count),
        "market_open_time": (
            f"{DEFAULT_MARKET_OPEN_HOUR:02d}:" f"{DEFAULT_MARKET_OPEN_MINUTE:02d}"
        ),
        "live_ema_calculation_mode_flag": (DEFAULT_LIVE_EMA_CALCULATION_MODE),
        "live_ema_calculation_mode": (get_live_ema_calculation_mode_text()),
        "opening_range_end_time": (opening_range_end_time),
        "scheduled_fetch_time": (
            f"{DEFAULT_FETCH_HOUR:02d}:" f"{DEFAULT_FETCH_MINUTE:02d}"
        ),
        "max_workers": normalized_max_workers,
        "total_instruments": total_instruments,
        "success_count": success_count,
        "failed_count": failed_count,
        "empty_count": empty_count,
        "insufficient_data_count": (insufficient_data_count),
        "backfill_touch_scan_enabled": (DEFAULT_BACKFILL_SCAN_ENABLED),
        "backfill_touch_events_count": (total_backfill_touch_events),
        "backfill_isolation": deepcopy(backfill_isolation),
        "latest_main_index_ltp": (touch_snapshot.get("latest_main_index_ltp")),
        "latest_main_index_ltp_source": (
            touch_snapshot.get("latest_main_index_ltp_source")
        ),
        "latest_main_index_ltp_updated_at": (
            touch_snapshot.get("latest_main_index_ltp_updated_at")
        ),
        "isolated_instrument": isolated_state,
        "isolated_ema_alerts_count": (isolated_ema_alerts_count),
        "results": results,
        "backfill_touch_events": (backfill_touch_events),
        "errors": errors,
    }

    output_file_path = None

    if bool(save_data):
        try:
            output_file_path = save_opening_range_results_to_file(
                summary=summary,
                output_file=DEFAULT_OUTPUT_FILE,
            )

            summary["output_file_path"] = output_file_path

            logger.info(
                "Saved Opening Range results. " "file_path=%s",
                output_file_path,
            )

        except Exception as ex:
            output_file_error = f"{type(ex).__name__}: {ex}"

            logger.exception(
                "Failed saving Opening Range results. " "error=%s",
                output_file_error,
            )

            summary["output_file_path"] = None
            summary["output_file_error"] = output_file_error

            errors["opening_range_result_storage"] = output_file_error
    else:
        summary["output_file_path"] = None

        logger.info(
            "Opening Range result file was not saved " "because save_data=False."
        )

    _update_cache_isolation_and_output(
        output_file_path=output_file_path,
        backfill_isolation=backfill_isolation,
    )

    if (
        DEFAULT_BACKFILL_TOUCH_ALERT_ENABLED
        and DEFAULT_TOUCH_ALERT_ENABLED
        and DEFAULT_LEGACY_TOUCH_TELEGRAM_ENABLED
        and backfill_touch_events
    ):
        try:
            legacy_alert_sent = send_touch_events_telegram_alert(
                events=backfill_touch_events,
                source="intraday_backfill_scan",
                force=True,
            )

            summary["legacy_backfill_touch_alert_sent"] = legacy_alert_sent

        except Exception as ex:
            legacy_alert_error = f"{type(ex).__name__}: {ex}"

            logger.exception(
                "Failed sending legacy backfill touch " "alert. error=%s",
                legacy_alert_error,
            )

            summary["legacy_backfill_touch_alert_sent"] = False

            summary["legacy_backfill_touch_alert_error"] = legacy_alert_error

    try:
        touch_test_file_path = save_touch_events_to_file_if_enabled()

        if touch_test_file_path:
            summary["touch_events_test_file_path"] = touch_test_file_path

    except Exception as ex:
        logger.exception(
            "Unexpected error while saving Opening Range "
            "touch-event test data. error=%s: %s",
            type(ex).__name__,
            ex,
        )

    runtime_state.synchronize_cache_counters()

    final_isolated_state = runtime_state.get_selected_or_state_snapshot()

    logger.info(
        "Opening Range calculation completed. "
        "status=%s, total_instruments=%s, success=%s, "
        "empty=%s, insufficient_data=%s, failed=%s, "
        "backfill_touch_events=%s, isolated_selected=%s, "
        "output_file=%s",
        overall_status,
        total_instruments,
        success_count,
        empty_count,
        insufficient_data_count,
        failed_count,
        total_backfill_touch_events,
        final_isolated_state.get("selected"),
        output_file_path,
    )

    logger.info(
        "================ OPENING RANGE INTRADAY FETCH " "COMPLETED ================"
    )

    return summary


__all__ = [
    "calculate_opening_range_for_instrument",
    "calculate_opening_range_for_all_subscribed",
]
