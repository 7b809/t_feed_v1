from copy import deepcopy
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from core import config
from core.logger import get_logger
from services.algo_app_service import algo_app_service
from services.option_service import normalize_candle, options_cache
from services.telegram_service import telegram_service

from . import state
from .candle_utils import (
    get_live_ema_calculation_mode_text,
    get_now_market_time,
    normalize_option_type,
    parse_candle_timestamp,
    safe_int,
)
from .constants import (
    DEFAULT_EMA_CROSS_INCLUDE_OPENING_RANGE_LEVELS,
    DEFAULT_EMA_ISOLATED_TELEGRAM_ENABLED,
    DEFAULT_LIVE_EMA_CALCULATION_MODE,
)

logger = get_logger(__file__)

logger.info(
    "EMA alerts service module initialized. isolated_telegram_enabled=%s, algo_app_enabled=%s, live_ema_mode=%s, include_opening_range_levels=%s",
    bool(DEFAULT_EMA_ISOLATED_TELEGRAM_ENABLED),
    bool(getattr(config, "ALGO_APP_ENABLED", False)),
    bool(DEFAULT_LIVE_EMA_CALCULATION_MODE),
    bool(DEFAULT_EMA_CROSS_INCLUDE_OPENING_RANGE_LEVELS),
)


def is_selected_or_instrument_locked() -> bool:
    with state.selected_or_lock:
        return bool(state.selected_or_instrument_state.get("selected"))


def get_selected_or_instrument_key() -> str | None:
    with state.selected_or_lock:
        instrument_key = state.selected_or_instrument_state.get("instrument_key")
    if instrument_key is None:
        return None
    normalized_key = str(instrument_key).strip()
    return normalized_key if normalized_key else None


def get_selected_or_instrument_state() -> dict:
    return state.get_selected_or_state_snapshot()


def get_selected_or_ema_alerts(limit: int = 100) -> list:
    normalized_limit = max(1, safe_int(limit, default=100))
    return state.get_selected_or_ema_alerts_snapshot(limit=normalized_limit)


def get_isolated_instrument_type_from_state(selected_state: dict) -> str | None:
    if not isinstance(selected_state, dict):
        return None
    contract_info = selected_state.get("contract_info") or {}
    if not isinstance(contract_info, dict):
        return None
    instrument_type = contract_info.get("instrument_type") or contract_info.get(
        "option_type"
    )
    return normalize_option_type(instrument_type)


def _format_numeric_value(
    value: Any,
    unavailable_text: str = "not_available",
    decimal_places: int | None = None,
) -> str:
    if value is None:
        return unavailable_text
    try:
        numeric_value = float(value)
    except (TypeError, ValueError, OverflowError):
        text = str(value).strip()
        return text if text else unavailable_text
    if decimal_places is not None:
        return f"{numeric_value:.{decimal_places}f}"
    if numeric_value.is_integer():
        return str(int(numeric_value))
    return f"{numeric_value:.4f}".rstrip("0").rstrip(".")


def _safe_float(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _format_price_value(
    value: Any, unavailable_text: str = "N/A", decimal_places: int | None = None
) -> str:
    if value is None:
        return unavailable_text
    if decimal_places is None:
        decimal_places = safe_int(
            getattr(config, "EMA_ISOLATED_ALERT_PRICE_DECIMAL_PLACES", 2), default=2
        )
    decimal_places = max(0, decimal_places)
    try:
        numeric_value = float(value)
    except (TypeError, ValueError, OverflowError):
        return unavailable_text
    return f"₹{numeric_value:.{decimal_places}f}"


def _format_volume_value(value: Any, unavailable_text: str = "N/A") -> str:
    if value is None:
        return unavailable_text
    try:
        numeric_value = int(float(value))
    except (TypeError, ValueError, OverflowError):
        return unavailable_text
    if numeric_value < 0:
        return unavailable_text
    return f"{numeric_value:,}"


def _format_cross_type_text(value: Any, unavailable_text: str = "N/A") -> str:
    normalized_value = str(value or "").strip().lower()
    cross_type_mapping = {
        "bullish": "Bullish",
        "bullish_cross": "Bullish Cross",
        "buy": "Bullish Cross",
        "long": "Bullish Cross",
        "up": "Bullish Cross",
        "bearish": "Bearish",
        "bearish_cross": "Bearish Cross",
        "sell": "Bearish Cross",
        "short": "Bearish Cross",
        "down": "Bearish Cross",
    }
    if normalized_value in cross_type_mapping:
        return cross_type_mapping[normalized_value]
    if not normalized_value:
        return unavailable_text
    return normalized_value.replace("_", " ").title()


def _get_ema_alert_icon(direction: Any = None, cross_type: Any = None) -> str:
    normalized_direction = str(direction or "").strip().lower()
    normalized_cross_type = str(cross_type or "").strip().lower()
    combined_value = f"{normalized_direction} {normalized_cross_type}"
    if "bullish" in combined_value:
        return "📈"
    if "bearish" in combined_value:
        return "📉"
    return "📊"


def _format_telegram_timestamp(
    timestamp_value: Any, unavailable_text: str = "N/A"
) -> str:
    parsed_timestamp = parse_candle_timestamp(timestamp_value)
    if parsed_timestamp is None:
        if timestamp_value is None:
            return unavailable_text
        timestamp_text = str(timestamp_value).strip()
        return timestamp_text if timestamp_text else unavailable_text
    market_timezone_name = str(
        getattr(config, "MARKET_TIMEZONE", "Asia/Kolkata") or "Asia/Kolkata"
    ).strip()
    try:
        market_timezone = ZoneInfo(market_timezone_name)
    except ZoneInfoNotFoundError:
        market_timezone = ZoneInfo("Asia/Kolkata")
    if parsed_timestamp.tzinfo is None:
        parsed_timestamp = parsed_timestamp.replace(tzinfo=market_timezone)
    else:
        parsed_timestamp = parsed_timestamp.astimezone(market_timezone)
    return parsed_timestamp.strftime("%d %b %Y, %I:%M %p IST")


def _format_market_timestamp(timestamp_value: Any) -> str | None:
    parsed_timestamp = parse_candle_timestamp(timestamp_value)
    if parsed_timestamp is None:
        if timestamp_value is None:
            return None
        text = str(timestamp_value).strip()
        return text if text else None
    return parsed_timestamp.isoformat()


def _format_option_label(strike: Any, option_type: Any) -> str:
    formatted_strike = _format_numeric_value(strike, unavailable_text="N/A")
    normalized_option_type = normalize_option_type(option_type)
    option_type_text = normalized_option_type if normalized_option_type else "N/A"
    return f"{formatted_strike} {option_type_text}"


def extract_ema_candle_details(ema_event: dict) -> dict:
    if not isinstance(ema_event, dict):
        ema_event = {}
    candle = ema_event.get("candle") or {}
    if not isinstance(candle, dict):
        candle = {}
    tick = ema_event.get("tick") or {}
    if not isinstance(tick, dict):
        tick = {}
    close_price = _safe_float(
        candle.get("close"),
        default=_safe_float(
            ema_event.get("close"), default=_safe_float(ema_event.get("ltp"))
        ),
    )
    low_price = _safe_float(candle.get("low"))
    open_price = _safe_float(candle.get("open"))
    high_price = _safe_float(candle.get("high"))
    volume = _safe_float(candle.get("volume"))
    candle_timestamp = (
        candle.get("timestamp") or ema_event.get("timestamp") or tick.get("timestamp")
    )
    close_minus_low = None
    if close_price is not None and low_price is not None:
        close_minus_low = round(close_price - low_price, 4)
    return {
        "timestamp": _format_market_timestamp(candle_timestamp),
        "open": open_price,
        "high": high_price,
        "low": low_price,
        "close": close_price,
        "volume": volume,
        "close_minus_low_points": close_minus_low,
    }


def get_suggested_order_option_type(
    instruments: list | None, cross_type: str, isolated_instrument_type: str | None
) -> str | None:
    normalized_isolated_type = normalize_option_type(isolated_instrument_type)
    normalized_cross_type = str(cross_type or "").strip().lower()
    if normalized_isolated_type:
        if "bullish" in normalized_cross_type:
            return normalized_isolated_type
        if "bearish" in normalized_cross_type:
            return "PE" if normalized_isolated_type == "CE" else "CE"
    if isinstance(instruments, list):
        for item in instruments:
            if not isinstance(item, dict):
                continue
            option_type = normalize_option_type(
                item.get("option_type") or item.get("instrument_type")
            )
            if option_type:
                return option_type
    if "bullish" in normalized_cross_type:
        return normalize_option_type(
            getattr(config, "EMA_ALERT_BULLISH_OPTION_TYPE", None)
        )
    if "bearish" in normalized_cross_type:
        return normalize_option_type(
            getattr(config, "EMA_ALERT_BEARISH_OPTION_TYPE", None)
        )
    return None


def get_option_chain_instruments_for_ema(
    *, cross_type: str, isolated_instrument_type: str | None
):
    from services.main_index_ltp_service import get_nearest_option_instruments

    suggested_order_option_type = get_suggested_order_option_type(
        instruments=[],
        cross_type=cross_type,
        isolated_instrument_type=isolated_instrument_type,
    )
    if not suggested_order_option_type:
        error_message = "Could not resolve suggested option type for isolated EMA alert."
        logger.warning(
            "%s cross_type=%s, isolated_type=%s",
            error_message,
            cross_type,
            isolated_instrument_type,
        )
        return {
            "status": "failed",
            "success": False,
            "suggested_order_option_type": None,
            "underlying_spot_price": None,
            "expiry_date": None,
            "data_source": None,
            "nearest_instruments": [],
            "nearest_strikes": [],
            "budget_instruments": [],
            "budget_range": {},
            "raw_result": {},
            "error": error_message,
        }
    requested_count = max(
        1,
        safe_int(getattr(config, "MAIN_INDEX_NEAREST_INSTRUMENTS_COUNT", 3), default=3),
    )
    try:
        option_chain_result = get_nearest_option_instruments(
            option_type=suggested_order_option_type, count=requested_count
        )
    except Exception as ex:
        error_message = f"{type(ex).__name__}: {ex}"
        logger.exception(
            "EMA option-chain instrument lookup failed. cross_type=%s, isolated_type=%s, order_side=%s",
            cross_type,
            isolated_instrument_type,
            suggested_order_option_type,
        )
        return {
            "status": "failed",
            "success": False,
            "suggested_order_option_type": suggested_order_option_type,
            "underlying_spot_price": None,
            "expiry_date": None,
            "data_source": None,
            "nearest_instruments": [],
            "nearest_strikes": [],
            "budget_instruments": [],
            "budget_range": {},
            "raw_result": {},
            "error": error_message,
        }
    if not isinstance(option_chain_result, dict):
        error_message = "Option-chain service returned an invalid response."
        return {
            "status": "failed",
            "success": False,
            "suggested_order_option_type": suggested_order_option_type,
            "underlying_spot_price": None,
            "expiry_date": None,
            "data_source": None,
            "nearest_instruments": [],
            "nearest_strikes": [],
            "budget_instruments": [],
            "budget_range": {},
            "raw_result": {},
            "error": error_message,
        }
    nearest = option_chain_result.get("nearest", {})
    if not isinstance(nearest, dict):
        nearest = {}
    budget_range = option_chain_result.get("budget_range", {})
    if not isinstance(budget_range, dict):
        budget_range = {}
    nearest_instruments = nearest.get("instruments", [])
    if not isinstance(nearest_instruments, list):
        nearest_instruments = []
    budget_instruments = budget_range.get("instruments", [])
    if not isinstance(budget_instruments, list):
        budget_instruments = []
    nearest_strikes = nearest.get("strikes", [])
    if not isinstance(nearest_strikes, list):
        nearest_strikes = []
    success = bool(option_chain_result.get("success"))
    logger.info(
        "EMA option-chain instruments resolved. success=%s, cross_type=%s, isolated_type=%s, order_side=%s, spot_price=%s, nearest_count=%s, budget_count=%s, expiry=%s",
        success,
        cross_type,
        isolated_instrument_type,
        suggested_order_option_type,
        option_chain_result.get("underlying_spot_price"),
        len(nearest_instruments),
        len(budget_instruments),
        option_chain_result.get("expiry_date"),
    )
    return {
        "status": option_chain_result.get("status", "failed"),
        "success": success,
        "suggested_order_option_type": suggested_order_option_type,
        "underlying_spot_price": option_chain_result.get("underlying_spot_price"),
        "expiry_date": option_chain_result.get("expiry_date"),
        "expiry_source": option_chain_result.get("expiry_source"),
        "data_source": option_chain_result.get("data_source"),
        "nearest_instruments": deepcopy(nearest_instruments),
        "nearest_strikes": deepcopy(nearest_strikes),
        "budget_instruments": deepcopy(budget_instruments),
        "budget_range": deepcopy(budget_range),
        "raw_result": deepcopy(option_chain_result),
        "error": option_chain_result.get("error"),
    }


def enrich_option_chain_instruments(
    *, instruments: list, isolated_instrument_key: str
) -> list:
    if not isinstance(instruments, list):
        return []
    normalized_isolated_key = str(isolated_instrument_key or "").strip()
    output = []
    for item in instruments:
        if not isinstance(item, dict):
            continue
        instrument = deepcopy(item)
        instrument_key = str(instrument.get("instrument_key") or "").strip()
        option_type = normalize_option_type(
            instrument.get("option_type") or instrument.get("instrument_type")
        )
        if option_type:
            instrument["instrument_type"] = option_type
            instrument["option_type"] = option_type
        instrument["is_isolated_instrument"] = bool(
            instrument_key
            and normalized_isolated_key
            and instrument_key == normalized_isolated_key
        )
        option_ltp = _safe_float(instrument.get("ltp"))
        market_data = instrument.get("market_data", {})
        if not isinstance(market_data, dict):
            market_data = {}
        if option_ltp is None:
            option_ltp = _safe_float(market_data.get("ltp"))
        instrument["ltp"] = option_ltp
        instrument["live_ltp"] = option_ltp
        instrument["market_data"] = market_data
        option_greeks = instrument.get("option_greeks", {})
        if not isinstance(option_greeks, dict):
            option_greeks = {}
        instrument["option_greeks"] = option_greeks
        output.append(instrument)
    return output


def format_suggested_order_instruments(instruments: list) -> str:
    if not isinstance(instruments, list) or not instruments:
        return "Nearest Option-Chain Instruments:\n- not_available"
    decimal_places = max(
        0,
        safe_int(
            getattr(config, "EMA_ISOLATED_ALERT_PRICE_DECIMAL_PLACES", 2), default=2
        ),
    )
    formatted_instruments = []
    for item in instruments:
        if not isinstance(item, dict):
            continue
        option_label = _format_option_label(
            item.get("strike_price"),
            (item.get("option_type") or item.get("instrument_type")),
        )
        market_data = item.get("market_data") or {}
        if not isinstance(market_data, dict):
            market_data = {}
        option_ltp = _safe_float(
            item.get("ltp"), default=_safe_float(market_data.get("ltp"))
        )
        volume = _safe_float(
            item.get("volume"), default=_safe_float(market_data.get("volume"))
        )
        ltp_text = _format_price_value(option_ltp, decimal_places=decimal_places)
        volume_text = _format_volume_value(volume)
        isolated_text = " [ISOLATED]" if item.get("is_isolated_instrument") else ""
        formatted_instruments.append(
            "\n".join(
                [
                    f"- {option_label}{isolated_text}",
                    f"  LTP: {ltp_text}",
                    f"  Volume: {volume_text}",
                ]
            )
        )
    if not formatted_instruments:
        return "Nearest Option-Chain Instruments:\n- not_available"
    return "Nearest Option-Chain Instruments:\n" + "\n\n".join(formatted_instruments)


def format_budget_range_instruments(
    instruments: list, order_option_type: str | None
) -> str:
    decimal_places = max(
        0,
        safe_int(
            getattr(config, "EMA_ISOLATED_ALERT_PRICE_DECIMAL_PLACES", 2), default=2
        ),
    )
    minimum_price = _format_price_value(
        getattr(config, "EMA_ALERT_BUDGET_MIN_PRICE", 50.0),
        decimal_places=decimal_places,
    )
    maximum_price = _format_price_value(
        getattr(config, "EMA_ALERT_BUDGET_MAX_PRICE", 120.0),
        decimal_places=decimal_places,
    )
    normalized_option_type = normalize_option_type(order_option_type)
    option_type_text = normalized_option_type if normalized_option_type else "option"
    heading = (
        f"Budget Range Option-Chain Instruments ({minimum_price} to {maximum_price}):"
    )
    if not isinstance(instruments, list) or not instruments:
        return f"{heading}\n- No matching {option_type_text} instruments"
    formatted_instruments = []
    for item in instruments:
        if not isinstance(item, dict):
            continue
        option_label = _format_option_label(
            item.get("strike_price"),
            (item.get("option_type") or item.get("instrument_type")),
        )
        market_data = item.get("market_data") or {}
        if not isinstance(market_data, dict):
            market_data = {}
        option_ltp = _safe_float(
            item.get("ltp"), default=_safe_float(market_data.get("ltp"))
        )
        ltp_text = _format_price_value(option_ltp, decimal_places=decimal_places)
        isolated_text = " [ISOLATED]" if item.get("is_isolated_instrument") else ""
        formatted_instruments.append(
            "\n".join([f"- {option_label}{isolated_text}", f"  LTP: {ltp_text}"])
        )
    if not formatted_instruments:
        return f"{heading}\n- No matching {option_type_text} instruments"
    return f"{heading}\n" + "\n\n".join(formatted_instruments)


def normalize_ema_cross_direction(ema_event: dict) -> str:
    if not isinstance(ema_event, dict):
        return "unknown"
    cross_type = str(ema_event.get("cross_type", "")).strip().lower()
    current_signal = str(ema_event.get("current_signal", "")).strip().lower()
    bullish_values = {"bullish", "bullish_cross", "buy", "long", "up"}
    bearish_values = {"bearish", "bearish_cross", "sell", "short", "down"}
    if (
        "bullish" in cross_type
        or cross_type in bullish_values
        or current_signal in bullish_values
    ):
        return "bullish"
    if (
        "bearish" in cross_type
        or cross_type in bearish_values
        or current_signal in bearish_values
    ):
        return "bearish"
    return "unknown"


def get_ema_alert_minute_bucket(timestamp_value: Any = None) -> str:
    if timestamp_value is not None:
        parsed = parse_candle_timestamp(timestamp_value)
        if parsed is not None:
            return parsed.strftime("%Y-%m-%dT%H:%M")
    return get_now_market_time().strftime("%Y-%m-%dT%H:%M")


def should_skip_isolated_ema_alert_for_minute_direction(
    instrument_key: str, ema_event: dict, timestamp_value: Any = None
) -> tuple[bool, str, str]:
    normalized_instrument_key = str(instrument_key or "unknown_instrument").strip()
    if not normalized_instrument_key:
        normalized_instrument_key = "unknown_instrument"
    direction = normalize_ema_cross_direction(ema_event)
    minute_bucket = get_ema_alert_minute_bucket(timestamp_value)
    alert_key = f"{normalized_instrument_key}_{minute_bucket}_{direction}"
    alert_date = minute_bucket[:10]
    skip_alert = state.check_and_reserve_ema_minute_key(
        alert_key=alert_key, state_date=alert_date
    )
    return skip_alert, alert_key, direction


def _send_telegram_message(title: str, message: str, level: str) -> bool:
    try:
        return bool(
            telegram_service.send_message(title=title, message=message, level=level)
        )
    except Exception as ex:
        logger.error(
            "Telegram delivery failed. title=%s, level=%s, error=%s",
            title,
            level,
            type(ex).__name__,
        )
        return False


def _dispatch_algo_app_payload(payload: dict) -> bool:
    if not config.ALGO_APP_ENABLED:
        return False
    if not isinstance(payload, dict):
        return False
    try:
        return bool(algo_app_service.dispatch_ema_alert(deepcopy(payload)))
    except Exception as ex:
        logger.error(
            "Algo App dispatch failed. event_id=%s, instrument_key=%s, error=%s",
            payload.get("event_id"),
            (payload.get("instrument") or {}).get("instrument_key"),
            type(ex).__name__,
        )
        return False


def build_isolated_ema_alert_payload(
    ema_event: dict,
    selected_state: dict,
    contract_info: dict,
    isolated_instrument_type: str | None,
    suggested_order_option_type: str | None,
    suggested_instruments: list,
    budget_instruments: list,
    ema_candle: dict,
    minute_alert_key: str | None,
    alert_direction: str,
    simulation: bool = False,
    dry_run: bool = False,
    requested_by: str | None = None,
    delivery_controls: dict | None = None,
) -> dict:
    if not isinstance(ema_event, dict):
        ema_event = {}

    if not isinstance(selected_state, dict):
        selected_state = {}

    if not isinstance(contract_info, dict):
        contract_info = {}

    if not isinstance(suggested_instruments, list):
        suggested_instruments = []

    if not isinstance(budget_instruments, list):
        budget_instruments = []

    if not isinstance(ema_candle, dict):
        ema_candle = {}

    if not isinstance(delivery_controls, dict):
        delivery_controls = {}

    simulation_metadata = ema_event.get("simulation")

    if not isinstance(simulation_metadata, dict):
        simulation_metadata = {}

    now_market = get_now_market_time()

    instrument_key = str(
        ema_event.get("instrument_key")
        or selected_state.get("instrument_key")
        or contract_info.get("instrument_key")
        or ""
    ).strip()

    strike_price = _safe_float(contract_info.get("strike_price"))

    normalized_isolated_type = normalize_option_type(
        isolated_instrument_type
        or contract_info.get("instrument_type")
        or contract_info.get("option_type")
    )

    normalized_order_side = normalize_option_type(suggested_order_option_type)

    selected_level = (
        selected_state.get("selected_level") or selected_state.get("level") or "N/A"
    )

    normalized_ema_candle = normalize_candle(ema_candle)

    if not normalized_ema_candle:
        normalized_ema_candle = extract_ema_candle_details(ema_event)

    event_timestamp = normalized_ema_candle.get(
        "timestamp"
    ) or _format_market_timestamp(ema_event.get("timestamp"))

    normalized_direction = (
        str(alert_direction or normalize_ema_cross_direction(ema_event) or "unknown")
        .strip()
        .lower()
    )

    event_id = (
        f"EMA-{instrument_key.replace('|', '-')}-"
        f"{now_market.strftime('%Y%m%dT%H%M%S')}-"
        f"{normalized_direction}-{uuid4().hex[:8]}"
    )

    nifty_ltp = state.get_latest_main_index_ltp_value()

    isolated_snapshot = (
        state.get_latest_instrument_ltp_snapshot(instrument_key)
        if instrument_key
        else {}
    )

    if not isinstance(isolated_snapshot, dict):
        isolated_snapshot = {}

    isolated_instrument_ltp = _safe_float(
        isolated_snapshot.get("live_ltp"),
        default=_safe_float(
            isolated_snapshot.get("ltp"),
            default=_safe_float(normalized_ema_candle.get("close")),
        ),
    )

    ema_calculation_mode = (
        str(
            ema_event.get(
                "ema_calculation_mode",
                get_live_ema_calculation_mode_text(),
            )
            or get_live_ema_calculation_mode_text()
        )
        .strip()
        .lower()
    )

    normalized_nearest_instruments = []

    for item in suggested_instruments:
        if not isinstance(item, dict):
            continue

        instrument = deepcopy(item)

        item_key = str(instrument.get("instrument_key") or "").strip()

        item_option_type = normalize_option_type(
            instrument.get("instrument_type") or instrument.get("option_type")
        )

        item_candle = normalize_candle(instrument.get("candle"))

        if not item_candle and item_key and item_key == instrument_key:
            item_candle = deepcopy(normalized_ema_candle)

        item_live_ltp = _safe_float(
            instrument.get("live_ltp"),
            default=_safe_float(instrument.get("ltp")),
        )

        if item_live_ltp is None and item_candle:
            item_live_ltp = _safe_float(item_candle.get("close"))

        normalized_nearest_instruments.append(
            {
                "instrument_key": item_key or None,
                "option_type": item_option_type,
                "instrument_type": item_option_type,
                "strike_price": _safe_float(instrument.get("strike_price")),
                "trading_symbol": instrument.get("trading_symbol"),
                "lot_size": safe_int(
                    instrument.get("lot_size"),
                    default=0,
                ),
                "underlying_key": instrument.get("underlying_key"),
                "underlying_spot_price": (
                    _safe_float(instrument.get("underlying_spot_price"))
                ),
                "pcr": _safe_float(instrument.get("pcr")),
                "ltp": item_live_ltp,
                "live_ltp": item_live_ltp,
                "close_price": _safe_float(instrument.get("close_price")),
                "candle": deepcopy(item_candle),
                "option_greeks": deepcopy(instrument.get("option_greeks")),
                "data_source": instrument.get("data_source"),
                "is_isolated_instrument": bool(
                    item_key and instrument_key and item_key == instrument_key
                ),
            }
        )

    normalized_budget_instruments = []

    for item in budget_instruments:
        if not isinstance(item, dict):
            continue

        instrument = deepcopy(item)

        item_key = str(instrument.get("instrument_key") or "").strip()

        item_option_type = normalize_option_type(
            instrument.get("instrument_type") or instrument.get("option_type")
        )

        item_candle = normalize_candle(instrument.get("candle"))

        if not item_candle and item_key and item_key == instrument_key:
            item_candle = deepcopy(normalized_ema_candle)

        item_live_ltp = _safe_float(
            instrument.get("live_ltp"),
            default=_safe_float(instrument.get("ltp")),
        )

        if item_live_ltp is None and item_candle:
            item_live_ltp = _safe_float(item_candle.get("close"))

        normalized_budget_instruments.append(
            {
                "instrument_key": item_key or None,
                "option_type": item_option_type,
                "instrument_type": item_option_type,
                "strike_price": _safe_float(instrument.get("strike_price")),
                "trading_symbol": instrument.get("trading_symbol"),
                "lot_size": safe_int(
                    instrument.get("lot_size"),
                    default=0,
                ),
                "underlying_key": instrument.get("underlying_key"),
                "underlying_spot_price": (
                    _safe_float(instrument.get("underlying_spot_price"))
                ),
                "pcr": _safe_float(instrument.get("pcr")),
                "ltp": item_live_ltp,
                "live_ltp": item_live_ltp,
                "close_price": _safe_float(instrument.get("close_price")),
                "candle": deepcopy(item_candle),
                "option_greeks": deepcopy(instrument.get("option_greeks")),
                "data_source": instrument.get("data_source"),
                "is_isolated_instrument": bool(
                    item_key and instrument_key and item_key == instrument_key
                ),
            }
        )

    normalized_requested_by = str(
        requested_by or simulation_metadata.get("requested_by") or ""
    ).strip()

    payload = {
        "schema_version": getattr(
            config,
            "ALGO_APP_PAYLOAD_SCHEMA_VERSION",
            "1.0",
        ),
        "event_id": (
            event_id
            if bool(
                getattr(
                    config,
                    "ALGO_APP_INCLUDE_EVENT_ID",
                    True,
                )
            )
            else None
        ),
        "event_type": (
            "simulated_isolated_instrument_ema_alert"
            if simulation
            else "isolated_instrument_ema_alert"
        ),
        "source": (
            "ema_alert_simulation"
            if simulation
            else getattr(
                config,
                "ALGO_APP_SOURCE_NAME",
                "option_feed_engine",
            )
        ),
        "market": "NSE",
        "timezone": getattr(
            config,
            "MARKET_TIMEZONE",
            "Asia/Kolkata",
        ),
        "created_at": now_market.isoformat(),
        "is_simulation": bool(simulation),
        "simulation": {
            "enabled": bool(simulation),
            "dry_run": bool(dry_run),
            "requested_by": (normalized_requested_by or None),
            "requested_at": (simulation_metadata.get("requested_at")),
            "live_state_modified": False,
            "selected_state_modified": False,
            "delivery_controls": {
                "send_telegram": bool(
                    delivery_controls.get(
                        "send_telegram",
                        False,
                    )
                ),
                "send_algo_app": bool(
                    delivery_controls.get(
                        "send_algo_app",
                        False,
                    )
                ),
            },
        },
        "instrument": {
            "instrument_key": instrument_key,
            "trading_symbol": contract_info.get("trading_symbol"),
            "underlying_symbol": (contract_info.get("underlying_symbol") or "NIFTY"),
            "underlying_type": contract_info.get("underlying_type"),
            "instrument_type": (normalized_isolated_type),
            "option_type": normalized_isolated_type,
            "strike_price": strike_price,
            "expiry": contract_info.get("expiry"),
            "lot_size": safe_int(
                contract_info.get("lot_size"),
                default=0,
            ),
            "live_ltp": isolated_instrument_ltp,
            "isolated": True,
        },
        "opening_range": {
            "selected_level": selected_level,
            "selected_level_value": _safe_float(
                selected_state.get("level_value")
                or selected_state.get("selected_level_value")
            ),
            "trigger_price": _safe_float(selected_state.get("trigger_price")),
            "trigger_field": selected_state.get("trigger_field"),
            "touch_source": (
                selected_state.get("touch_source")
                or selected_state.get("selection_source")
            ),
            "touch_time": (
                selected_state.get("touch_time") or selected_state.get("selected_at")
            ),
            "selected_at": selected_state.get("selected_at"),
            "selection_priority": (selected_state.get("selection_priority")),
            "selection_reason": selected_state.get("selection_reason"),
            "reference_average": _safe_float(selected_state.get("reference_average")),
            "average_window": deepcopy(selected_state.get("average_window")),
            "range": deepcopy(selected_state.get("range")),
            "levels": deepcopy(selected_state.get("levels")),
        },
        "market_snapshot": {
            "nifty_ltp": nifty_ltp,
            "isolated_instrument_ltp": (isolated_instrument_ltp),
            "snapshot_at": now_market.isoformat(),
        },
        "ema": {
            "cross_type": ema_event.get("cross_type"),
            "calculation_mode": (ema_calculation_mode),
            "previous_signal": ema_event.get("previous_signal"),
            "current_signal": ema_event.get("current_signal"),
            "fast_period": safe_int(
                ema_event.get(
                    "ema_fast_period",
                    ema_event.get("fast_period"),
                ),
                default=0,
            ),
            "slow_period": safe_int(
                ema_event.get(
                    "ema_slow_period",
                    ema_event.get("slow_period"),
                ),
                default=0,
            ),
            "fast_value": _safe_float(
                ema_event.get(
                    "ema_fast",
                    ema_event.get("fast_value"),
                )
            ),
            "slow_value": _safe_float(
                ema_event.get(
                    "ema_slow",
                    ema_event.get("slow_value"),
                )
            ),
            "previous_fast_value": _safe_float(
                ema_event.get(
                    "previous_ema_fast",
                    ema_event.get("previous_fast_value"),
                )
            ),
            "previous_slow_value": _safe_float(
                ema_event.get(
                    "previous_ema_slow",
                    ema_event.get("previous_slow_value"),
                )
            ),
            "price": _safe_float(
                ema_event.get(
                    "close",
                    ema_event.get("ltp"),
                )
            ),
            "source": ema_event.get("source"),
            "timestamp": event_timestamp,
            "candle": {
                "timestamp": (normalized_ema_candle.get("timestamp")),
                "open": _safe_float(normalized_ema_candle.get("open")),
                "high": _safe_float(normalized_ema_candle.get("high")),
                "low": _safe_float(normalized_ema_candle.get("low")),
                "close": _safe_float(normalized_ema_candle.get("close")),
                "volume": _safe_float(normalized_ema_candle.get("volume")),
                "close_minus_low_points": (
                    _safe_float(normalized_ema_candle.get("close_minus_low_points"))
                ),
                "high_minus_low_points": (
                    _safe_float(normalized_ema_candle.get("high_minus_low_points"))
                ),
            },
        },
        "order_suggestion": {
            "rule": ("bullish_same_side_" "bearish_opposite_side"),
            "isolated_instrument_type": (normalized_isolated_type),
            "suggested_order_side": (normalized_order_side),
            "nearest_instruments": (normalized_nearest_instruments),
            "budget_filter": {
                "enabled": bool(
                    getattr(
                        config,
                        "EMA_ALERT_BUDGET_RANGE_ENABLED",
                        True,
                    )
                ),
                "minimum_price": _safe_float(
                    getattr(
                        config,
                        "EMA_ALERT_BUDGET_MIN_PRICE",
                        50.0,
                    )
                ),
                "maximum_price": _safe_float(
                    getattr(
                        config,
                        "EMA_ALERT_BUDGET_MAX_PRICE",
                        120.0,
                    )
                ),
                "maximum_instruments": safe_int(
                    getattr(
                        config,
                        "EMA_ALERT_BUDGET_MAX_INSTRUMENTS",
                        2,
                    ),
                    default=2,
                ),
                "sort_mode": str(
                    getattr(
                        config,
                        "EMA_ALERT_BUDGET_SORT_MODE",
                        "nearest_to_budget_midpoint",
                    )
                )
                .strip()
                .lower(),
                "matched_count": len(normalized_budget_instruments),
                "instruments": (normalized_budget_instruments),
            },
        },
        "duplicate_control": {
            "minute_alert_key": minute_alert_key,
            "direction": normalized_direction,
            "bypassed_for_simulation": bool(simulation),
        },
        "raw_ema_event": deepcopy(ema_event),
    }

    if not bool(
        getattr(
            config,
            "EMA_ALGO_PAYLOAD_INCLUDE_OPENING_RANGE",
            True,
        )
    ):
        payload.pop("opening_range", None)

    if not bool(
        getattr(
            config,
            "EMA_ALGO_PAYLOAD_INCLUDE_EMA_VALUES",
            True,
        )
    ):
        payload["ema"].pop("fast_period", None)
        payload["ema"].pop("slow_period", None)
        payload["ema"].pop("fast_value", None)
        payload["ema"].pop("slow_value", None)
        payload["ema"].pop(
            "previous_fast_value",
            None,
        )
        payload["ema"].pop(
            "previous_slow_value",
            None,
        )

    if not bool(
        getattr(
            config,
            "EMA_ALGO_PAYLOAD_INCLUDE_CANDLE",
            True,
        )
    ):
        payload["ema"].pop("candle", None)

    if not bool(
        getattr(
            config,
            "EMA_ALGO_PAYLOAD_INCLUDE_NEAREST_INSTRUMENTS",
            True,
        )
    ):
        payload["order_suggestion"]["nearest_instruments"] = []

    include_nearest_candles = bool(
        getattr(
            config,
            "EMA_ALGO_PAYLOAD_INCLUDE_NEAREST_INSTRUMENT_CANDLES",
            True,
        )
    )

    if not include_nearest_candles:
        for instrument in payload["order_suggestion"]["nearest_instruments"]:
            if isinstance(instrument, dict):
                instrument.pop("candle", None)

    if not bool(
        getattr(
            config,
            "EMA_ALGO_PAYLOAD_INCLUDE_BUDGET_INSTRUMENTS",
            True,
        )
    ):
        payload["order_suggestion"]["budget_filter"]["instruments"] = []

        payload["order_suggestion"]["budget_filter"]["matched_count"] = 0

    include_budget_candles = bool(
        getattr(
            config,
            "EMA_ALGO_PAYLOAD_INCLUDE_BUDGET_INSTRUMENT_CANDLES",
            True,
        )
    )

    if not include_budget_candles:
        for instrument in payload["order_suggestion"]["budget_filter"]["instruments"]:
            if isinstance(instrument, dict):
                instrument.pop("candle", None)

    if not bool(
        getattr(
            config,
            "EMA_ALGO_PAYLOAD_INCLUDE_RAW_EMA_EVENT",
            True,
        )
    ):
        payload.pop("raw_ema_event", None)

    return payload


def build_isolated_ema_telegram_message(payload: dict) -> str:
    if not isinstance(payload, dict):
        payload = {}
    decimal_places = max(
        0,
        safe_int(
            getattr(config, "EMA_ISOLATED_ALERT_PRICE_DECIMAL_PLACES", 2), default=2
        ),
    )
    instrument = payload.get("instrument") or {}
    opening_range = payload.get("opening_range") or {}
    market_snapshot = payload.get("market_snapshot") or {}
    ema_data = payload.get("ema") or {}
    order_suggestion = payload.get("order_suggestion") or {}
    if not isinstance(instrument, dict):
        instrument = {}
    if not isinstance(opening_range, dict):
        opening_range = {}
    if not isinstance(market_snapshot, dict):
        market_snapshot = {}
    if not isinstance(ema_data, dict):
        ema_data = {}
    if not isinstance(order_suggestion, dict):
        order_suggestion = {}
    candle = ema_data.get("candle") or {}
    if not isinstance(candle, dict):
        candle = {}
    strike = _format_numeric_value(
        instrument.get("strike_price"), unavailable_text="N/A"
    )
    option_type = (
        normalize_option_type(
            instrument.get("instrument_type") or instrument.get("option_type")
        )
        or "N/A"
    )
    selected_level = str(opening_range.get("selected_level") or "N/A").strip().upper()
    nifty_ltp = _format_numeric_value(
        market_snapshot.get("nifty_ltp"),
        unavailable_text="N/A",
        decimal_places=decimal_places,
    )
    cross_type = _format_cross_type_text(ema_data.get("cross_type"))
    raw_signal = ema_data.get("current_signal") or ema_data.get("signal") or "N/A"
    signal = str(raw_signal).strip()
    if not signal or signal.lower() in {
        "n/a",
        "na",
        "none",
        "null",
        "not_available",
        "unknown",
    }:
        signal = "N/A"
    else:
        signal = signal.replace("_", " ").title()
    suggested_order_side = (
        normalize_option_type(order_suggestion.get("suggested_order_side")) or "N/A"
    )
    raw_calculation_mode = str(ema_data.get("calculation_mode") or "").strip()
    if raw_calculation_mode:
        calculation_mode = (
            raw_calculation_mode.replace("_", " ").title().replace("Ltp", "LTP")
        )
    else:
        calculation_mode = "N/A"
    candle_close = _format_price_value(
        candle.get("close"), decimal_places=decimal_places
    )
    candle_low = _format_price_value(candle.get("low"), decimal_places=decimal_places)
    close_minus_low = _safe_float(candle.get("close_minus_low_points"))
    if close_minus_low is None:
        close_value = _safe_float(candle.get("close"))
        low_value = _safe_float(candle.get("low"))
        if close_value is not None and low_value is not None:
            close_minus_low = round(close_value - low_value, decimal_places)
    close_low_movement = _format_numeric_value(
        close_minus_low, unavailable_text="N/A", decimal_places=decimal_places
    )
    candle_time = _format_telegram_timestamp(
        candle.get("timestamp") or ema_data.get("timestamp")
    )
    instrument_key = str(instrument.get("instrument_key") or "not_available").strip()
    message_sections = [
        f"{strike} {option_type} | {selected_level} Cross | NIFTY {nifty_ltp}"
    ]

    if bool(payload.get("is_simulation")):
        simulation_data = payload.get("simulation")

        if not isinstance(simulation_data, dict):
            simulation_data = {}

        simulation_mode = "DRY RUN" if simulation_data.get("dry_run") else "SIMULATION"

        message_sections.insert(
            0,
            f"TEST ALERT | {simulation_mode} | NO LIVE ORDER",
        )

    ema_detail_lines = [
        "EMA Cross Details:",
        f"Cross Type: {cross_type}",
        f"Signal: {signal}",
        f"Isolated Instrument Type: {option_type}",
        f"Suggested Order Side: {suggested_order_side}",
        f"EMA Calculation Mode: {calculation_mode}",
    ]
    if bool(getattr(config, "EMA_ISOLATED_ALERT_INCLUDE_CANDLE_CLOSE", True)):
        ema_detail_lines.append(f"EMA Candle Close: {candle_close}")
    if bool(getattr(config, "EMA_ISOLATED_ALERT_INCLUDE_CANDLE_LOW", True)):
        ema_detail_lines.append(f"EMA Candle Low: {candle_low}")
    if bool(getattr(config, "EMA_ISOLATED_ALERT_INCLUDE_CLOSE_LOW_DIFFERENCE", True)):
        if close_low_movement == "N/A":
            movement_text = "N/A"
        else:
            movement_text = f"{close_low_movement} points"
        ema_detail_lines.append(f"EMA Close-Low Movement: {movement_text}")
    message_sections.append("\n".join(ema_detail_lines))
    if bool(getattr(config, "EMA_ISOLATED_ALERT_INCLUDE_CANDLE_TIME", True)):
        message_sections.append(f"EMA Candle Time:\n{candle_time}")
    message_sections.append(f"Instrument Key:\n{instrument_key}")
    if bool(
        getattr(config, "EMA_ISOLATED_ALERT_INCLUDE_NEAREST_ORDER_INSTRUMENTS", True)
    ):
        nearest_instruments = order_suggestion.get("nearest_instruments") or []
        nearest_text = format_suggested_order_instruments(nearest_instruments)
        if nearest_text:
            message_sections.append(nearest_text)
    if bool(getattr(config, "EMA_ISOLATED_ALERT_INCLUDE_BUDGET_INSTRUMENTS", True)):
        budget_filter = order_suggestion.get("budget_filter") or {}
        if not isinstance(budget_filter, dict):
            budget_filter = {}
        budget_instruments = budget_filter.get("instruments") or []
        budget_text = format_budget_range_instruments(
            budget_instruments, suggested_order_side
        )
        if budget_text:
            message_sections.append(budget_text)
    return "\n\n".join(
        section.strip()
        for section in message_sections
        if isinstance(section, str) and section.strip()
    )


def process_selected_or_ema_cross_alert_detailed(
    ema_event: dict,
    selected_state_override: dict | None = None,
    simulation: bool = False,
    dry_run: bool = False,
    send_telegram: bool | None = None,
    send_algo_app: bool | None = None,
) -> dict:
    logger.info(
        "Processing isolated EMA alert. instrument_key=%s, cross_type=%s, simulation=%s, dry_run=%s",
        ema_event.get("instrument_key") if isinstance(ema_event, dict) else None,
        ema_event.get("cross_type") if isinstance(ema_event, dict) else None,
        simulation,
        dry_run,
    )

    result = {
        "success": False,
        "accepted": False,
        "processed": False,
        "simulation": bool(simulation),
        "dry_run": bool(dry_run),
        "event_id": None,
        "instrument_key": None,
        "cross_type": None,
        "direction": None,
        "message": None,
        "warnings": [],
        "payload": None,
        "telegram_message": None,
        "option_chain_selection": None,
        "delivery": {
            "telegram": {
                "enabled": False,
                "attempted": False,
                "success": False,
                "title": None,
            },
            "algo_app": {
                "enabled": False,
                "attempted": False,
                "dispatched": False,
                "delivery_mode": None,
            },
        },
        "duplicate_control": {
            "enabled": False,
            "checked": False,
            "reserved": False,
            "released": False,
            "minute_alert_key": None,
        },
        "state_changes": {
            "alert_record_appended": False,
            "duplicate_key_reserved": False,
        },
        "skip_reason": None,
        "error": None,
    }

    if not isinstance(ema_event, dict):
        result["skip_reason"] = "invalid_ema_event"
        result["error"] = "ema_event must be a dictionary."
        logger.warning("EMA alert processing skipped. reason=%s", result["skip_reason"])
        return result

    event_key = str(ema_event.get("instrument_key") or "").strip()

    cross_type = str(ema_event.get("cross_type") or "").strip()

    result["instrument_key"] = event_key or None
    result["cross_type"] = cross_type or None

    simulation_metadata = ema_event.get("simulation")

    if not isinstance(simulation_metadata, dict):
        simulation_metadata = {}

    simulation = bool(
        simulation
        or ema_event.get("is_simulation")
        or simulation_metadata.get("enabled")
    )

    dry_run = bool(
        dry_run
        or (
            simulation
            and simulation_metadata.get(
                "dry_run",
                False,
            )
        )
    )

    result["simulation"] = simulation
    result["dry_run"] = dry_run

    if simulation and not bool(
        getattr(
            config,
            "EMA_ALERT_SIMULATION_ENABLED",
            True,
        )
    ):
        result["skip_reason"] = "simulation_disabled"
        result["error"] = "EMA alert simulation is disabled."
        logger.warning("EMA alert processing skipped. reason=%s", result["skip_reason"])
        return result

    if dry_run and not simulation:
        result["skip_reason"] = "invalid_dry_run_mode"
        result["error"] = "dry_run is allowed only for simulations."
        logger.warning("EMA alert processing skipped. reason=%s", result["skip_reason"])
        return result

    telegram_enabled = (
        False
        if dry_run
        else (
            bool(DEFAULT_EMA_ISOLATED_TELEGRAM_ENABLED)
            if send_telegram is None
            else bool(send_telegram)
        )
    )

    algo_enabled = (
        False
        if dry_run
        else (
            bool(
                getattr(
                    config,
                    "ALGO_APP_ENABLED",
                    False,
                )
            )
            if send_algo_app is None
            else bool(send_algo_app)
        )
    )

    result["delivery"]["telegram"]["enabled"] = telegram_enabled
    result["delivery"]["algo_app"]["enabled"] = algo_enabled

    if not simulation and not telegram_enabled and not algo_enabled:
        result["skip_reason"] = "all_delivery_channels_disabled"
        result["message"] = "Telegram and Algo App delivery are disabled."
        logger.warning("EMA alert processing skipped. reason=%s", result["skip_reason"])
        return result

    if selected_state_override is not None:
        if not simulation:
            result["skip_reason"] = "selected_state_override_not_allowed"
            result["error"] = (
                "selected_state_override is allowed only for simulation requests."
            )
            logger.warning(
                "EMA alert processing skipped. reason=%s", result["skip_reason"]
            )
            return result

        if not isinstance(
            selected_state_override,
            dict,
        ):
            result["skip_reason"] = "invalid_selected_state_override"
            result["error"] = "selected_state_override must be a dictionary."
            logger.warning(
                "EMA alert processing skipped. reason=%s", result["skip_reason"]
            )
            return result

        selected_state = deepcopy(selected_state_override)
    else:
        state.ensure_current_market_day()
        selected_state = get_selected_or_instrument_state()

    if not isinstance(selected_state, dict):
        result["skip_reason"] = "invalid_selected_state"
        result["error"] = "Selected instrument state is invalid."
        logger.warning("EMA alert processing skipped. reason=%s", result["skip_reason"])
        return result

    if not selected_state.get("selected"):
        result["skip_reason"] = "isolated_instrument_not_selected"
        result["message"] = "No isolated instrument is currently selected."
        logger.info("EMA alert processing skipped. reason=%s", result["skip_reason"])
        return result

    isolated_key = str(selected_state.get("instrument_key") or "").strip()

    if not isolated_key or not event_key:
        result["skip_reason"] = "instrument_key_unavailable"
        result["error"] = "Selected instrument key or EMA event instrument key is unavailable."
        logger.warning("EMA alert processing skipped. reason=%s", result["skip_reason"])
        return result

    if isolated_key != event_key:
        result["skip_reason"] = "instrument_key_mismatch"
        result["error"] = (
            f"Selected instrument {isolated_key} does not match EMA event instrument {event_key}."
        )
        logger.warning("EMA alert processing skipped. reason=%s", result["skip_reason"])
        return result

    contract_info = selected_state.get("contract_info") or {}

    if not isinstance(contract_info, dict):
        contract_info = {}

    if not contract_info:
        contracts_by_key = options_cache.get(
            "contracts_by_key",
            {},
        )

        if isinstance(contracts_by_key, dict):
            cached_contract = contracts_by_key.get(event_key)

            if isinstance(cached_contract, dict):
                contract_info = deepcopy(cached_contract)

    isolated_instrument_type = get_isolated_instrument_type_from_state(selected_state)

    if not isolated_instrument_type:
        isolated_instrument_type = normalize_option_type(
            contract_info.get("instrument_type") or contract_info.get("option_type")
        )

    if not isolated_instrument_type:
        result["skip_reason"] = "instrument_type_unavailable"
        result["error"] = "Could not resolve the isolated instrument option type."
        logger.warning("EMA alert processing skipped. reason=%s", result["skip_reason"])
        return result

    if not cross_type:
        result["skip_reason"] = "cross_type_unavailable"
        result["error"] = "EMA cross_type is unavailable."
        logger.warning("EMA alert processing skipped. reason=%s", result["skip_reason"])
        return result

    alert_direction = normalize_ema_cross_direction(ema_event)

    result["direction"] = alert_direction

    if alert_direction not in {
        "bullish",
        "bearish",
    }:
        result["skip_reason"] = "direction_unresolved"
        result["error"] = "EMA alert direction could not be resolved."
        logger.warning("EMA alert processing skipped. reason=%s", result["skip_reason"])
        return result

    alert_icon = _get_ema_alert_icon(
        direction=alert_direction,
        cross_type=cross_type,
    )

    telegram_title = f"{alert_icon} Isolated Instrument EMA Alert"

    if simulation:
        telegram_title = f"TEST | {telegram_title}"

    result["delivery"]["telegram"]["title"] = telegram_title

    event_candle = ema_event.get("candle")

    if not isinstance(event_candle, dict):
        event_candle = {}

    event_timestamp = ema_event.get("timestamp") or event_candle.get("timestamp")

    minute_alert_key = None
    duplicate_key_reserved = False

    duplicate_control_enabled = bool(
        DEFAULT_LIVE_EMA_CALCULATION_MODE and not simulation and not dry_run
    )

    result["duplicate_control"]["enabled"] = duplicate_control_enabled

    if duplicate_control_enabled:
        result["duplicate_control"]["checked"] = True

        (
            skip_alert,
            minute_alert_key,
            alert_direction,
        ) = should_skip_isolated_ema_alert_for_minute_direction(
            instrument_key=event_key,
            ema_event=ema_event,
            timestamp_value=event_timestamp,
        )

        result["direction"] = alert_direction
        result["duplicate_control"]["minute_alert_key"] = minute_alert_key

        if skip_alert:
            result["skip_reason"] = "duplicate_alert"
            result["message"] = "A matching EMA alert was already processed for this instrument, minute, and direction."
            logger.info(
                "EMA alert skipped due to duplicate. reason=%s, key=%s",
                result["skip_reason"],
                minute_alert_key,
            )
            return result

        duplicate_key_reserved = bool(minute_alert_key)

        result["duplicate_control"]["reserved"] = duplicate_key_reserved
        result["state_changes"]["duplicate_key_reserved"] = duplicate_key_reserved

        if duplicate_key_reserved:
            logger.debug(
                "EMA duplicate key reserved. key=%s, instrument_key=%s",
                minute_alert_key,
                event_key,
            )

    try:
        ema_candle = extract_ema_candle_details(ema_event)

        if not isinstance(ema_candle, dict):
            ema_candle = {}

        suggested_order_option_type = get_suggested_order_option_type(
            instruments=[],
            cross_type=cross_type,
            isolated_instrument_type=(isolated_instrument_type),
        )

        if not suggested_order_option_type:
            result["skip_reason"] = "order_side_unresolved"
            result["error"] = "Could not resolve the suggested order option type."

            if duplicate_key_reserved and minute_alert_key:
                state.release_ema_minute_key(minute_alert_key)
                result["duplicate_control"]["released"] = True
                logger.debug(
                    "Released duplicate key after failure. key=%s", minute_alert_key
                )

            logger.warning(
                "EMA alert processing skipped. reason=%s, isolated_type=%s",
                result["skip_reason"],
                isolated_instrument_type,
            )
            return result

        option_chain_selection = get_option_chain_instruments_for_ema(
            cross_type=cross_type,
            isolated_instrument_type=(isolated_instrument_type),
        )

        if not isinstance(
            option_chain_selection,
            dict,
        ):
            option_chain_selection = {
                "status": "failed",
                "success": False,
                "nearest_instruments": [],
                "nearest_strikes": [],
                "budget_instruments": [],
                "budget_range": {},
                "error": ("Invalid option-chain selection result."),
            }

        result["option_chain_selection"] = deepcopy(option_chain_selection)

        if not option_chain_selection.get("success"):
            option_chain_error = (
                option_chain_selection.get("error")
                or "Option-chain lookup was not successful."
            )

            result["warnings"].append(str(option_chain_error))
            logger.warning(
                "Option-chain lookup was not fully successful. error=%s, cross_type=%s",
                option_chain_error,
                cross_type,
            )

        suggested_instruments = option_chain_selection.get(
            "nearest_instruments",
            [],
        )

        budget_instruments = option_chain_selection.get(
            "budget_instruments",
            [],
        )

        if not isinstance(suggested_instruments, list):
            suggested_instruments = []

        if not isinstance(budget_instruments, list):
            budget_instruments = []

        enriched_nearest_instruments = enrich_option_chain_instruments(
            instruments=suggested_instruments,
            isolated_instrument_key=event_key,
        )

        enriched_budget_instruments = enrich_option_chain_instruments(
            instruments=budget_instruments,
            isolated_instrument_key=event_key,
        )

        option_chain_spot_price = _safe_float(
            option_chain_selection.get("underlying_spot_price")
        )

        nifty_ltp = option_chain_spot_price

        if nifty_ltp is None:
            nifty_ltp = state.get_latest_main_index_ltp_value()

        payload = build_isolated_ema_alert_payload(
            ema_event=ema_event,
            selected_state=selected_state,
            contract_info=contract_info,
            isolated_instrument_type=(isolated_instrument_type),
            suggested_order_option_type=(suggested_order_option_type),
            suggested_instruments=(enriched_nearest_instruments),
            budget_instruments=(enriched_budget_instruments),
            ema_candle=ema_candle,
            minute_alert_key=minute_alert_key,
            alert_direction=alert_direction,
            simulation=simulation,
            dry_run=dry_run,
            requested_by=simulation_metadata.get("requested_by"),
            delivery_controls={
                "send_telegram": telegram_enabled,
                "send_algo_app": algo_enabled,
            },
        )

        if not isinstance(payload, dict):
            result["skip_reason"] = "invalid_payload"
            result["error"] = "EMA alert payload builder returned an invalid result."

            if duplicate_key_reserved and minute_alert_key:
                state.release_ema_minute_key(minute_alert_key)
                result["duplicate_control"]["released"] = True
                logger.debug(
                    "Released duplicate key after payload build failure. key=%s",
                    minute_alert_key,
                )

            logger.warning(
                "EMA alert processing skipped. reason=%s", result["skip_reason"]
            )
            return result

        order_suggestion = payload.get(
            "order_suggestion",
            {},
        )

        if not isinstance(order_suggestion, dict):
            order_suggestion = {}

        include_nearest_instruments = bool(
            getattr(
                config,
                "EMA_ALGO_PAYLOAD_INCLUDE_NEAREST_INSTRUMENTS",
                True,
            )
        )

        include_nearest_candles = bool(
            getattr(
                config,
                "EMA_ALGO_PAYLOAD_INCLUDE_NEAREST_INSTRUMENT_CANDLES",
                True,
            )
        )

        nearest_payload_instruments = (
            deepcopy(enriched_nearest_instruments)
            if include_nearest_instruments
            else []
        )

        if not include_nearest_candles:
            for instrument in nearest_payload_instruments:
                if isinstance(instrument, dict):
                    instrument.pop("candle", None)

        order_suggestion.update(
            {
                "data_source": (option_chain_selection.get("data_source")),
                "underlying_spot_price": (option_chain_spot_price),
                "expiry_date": (option_chain_selection.get("expiry_date")),
                "expiry_source": (option_chain_selection.get("expiry_source")),
                "nearest_strikes": deepcopy(
                    option_chain_selection.get(
                        "nearest_strikes",
                        [],
                    )
                ),
                "nearest_instruments": (nearest_payload_instruments),
            }
        )

        budget_filter = order_suggestion.get(
            "budget_filter",
            {},
        )

        if not isinstance(budget_filter, dict):
            budget_filter = {}

        option_chain_budget = option_chain_selection.get(
            "budget_range",
            {},
        )

        if not isinstance(option_chain_budget, dict):
            option_chain_budget = {}

        include_budget_instruments = bool(
            getattr(
                config,
                "EMA_ALGO_PAYLOAD_INCLUDE_BUDGET_INSTRUMENTS",
                True,
            )
        )

        include_budget_candles = bool(
            getattr(
                config,
                "EMA_ALGO_PAYLOAD_INCLUDE_BUDGET_INSTRUMENT_CANDLES",
                True,
            )
        )

        budget_payload_instruments = (
            deepcopy(enriched_budget_instruments) if include_budget_instruments else []
        )

        if not include_budget_candles:
            for instrument in budget_payload_instruments:
                if isinstance(instrument, dict):
                    instrument.pop("candle", None)

        budget_filter.update(
            {
                "enabled": option_chain_budget.get(
                    "enabled",
                    bool(
                        getattr(
                            config,
                            "EMA_ALERT_BUDGET_RANGE_ENABLED",
                            True,
                        )
                    ),
                ),
                "minimum_price": (
                    option_chain_budget.get(
                        "minimum_price",
                        getattr(
                            config,
                            "EMA_ALERT_BUDGET_MIN_PRICE",
                            50.0,
                        ),
                    )
                ),
                "maximum_price": (
                    option_chain_budget.get(
                        "maximum_price",
                        getattr(
                            config,
                            "EMA_ALERT_BUDGET_MAX_PRICE",
                            120.0,
                        ),
                    )
                ),
                "maximum_instruments": (
                    option_chain_budget.get(
                        "maximum_instruments",
                        getattr(
                            config,
                            "EMA_ALERT_BUDGET_MAX_INSTRUMENTS",
                            2,
                        ),
                    )
                ),
                "sort_mode": option_chain_budget.get(
                    "sort_mode",
                    getattr(
                        config,
                        "EMA_ALERT_BUDGET_SORT_MODE",
                        "nearest_to_budget_midpoint",
                    ),
                ),
                "range_inclusive": (
                    option_chain_budget.get(
                        "range_inclusive",
                        True,
                    )
                ),
                "matched_count": len(enriched_budget_instruments),
                "included_count": len(budget_payload_instruments),
                "instruments": budget_payload_instruments,
                "data_source": (option_chain_selection.get("data_source")),
            }
        )

        order_suggestion["budget_filter"] = budget_filter
        payload["order_suggestion"] = order_suggestion

        market_snapshot = payload.get(
            "market_snapshot",
            {},
        )

        if not isinstance(market_snapshot, dict):
            market_snapshot = {}

        market_snapshot.update(
            {
                "nifty_ltp": nifty_ltp,
                "underlying_spot_price": (option_chain_spot_price),
                "option_chain_expiry": (option_chain_selection.get("expiry_date")),
                "option_data_source": (option_chain_selection.get("data_source")),
            }
        )

        payload["market_snapshot"] = market_snapshot

        telegram_message = build_isolated_ema_telegram_message(payload)

        result["event_id"] = payload.get("event_id")
        result["payload"] = deepcopy(payload)
        result["telegram_message"] = telegram_message
        result["processed"] = True

        if dry_run:
            result["success"] = True
            result["accepted"] = True
            result["message"] = "EMA alert payload and Telegram preview generated successfully. No delivery was attempted."
            logger.info(
                "EMA alert dry-run completed successfully. event_id=%s, instrument_key=%s",
                result["event_id"],
                event_key,
            )
            return result

        telegram_sent = False
        algo_dispatched = False

        if telegram_enabled:
            result["delivery"]["telegram"]["attempted"] = True

            logger.debug(
                "Attempting Telegram delivery. event_id=%s, title=%s",
                result["event_id"],
                telegram_title,
            )

            telegram_sent = _send_telegram_message(
                title=telegram_title,
                message=telegram_message,
                level="EMA",
            )

            logger.debug(
                "Telegram delivery completed. event_id=%s, success=%s",
                result["event_id"],
                telegram_sent,
            )

        if algo_enabled:
            result["delivery"]["algo_app"]["attempted"] = True

            logger.debug(
                "Attempting Algo App dispatch. event_id=%s",
                result["event_id"],
            )

            algo_dispatched = _dispatch_algo_app_payload(payload)

            logger.debug(
                "Algo App dispatch completed. event_id=%s, dispatched=%s",
                result["event_id"],
                algo_dispatched,
            )

        result["delivery"]["telegram"]["success"] = telegram_sent

        result["delivery"]["algo_app"].update(
            {
                "dispatched": algo_dispatched,
                "delivery_mode": (
                    "background"
                    if bool(
                        getattr(
                            config,
                            "ALGO_APP_SEND_IN_BACKGROUND",
                            True,
                        )
                    )
                    else "synchronous"
                ),
            }
        )

        delivery_accepted = bool(telegram_sent or algo_dispatched)

        result["success"] = delivery_accepted
        result["accepted"] = delivery_accepted

        if not delivery_accepted and duplicate_key_reserved and minute_alert_key:
            state.release_ema_minute_key(minute_alert_key)
            result["duplicate_control"]["released"] = True
            logger.debug(
                "Released duplicate key because no delivery channel accepted. key=%s",
                minute_alert_key,
            )

        alert_record = {
            "type": (
                "simulated_isolated_instrument_ema_alert"
                if simulation
                else "isolated_instrument_ema_alert"
            ),
            "event_id": payload.get("event_id"),
            "instrument_key": event_key,
            "contract_info": deepcopy(contract_info),
            "selected_level": selected_state.get("selected_level"),
            "nifty_ltp": nifty_ltp,
            "isolated_instrument_type": (isolated_instrument_type),
            "suggested_order_option_type": (suggested_order_option_type),
            "minute_alert_key": minute_alert_key,
            "alert_direction": alert_direction,
            "telegram_title": telegram_title,
            "ema_calculation_mode": (payload.get("ema", {}).get("calculation_mode")),
            "ema_event": deepcopy(ema_event),
            "ema_candle": deepcopy(ema_candle),
            "option_chain_selection": deepcopy(option_chain_selection),
            "suggested_order_instruments": deepcopy(enriched_nearest_instruments),
            "budget_range_instruments": deepcopy(enriched_budget_instruments),
            "payload": deepcopy(payload),
            "delivery": deepcopy(result["delivery"]),
            "simulation": bool(simulation),
            "dry_run": bool(dry_run),
            "created_at": (get_now_market_time().isoformat()),
        }

        if delivery_accepted and not simulation:
            state.append_selected_or_ema_alert(alert_record)
            result["state_changes"]["alert_record_appended"] = True
            logger.debug(
                "EMA alert record appended to state. event_id=%s, instrument_key=%s",
                result["event_id"],
                event_key,
            )

        if delivery_accepted:
            result["message"] = "EMA alert delivery was accepted."
            logger.info(
                "EMA alert processed successfully. event_id=%s, instrument_key=%s, direction=%s, telegram=%s, algo_app=%s",
                result["event_id"],
                event_key,
                alert_direction,
                telegram_sent,
                algo_dispatched,
            )
        else:
            result["skip_reason"] = "delivery_not_accepted"
            result["message"] = "EMA alert was not accepted by any delivery channel."
            logger.warning(
                "EMA alert processing completed with no accepted delivery. event_id=%s, instrument_key=%s",
                result["event_id"],
                event_key,
            )

        return result

    except Exception as ex:
        if duplicate_key_reserved and minute_alert_key:
            try:
                state.release_ema_minute_key(minute_alert_key)
                result["duplicate_control"]["released"] = True
                logger.debug(
                    "Released duplicate key after exception. key=%s", minute_alert_key
                )
            except Exception as release_ex:
                logger.error(
                    "Failed releasing EMA duplicate key after exception. key=%s, error=%s",
                    minute_alert_key,
                    type(release_ex).__name__,
                )

        result["success"] = False
        result["accepted"] = False
        result["processed"] = False
        result["skip_reason"] = "processing_exception"
        result["error"] = f"{type(ex).__name__}: {ex}"
        result["message"] = "EMA alert processing failed."

        logger.exception(
            "Isolated EMA processing failed. instrument_key=%s, cross_type=%s, simulation=%s",
            event_key,
            cross_type,
            simulation,
        )

        return result


def process_selected_or_ema_cross_alert(
    ema_event: dict,
) -> bool:
    result = process_selected_or_ema_cross_alert_detailed(
        ema_event=ema_event,
        selected_state_override=None,
        simulation=False,
        dry_run=False,
        send_telegram=None,
        send_algo_app=None,
    )

    return bool(result.get("accepted"))


def _build_default_touch_status() -> dict:
    return {
        "r2_touched": False,
        "s2_touched": False,
        "r3_touched": False,
        "s3_touched": False,
        "r2_touch_time": None,
        "s2_touch_time": None,
        "r3_touch_time": None,
        "s3_touch_time": None,
        "r2_alert_sent": False,
        "s2_alert_sent": False,
        "r3_alert_sent": False,
        "s3_alert_sent": False,
        "first_touch_level": None,
        "first_touch_source": None,
        "first_touch_time": None,
        "events": [],
    }


def _build_empty_opening_range_ema_payload(
    latest_main_index_ltp: float | None = None,
) -> dict:
    return {
        "opening_range": {},
        "touch_status": _build_default_touch_status(),
        "latest_intraday_close": None,
        "latest_main_index_ltp": latest_main_index_ltp,
        "processed_at": None,
        "isolated_instrument": get_selected_or_instrument_state(),
    }


def get_opening_range_levels_for_ema_event(instrument_key: str) -> dict:
    latest_main_index_ltp = state.get_latest_main_index_ltp_value()
    if not DEFAULT_EMA_CROSS_INCLUDE_OPENING_RANGE_LEVELS:
        return _build_empty_opening_range_ema_payload(
            latest_main_index_ltp=latest_main_index_ltp
        )
    if instrument_key is None:
        return _build_empty_opening_range_ema_payload(
            latest_main_index_ltp=latest_main_index_ltp
        )
    normalized_instrument_key = str(instrument_key).strip()
    if not normalized_instrument_key:
        return _build_empty_opening_range_ema_payload(
            latest_main_index_ltp=latest_main_index_ltp
        )
    with state.opening_range_cache_lock:
        cache_data = state.opening_range_cache.get("data", {})
        if not isinstance(cache_data, dict):
            cache_data = {}
        cached_item = cache_data.get(normalized_instrument_key)
        item = deepcopy(cached_item) if isinstance(cached_item, dict) else None
        cached_main_index_ltp = state.opening_range_cache.get("latest_main_index_ltp")
    if cached_main_index_ltp is not None:
        latest_main_index_ltp = cached_main_index_ltp
    if not item:
        return _build_empty_opening_range_ema_payload(
            latest_main_index_ltp=latest_main_index_ltp
        )
    levels = item.get("levels") or {}
    if not isinstance(levels, dict):
        levels = {}
    compact_levels = {
        "r1": levels.get("r1"),
        "s1": levels.get("s1"),
        "r2": levels.get("r2"),
        "s2": levels.get("s2"),
        "r3": levels.get("r3"),
        "s3": levels.get("s3"),
        "sub_resistance": levels.get("sub_resistance"),
        "sub_support": levels.get("sub_support"),
    }
    touch_status = item.get("touch_status")
    if not isinstance(touch_status, dict):
        touch_status = _build_default_touch_status()
    else:
        touch_status = deepcopy(touch_status)
    return {
        "opening_range": compact_levels,
        "touch_status": touch_status,
        "latest_intraday_close": item.get("latest_intraday_close"),
        "latest_main_index_ltp": latest_main_index_ltp,
        "processed_at": item.get("processed_at"),
        "isolated_instrument": get_selected_or_instrument_state(),
    }


__all__ = [
    "is_selected_or_instrument_locked",
    "get_selected_or_instrument_key",
    "get_selected_or_instrument_state",
    "get_selected_or_ema_alerts",
    "get_isolated_instrument_type_from_state",
    "extract_ema_candle_details",
    "get_suggested_order_option_type",
    "get_option_chain_instruments_for_ema",
    "enrich_option_chain_instruments",
    "format_suggested_order_instruments",
    "format_budget_range_instruments",
    "normalize_ema_cross_direction",
    "get_ema_alert_minute_bucket",
    "should_skip_isolated_ema_alert_for_minute_direction",
    "build_isolated_ema_alert_payload",
    "build_isolated_ema_telegram_message",
    "process_selected_or_ema_cross_alert",
    "process_selected_or_ema_cross_alert_detailed",
    "get_opening_range_levels_for_ema_event",
]