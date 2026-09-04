"""
Opening Range calculation orchestration service.

This module coordinates the complete Opening Range workflow:

1. Fetch intraday candles for one or more instruments.
2. Select the configured market-opening candles.
3. Calculate Opening Range levels.
4. Scan post-Opening-Range candles for historical touches.
5. Build per-instrument calculation results.
6. Update the centralized runtime cache.
7. Select an isolated instrument from all backfill touch events.
8. Optionally send legacy grouped touch alerts.
9. Optionally save calculation results and test event data.

All mutable process-level state is owned by state.py.
"""

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from typing import Any

from core.logger import get_logger

from . import state as runtime_state
from .candle_utils import (
    get_contract_info_by_key, get_live_ema_calculation_mode_text, get_now_market_time,
    get_opening_range_end_datetime, get_subscribed_instrument_keys, is_opening_range_enabled,
    safe_float, safe_int, select_opening_range_candles, select_post_opening_range_candles,
    serialize_candle
)
from .constants import (
    DEFAULT_BACKFILL_SCAN_ENABLED, DEFAULT_BACKFILL_TOUCH_ALERT_ENABLED,
    DEFAULT_FETCH_HOUR, DEFAULT_FETCH_MINUTE, DEFAULT_INTRADAY_INTERVAL, DEFAULT_INTRADAY_UNIT,
    DEFAULT_LEGACY_TOUCH_TELEGRAM_ENABLED, DEFAULT_LIVE_EMA_CALCULATION_MODE,
    DEFAULT_MARKET_OPEN_HOUR, DEFAULT_MARKET_OPEN_MINUTE, DEFAULT_MAX_WORKERS,
    DEFAULT_OPENING_RANGE_CANDLE_COUNT, DEFAULT_OPENING_RANGE_INTERVAL, DEFAULT_OUTPUT_FILE,
    DEFAULT_SAVE_FILE, DEFAULT_SLEEP_SECONDS, DEFAULT_TOUCH_ALERT_ENABLED
)
from .intraday import fetch_intraday_candles_for_instrument
from .isolation import try_isolate_from_touch_events
from .live_touch import build_touch_status_from_events, get_default_touch_status, scan_backfill_touches, update_latest_main_index_ltp
from .range_calculator import calculate_opening_range_levels
from .storage import save_opening_range_results_to_file, save_touch_events_to_file_if_enabled
from .touch_events import send_touch_events_telegram_alert

logger = get_logger(__file__)

# ============================================================
# Internal Helpers
# ============================================================

def _normalize_candle_count(candle_count: Any) -> int:
    """Returns a valid positive Opening Range candle count."""
    return max(1, safe_int(candle_count, default=DEFAULT_OPENING_RANGE_CANDLE_COUNT))

def _normalize_max_workers(max_workers: Any, total_instruments: int | None = None) -> int:
    """Returns a valid worker count. Limited by total instruments if known."""
    normalized_workers = max(1, safe_int(max_workers, default=DEFAULT_MAX_WORKERS))
    if total_instruments is not None:
        normalized_total = max(1, safe_int(total_instruments, default=1))
        normalized_workers = min(normalized_workers, normalized_total)
    return normalized_workers

def _build_base_instrument_payload(*, instrument_key: str, candle_count: int, contract_info: dict, processed_at: str, processing_date: str) -> dict:
    """Builds common per-instrument Opening Range fields."""
    return {
        "instrument_key": instrument_key,
        "source": "intraday_api",
        "date": processing_date,
        "interval": DEFAULT_OPENING_RANGE_INTERVAL,
        "unit": DEFAULT_INTRADAY_UNIT,
        "intraday_interval": DEFAULT_INTRADAY_INTERVAL,
        "opening_range_candle_count": candle_count,
        "contract_info": deepcopy(contract_info) if isinstance(contract_info, dict) else {},
        "processed_at": processed_at,
    }

def _build_instrument_failure_result(*, instrument_key: str, candle_count: int, processing_date: str, message: str, error: str) -> dict:
    """Builds a result when an instrument worker raises an exception."""
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

def _build_short_summary(*, status: str, message: str) -> dict:
    """Builds a result for disabled or skipped calculations."""
    return {
        "status": status,
        "message": message,
        "total_instruments": 0,
        "success_count": 0,
        "failed_count": 0,
        "empty_count": 0,
        "insufficient_data_count": 0,
        "backfill_touch_events_count": 0,
        "results": {},
        "errors": {},
    }

def _get_overall_status(*, success_count: int, failed_count: int, empty_count: int, insufficient_data_count: int) -> str:
    """Returns the overall status for a multi-instrument run."""
    if failed_count == 0: return "success"
    if success_count > 0 or empty_count > 0 or insufficient_data_count > 0: return "partial_success"
    return "failed"

def _get_overall_message(overall_status: str) -> str:
    """Returns a readable overall calculation message."""
    if overall_status == "success": return "Opening range calculation completed successfully."
    if overall_status == "partial_success": return "Opening range calculation completed with some instrument failures."
    return "Opening range calculation failed."

def _get_shared_runtime_snapshots() -> tuple[dict, dict, list]:
    """Returns touch, isolated-state, and isolated-alert snapshots."""
    touch_snapshot = runtime_state.get_touch_state_snapshot()
    isolated_state = runtime_state.get_selected_or_state_snapshot()
    isolated_ema_alerts = runtime_state.get_selected_or_ema_alerts_snapshot()
    return touch_snapshot, isolated_state, isolated_ema_alerts

def _update_main_cache_after_calculation(*, completed_at: str, current_date: str, overall_status: str, overall_message: str,
                                         candle_count: int, total_instruments: int, success_count: int, failed_count: int,
                                         empty_count: int, insufficient_data_count: int, results: dict, errors: dict) -> None:
    """Updates the centralized Opening Range cache after calculation."""
    touch_snapshot, isolated_state, isolated_ema_alerts = _get_shared_runtime_snapshots()
    with runtime_state.opening_range_cache_lock:
        cache = runtime_state.opening_range_cache
        cache["last_run_at"] = completed_at
        cache["date"] = current_date
        cache["status"] = overall_status
        cache["message"] = overall_message
        cache["source"] = "intraday_api"
        cache["interval"] = DEFAULT_OPENING_RANGE_INTERVAL
        cache["opening_range_candle_count"] = candle_count
        cache["market_open_time"] = f"{DEFAULT_MARKET_OPEN_HOUR:02d}:{DEFAULT_MARKET_OPEN_MINUTE:02d}"
        cache["fetch_time"] = f"{DEFAULT_FETCH_HOUR:02d}:{DEFAULT_FETCH_MINUTE:02d}"
        cache["total_instruments"] = total_instruments
        cache["success_count"] = success_count
        cache["failed_count"] = failed_count
        cache["empty_count"] = empty_count
        cache["insufficient_data_count"] = insufficient_data_count
        cache["latest_main_index_ltp"] = touch_snapshot.get("latest_main_index_ltp")
        cache["latest_main_index_ltp_source"] = touch_snapshot.get("latest_main_index_ltp_source")
        cache["latest_main_index_ltp_updated_at"] = touch_snapshot.get("latest_main_index_ltp_updated_at")
        cache["touch_events_count"] = touch_snapshot.get("events_count", 0)
        cache["pending_touch_events_count"] = touch_snapshot.get("pending_events_count", 0)
        cache["alert_sent_keys_count"] = touch_snapshot.get("alert_sent_keys_count", 0)
        cache["data"] = deepcopy(results)
        cache["touch_events"] = deepcopy(touch_snapshot.get("events", []))
        cache["errors"] = deepcopy(errors)
        cache["isolated_instrument"] = isolated_state
        cache["isolated_instrument_selected"] = bool(isolated_state.get("selected"))
        cache["isolated_instrument_selected_at"] = isolated_state.get("selected_at")
        cache["isolated_instrument_selection_reason"] = isolated_state.get("selection_reason")
        cache["isolated_ema_alerts_count"] = len(isolated_ema_alerts)

def _update_cache_isolation_and_output(output_file_path: str | None) -> None:
    """Synchronizes output-file and isolated-instrument state into cache."""
    isolated_state = runtime_state.get_selected_or_state_snapshot()
    isolated_ema_alerts_count = len(runtime_state.get_selected_or_ema_alerts_snapshot())
    with runtime_state.opening_range_cache_lock:
        cache = runtime_state.opening_range_cache
        cache["output_file_path"] = output_file_path
        cache["isolated_instrument"] = isolated_state
        cache["isolated_instrument_selected"] = bool(isolated_state.get("selected"))
        cache["isolated_instrument_selected_at"] = isolated_state.get("selected_at")
        cache["isolated_instrument_selection_reason"] = isolated_state.get("selection_reason")
        cache["isolated_ema_alerts_count"] = isolated_ema_alerts_count

# ============================================================
# Single Instrument Opening Range
# ============================================================

def calculate_opening_range_for_instrument(instrument_key: str, candle_count: int = DEFAULT_OPENING_RANGE_CANDLE_COUNT) -> dict:
    """
    Fetches intraday candles for one instrument and calculates its Opening Range levels.
    The function also scans post-Opening-Range candles for historical R2, R3, S2, and S3 touches.
    """
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
            "opening_range_candle_count": normalized_candle_count,
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
    latest_intraday_close = safe_float(latest_candle.get("close"), default=0.0)

    # Determine if main index
    if hasattr(runtime_state, "DEFAULT_MAIN_INDEX_KEY"):
        is_main_index = normalized_instrument_key == runtime_state.DEFAULT_MAIN_INDEX_KEY
    else:
        from .constants import DEFAULT_MAIN_INDEX_KEY
        is_main_index = normalized_instrument_key == DEFAULT_MAIN_INDEX_KEY

    if is_main_index and latest_intraday_close > 0:
        update_latest_main_index_ltp(ltp=latest_intraday_close, source="intraday_api", updated_at=latest_candle.get("timestamp"))

    selected_candles = select_opening_range_candles(candles=candles, candle_count=normalized_candle_count)
    serialized_selected_candles = [serialize_candle(candle) for candle in selected_candles]

    if len(selected_candles) < normalized_candle_count:
        return {
            **base_payload,
            "status": "insufficient_data",
            "message": f"Need {normalized_candle_count} Opening Range candles, but only {len(selected_candles)} are available.",
            "candles_count": len(candles),
            "selected_candles_count": len(selected_candles),
            "latest_intraday_close": latest_intraday_close if latest_intraday_close > 0 else None,
            "range": None,
            "levels": None,
            "selected_candles": serialized_selected_candles,
            "post_or_candles_count": 0,
            "touch_status": get_default_touch_status(),
            "backfill_touch_events": [],
            "error": None,
        }

    calculation = calculate_opening_range_levels(selected_candles)
    calculation_status = calculation.get("status")
    levels = calculation.get("levels") or {}

    post_or_candles = select_post_opening_range_candles(candles=candles, candle_count=normalized_candle_count)

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
        "latest_intraday_close": latest_intraday_close if latest_intraday_close > 0 else None,
        "range": calculation.get("range"),
        "levels": levels,
        "selected_candles": serialized_selected_candles,
        "post_or_candles_count": len(post_or_candles),
        "touch_status": touch_status,
        "backfill_touch_events": backfill_touch_events,
        "error": None if calculation_status == "success" else calculation.get("message"),
    }

# ============================================================
# All Subscribed Instruments Opening Range
# ============================================================

def calculate_opening_range_for_all_subscribed(
    candle_count: int = DEFAULT_OPENING_RANGE_CANDLE_COUNT,
    save_data: bool = DEFAULT_SAVE_FILE,
    max_workers: int = DEFAULT_MAX_WORKERS,
) -> dict:
    """
    Fetches intraday candles and calculates Opening Range levels for all subscribed instruments.
    Backfill touch events are collected from all instruments before isolated-instrument selection.
    """
    runtime_state.ensure_current_market_day()

    if not is_opening_range_enabled():
        logger.info("Opening Range calculation skipped because it is disabled.")
        result = _build_short_summary(status="disabled", message="Opening Range calculation is disabled.")
        with runtime_state.opening_range_cache_lock:
            runtime_state.opening_range_cache["status"] = "disabled"
            runtime_state.opening_range_cache["message"] = result["message"]
        return result

    subscribed_keys = get_subscribed_instrument_keys()
    if not subscribed_keys:
        logger.warning("Opening Range calculation skipped because no subscribed instruments were found.")
        result = _build_short_summary(status="skipped", message="No subscribed instruments found.")
        with runtime_state.opening_range_cache_lock:
            runtime_state.opening_range_cache["status"] = "skipped"
            runtime_state.opening_range_cache["message"] = result["message"]
            runtime_state.opening_range_cache["total_instruments"] = 0
        return result

    normalized_candle_count = _normalize_candle_count(candle_count)
    total_instruments = len(subscribed_keys)
    normalized_max_workers = _normalize_max_workers(max_workers, total_instruments=total_instruments)

    now_market = get_now_market_time()
    started_at = now_market.isoformat()
    current_date = now_market.date().isoformat()

    logger.info("================ OPENING RANGE INTRADAY FETCH STARTED ================")
    logger.info("Calculating Opening Range for %s instruments. date=%s, candle_count=%s, interval=%s, unit=%s, intraday_interval=%s, market_open=%02d:%02d, max_workers=%s",
                total_instruments, current_date, normalized_candle_count, DEFAULT_OPENING_RANGE_INTERVAL,
                DEFAULT_INTRADAY_UNIT, DEFAULT_INTRADAY_INTERVAL, DEFAULT_MARKET_OPEN_HOUR, DEFAULT_MARKET_OPEN_MINUTE,
                normalized_max_workers)

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
        result = calculate_opening_range_for_instrument(instrument_key=worker_instrument_key, candle_count=normalized_candle_count)
        if DEFAULT_SLEEP_SECONDS > 0:
            time.sleep(DEFAULT_SLEEP_SECONDS)
        return result

    with ThreadPoolExecutor(max_workers=normalized_max_workers) as executor:
        future_to_instrument = {executor.submit(worker, instrument_key): instrument_key for instrument_key in subscribed_keys}
        for future in as_completed(future_to_instrument):
            instrument_key = future_to_instrument[future]
            completed_count += 1
            logger.info("Opening Range progress: %s/%s. instrument_key=%s", completed_count, total_instruments, instrument_key)
            try:
                result = future.result()
                if not isinstance(result, dict):
                    raise TypeError("Opening Range worker returned a non-dictionary result.")
                result_status = result.get("status")
                results[instrument_key] = result
                instrument_backfill_events = result.get("backfill_touch_events", [])
                if isinstance(instrument_backfill_events, list) and instrument_backfill_events:
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
                    errors[instrument_key] = result.get("error") or result.get("message") or "Unknown calculation failure."
            except Exception as ex:
                error_message = f"{type(ex).__name__}: {ex}"
                logger.exception("Opening Range calculation failed. instrument_key=%s, error=%s", instrument_key, error_message)
                failed_count += 1
                errors[instrument_key] = error_message
                results[instrument_key] = _build_instrument_failure_result(
                    instrument_key=instrument_key,
                    candle_count=normalized_candle_count,
                    processing_date=current_date,
                    message="Opening Range worker failed.",
                    error=error_message,
                )

    completed_at = get_now_market_time().isoformat()
    overall_status = _get_overall_status(success_count=success_count, failed_count=failed_count, empty_count=empty_count, insufficient_data_count=insufficient_data_count)
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
        insufficient_data_count=insufficient_data_count,
        results=results,
        errors=errors,
    )

    if backfill_touch_events:
        try:
            try_isolate_from_touch_events(backfill_touch_events)
        except Exception as ex:
            isolation_error = f"{type(ex).__name__}: {ex}"
            logger.exception("Opening Range backfill isolation failed. error=%s", isolation_error)
            errors["isolated_instrument_selection"] = isolation_error

    touch_snapshot = runtime_state.get_touch_state_snapshot()
    isolated_state = runtime_state.get_selected_or_state_snapshot()
    isolated_ema_alerts_count = len(runtime_state.get_selected_or_ema_alerts_snapshot())
    opening_range_end_time = get_opening_range_end_datetime(candle_count=normalized_candle_count, target_date=now_market.date()).strftime("%H:%M")

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
        "opening_range_candle_count": normalized_candle_count,
        "market_open_time": f"{DEFAULT_MARKET_OPEN_HOUR:02d}:{DEFAULT_MARKET_OPEN_MINUTE:02d}",
        "live_ema_calculation_mode_flag": DEFAULT_LIVE_EMA_CALCULATION_MODE,
        "live_ema_calculation_mode": get_live_ema_calculation_mode_text(),
        "opening_range_end_time": opening_range_end_time,
        "scheduled_fetch_time": f"{DEFAULT_FETCH_HOUR:02d}:{DEFAULT_FETCH_MINUTE:02d}",
        "max_workers": normalized_max_workers,
        "total_instruments": total_instruments,
        "success_count": success_count,
        "failed_count": failed_count,
        "empty_count": empty_count,
        "insufficient_data_count": insufficient_data_count,
        "backfill_touch_scan_enabled": DEFAULT_BACKFILL_SCAN_ENABLED,
        "backfill_touch_events_count": total_backfill_touch_events,
        "latest_main_index_ltp": touch_snapshot.get("latest_main_index_ltp"),
        "latest_main_index_ltp_source": touch_snapshot.get("latest_main_index_ltp_source"),
        "latest_main_index_ltp_updated_at": touch_snapshot.get("latest_main_index_ltp_updated_at"),
        "isolated_instrument": isolated_state,
        "isolated_ema_alerts_count": isolated_ema_alerts_count,
        "results": results,
        "backfill_touch_events": backfill_touch_events,
        "errors": errors,
    }

    output_file_path = None
    if bool(save_data):
        try:
            output_file_path = save_opening_range_results_to_file(summary=summary, output_file=DEFAULT_OUTPUT_FILE)
            summary["output_file_path"] = output_file_path
            logger.info("Saved Opening Range results. file_path=%s", output_file_path)
        except Exception as ex:
            output_file_error = f"{type(ex).__name__}: {ex}"
            logger.exception("Failed saving Opening Range results. error=%s", output_file_error)
            summary["output_file_path"] = None
            summary["output_file_error"] = output_file_error
            errors["opening_range_result_storage"] = output_file_error
    else:
        summary["output_file_path"] = None
        logger.info("Opening Range result file was not saved because save_data=False.")

    _update_cache_isolation_and_output(output_file_path=output_file_path)

    if DEFAULT_BACKFILL_TOUCH_ALERT_ENABLED and DEFAULT_TOUCH_ALERT_ENABLED and DEFAULT_LEGACY_TOUCH_TELEGRAM_ENABLED and backfill_touch_events:
        try:
            legacy_alert_sent = send_touch_events_telegram_alert(events=backfill_touch_events, source="intraday_backfill_scan", force=True)
            summary["legacy_backfill_touch_alert_sent"] = legacy_alert_sent
        except Exception as ex:
            legacy_alert_error = f"{type(ex).__name__}: {ex}"
            logger.exception("Failed sending legacy backfill touch alert. error=%s", legacy_alert_error)
            summary["legacy_backfill_touch_alert_sent"] = False
            summary["legacy_backfill_touch_alert_error"] = legacy_alert_error

    try:
        touch_test_file_path = save_touch_events_to_file_if_enabled()
        if touch_test_file_path:
            summary["touch_events_test_file_path"] = touch_test_file_path
    except Exception as ex:
        logger.exception("Unexpected error while saving Opening Range touch-event test data. error=%s: %s", type(ex).__name__, ex)

    runtime_state.synchronize_cache_counters()
    final_isolated_state = runtime_state.get_selected_or_state_snapshot()

    logger.info("Opening Range calculation completed. status=%s, total_instruments=%s, success=%s, empty=%s, insufficient_data=%s, failed=%s, backfill_touch_events=%s, isolated_selected=%s, output_file=%s",
                overall_status, total_instruments, success_count, empty_count, insufficient_data_count, failed_count,
                total_backfill_touch_events, final_isolated_state.get("selected"), output_file_path)
    logger.info("================ OPENING RANGE INTRADAY FETCH COMPLETED ================")

    return summary

# ============================================================
# Public API
# ============================================================

__all__ = [
    "calculate_opening_range_for_instrument",
    "calculate_opening_range_for_all_subscribed",
]