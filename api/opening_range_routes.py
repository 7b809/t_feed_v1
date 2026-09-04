from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.concurrency import run_in_threadpool

from core import config
from core.logger import get_logger
from services.algo_app_service import algo_app_service
from services.opening_range_service import (
    calculate_opening_range_for_all_subscribed,
    calculate_opening_range_for_instrument,
    flush_pending_touch_alerts,
    get_opening_range_cache,
    get_opening_range_dashboard_summary,
    get_opening_range_for_instrument_from_cache,
    get_opening_range_levels_for_ema_event,
    get_opening_range_pending_touch_events,
    get_opening_range_status,
    get_opening_range_touch_events,
    get_selected_or_ema_alerts,
    get_selected_or_instrument_state,
)
from services.option_service import options_cache
from services.telegram_service import telegram_service

logger = get_logger(__file__)

router = APIRouter()

def get_live_ema_calculation_mode_text() -> str:
    return "tick_ltp" if bool(getattr(config, "LIVE_EMA_CALCULATION_MODE", False)) else "candle_close"

def get_live_ema_calculation_mode_payload() -> dict:
    flag = bool(getattr(config, "LIVE_EMA_CALCULATION_MODE", False))
    mode = get_live_ema_calculation_mode_text()
    return {"flag": flag, "mode": mode, "description": "live tick/LTP based EMA calculation" if flag else "completed candle close based EMA calculation"}

def get_alert_transport_mode() -> str:
    return "local_test" if bool(getattr(config, "LOCAL_ALERT_TEST_MODE", False)) else "production"

def get_alert_execution_mode() -> str:
    return "background" if bool(getattr(config, "ALGO_APP_SEND_IN_BACKGROUND", True)) else "synchronous"

def get_local_alert_test_payload() -> dict:
    local_test_mode = bool(getattr(config, "LOCAL_ALERT_TEST_MODE", False))
    local_base_url = str(getattr(config, "LOCAL_ALERT_APP_BASE_URL", "") or "").strip()
    local_telegram_url = str(getattr(config, "LOCAL_TELEGRAM_URL", "") or "").strip()
    local_algo_url = str(getattr(config, "LOCAL_ALGO_APP_URL", "") or "").strip()
    return {
        "enabled": local_test_mode,
        "transport_mode": get_alert_transport_mode(),
        "production_delivery_blocked": bool(local_test_mode and getattr(config, "LOCAL_ALERT_PRODUCTION_DELIVERY_BLOCKED", True)),
        "local_alert_app_base_url": (local_base_url if local_test_mode else None),
        "telegram_url": (local_telegram_url if local_test_mode else None),
        "algo_app_url": (local_algo_url if local_test_mode else None),
        "telegram_url_configured": bool(local_telegram_url),
        "algo_app_url_configured": bool(local_algo_url),
        "timeout_seconds": (float(getattr(config, "LOCAL_ALERT_TIMEOUT_SECONDS", 10.0)) if local_test_mode else None),
        "source_name": (str(getattr(config, "LOCAL_ALERT_SOURCE_NAME", "option_feed_engine_local_test")) if local_test_mode else None),
    }

def get_telegram_payload() -> dict:
    try:
        service_status = telegram_service.get_status()
        if not isinstance(service_status, dict):
            service_status = {}
        return {
            "enabled": bool(service_status.get("enabled", getattr(config, "TELEGRAM_ENABLED", False))),
            "configured": bool(service_status.get("configured", False)),
            "delivery_mode": str(service_status.get("delivery_mode", get_alert_transport_mode())),
            "local_test_mode": bool(service_status.get("local_test_mode", False)),
            "target_url_configured": bool(service_status.get("target_url_configured", False)),
            "target_url": (service_status.get("target_url") if get_alert_transport_mode() == "local_test" else None),
            "bot_token_configured": bool(service_status.get("bot_token_configured", False)),
            "chat_id_configured": bool(service_status.get("chat_id_configured", False)),
            "timeout_seconds": service_status.get("timeout_seconds"),
            "isolated_instrument_alert_enabled": bool(getattr(config, "OPENING_RANGE_ISOLATED_INSTRUMENT_NOTIFY_ENABLED", True)),
            "isolated_ema_alert_enabled": bool(getattr(config, "EMA_ISOLATED_INSTRUMENT_TELEGRAM_ENABLED", True)),
            "production_delivery_blocked": bool(service_status.get("production_delivery_blocked", False)),
            "secrets_exposed": False,
        }
    except Exception as ex:
        logger.warning("Could not retrieve Telegram service status. error=%s: %s", type(ex).__name__, ex)
        return {
            "enabled": bool(getattr(config, "TELEGRAM_ENABLED", False)),
            "configured": False,
            "delivery_mode": get_alert_transport_mode(),
            "local_test_mode": bool(getattr(config, "LOCAL_ALERT_TEST_MODE", False)),
            "error": f"{type(ex).__name__}: {ex}",
            "secrets_exposed": False,
        }

def get_ema_order_side_rule_payload() -> dict:
    return {
        "mode": "dynamic_isolated_instrument_side",
        "rules": {"bullish_cross": "same_side_as_isolated_instrument", "bearish_cross": "opposite_side_of_isolated_instrument"},
        "examples": [
            {"isolated_instrument_type": "CE", "cross_type": "bullish_cross", "suggested_order_side": "CE"},
            {"isolated_instrument_type": "CE", "cross_type": "bearish_cross", "suggested_order_side": "PE"},
            {"isolated_instrument_type": "PE", "cross_type": "bullish_cross", "suggested_order_side": "PE"},
            {"isolated_instrument_type": "PE", "cross_type": "bearish_cross", "suggested_order_side": "CE"},
        ],
        "strike_selection": {
            "basis": "current_nifty_spot_ltp",
            "strike_step": getattr(config, "EMA_ALERT_STRIKE_STEP", 50),
            "offsets": getattr(config, "EMA_ALERT_NEAREST_STRIKE_OFFSETS", [-50, 0, 50]),
            "max_order_instruments": getattr(config, "EMA_ALERT_MAX_ORDER_INSTRUMENTS", 3),
            "clamp_to_filter_range": getattr(config, "EMA_ALERT_ORDER_STRIKES_CLAMP_TO_FILTER_RANGE", True),
        },
        "fallback": {
            "used_only_when_isolated_type_missing": True,
            "bullish_option_type": getattr(config, "EMA_ALERT_BULLISH_OPTION_TYPE", "CE"),
            "bearish_option_type": getattr(config, "EMA_ALERT_BEARISH_OPTION_TYPE", "PE"),
        },
    }

def get_budget_range_payload() -> dict:
    return {
        "enabled": bool(getattr(config, "EMA_ALERT_BUDGET_RANGE_ENABLED", True)),
        "minimum_price": getattr(config, "EMA_ALERT_BUDGET_MIN_PRICE", 50.0),
        "maximum_price": getattr(config, "EMA_ALERT_BUDGET_MAX_PRICE", 120.0),
        "maximum_instruments": getattr(config, "EMA_ALERT_BUDGET_MAX_INSTRUMENTS", 2),
        "use_suggested_order_side": bool(getattr(config, "EMA_ALERT_BUDGET_USE_SUGGESTED_ORDER_SIDE", True)),
        "subscribed_only": bool(getattr(config, "EMA_ALERT_BUDGET_SUBSCRIBED_ONLY", True)),
        "require_live_ltp": bool(getattr(config, "EMA_ALERT_BUDGET_REQUIRE_LIVE_LTP", True)),
        "sort_mode": getattr(config, "EMA_ALERT_BUDGET_SORT_MODE", "nearest_to_budget_midpoint"),
        "inclusive": bool(getattr(config, "EMA_ALERT_BUDGET_RANGE_INCLUSIVE", True)),
    }

def get_algo_app_payload() -> dict:
    try:
        service_status = algo_app_service.get_status()
        if not isinstance(service_status, dict):
            service_status = {}
        return {
            "enabled": bool(service_status.get("enabled", False)),
            "configured": bool(service_status.get("configured", False)),
            "execution_mode": get_alert_execution_mode(),
            "transport_mode": service_status.get("delivery_mode", get_alert_transport_mode()),
            "local_test_mode": bool(service_status.get("local_test_mode", False)),
            "url_configured": bool(service_status.get("url_configured", False)),
            "effective_url": (service_status.get("effective_url") if get_alert_transport_mode() == "local_test" else None),
            "production_url_configured": bool(service_status.get("production_url_configured", False)),
            "local_url_configured": bool(service_status.get("local_url_configured", False)),
            "auth_type": service_status.get("auth_type", "none"),
            "authentication_configured": bool(service_status.get("authentication_configured", False)),
            "timeout_seconds": service_status.get("timeout_seconds", 10.0),
            "verify_ssl": bool(service_status.get("verify_ssl", False)),
            "max_retries": service_status.get("max_retries", 3),
            "retry_delay_seconds": service_status.get("retry_delay_seconds", 2.0),
            "send_in_background": bool(service_status.get("send_in_background", True)),
            "pending_count": service_status.get("pending_count", 0),
            "delivery_success_count": service_status.get("delivery_success_count", 0),
            "delivery_failed_count": service_status.get("delivery_failed_count", 0),
            "retry_count": service_status.get("retry_count", 0),
            "last_success_at": service_status.get("last_success_at"),
            "last_failure_at": service_status.get("last_failure_at"),
            "last_error": service_status.get("last_error"),
            "include_event_id": bool(getattr(config, "ALGO_APP_INCLUDE_EVENT_ID", True)),
            "schema_version": getattr(config, "ALGO_APP_PAYLOAD_SCHEMA_VERSION", "1.0"),
            "source_name": getattr(config, "ALGO_APP_SOURCE_NAME", "option_feed_engine"),
            "production_delivery_blocked": bool(service_status.get("production_delivery_blocked", False)),
            "secrets_exposed": False,
        }
    except Exception as ex:
        logger.warning("Could not retrieve Algo App service status. error=%s: %s", type(ex).__name__, ex)
        return {
            "enabled": bool(getattr(config, "ALGO_APP_ENABLED", False)),
            "configured": False,
            "execution_mode": get_alert_execution_mode(),
            "transport_mode": get_alert_transport_mode(),
            "local_test_mode": bool(getattr(config, "LOCAL_ALERT_TEST_MODE", False)),
            "error": f"{type(ex).__name__}: {ex}",
            "secrets_exposed": False,
        }

def get_delivery_flow_payload() -> dict:
    transport_mode = get_alert_transport_mode()
    return {
        "opening_range": "all_subscribed_instruments",
        "live_ema": "all_initialized_instruments",
        "isolated_instrument": "one_instrument_per_market_day",
        "telegram": "isolated_instrument_only",
        "algo_app": "isolated_instrument_only",
        "telegram_and_algo_delivery": "independent",
        "transport_mode": transport_mode,
        "algo_execution_mode": get_alert_execution_mode(),
        "telegram_destination": "local_telegram_simulator" if transport_mode == "local_test" else "telegram_api",
        "algo_app_destination": "local_algo_app_simulator" if transport_mode == "local_test" else "configured_algo_app_url",
        "production_delivery_blocked": bool(transport_mode == "local_test" and getattr(config, "LOCAL_ALERT_PRODUCTION_DELIVERY_BLOCKED", True)),
    }

def get_ema_alert_content_payload() -> dict:
    return {
        "include_level_name": bool(getattr(config, "EMA_ISOLATED_ALERT_INCLUDE_LEVEL_NAME", True)),
        "include_nifty_ltp": bool(getattr(config, "EMA_ISOLATED_ALERT_INCLUDE_NIFTY_LTP", True)),
        "include_ema_details": bool(getattr(config, "EMA_ISOLATED_ALERT_INCLUDE_EMA_DETAILS", True)),
        "include_nearest_instruments": bool(getattr(config, "EMA_ISOLATED_ALERT_INCLUDE_NEAREST_ORDER_INSTRUMENTS", True)),
        "include_budget_instruments": bool(getattr(config, "EMA_ISOLATED_ALERT_INCLUDE_BUDGET_INSTRUMENTS", True)),
        "include_candle_close": bool(getattr(config, "EMA_ISOLATED_ALERT_INCLUDE_CANDLE_CLOSE", True)),
        "include_candle_low": bool(getattr(config, "EMA_ISOLATED_ALERT_INCLUDE_CANDLE_LOW", True)),
        "include_close_low_difference": bool(getattr(config, "EMA_ISOLATED_ALERT_INCLUDE_CLOSE_LOW_DIFFERENCE", True)),
        "include_candle_time": bool(getattr(config, "EMA_ISOLATED_ALERT_INCLUDE_CANDLE_TIME", True)),
        "price_decimal_places": getattr(config, "EMA_ISOLATED_ALERT_PRICE_DECIMAL_PLACES", 2),
    }

def get_ema_algo_payload_config() -> dict:
    return {
        "include_opening_range": bool(getattr(config, "EMA_ALGO_PAYLOAD_INCLUDE_OPENING_RANGE", True)),
        "include_ema_values": bool(getattr(config, "EMA_ALGO_PAYLOAD_INCLUDE_EMA_VALUES", True)),
        "include_candle": bool(getattr(config, "EMA_ALGO_PAYLOAD_INCLUDE_CANDLE", True)),
        "include_nearest_instruments": bool(getattr(config, "EMA_ALGO_PAYLOAD_INCLUDE_NEAREST_INSTRUMENTS", True)),
        "include_nearest_instrument_candles": bool(getattr(config, "EMA_ALGO_PAYLOAD_INCLUDE_NEAREST_INSTRUMENT_CANDLES", True)),
        "include_budget_instruments": bool(getattr(config, "EMA_ALGO_PAYLOAD_INCLUDE_BUDGET_INSTRUMENTS", True)),
        "include_budget_instrument_candles": bool(getattr(config, "EMA_ALGO_PAYLOAD_INCLUDE_BUDGET_INSTRUMENT_CANDLES", True)),
        "include_delivery_metadata": bool(getattr(config, "EMA_ALGO_PAYLOAD_INCLUDE_DELIVERY_METADATA", False)),
        "include_raw_ema_event": bool(getattr(config, "EMA_ALGO_PAYLOAD_INCLUDE_RAW_EMA_EVENT", True)),
        "transport_mode": get_alert_transport_mode(),
    }

def resolve_opening_range_instrument_key(
    instrument_key: str | None = None,
    strike: float | None = None,
    striketype: str | None = None,
) -> str:
    if instrument_key:
        normalized_key = str(instrument_key).strip()
        if normalized_key:
            return normalized_key
    if strike is None or not striketype:
        raise HTTPException(status_code=400, detail="Provide instrument_key or both strike and striketype.")
    option_type = str(striketype).strip().upper()
    if option_type not in {"CE", "PE"}:
        raise HTTPException(status_code=400, detail="Invalid striketype. Allowed values: ce, pe")
    try:
        target_strike = float(strike)
    except (TypeError, ValueError, OverflowError) as ex:
        raise HTTPException(status_code=400, detail="Invalid strike value.") from ex
    cache_data = options_cache.get("data", [])
    if not isinstance(cache_data, list):
        cache_data = []
    for item in cache_data:
        if not isinstance(item, dict):
            continue
        item_strike = item.get("strike_price")
        item_type = str(item.get("instrument_type", "")).strip().upper()
        try:
            strike_matches = float(item_strike) == target_strike
        except (TypeError, ValueError, OverflowError):
            continue
        if strike_matches and item_type == option_type:
            resolved_key = item.get("instrument_key")
            if resolved_key:
                return str(resolved_key)
    raise HTTPException(status_code=404, detail=f"No option instrument found for strike={target_strike}, striketype={option_type}.")

@router.get("/opening-range/status")
async def get_opening_range_latest_status():
    isolated_state = get_selected_or_instrument_state()
    return {
        "status": "success",
        "opening_range_status": get_opening_range_status(),
        "isolated_instrument": isolated_state,
        "selected_or_instrument": isolated_state,
        "live_ema_calculation": get_live_ema_calculation_mode_payload(),
        "ema_order_side_rule": get_ema_order_side_rule_payload(),
        "budget_range": get_budget_range_payload(),
        "local_alert_test": get_local_alert_test_payload(),
        "telegram": get_telegram_payload(),
        "algo_app": get_algo_app_payload(),
        "ema_alert_content": get_ema_alert_content_payload(),
        "ema_algo_payload": get_ema_algo_payload_config(),
        "flow": get_delivery_flow_payload(),
    }

@router.get("/opening-range/dashboard")
async def get_opening_range_dashboard(
    touch_limit: int = Query(default=100, ge=1, le=1000),
    alert_limit: int = Query(default=100, ge=1, le=1000),
):
    summary = get_opening_range_dashboard_summary(touch_limit=touch_limit, alert_limit=alert_limit)
    if not isinstance(summary, dict):
        summary = {"dashboard": summary}
    summary["budget_range"] = get_budget_range_payload()
    summary["local_alert_test"] = get_local_alert_test_payload()
    summary["telegram"] = get_telegram_payload()
    summary["algo_app"] = get_algo_app_payload()
    summary["ema_alert_content"] = get_ema_alert_content_payload()
    summary["delivery_flow"] = get_delivery_flow_payload()
    return summary

@router.post("/opening-range/fetch")
async def trigger_opening_range_fetch(
    candle_count: int | None = Query(default=None, ge=1, le=60),
    save_results: bool | None = Query(default=None),
    max_workers: int | None = Query(default=None, ge=1, le=25),
):
    selected_candle_count = candle_count if candle_count is not None else getattr(config, "OPENING_RANGE_CANDLE_COUNT", 1)
    selected_save_results = save_results if save_results is not None else bool(getattr(config, "OPENING_RANGE_SAVE_FILE", True))
    selected_max_workers = max_workers if max_workers is not None else getattr(config, "OPENING_RANGE_MAX_WORKERS", 5)
    logger.info("Manual Opening Range fetch requested. candle_count=%s, save_results=%s, max_workers=%s, transport_mode=%s",
                selected_candle_count, selected_save_results, selected_max_workers, get_alert_transport_mode())
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
            "opening_range_results_saved": selected_save_results,
            "isolated_instrument": isolated_state,
            "selected_or_instrument": isolated_state,
            "live_ema_calculation": get_live_ema_calculation_mode_payload(),
            "ema_order_side_rule": get_ema_order_side_rule_payload(),
            "budget_range": get_budget_range_payload(),
            "local_alert_test": get_local_alert_test_payload(),
            "telegram": get_telegram_payload(),
            "algo_app": get_algo_app_payload(),
            "delivery_flow": get_delivery_flow_payload(),
            "summary": summary,
        }
    except Exception as ex:
        error_message = f"{type(ex).__name__}: {ex}"
        logger.error("Manual Opening Range fetch failed: %s", error_message)
        raise HTTPException(status_code=500, detail=f"Opening Range fetch failed: {error_message}") from ex

@router.post("/opening-range/instrument/fetch")
async def fetch_opening_range_for_single_instrument(
    instrument_key: str | None = Query(default=None),
    strike: float | None = Query(default=None),
    striketype: str | None = Query(default=None),
    candle_count: int | None = Query(default=None, ge=1, le=60),
):
    resolved_instrument_key = resolve_opening_range_instrument_key(instrument_key=instrument_key, strike=strike, striketype=striketype)
    selected_candle_count = candle_count if candle_count is not None else getattr(config, "OPENING_RANGE_CANDLE_COUNT", 1)
    try:
        result = await run_in_threadpool(
            calculate_opening_range_for_instrument,
            instrument_key=resolved_instrument_key,
            candle_count=selected_candle_count,
        )
        return {
            "status": "success",
            "instrument_key": resolved_instrument_key,
            "input": {"instrument_key": instrument_key, "strike": strike, "striketype": striketype, "candle_count": candle_count},
            "live_ema_calculation": get_live_ema_calculation_mode_payload(),
            "ema_order_side_rule": get_ema_order_side_rule_payload(),
            "local_alert_test": get_local_alert_test_payload(),
            "result": result,
        }
    except Exception as ex:
        error_message = f"{type(ex).__name__}: {ex}"
        logger.error("Single instrument Opening Range fetch failed. instrument_key=%s, error=%s", resolved_instrument_key, error_message)
        raise HTTPException(status_code=500, detail=f"Opening Range fetch failed for {resolved_instrument_key}: {error_message}") from ex

@router.get("/opening-range/cache")
async def get_opening_range_full_cache():
    return {
        "status": "success",
        "live_ema_calculation": get_live_ema_calculation_mode_payload(),
        "ema_order_side_rule": get_ema_order_side_rule_payload(),
        "budget_range": get_budget_range_payload(),
        "local_alert_test": get_local_alert_test_payload(),
        "telegram": get_telegram_payload(),
        "algo_app": get_algo_app_payload(),
        "cache": get_opening_range_cache(),
    }

@router.get("/opening-range/instrument")
async def get_opening_range_instrument(
    instrument_key: str | None = Query(default=None),
    strike: float | None = Query(default=None),
    striketype: str | None = Query(default=None),
):
    resolved_instrument_key = resolve_opening_range_instrument_key(instrument_key=instrument_key, strike=strike, striketype=striketype)
    result = get_opening_range_for_instrument_from_cache(resolved_instrument_key)
    if not result:
        raise HTTPException(status_code=404, detail=f"Opening Range result not found for {resolved_instrument_key}.")
    return {
        "status": "success",
        "instrument_key": resolved_instrument_key,
        "input": {"instrument_key": instrument_key, "strike": strike, "striketype": striketype},
        "live_ema_calculation": get_live_ema_calculation_mode_payload(),
        "ema_order_side_rule": get_ema_order_side_rule_payload(),
        "result": result,
    }

@router.get("/opening-range/ema-context")
async def get_opening_range_ema_context(
    instrument_key: str | None = Query(default=None),
    strike: float | None = Query(default=None),
    striketype: str | None = Query(default=None),
):
    resolved_instrument_key = resolve_opening_range_instrument_key(instrument_key=instrument_key, strike=strike, striketype=striketype)
    context = get_opening_range_levels_for_ema_event(resolved_instrument_key)
    return {
        "status": "success",
        "instrument_key": resolved_instrument_key,
        "input": {"instrument_key": instrument_key, "strike": strike, "striketype": striketype},
        "live_ema_calculation": get_live_ema_calculation_mode_payload(),
        "ema_order_side_rule": get_ema_order_side_rule_payload(),
        "local_alert_test": get_local_alert_test_payload(),
        "opening_range": context,
    }

@router.get("/opening-range/selected-instrument")
async def get_selected_opening_range_instrument():
    isolated_state = get_selected_or_instrument_state()
    return {
        "status": "success",
        "flow": "isolated_instrument",
        "live_ema_calculation": get_live_ema_calculation_mode_payload(),
        "ema_order_side_rule": get_ema_order_side_rule_payload(),
        "budget_range": get_budget_range_payload(),
        "local_alert_test": get_local_alert_test_payload(),
        "telegram": get_telegram_payload(),
        "algo_app": get_algo_app_payload(),
        "delivery_flow": get_delivery_flow_payload(),
        "selected_or_instrument": isolated_state,
        "isolated_instrument": isolated_state,
    }

@router.get("/opening-range/selected-instrument/ema-alerts")
async def get_selected_opening_range_ema_alerts(limit: int = Query(default=100, ge=1, le=1000)):
    isolated_state = get_selected_or_instrument_state()
    return {
        "status": "success",
        "flow": "isolated_instrument",
        "limit": limit,
        "live_ema_calculation": get_live_ema_calculation_mode_payload(),
        "ema_order_side_rule": get_ema_order_side_rule_payload(),
        "budget_range": get_budget_range_payload(),
        "local_alert_test": get_local_alert_test_payload(),
        "telegram": get_telegram_payload(),
        "algo_app": get_algo_app_payload(),
        "selected_or_instrument": isolated_state,
        "isolated_instrument": isolated_state,
        "alerts": get_selected_or_ema_alerts(limit=limit),
    }

@router.get("/opening-range/isolated-instrument")
async def get_isolated_opening_range_instrument():
    return {
        "status": "success",
        "flow": "isolated_instrument",
        "live_ema_calculation": get_live_ema_calculation_mode_payload(),
        "ema_order_side_rule": get_ema_order_side_rule_payload(),
        "budget_range": get_budget_range_payload(),
        "local_alert_test": get_local_alert_test_payload(),
        "telegram": get_telegram_payload(),
        "algo_app": get_algo_app_payload(),
        "delivery_flow": get_delivery_flow_payload(),
        "isolated_instrument": get_selected_or_instrument_state(),
    }

@router.get("/opening-range/isolated-instrument/ema-alerts")
async def get_isolated_opening_range_ema_alerts(limit: int = Query(default=100, ge=1, le=1000)):
    return {
        "status": "success",
        "flow": "isolated_instrument",
        "limit": limit,
        "live_ema_calculation": get_live_ema_calculation_mode_payload(),
        "ema_order_side_rule": get_ema_order_side_rule_payload(),
        "budget_range": get_budget_range_payload(),
        "local_alert_test": get_local_alert_test_payload(),
        "telegram": get_telegram_payload(),
        "algo_app": get_algo_app_payload(),
        "isolated_instrument": get_selected_or_instrument_state(),
        "alerts": get_selected_or_ema_alerts(limit=limit),
    }

@router.get("/opening-range/touch-events")
async def get_opening_range_touch_events_route(limit: int = Query(default=100, ge=1, le=1000)):
    return {
        "status": "success",
        "limit": limit,
        "live_ema_calculation": get_live_ema_calculation_mode_payload(),
        "ema_order_side_rule": get_ema_order_side_rule_payload(),
        "local_alert_test": get_local_alert_test_payload(),
        "events": get_opening_range_touch_events(limit=limit),
        "isolated_instrument": get_selected_or_instrument_state(),
    }

@router.get("/opening-range/touch-events/pending")
async def get_opening_range_pending_touch_events_route():
    return {
        "status": "success",
        "legacy_touch_telegram_enabled": bool(getattr(config, "OPENING_RANGE_LEGACY_TOUCH_TELEGRAM_ENABLED", False)),
        "live_ema_calculation": get_live_ema_calculation_mode_payload(),
        "local_alert_test": get_local_alert_test_payload(),
        "telegram": get_telegram_payload(),
        "pending_events": get_opening_range_pending_touch_events(),
    }

@router.post("/opening-range/touch-events/flush")
async def flush_opening_range_pending_touch_events_route(force: bool = Query(default=True)):
    try:
        sent = await run_in_threadpool(flush_pending_touch_alerts, force=force, source="manual_api_flush")
        transport_mode = get_alert_transport_mode()
        return {
            "status": "success",
            "telegram_sent": sent,
            "transport_mode": transport_mode,
            "local_test_mode": (transport_mode == "local_test"),
            "legacy_touch_telegram_enabled": bool(getattr(config, "OPENING_RANGE_LEGACY_TOUCH_TELEGRAM_ENABLED", False)),
            "local_alert_test": get_local_alert_test_payload(),
            "message": (
                "Pending touch events sent to the local Telegram simulator."
                if (sent and transport_mode == "local_test")
                else ("Pending touch events flushed." if sent else "No Telegram message was sent.")
            ),
        }
    except Exception as ex:
        error_message = f"{type(ex).__name__}: {ex}"
        logger.error("Touch alert flush failed: %s", error_message)
        raise HTTPException(status_code=500, detail=f"Touch alert flush failed: {error_message}") from ex

@router.get("/opening-range/file")
async def get_opening_range_file_status():
    opening_range_file = Path(getattr(config, "OPENING_RANGE_OUTPUT_FILE", "data/opening_range_results.json"))
    touch_events_file = Path(getattr(config, "OPENING_RANGE_TOUCH_EVENTS_OUTPUT_FILE", "data/opening_range_touch_events.json"))
    isolated_file = Path(getattr(config, "OPENING_RANGE_ISOLATED_INSTRUMENT_OUTPUT_FILE", "data/isolated_opening_range_instrument.json"))
    return {
        "status": "success",
        "opening_range_file_exists": opening_range_file.exists(),
        "opening_range_file_path": str(opening_range_file),
        "touch_events_file_exists": touch_events_file.exists(),
        "touch_events_file_path": str(touch_events_file),
        "isolated_instrument_file_exists": isolated_file.exists(),
        "isolated_instrument_file_path": str(isolated_file),
    }

@router.get("/opening-range/config")
async def get_opening_range_config():
    return {
        "status": "success",
        "live_ema_calculation": get_live_ema_calculation_mode_payload(),
        "ema_order_side_rule": get_ema_order_side_rule_payload(),
        "budget_range": get_budget_range_payload(),
        "local_alert_test": get_local_alert_test_payload(),
        "telegram": get_telegram_payload(),
        "algo_app": get_algo_app_payload(),
        "delivery_flow": get_delivery_flow_payload(),
        "ema_alert_content": get_ema_alert_content_payload(),
        "ema_algo_payload": get_ema_algo_payload_config(),
        "config": {
            "opening_range_enabled": getattr(config, "OPENING_RANGE_ENABLED", True),
            "opening_range_interval": getattr(config, "OPENING_RANGE_INTERVAL", "1minute"),
            "opening_range_candle_count": getattr(config, "OPENING_RANGE_CANDLE_COUNT", 1),
            "market_timezone": getattr(config, "MARKET_TIMEZONE", "Asia/Kolkata"),
            "market_open_hour": getattr(config, "OPENING_RANGE_MARKET_OPEN_HOUR", 9),
            "market_open_minute": getattr(config, "OPENING_RANGE_MARKET_OPEN_MINUTE", 15),
            "fetch_hour": getattr(config, "OPENING_RANGE_FETCH_HOUR", 9),
            "fetch_minute": getattr(config, "OPENING_RANGE_FETCH_MINUTE", 18),
            "max_workers": getattr(config, "OPENING_RANGE_MAX_WORKERS", 5),
            "save_file": getattr(config, "OPENING_RANGE_SAVE_FILE", True),
            "output_file": getattr(config, "OPENING_RANGE_OUTPUT_FILE", "data/opening_range_results.json"),
            "backfill_touch_scan_enabled": getattr(config, "OPENING_RANGE_BACKFILL_TOUCH_SCAN_ENABLED", True),
            "touch_alert_enabled": getattr(config, "OPENING_RANGE_TOUCH_ALERT_ENABLED", True),
            "touch_alert_once_per_level": getattr(config, "OPENING_RANGE_TOUCH_ALERT_ONCE_PER_LEVEL", True),
            "touch_alert_options_only": getattr(config, "OPENING_RANGE_TOUCH_ALERT_OPTIONS_ONLY", True),
            "touch_check_mode": getattr(config, "OPENING_RANGE_TOUCH_CHECK_MODE", "high_low"),
            "isolation_enabled": getattr(config, "OPENING_RANGE_ISOLATED_INSTRUMENT_ENABLED", True),
            "isolation_window_points": getattr(config, "OPENING_RANGE_ISOLATION_AVERAGE_WINDOW_POINTS", 500.0),
            "isolation_touch_levels": getattr(config, "OPENING_RANGE_ISOLATION_TOUCH_LEVELS", ["R2", "R3", "S2", "S3"]),
            "isolation_priority_levels": getattr(config, "OPENING_RANGE_ISOLATION_PRIORITY_LEVELS", ["R3", "S3", "R2", "S2"]),
            "isolation_lock_for_day": getattr(config, "OPENING_RANGE_ISOLATION_LOCK_FOR_DAY", True),
            "isolation_allow_priority_upgrade": getattr(config, "OPENING_RANGE_ISOLATION_ALLOW_PRIORITY_UPGRADE", True),
            "isolation_allow_backfill_touch": getattr(config, "OPENING_RANGE_ISOLATION_ALLOW_BACKFILL_TOUCH", True),
            "isolation_allow_live_touch": getattr(config, "OPENING_RANGE_ISOLATION_ALLOW_LIVE_TOUCH", True),
            "isolated_notify_enabled": getattr(config, "OPENING_RANGE_ISOLATED_INSTRUMENT_NOTIFY_ENABLED", True),
            "legacy_touch_telegram_enabled": getattr(config, "OPENING_RANGE_LEGACY_TOUCH_TELEGRAM_ENABLED", False),
            "isolated_ema_telegram_enabled": getattr(config, "EMA_ISOLATED_INSTRUMENT_TELEGRAM_ENABLED", True),
            "isolated_ema_every_cross": getattr(config, "EMA_ISOLATED_ALERT_EVERY_CROSS", True),
            "ema_cross_include_opening_range": getattr(config, "EMA_CROSS_INCLUDE_OPENING_RANGE_LEVELS", True),
            "ema_cross_broadcast_without_opening_range": getattr(config, "EMA_CROSS_BROADCAST_WITHOUT_OPENING_RANGE", True),
            "live_ema_enabled": getattr(config, "LIVE_EMA_ENABLED", True),
            "live_ema_calculation_mode": get_live_ema_calculation_mode_text(),
            "live_ema_interval_minutes": getattr(config, "LIVE_EMA_INTERVAL_MINUTES", 1),
            "live_ema_fast_period": getattr(config, "LIVE_EMA_FAST_PERIOD", 9),
            "live_ema_slow_period": getattr(config, "LIVE_EMA_SLOW_PERIOD", 21),
            "local_alert_test_mode": bool(getattr(config, "LOCAL_ALERT_TEST_MODE", False)),
            "local_alert_production_delivery_blocked": bool(getattr(config, "LOCAL_ALERT_TEST_MODE", False) and getattr(config, "LOCAL_ALERT_PRODUCTION_DELIVERY_BLOCKED", True)),
            "alert_transport_mode": get_alert_transport_mode(),
            "algo_execution_mode": get_alert_execution_mode(),
        },
    }

__all__ = [
    "router",
    "get_live_ema_calculation_mode_text",
    "get_live_ema_calculation_mode_payload",
    "get_alert_transport_mode",
    "get_alert_execution_mode",
    "get_local_alert_test_payload",
    "get_telegram_payload",
    "get_ema_order_side_rule_payload",
    "get_budget_range_payload",
    "get_algo_app_payload",
    "get_delivery_flow_payload",
    "get_ema_alert_content_payload",
    "get_ema_algo_payload_config",
    "resolve_opening_range_instrument_key",
]