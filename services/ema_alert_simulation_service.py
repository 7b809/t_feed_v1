from copy import deepcopy
from math import isfinite
from typing import Any

from core.logger import get_logger
from services.live_ema_service import live_ema_service
from services.algo_app_service import algo_app_service
from services.telegram_service import telegram_service
from services.option_service import (
    get_contract_info_by_instrument_key,
    normalize_option_type,
)
from services.opening_range import state
from services.opening_range.candle_utils import get_now_market_time
from services.opening_range.ema_alerts import (
    process_selected_or_ema_cross_alert_detailed,
)

logger = get_logger(__file__)


class EmaAlertSimulationError(Exception):
    pass


SUPPORTED_CROSS_TYPES = {
    "bullish": "bullish_cross",
    "bullish_cross": "bullish_cross",
    "buy": "bullish_cross",
    "long": "bullish_cross",
    "up": "bullish_cross",
    "bearish": "bearish_cross",
    "bearish_cross": "bearish_cross",
    "sell": "bearish_cross",
    "short": "bearish_cross",
    "down": "bearish_cross",
}


def safe_float(
    value: Any,
    default: float | None = None,
) -> float | None:
    try:
        if value is None:
            return default

        numeric_value = float(value)

        if not isfinite(numeric_value):
            return default

        return numeric_value
    except (TypeError, ValueError, OverflowError):
        return default


def safe_int(
    value: Any,
    default: int = 0,
) -> int:
    try:
        if value is None:
            return default

        return int(float(value))
    except (TypeError, ValueError, OverflowError):
        return default


def normalize_cross_type(value: Any) -> str | None:
    normalized_value = str(value or "").strip().lower()
    return SUPPORTED_CROSS_TYPES.get(normalized_value)


def get_cross_direction(cross_type: str) -> str:
    if cross_type == "bullish_cross":
        return "bullish"

    if cross_type == "bearish_cross":
        return "bearish"

    return "unknown"


def get_instrument_contract_info(
    instrument_key: str,
) -> dict:
    contract_info = get_contract_info_by_instrument_key(instrument_key)

    if not isinstance(contract_info, dict) or not contract_info:
        raise EmaAlertSimulationError(f"Instrument was not found: {instrument_key}")

    resolved_contract = deepcopy(contract_info)
    resolved_contract.setdefault(
        "instrument_key",
        instrument_key,
    )

    option_type = normalize_option_type(
        resolved_contract.get("instrument_type") or resolved_contract.get("option_type")
    )

    if option_type is None:
        raise EmaAlertSimulationError(
            "EMA alert simulation currently supports only "
            "CE and PE option instruments."
        )

    resolved_contract["instrument_type"] = option_type
    resolved_contract["option_type"] = option_type

    return resolved_contract


def get_live_instrument_state(
    instrument_key: str,
) -> dict:
    try:
        instrument_state = live_ema_service.get_instrument_state(instrument_key)
    except Exception as ex:
        logger.warning(
            "Could not read live EMA state for simulation. "
            "instrument_key=%s, error=%s: %s",
            instrument_key,
            type(ex).__name__,
            ex,
        )
        instrument_state = None

    return deepcopy(instrument_state) if isinstance(instrument_state, dict) else {}


def get_cached_instrument_price(
    instrument_key: str,
    instrument_state: dict,
) -> float | None:
    candidates = [
        instrument_state.get("latest_close"),
        instrument_state.get("last_processed_tick_ltp"),
    ]

    try:
        latest_snapshot = state.get_latest_instrument_ltp_snapshot(instrument_key)
    except Exception as ex:
        logger.warning(
            "Could not read latest instrument snapshot. "
            "instrument_key=%s, error=%s: %s",
            instrument_key,
            type(ex).__name__,
            ex,
        )
        latest_snapshot = {}

    if isinstance(latest_snapshot, dict):
        candidates.extend(
            [
                latest_snapshot.get("live_ltp"),
                latest_snapshot.get("ltp"),
            ]
        )

    for candidate in candidates:
        numeric_value = safe_float(candidate)

        if numeric_value is not None and numeric_value > 0:
            return numeric_value

    return None


def resolve_simulation_price(
    instrument_key: str,
    requested_price: Any,
    candle: dict | None,
    instrument_state: dict,
) -> float:
    price = safe_float(requested_price)

    if price is None and isinstance(candle, dict):
        price = safe_float(candle.get("close"))

    if price is None:
        price = get_cached_instrument_price(
            instrument_key=instrument_key,
            instrument_state=instrument_state,
        )

    if price is None or price <= 0:
        raise EmaAlertSimulationError(
            "A positive price is required because no valid current "
            "instrument price is available."
        )

    return price


def normalize_simulation_candle(
    candle: dict | None,
    price: float,
) -> dict:
    now_market = get_now_market_time()

    normalized_candle = deepcopy(candle) if isinstance(candle, dict) else {}

    open_price = safe_float(
        normalized_candle.get("open"),
        default=price,
    )
    high_price = safe_float(
        normalized_candle.get("high"),
        default=price,
    )
    low_price = safe_float(
        normalized_candle.get("low"),
        default=price,
    )
    close_price = safe_float(
        normalized_candle.get("close"),
        default=price,
    )
    volume = safe_float(
        normalized_candle.get("volume"),
        default=0.0,
    )

    if (
        open_price is None
        or high_price is None
        or low_price is None
        or close_price is None
    ):
        raise EmaAlertSimulationError(
            "The simulation candle contains invalid OHLC values."
        )

    if (
        min(
            open_price,
            high_price,
            low_price,
            close_price,
        )
        <= 0
    ):
        raise EmaAlertSimulationError("Simulation candle OHLC values must be positive.")

    if high_price < low_price:
        raise EmaAlertSimulationError(
            "Simulation candle high cannot be lower than low."
        )

    if high_price < max(open_price, close_price):
        raise EmaAlertSimulationError(
            "Simulation candle high cannot be lower than " "open or close."
        )

    if low_price > min(open_price, close_price):
        raise EmaAlertSimulationError(
            "Simulation candle low cannot be higher than " "open or close."
        )

    if volume is None or volume < 0:
        raise EmaAlertSimulationError("Simulation candle volume cannot be negative.")

    timestamp = normalized_candle.get("timestamp") or now_market.isoformat()

    timestamp_ms = normalized_candle.get("timestamp_ms")

    return {
        "timestamp": timestamp,
        "timestamp_ms": timestamp_ms,
        "open": open_price,
        "high": high_price,
        "low": low_price,
        "close": close_price,
        "volume": volume,
        "close_minus_low_points": round(
            close_price - low_price,
            4,
        ),
        "high_minus_low_points": round(
            high_price - low_price,
            4,
        ),
    }


def calculate_simulated_ema_values(
    instrument_state: dict,
    cross_type: str,
    price: float,
) -> dict:
    fast_period = safe_int(
        instrument_state.get("fast_period"),
        default=safe_int(
            getattr(live_ema_service, "fast_period", 9),
            default=9,
        ),
    )

    slow_period = safe_int(
        instrument_state.get("slow_period"),
        default=safe_int(
            getattr(live_ema_service, "slow_period", 21),
            default=21,
        ),
    )

    fast_period = max(1, fast_period)
    slow_period = max(1, slow_period)

    previous_fast = safe_float(instrument_state.get("previous_ema_fast"))
    previous_slow = safe_float(instrument_state.get("previous_ema_slow"))

    if previous_fast is None:
        previous_fast = price

    if previous_slow is None:
        previous_slow = price

    difference = max(
        abs(price) * 0.0001,
        0.01,
    )

    if cross_type == "bullish_cross":
        previous_slow = max(
            previous_slow,
            previous_fast,
        )
        previous_fast = min(
            previous_fast,
            previous_slow,
        )

        current_slow = previous_slow
        current_fast = current_slow + difference
        previous_signal = "bearish"
        current_signal = "bullish"
    else:
        previous_slow = min(
            previous_slow,
            previous_fast,
        )
        previous_fast = max(
            previous_fast,
            previous_slow,
        )

        current_slow = previous_slow
        current_fast = current_slow - difference
        previous_signal = "bullish"
        current_signal = "bearish"

    return {
        "ema_fast_period": fast_period,
        "ema_slow_period": slow_period,
        "previous_ema_fast": round(previous_fast, 4),
        "previous_ema_slow": round(previous_slow, 4),
        "ema_fast": round(current_fast, 4),
        "ema_slow": round(current_slow, 4),
        "previous_signal": previous_signal,
        "current_signal": current_signal,
    }


def build_simulated_selected_state(
    instrument_key: str,
    contract_info: dict,
    price: float,
) -> dict:
    now_market = get_now_market_time()

    try:
        current_state = state.get_selected_or_state_snapshot()
    except Exception:
        current_state = {}

    if not isinstance(current_state, dict):
        current_state = {}

    levels = current_state.get("levels")

    if not isinstance(levels, dict):
        levels = {}

    selected_level = current_state.get("selected_level") or "SIMULATED"

    latest_main_index_ltp = state.get_latest_main_index_ltp_value()

    return {
        "selected": True,
        "instrument_key": instrument_key,
        "selected_level": selected_level,
        "level_value": price,
        "selected_level_value": price,
        "trigger_price": price,
        "trigger_field": "simulation_price",
        "touch_time": now_market.isoformat(),
        "touch_source": "manual_simulation",
        "selected_at": now_market.isoformat(),
        "selection_priority": 999,
        "selection_reason": "manual_ema_alert_simulation",
        "locked_for_market_day": False,
        "reference_average": current_state.get("reference_average"),
        "average_window": deepcopy(current_state.get("average_window")),
        "contract_info": deepcopy(contract_info),
        "range": deepcopy(current_state.get("range")),
        "levels": deepcopy(levels),
        "latest_live_data": {
            "ltp": price,
            "updated_at": now_market.isoformat(),
        },
        "latest_main_index_ltp": latest_main_index_ltp,
        "live_ema_calculation_mode_flag": bool(
            getattr(
                live_ema_service,
                "tick_based_mode",
                False,
            )
        ),
        "live_ema_calculation_mode": str(
            getattr(
                live_ema_service,
                "calculation_mode",
                "candle_close",
            )
        ),
        "ema_alerts_count": 0,
        "disabled": False,
        "simulation": True,
        "message": "Temporary selected state for EMA simulation.",
    }


def build_simulated_ema_event(
    instrument_key: str,
    contract_info: dict,
    cross_type: str,
    price: float,
    candle: dict,
    instrument_state: dict,
    dry_run: bool,
    send_telegram: bool,
    send_algo_app: bool,
    requested_by: str,
) -> dict:
    now_market = get_now_market_time()

    ema_values = calculate_simulated_ema_values(
        instrument_state=instrument_state,
        cross_type=cross_type,
        price=price,
    )

    tick_based_mode = bool(
        getattr(
            live_ema_service,
            "tick_based_mode",
            False,
        )
    )

    calculation_mode = "tick_ltp" if tick_based_mode else "candle_close"

    tick = None

    if tick_based_mode:
        tick = {
            "timestamp": candle.get("timestamp"),
            "timestamp_ms": candle.get("timestamp_ms"),
            "ltp": price,
            "ltq": 0,
            "ltt": candle.get("timestamp_ms"),
        }

    return {
        "type": "live_ema_cross",
        "instrument_key": instrument_key,
        "timestamp": candle.get("timestamp"),
        "timestamp_ms": candle.get("timestamp_ms"),
        "cross_type": cross_type,
        "direction": get_cross_direction(cross_type),
        "interval_minutes": (
            0
            if tick_based_mode
            else safe_int(
                getattr(
                    live_ema_service,
                    "interval_minutes",
                    1,
                ),
                default=1,
            )
        ),
        "close": price,
        "ltp": price,
        **ema_values,
        "source": "manual_simulation",
        "ema_calculation_mode": calculation_mode,
        "created_at": now_market.isoformat(),
        "candle": deepcopy(candle),
        "tick": tick,
        "contract_info": deepcopy(contract_info),
        "info": deepcopy(contract_info),
        "telegram_alert_scope": "simulated_instrument",
        "is_simulation": True,
        "simulation": {
            "enabled": True,
            "dry_run": bool(dry_run),
            "requested_by": requested_by,
            "requested_at": now_market.isoformat(),
            "send_telegram": bool(send_telegram),
            "send_algo_app": bool(send_algo_app),
            "live_state_modified": False,
            "selected_state_modified": False,
        },
    }


def simulate_ema_alert(
    instrument_key: str,
    cross_type: str,
    price: float | None = None,
    candle: dict | None = None,
    dry_run: bool = True,
    send_telegram: bool = False,
    send_algo_app: bool = False,
    requested_by: str = "ema_simulation_api",
) -> dict:
    normalized_instrument_key = str(instrument_key or "").strip()

    if not normalized_instrument_key:
        raise EmaAlertSimulationError("instrument_key is required.")

    normalized_cross_type = normalize_cross_type(cross_type)

    if normalized_cross_type is None:
        raise EmaAlertSimulationError(
            "cross_type must be bullish_cross " "or bearish_cross."
        )

    normalized_requested_by = str(requested_by or "ema_simulation_api").strip()

    if not normalized_requested_by:
        normalized_requested_by = "ema_simulation_api"

    if dry_run and (send_telegram or send_algo_app):
        raise EmaAlertSimulationError(
            "send_telegram and send_algo_app must be false when dry_run is true."
        )

    if not dry_run and not (send_telegram or send_algo_app):
        raise EmaAlertSimulationError(
            "At least one delivery option must be enabled when dry_run is false."
        )

    contract_info = get_instrument_contract_info(normalized_instrument_key)

    instrument_state = get_live_instrument_state(normalized_instrument_key)

    resolved_price = resolve_simulation_price(
        instrument_key=normalized_instrument_key,
        requested_price=price,
        candle=candle,
        instrument_state=instrument_state,
    )

    normalized_candle = normalize_simulation_candle(
        candle=candle,
        price=resolved_price,
    )

    selected_state = build_simulated_selected_state(
        instrument_key=normalized_instrument_key,
        contract_info=contract_info,
        price=resolved_price,
    )

    ema_event = build_simulated_ema_event(
        instrument_key=normalized_instrument_key,
        contract_info=contract_info,
        cross_type=normalized_cross_type,
        price=resolved_price,
        candle=normalized_candle,
        instrument_state=instrument_state,
        dry_run=dry_run,
        send_telegram=send_telegram,
        send_algo_app=send_algo_app,
        requested_by=normalized_requested_by,
    )

    logger.warning(
        "EMA alert simulation started. "
        "instrument_key=%s, cross_type=%s, price=%s, "
        "dry_run=%s, requested_by=%s",
        normalized_instrument_key,
        normalized_cross_type,
        resolved_price,
        dry_run,
        normalized_requested_by,
    )

    try:
        processing_result = process_selected_or_ema_cross_alert_detailed(
            ema_event=ema_event,
            selected_state_override=selected_state,
            simulation=True,
            dry_run=dry_run,
            send_telegram=send_telegram,
            send_algo_app=send_algo_app,
        )
    except EmaAlertSimulationError:
        raise
    except Exception as ex:
        logger.exception(
            "EMA alert simulation processing failed. "
            "instrument_key=%s, cross_type=%s, error=%s: %s",
            normalized_instrument_key,
            normalized_cross_type,
            type(ex).__name__,
            ex,
        )

        raise EmaAlertSimulationError(
            "EMA alert simulation processing failed: " f"{type(ex).__name__}: {ex}"
        ) from ex

    if not isinstance(processing_result, dict):
        raise EmaAlertSimulationError("EMA alert processor returned an invalid result.")

    accepted = bool(
        processing_result.get(
            "accepted",
            processing_result.get("success", False),
        )
    )

    event_id = processing_result.get("event_id")

    if event_id is None:
        payload = processing_result.get("payload")

        if isinstance(payload, dict):
            event_id = payload.get("event_id")

    logger.info(
        "EMA alert simulation completed. "
        "instrument_key=%s, cross_type=%s, accepted=%s, "
        "event_id=%s, dry_run=%s",
        normalized_instrument_key,
        normalized_cross_type,
        accepted,
        event_id,
        True,
    )

    delivery = {
        "dry_run": bool(dry_run),
        "send_telegram": bool(send_telegram),
        "send_algo_app": bool(send_algo_app),
        "telegram": None,
        "algo_app": None,
    }
    if isinstance(processing_result, dict):
        for key in ("telegram", "telegram_delivery", "telegram_result"):
            if key in processing_result:
                delivery["telegram"] = deepcopy(processing_result.get(key))
                break
        for key in ("algo_app", "algo_app_delivery", "algo_app_result"):
            if key in processing_result:
                delivery["algo_app"] = deepcopy(processing_result.get(key))
                break

    return {
        "success": accepted,
        "simulation": True,
        "dry_run": bool(dry_run),
        "instrument_key": normalized_instrument_key,
        "cross_type": normalized_cross_type,
        "price": resolved_price,
        "message": (
            "EMA alert simulation dry-run completed successfully."
            if dry_run and accepted
            else (
                "EMA alert simulation delivered successfully."
                if accepted
                else "EMA alert simulation was not accepted."
            )
        ),
        "ema_event": deepcopy(ema_event),
        "selected_state": deepcopy(selected_state),
        "delivery": delivery,
        "result": deepcopy(processing_result),
    }


class EmaAlertSimulationService:
    def simulate(
        self,
        instrument_key: str,
        cross_type: str,
        price: float | None = None,
        candle: dict | None = None,
        dry_run: bool = True,
        send_telegram: bool = False,
        send_algo_app: bool = False,
        requested_by: str = "ema_simulation_api",
    ) -> dict:
        return simulate_ema_alert(
            instrument_key=instrument_key,
            cross_type=cross_type,
            price=price,
            candle=candle,
            dry_run=dry_run,
            send_telegram=send_telegram,
            send_algo_app=send_algo_app,
            requested_by=requested_by,
        )


ema_alert_simulation_service = EmaAlertSimulationService()


__all__ = [
    "EmaAlertSimulationError",
    "EmaAlertSimulationService",
    "SUPPORTED_CROSS_TYPES",
    "safe_float",
    "safe_int",
    "normalize_cross_type",
    "get_cross_direction",
    "get_instrument_contract_info",
    "get_live_instrument_state",
    "get_cached_instrument_price",
    "resolve_simulation_price",
    "normalize_simulation_candle",
    "calculate_simulated_ema_values",
    "build_simulated_selected_state",
    "build_simulated_ema_event",
    "simulate_ema_alert",
    "ema_alert_simulation_service",
]
