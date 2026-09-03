from copy import deepcopy
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from core import config
from core.logger import get_logger
from services.algo_app_service import algo_app_service
from services.option_service import (
    normalize_candle,
    options_cache,
)
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


# ============================================================
# Isolated Instrument State
# ============================================================


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


def get_selected_or_ema_alerts(
    limit: int = 100,
) -> list:
    normalized_limit = max(
        1,
        safe_int(limit, default=100),
    )

    return state.get_selected_or_ema_alerts_snapshot(limit=normalized_limit)


def get_isolated_instrument_type_from_state(
    selected_state: dict,
) -> str | None:
    if not isinstance(selected_state, dict):
        return None

    contract_info = selected_state.get("contract_info") or {}

    if not isinstance(contract_info, dict):
        return None

    instrument_type = contract_info.get("instrument_type") or contract_info.get(
        "option_type"
    )

    return normalize_option_type(instrument_type)


# ============================================================
# Value Formatting
# ============================================================


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


def _safe_float(
    value: Any,
    default: float | None = None,
) -> float | None:
    try:
        if value is None:
            return default

        return float(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _format_price_value(
    value: Any,
    unavailable_text: str = "N/A",
    decimal_places: int | None = None,
) -> str:
    if value is None:
        return unavailable_text

    if decimal_places is None:
        decimal_places = safe_int(
            getattr(
                config,
                "EMA_ISOLATED_ALERT_PRICE_DECIMAL_PLACES",
                2,
            ),
            default=2,
        )

    decimal_places = max(
        0,
        decimal_places,
    )

    try:
        numeric_value = float(value)
    except (
        TypeError,
        ValueError,
        OverflowError,
    ):
        return unavailable_text

    return f"₹{numeric_value:.{decimal_places}f}"


def _format_volume_value(
    value: Any,
    unavailable_text: str = "N/A",
) -> str:
    if value is None:
        return unavailable_text

    try:
        numeric_value = int(float(value))
    except (
        TypeError,
        ValueError,
        OverflowError,
    ):
        return unavailable_text

    if numeric_value < 0:
        return unavailable_text

    return f"{numeric_value:,}"


def _format_cross_type_text(
    value: Any,
    unavailable_text: str = "N/A",
) -> str:
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

    return normalized_value.replace(
        "_",
        " ",
    ).title()


def _get_ema_alert_icon(
    direction: Any = None,
    cross_type: Any = None,
) -> str:
    normalized_direction = str(direction or "").strip().lower()

    normalized_cross_type = str(cross_type or "").strip().lower()

    combined_value = f"{normalized_direction} " f"{normalized_cross_type}"

    if "bullish" in combined_value:
        return "📈"

    if "bearish" in combined_value:
        return "📉"

    return "📊"


def _format_telegram_timestamp(
    timestamp_value: Any,
    unavailable_text: str = "N/A",
) -> str:
    parsed_timestamp = parse_candle_timestamp(timestamp_value)

    if parsed_timestamp is None:
        if timestamp_value is None:
            return unavailable_text

        timestamp_text = str(timestamp_value).strip()

        return timestamp_text if timestamp_text else unavailable_text

    market_timezone_name = str(
        getattr(
            config,
            "MARKET_TIMEZONE",
            "Asia/Kolkata",
        )
        or "Asia/Kolkata"
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


def _format_market_timestamp(
    timestamp_value: Any,
) -> str | None:
    parsed_timestamp = parse_candle_timestamp(timestamp_value)

    if parsed_timestamp is None:
        if timestamp_value is None:
            return None

        text = str(timestamp_value).strip()
        return text if text else None

    return parsed_timestamp.isoformat()


def _format_option_label(
    strike: Any,
    option_type: Any,
) -> str:
    formatted_strike = _format_numeric_value(
        strike,
        unavailable_text="N/A",
    )

    normalized_option_type = normalize_option_type(option_type)

    option_type_text = normalized_option_type if normalized_option_type else "N/A"

    return f"{formatted_strike} {option_type_text}"


# ============================================================
# EMA Candle Data
# ============================================================


def extract_ema_candle_details(
    ema_event: dict,
) -> dict:
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
            ema_event.get("close"),
            default=_safe_float(ema_event.get("ltp")),
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
        close_minus_low = round(
            close_price - low_price,
            4,
        )

    return {
        "timestamp": _format_market_timestamp(candle_timestamp),
        "open": open_price,
        "high": high_price,
        "low": low_price,
        "close": close_price,
        "volume": volume,
        "close_minus_low_points": close_minus_low,
    }


# ============================================================
# Suggested Order Instruments
# ============================================================

# ============================================================
# Suggested Order Side
# ============================================================


def get_suggested_order_option_type(
    instruments: list | None,
    cross_type: str,
    isolated_instrument_type: str | None,
) -> str | None:
    """
    Resolves the option side to use for an EMA alert.

    Rule:

        bullish cross:
            same option type as isolated instrument

        bearish cross:
            opposite option type from isolated instrument
    """

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
            getattr(
                config,
                "EMA_ALERT_BULLISH_OPTION_TYPE",
                None,
            )
        )

    if "bearish" in normalized_cross_type:
        return normalize_option_type(
            getattr(
                config,
                "EMA_ALERT_BEARISH_OPTION_TYPE",
                None,
            )
        )

    return None


# ============================================================
# Option Chain Alert Instruments
# ============================================================


def get_option_chain_instruments_for_ema(
    *,
    cross_type: str,
    isolated_instrument_type: str | None,
):

    from services.main_index_ltp_service import (
        get_nearest_option_instruments,
    )

    suggested_order_option_type = get_suggested_order_option_type(
        instruments=[],
        cross_type=cross_type,
        isolated_instrument_type=(isolated_instrument_type),
    )

    if not suggested_order_option_type:
        error_message = (
            "Could not resolve suggested option type " "for isolated EMA alert."
        )

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
        safe_int(
            getattr(
                config,
                "MAIN_INDEX_NEAREST_INSTRUMENTS_COUNT",
                3,
            ),
            default=3,
        ),
    )

    try:
        option_chain_result = get_nearest_option_instruments(
            option_type=(suggested_order_option_type),
            count=requested_count,
        )

    except Exception as ex:
        error_message = f"{type(ex).__name__}: {ex}"

        logger.exception(
            "EMA option-chain instrument lookup failed. "
            "cross_type=%s, isolated_type=%s, "
            "order_side=%s, error=%s",
            cross_type,
            isolated_instrument_type,
            suggested_order_option_type,
            error_message,
        )

        return {
            "status": "failed",
            "success": False,
            "suggested_order_option_type": (suggested_order_option_type),
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

    if not isinstance(
        option_chain_result,
        dict,
    ):
        error_message = "Option-chain service returned an invalid response."

        return {
            "status": "failed",
            "success": False,
            "suggested_order_option_type": (suggested_order_option_type),
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

    nearest = option_chain_result.get(
        "nearest",
        {},
    )

    if not isinstance(nearest, dict):
        nearest = {}

    budget_range = option_chain_result.get(
        "budget_range",
        {},
    )

    if not isinstance(budget_range, dict):
        budget_range = {}

    nearest_instruments = nearest.get(
        "instruments",
        [],
    )

    if not isinstance(
        nearest_instruments,
        list,
    ):
        nearest_instruments = []

    budget_instruments = budget_range.get(
        "instruments",
        [],
    )

    if not isinstance(
        budget_instruments,
        list,
    ):
        budget_instruments = []

    nearest_strikes = nearest.get(
        "strikes",
        [],
    )

    if not isinstance(nearest_strikes, list):
        nearest_strikes = []

    success = bool(option_chain_result.get("success"))

    logger.info(
        "EMA option-chain instruments resolved. "
        "success=%s, cross_type=%s, "
        "isolated_type=%s, order_side=%s, "
        "spot_price=%s, nearest_count=%s, "
        "budget_count=%s, expiry=%s",
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
        "status": option_chain_result.get(
            "status",
            "failed",
        ),
        "success": success,
        "suggested_order_option_type": (suggested_order_option_type),
        "underlying_spot_price": (option_chain_result.get("underlying_spot_price")),
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
    *,
    instruments: list,
    isolated_instrument_key: str,
) -> list:
    """
    Adds isolated-instrument metadata without removing option-chain
    market data or Greeks.
    """

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

        market_data = instrument.get(
            "market_data",
            {},
        )

        if not isinstance(market_data, dict):
            market_data = {}

        if option_ltp is None:
            option_ltp = _safe_float(market_data.get("ltp"))

        instrument["ltp"] = option_ltp
        instrument["live_ltp"] = option_ltp
        instrument["market_data"] = market_data

        option_greeks = instrument.get(
            "option_greeks",
            {},
        )

        if not isinstance(option_greeks, dict):
            option_greeks = {}

        instrument["option_greeks"] = option_greeks

        output.append(instrument)

    return output


def format_suggested_order_instruments(
    instruments: list,
) -> str:
    """
    Formats nearest option-chain instruments for Telegram.

    Displays the option label, LTP, and volume.
    """
    if not isinstance(instruments, list) or not instruments:
        return "Nearest Option-Chain Instruments:\n" "- not_available"

    decimal_places = max(
        0,
        safe_int(
            getattr(
                config,
                "EMA_ISOLATED_ALERT_PRICE_DECIMAL_PLACES",
                2,
            ),
            default=2,
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
            item.get("ltp"),
            default=_safe_float(market_data.get("ltp")),
        )

        volume = _safe_float(
            item.get("volume"),
            default=_safe_float(market_data.get("volume")),
        )

        ltp_text = _format_price_value(
            option_ltp,
            decimal_places=decimal_places,
        )

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
        return "Nearest Option-Chain Instruments:\n" "- not_available"

    return "Nearest Option-Chain Instruments:\n" + "\n\n".join(formatted_instruments)


# ============================================================
# Budget Range Instruments
# ============================================================
def format_budget_range_instruments(
    instruments: list,
    order_option_type: str | None,
) -> str:
    """
    Formats budget-range option-chain instruments for Telegram.

    Displays the option label and LTP.
    """
    decimal_places = max(
        0,
        safe_int(
            getattr(
                config,
                "EMA_ISOLATED_ALERT_PRICE_DECIMAL_PLACES",
                2,
            ),
            default=2,
        ),
    )

    minimum_price = _format_price_value(
        getattr(
            config,
            "EMA_ALERT_BUDGET_MIN_PRICE",
            50.0,
        ),
        decimal_places=decimal_places,
    )

    maximum_price = _format_price_value(
        getattr(
            config,
            "EMA_ALERT_BUDGET_MAX_PRICE",
            120.0,
        ),
        decimal_places=decimal_places,
    )
    normalized_option_type = normalize_option_type(order_option_type)

    option_type_text = normalized_option_type if normalized_option_type else "option"

    heading = (
        "Budget Range Option-Chain Instruments "
        f"({minimum_price} to {maximum_price}):"
    )

    if not isinstance(instruments, list) or not instruments:
        return f"{heading}\n" f"- No matching {option_type_text} instruments"

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
            item.get("ltp"),
            default=_safe_float(market_data.get("ltp")),
        )

        ltp_text = _format_price_value(
            option_ltp,
            decimal_places=decimal_places,
        )

        isolated_text = " [ISOLATED]" if item.get("is_isolated_instrument") else ""

        formatted_instruments.append(
            "\n".join(
                [
                    f"- {option_label}{isolated_text}",
                    f"  LTP: {ltp_text}",
                ]
            )
        )

    if not formatted_instruments:
        return f"{heading}\n" f"- No matching {option_type_text} instruments"

    return f"{heading}\n" + "\n\n".join(formatted_instruments)


# ============================================================
# EMA Direction and Duplicate Control
# ============================================================


def normalize_ema_cross_direction(
    ema_event: dict,
) -> str:
    if not isinstance(ema_event, dict):
        return "unknown"

    cross_type = str(ema_event.get("cross_type", "")).strip().lower()

    current_signal = str(ema_event.get("current_signal", "")).strip().lower()

    bullish_values = {
        "bullish",
        "bullish_cross",
        "buy",
        "long",
        "up",
    }

    bearish_values = {
        "bearish",
        "bearish_cross",
        "sell",
        "short",
        "down",
    }

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


def get_ema_alert_minute_bucket(
    timestamp_value: Any = None,
) -> str:
    if timestamp_value is not None:
        parsed = parse_candle_timestamp(timestamp_value)

        if parsed is not None:
            return parsed.strftime("%Y-%m-%dT%H:%M")

    return get_now_market_time().strftime("%Y-%m-%dT%H:%M")


def should_skip_isolated_ema_alert_for_minute_direction(
    instrument_key: str,
    ema_event: dict,
    timestamp_value: Any = None,
) -> tuple[bool, str, str]:
    normalized_instrument_key = str(instrument_key or "unknown_instrument").strip()

    if not normalized_instrument_key:
        normalized_instrument_key = "unknown_instrument"

    direction = normalize_ema_cross_direction(ema_event)

    minute_bucket = get_ema_alert_minute_bucket(timestamp_value)

    alert_key = f"{normalized_instrument_key}_" f"{minute_bucket}_" f"{direction}"

    alert_date = minute_bucket[:10]

    skip_alert = state.check_and_reserve_ema_minute_key(
        alert_key=alert_key,
        state_date=alert_date,
    )

    return skip_alert, alert_key, direction


# ============================================================
# Telegram Delivery
# ============================================================


def _send_telegram_message(
    title: str,
    message: str,
    level: str,
) -> bool:
    try:
        return bool(
            telegram_service.send_message(
                title=title,
                message=message,
                level=level,
            )
        )
    except Exception as ex:
        logger.error(
            "Telegram delivery failed. " "title=%s, level=%s, error=%s: %s",
            title,
            level,
            type(ex).__name__,
            ex,
        )

        return False


# ============================================================
# Algo App Delivery
# ============================================================


def _dispatch_algo_app_payload(
    payload: dict,
) -> bool:
    if not config.ALGO_APP_ENABLED:
        return False

    if not isinstance(payload, dict):
        return False

    try:
        return bool(algo_app_service.dispatch_ema_alert(deepcopy(payload)))
    except Exception as ex:
        logger.error(
            "Algo App dispatch failed. "
            "event_id=%s, instrument_key=%s, "
            "error=%s: %s",
            payload.get("event_id"),
            (payload.get("instrument") or {}).get("instrument_key"),
            type(ex).__name__,
            ex,
        )

        return False


# ============================================================
# Canonical EMA Alert Payload
# ============================================================
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
        f"EMA-"
        f"{instrument_key.replace('|', '-')}-"
        f"{now_market.strftime('%Y%m%dT%H%M%S')}-"
        f"{normalized_direction}-"
        f"{uuid4().hex[:8]}"
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

        item_is_isolated = bool(
            item_key and instrument_key and item_key == instrument_key
        )

        normalized_instrument = {
            "instrument_key": (item_key or None),
            "option_type": item_option_type,
            "strike_price": _safe_float(instrument.get("strike_price")),
            "trading_symbol": instrument.get("trading_symbol"),
            "lot_size": safe_int(
                instrument.get("lot_size"),
                default=0,
            ),
            "underlying_key": instrument.get("underlying_key"),
            "underlying_spot_price": _safe_float(
                instrument.get("underlying_spot_price")
            ),
            "pcr": _safe_float(instrument.get("pcr")),
            "ltp": item_live_ltp,
            "live_ltp": item_live_ltp,
            "close_price": _safe_float(instrument.get("close_price")),
            "option_greeks": deepcopy(instrument.get("option_greeks")),
            "data_source": instrument.get("data_source"),
            "is_isolated_instrument": (item_is_isolated),
        }
        normalized_nearest_instruments.append(normalized_instrument)

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

        item_is_isolated = bool(
            item_key and instrument_key and item_key == instrument_key
        )

        normalized_instrument = {
            "instrument_key": (item_key or None),
            "option_type": item_option_type,
            "strike_price": _safe_float(instrument.get("strike_price")),
            "trading_symbol": instrument.get("trading_symbol"),
            "lot_size": safe_int(
                instrument.get("lot_size"),
                default=0,
            ),
            "underlying_key": instrument.get("underlying_key"),
            "underlying_spot_price": _safe_float(
                instrument.get("underlying_spot_price")
            ),
            "pcr": _safe_float(instrument.get("pcr")),
            "ltp": item_live_ltp,
            "live_ltp": item_live_ltp,
            "close_price": _safe_float(instrument.get("close_price")),
            "option_greeks": deepcopy(instrument.get("option_greeks")),
            "data_source": instrument.get("data_source"),
            "is_isolated_instrument": (item_is_isolated),
        }
        normalized_budget_instruments.append(normalized_instrument)

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
        "event_type": ("isolated_instrument_ema_alert"),
        "source": getattr(
            config,
            "ALGO_APP_SOURCE_NAME",
            "option_feed_engine",
        ),
        "market": "NSE",
        "timezone": getattr(
            config,
            "MARKET_TIMEZONE",
            "Asia/Kolkata",
        ),
        "created_at": now_market.isoformat(),
        "instrument": {
            "instrument_key": instrument_key,
            "trading_symbol": contract_info.get("trading_symbol"),
            "underlying_symbol": (contract_info.get("underlying_symbol") or "NIFTY"),
            "underlying_type": contract_info.get("underlying_type"),
            "instrument_type": (normalized_isolated_type),
            "option_type": (normalized_isolated_type),
            "strike_price": strike_price,
            "expiry": contract_info.get("expiry"),
            "lot_size": safe_int(
                contract_info.get("lot_size"),
                default=0,
            ),
            "live_ltp": (isolated_instrument_ltp),
            "isolated": True,
        },
        "opening_range": {
            "selected_level": selected_level,
            "selected_level_value": _safe_float(
                selected_state.get("level_value")
                or selected_state.get("selected_level_value")
            ),
            "trigger_price": _safe_float(selected_state.get("trigger_price")),
            "trigger_field": (selected_state.get("trigger_field")),
            "touch_source": (
                selected_state.get("touch_source")
                or selected_state.get("selection_source")
            ),
            "touch_time": (
                selected_state.get("touch_time") or selected_state.get("selected_at")
            ),
            "selected_at": (selected_state.get("selected_at")),
            "selection_priority": (selected_state.get("selection_priority")),
            "selection_reason": (selected_state.get("selection_reason")),
            "reference_average": _safe_float(selected_state.get("reference_average")),
            "average_window": deepcopy(selected_state.get("average_window")),
            "range": deepcopy(selected_state.get("range")),
            "levels": deepcopy(selected_state.get("levels")),
        },
        "market_snapshot": {
            "nifty_ltp": nifty_ltp,
            "isolated_instrument_ltp": (isolated_instrument_ltp),
            "snapshot_at": (now_market.isoformat()),
        },
        "ema": {
            "cross_type": ema_event.get("cross_type"),
            "calculation_mode": ema_calculation_mode,
            "previous_signal": ema_event.get("previous_signal"),
            "current_signal": ema_event.get("current_signal"),
            "price": _safe_float(
                ema_event.get(
                    "close",
                    ema_event.get("ltp"),
                )
            ),
            "source": ema_event.get("source"),
            "timestamp": event_timestamp,
            "candle": {
                "timestamp": normalized_ema_candle.get("timestamp"),
                "open": _safe_float(normalized_ema_candle.get("open")),
                "high": _safe_float(normalized_ema_candle.get("high")),
                "low": _safe_float(normalized_ema_candle.get("low")),
                "close": _safe_float(normalized_ema_candle.get("close")),
                "volume": _safe_float(normalized_ema_candle.get("volume")),
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
            "minute_alert_key": (minute_alert_key),
            "direction": normalized_direction,
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
        payload.pop(
            "opening_range",
            None,
        )

    if not bool(
        getattr(
            config,
            "EMA_ALGO_PAYLOAD_INCLUDE_EMA_VALUES",
            True,
        )
    ):
        payload["ema"].pop(
            "fast_period",
            None,
        )
        payload["ema"].pop(
            "slow_period",
            None,
        )
        payload["ema"].pop(
            "fast_value",
            None,
        )
        payload["ema"].pop(
            "slow_value",
            None,
        )
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
        payload["ema"].pop(
            "candle",
            None,
        )

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
                instrument.pop(
                    "candle",
                    None,
                )

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
                instrument.pop(
                    "candle",
                    None,
                )

    if not bool(
        getattr(
            config,
            "EMA_ALGO_PAYLOAD_INCLUDE_RAW_EMA_EVENT",
            True,
        )
    ):
        payload.pop(
            "raw_ema_event",
            None,
        )

    return payload


# ============================================================
# Telegram Message Formatting
# ============================================================
def build_isolated_ema_telegram_message(
    payload: dict,
) -> str:
    if not isinstance(payload, dict):
        payload = {}

    decimal_places = max(
        0,
        safe_int(
            getattr(
                config,
                "EMA_ISOLATED_ALERT_PRICE_DECIMAL_PLACES",
                2,
            ),
            default=2,
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
        instrument.get("strike_price"),
        unavailable_text="N/A",
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
        signal = signal.replace(
            "_",
            " ",
        ).title()

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
        candle.get("close"),
        decimal_places=decimal_places,
    )

    candle_low = _format_price_value(
        candle.get("low"),
        decimal_places=decimal_places,
    )

    close_minus_low = _safe_float(candle.get("close_minus_low_points"))

    if close_minus_low is None:
        close_value = _safe_float(candle.get("close"))

        low_value = _safe_float(candle.get("low"))

        if close_value is not None and low_value is not None:
            close_minus_low = round(
                close_value - low_value,
                decimal_places,
            )

    close_low_movement = _format_numeric_value(
        close_minus_low,
        unavailable_text="N/A",
        decimal_places=decimal_places,
    )

    candle_time = _format_telegram_timestamp(
        candle.get("timestamp") or ema_data.get("timestamp")
    )

    instrument_key = str(instrument.get("instrument_key") or "not_available").strip()

    message_sections = [
        f"{strike} {option_type} " f"| {selected_level} Cross " f"| NIFTY {nifty_ltp}"
    ]

    ema_detail_lines = [
        "EMA Cross Details:",
        f"Cross Type: {cross_type}",
        f"Signal: {signal}",
        f"Isolated Instrument Type: {option_type}",
        f"Suggested Order Side: {suggested_order_side}",
        f"EMA Calculation Mode: {calculation_mode}",
    ]

    if bool(
        getattr(
            config,
            "EMA_ISOLATED_ALERT_INCLUDE_CANDLE_CLOSE",
            True,
        )
    ):
        ema_detail_lines.append(f"EMA Candle Close: {candle_close}")

    if bool(
        getattr(
            config,
            "EMA_ISOLATED_ALERT_INCLUDE_CANDLE_LOW",
            True,
        )
    ):
        ema_detail_lines.append(f"EMA Candle Low: {candle_low}")

    if bool(
        getattr(
            config,
            "EMA_ISOLATED_ALERT_INCLUDE_CLOSE_LOW_DIFFERENCE",
            True,
        )
    ):
        if close_low_movement == "N/A":
            movement_text = "N/A"
        else:
            movement_text = f"{close_low_movement} points"

        ema_detail_lines.append("EMA Close-Low Movement: " f"{movement_text}")

    message_sections.append("\n".join(ema_detail_lines))

    if bool(
        getattr(
            config,
            "EMA_ISOLATED_ALERT_INCLUDE_CANDLE_TIME",
            True,
        )
    ):
        message_sections.append("EMA Candle Time:\n" f"{candle_time}")

    message_sections.append("Instrument Key:\n" f"{instrument_key}")

    if bool(
        getattr(
            config,
            "EMA_ISOLATED_ALERT_INCLUDE_NEAREST_ORDER_INSTRUMENTS",
            True,
        )
    ):
        nearest_instruments = order_suggestion.get("nearest_instruments") or []

        nearest_text = format_suggested_order_instruments(nearest_instruments)

        if nearest_text:
            message_sections.append(nearest_text)

    if bool(
        getattr(
            config,
            "EMA_ISOLATED_ALERT_INCLUDE_BUDGET_INSTRUMENTS",
            True,
        )
    ):
        budget_filter = order_suggestion.get("budget_filter") or {}

        if not isinstance(budget_filter, dict):
            budget_filter = {}

        budget_instruments = budget_filter.get("instruments") or []

        budget_text = format_budget_range_instruments(
            budget_instruments,
            suggested_order_side,
        )

        if budget_text:
            message_sections.append(budget_text)

    return "\n\n".join(
        section.strip()
        for section in message_sections
        if isinstance(section, str) and section.strip()
    )


# ============================================================
# Isolated EMA Alert Processing
# ============================================================
def process_selected_or_ema_cross_alert(
    ema_event: dict,
) -> bool:
    """
    Processes an accepted isolated-instrument EMA cross.

    One option-chain request is made per accepted EMA alert.
    The result is reused for Telegram and Algo App delivery.
    """
    if not isinstance(ema_event, dict):
        logger.warning("Isolated EMA alert skipped because " "ema_event is invalid.")
        return False

    telegram_enabled = bool(DEFAULT_EMA_ISOLATED_TELEGRAM_ENABLED)

    algo_enabled = bool(
        getattr(
            config,
            "ALGO_APP_ENABLED",
            False,
        )
    )

    if not telegram_enabled and not algo_enabled:
        return False

    state.ensure_current_market_day()

    selected_state = get_selected_or_instrument_state()

    if not isinstance(selected_state, dict):
        logger.warning(
            "Isolated EMA alert skipped because "
            "selected instrument state is invalid."
        )
        return False

    if not selected_state.get("selected"):
        return False

    isolated_key = str(selected_state.get("instrument_key") or "").strip()

    event_key = str(ema_event.get("instrument_key") or "").strip()

    if not isolated_key or not event_key:
        logger.warning(
            "Isolated EMA alert skipped because "
            "instrument key is unavailable. "
            "isolated_key=%s, event_key=%s",
            isolated_key or None,
            event_key or None,
        )
        return False

    if isolated_key != event_key:
        return False

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

    cross_type = str(ema_event.get("cross_type") or "").strip()

    if not cross_type:
        logger.warning(
            "Isolated EMA alert skipped because "
            "cross_type is unavailable. "
            "instrument_key=%s",
            event_key,
        )
        return False

    alert_direction = normalize_ema_cross_direction(ema_event)

    if alert_direction not in {
        "bullish",
        "bearish",
    }:
        logger.warning(
            "Isolated EMA alert skipped because "
            "direction could not be resolved. "
            "instrument_key=%s, cross_type=%s, "
            "current_signal=%s",
            event_key,
            cross_type,
            ema_event.get("current_signal"),
        )
        return False

    alert_icon = _get_ema_alert_icon(
        direction=alert_direction,
        cross_type=cross_type,
    )

    telegram_title = f"{alert_icon} Isolated Instrument EMA Alert"

    event_candle = ema_event.get("candle")

    if not isinstance(event_candle, dict):
        event_candle = {}

    event_timestamp = ema_event.get("timestamp") or event_candle.get("timestamp")

    minute_alert_key = None
    duplicate_key_reserved = False

    if DEFAULT_LIVE_EMA_CALCULATION_MODE:
        (
            skip_alert,
            minute_alert_key,
            alert_direction,
        ) = should_skip_isolated_ema_alert_for_minute_direction(
            instrument_key=event_key,
            ema_event=ema_event,
            timestamp_value=event_timestamp,
        )

        if skip_alert:
            logger.info(
                "Skipping duplicate isolated EMA alert. "
                "instrument_key=%s, key=%s, "
                "direction=%s",
                event_key,
                minute_alert_key,
                alert_direction,
            )
            return False

        duplicate_key_reserved = bool(minute_alert_key)

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
            logger.error(
                "Isolated EMA order side could not be "
                "resolved. instrument_key=%s, "
                "cross_type=%s, isolated_type=%s",
                event_key,
                cross_type,
                isolated_instrument_type,
            )

            if duplicate_key_reserved and minute_alert_key:
                state.release_ema_minute_key(minute_alert_key)

            return False

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

        suggested_instruments = option_chain_selection.get(
            "nearest_instruments",
            [],
        )

        budget_instruments = option_chain_selection.get(
            "budget_instruments",
            [],
        )

        if not isinstance(
            suggested_instruments,
            list,
        ):
            suggested_instruments = []

        if not isinstance(
            budget_instruments,
            list,
        ):
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
        )

        if not isinstance(payload, dict):
            logger.error(
                "Isolated EMA payload builder returned "
                "an invalid result. instrument_key=%s",
                event_key,
            )

            if duplicate_key_reserved and minute_alert_key:
                state.release_ema_minute_key(minute_alert_key)

            return False

        order_suggestion = payload.get(
            "order_suggestion",
            {},
        )

        if not isinstance(order_suggestion, dict):
            order_suggestion = {}

        order_suggestion["data_source"] = option_chain_selection.get("data_source")

        order_suggestion["underlying_spot_price"] = option_chain_spot_price

        order_suggestion["expiry_date"] = option_chain_selection.get("expiry_date")

        order_suggestion["expiry_source"] = option_chain_selection.get("expiry_source")

        order_suggestion["nearest_strikes"] = deepcopy(
            option_chain_selection.get(
                "nearest_strikes",
                [],
            )
        )

        order_suggestion["nearest_instruments"] = deepcopy(enriched_nearest_instruments)

        budget_filter_payload = order_suggestion.get(
            "budget_filter",
            {},
        )

        if not isinstance(
            budget_filter_payload,
            dict,
        ):
            budget_filter_payload = {}

        option_chain_budget = option_chain_selection.get(
            "budget_range",
            {},
        )

        if not isinstance(
            option_chain_budget,
            dict,
        ):
            option_chain_budget = {}

        budget_filter_payload.update(
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
                "sort_mode": (
                    option_chain_budget.get(
                        "sort_mode",
                        getattr(
                            config,
                            "EMA_ALERT_BUDGET_SORT_MODE",
                            "nearest_to_budget_midpoint",
                        ),
                    )
                ),
                "range_inclusive": (
                    option_chain_budget.get(
                        "range_inclusive",
                        True,
                    )
                ),
                "matched_count": len(enriched_budget_instruments),
                "instruments": deepcopy(enriched_budget_instruments),
                "data_source": (option_chain_selection.get("data_source")),
            }
        )

        order_suggestion["budget_filter"] = budget_filter_payload

        payload["order_suggestion"] = order_suggestion

        market_snapshot = payload.get(
            "market_snapshot",
            {},
        )

        if not isinstance(market_snapshot, dict):
            market_snapshot = {}

        market_snapshot["nifty_ltp"] = nifty_ltp

        market_snapshot["underlying_spot_price"] = option_chain_spot_price

        market_snapshot["option_chain_expiry"] = option_chain_selection.get(
            "expiry_date"
        )

        market_snapshot["option_data_source"] = option_chain_selection.get(
            "data_source"
        )

        payload["market_snapshot"] = market_snapshot

        telegram_message = build_isolated_ema_telegram_message(payload)

        logger.info(
            "Processing isolated EMA alert. "
            "event_id=%s, instrument_key=%s, "
            "cross_type=%s, direction=%s, "
            "isolated_type=%s, order_side=%s, "
            "option_chain_success=%s, "
            "nearest_instruments=%s, "
            "budget_matches=%s",
            payload.get("event_id"),
            event_key,
            cross_type,
            alert_direction,
            isolated_instrument_type,
            suggested_order_option_type,
            option_chain_selection.get("success"),
            len(enriched_nearest_instruments),
            len(enriched_budget_instruments),
        )

        telegram_sent = False
        algo_dispatched = False

        if telegram_enabled:
            telegram_sent = _send_telegram_message(
                title=telegram_title,
                message=telegram_message,
                level="EMA",
            )

        if algo_enabled:
            algo_dispatched = _dispatch_algo_app_payload(payload)

        delivery_accepted = bool(telegram_sent or algo_dispatched)

        if not delivery_accepted and duplicate_key_reserved and minute_alert_key:
            state.release_ema_minute_key(minute_alert_key)

        delivery_record = {
            "telegram": {
                "enabled": telegram_enabled,
                "attempted": telegram_enabled,
                "success": telegram_sent,
                "title": telegram_title,
            },
            "algo_app": {
                "enabled": algo_enabled,
                "attempted": algo_enabled,
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
            },
        }

        alert_record = {
            "type": ("isolated_instrument_ema_alert"),
            "event_id": payload.get("event_id"),
            "instrument_key": event_key,
            "contract_info": deepcopy(contract_info),
            "selected_level": (selected_state.get("selected_level")),
            "nifty_ltp": nifty_ltp,
            "isolated_instrument_type": (isolated_instrument_type),
            "suggested_order_option_type": (suggested_order_option_type),
            "minute_alert_key": minute_alert_key,
            "alert_direction": alert_direction,
            "telegram_title": telegram_title,
            "ema_calculation_mode": (
                payload.get(
                    "ema",
                    {},
                ).get("calculation_mode")
            ),
            "ema_event": deepcopy(ema_event),
            "ema_candle": deepcopy(ema_candle),
            "option_chain_selection": deepcopy(option_chain_selection),
            "suggested_order_instruments": (deepcopy(enriched_nearest_instruments)),
            "budget_range_instruments": (deepcopy(enriched_budget_instruments)),
            "payload": deepcopy(payload),
            "delivery": delivery_record,
            "created_at": (get_now_market_time().isoformat()),
        }

        if delivery_accepted:
            state.append_selected_or_ema_alert(alert_record)

            logger.info(
                "Isolated EMA alert accepted. "
                "event_id=%s, instrument_key=%s, "
                "telegram_sent=%s, "
                "algo_dispatched=%s",
                payload.get("event_id"),
                event_key,
                telegram_sent,
                algo_dispatched,
            )
        else:
            logger.warning(
                "Isolated EMA alert was not accepted "
                "by Telegram or Algo App. "
                "instrument_key=%s, event_id=%s",
                event_key,
                payload.get("event_id"),
            )

        return delivery_accepted

    except Exception as ex:
        if duplicate_key_reserved and minute_alert_key:
            try:
                state.release_ema_minute_key(minute_alert_key)
            except Exception as release_ex:
                logger.error(
                    "Failed releasing isolated EMA "
                    "duplicate key after processing failure. "
                    "key=%s, error=%s: %s",
                    minute_alert_key,
                    type(release_ex).__name__,
                    release_ex,
                )

        logger.exception(
            "Isolated EMA alert processing failed. "
            "instrument_key=%s, cross_type=%s, "
            "error=%s: %s",
            event_key,
            cross_type,
            type(ex).__name__,
            ex,
        )

        return False


# ============================================================
# Opening Range EMA Payload
# ============================================================


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
        "touch_status": (_build_default_touch_status()),
        "latest_intraday_close": None,
        "latest_main_index_ltp": (latest_main_index_ltp),
        "processed_at": None,
        "isolated_instrument": (get_selected_or_instrument_state()),
    }


def get_opening_range_levels_for_ema_event(
    instrument_key: str,
) -> dict:
    latest_main_index_ltp = state.get_latest_main_index_ltp_value()

    if not DEFAULT_EMA_CROSS_INCLUDE_OPENING_RANGE_LEVELS:
        return _build_empty_opening_range_ema_payload(
            latest_main_index_ltp=(latest_main_index_ltp)
        )

    if instrument_key is None:
        return _build_empty_opening_range_ema_payload(
            latest_main_index_ltp=(latest_main_index_ltp)
        )

    normalized_instrument_key = str(instrument_key).strip()

    if not normalized_instrument_key:
        return _build_empty_opening_range_ema_payload(
            latest_main_index_ltp=(latest_main_index_ltp)
        )

    with state.opening_range_cache_lock:
        cache_data = state.opening_range_cache.get(
            "data",
            {},
        )

        if not isinstance(cache_data, dict):
            cache_data = {}

        cached_item = cache_data.get(normalized_instrument_key)

        item = deepcopy(cached_item) if isinstance(cached_item, dict) else None

        cached_main_index_ltp = state.opening_range_cache.get("latest_main_index_ltp")

    if cached_main_index_ltp is not None:
        latest_main_index_ltp = cached_main_index_ltp

    if not item:
        return _build_empty_opening_range_ema_payload(
            latest_main_index_ltp=(latest_main_index_ltp)
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
        "latest_intraday_close": (item.get("latest_intraday_close")),
        "latest_main_index_ltp": (latest_main_index_ltp),
        "processed_at": item.get("processed_at"),
        "isolated_instrument": (get_selected_or_instrument_state()),
    }


# ============================================================
# Public API
# ============================================================
# ============================================================
# Public API
# ============================================================


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
    "get_opening_range_levels_for_ema_event",
]
