from copy import deepcopy
from typing import Any
from uuid import uuid4

from core import config
from core.logger import get_logger
from services.algo_app_service import algo_app_service
from services.option_service import (
    get_nearest_order_instruments_for_ema_cross,
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


def _format_telegram_timestamp(
    timestamp_value: Any,
) -> str:
    parsed_timestamp = parse_candle_timestamp(timestamp_value)

    if parsed_timestamp is None:
        if timestamp_value is None:
            return "not_available"

        text = str(timestamp_value).strip()
        return text if text else "not_available"

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

    return f"{formatted_strike}{option_type_text}"


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


def get_suggested_order_instruments_for_ema(
    cross_type: str,
    isolated_instrument_type: str | None = None,
) -> list:
    nifty_ltp = state.get_latest_main_index_ltp_value()

    if nifty_ltp is None or nifty_ltp <= 0:
        logger.warning(
            "Suggested EMA instruments could not be resolved "
            "because NIFTY LTP is unavailable."
        )
        return []

    normalized_cross_type = str(cross_type or "").strip()

    normalized_isolated_type = normalize_option_type(isolated_instrument_type)

    try:
        instruments = get_nearest_order_instruments_for_ema_cross(
            current_nifty_ltp=nifty_ltp,
            cross_type=normalized_cross_type,
            isolated_instrument_type=(normalized_isolated_type),
        )
    except Exception as ex:
        logger.error(
            "Failed resolving suggested EMA instruments. "
            "nifty_ltp=%s, cross_type=%s, "
            "isolated_type=%s, error=%s: %s",
            nifty_ltp,
            normalized_cross_type,
            normalized_isolated_type,
            type(ex).__name__,
            ex,
        )
        return []

    if not isinstance(instruments, (list, tuple)):
        return []

    output = []

    for item in instruments:
        if not isinstance(item, dict):
            continue

        instrument = dict(item)

        instrument_key = instrument.get("instrument_key")

        live_ltp = None
        live_ltp_updated_at = None

        if instrument_key:
            ltp_snapshot = state.get_latest_instrument_ltp_snapshot(instrument_key)

            live_ltp = ltp_snapshot.get("ltp")
            live_ltp_updated_at = ltp_snapshot.get("updated_at")

        option_type = normalize_option_type(
            instrument.get("instrument_type") or instrument.get("option_type")
        )

        if option_type:
            instrument["instrument_type"] = option_type

        instrument["live_ltp"] = _safe_float(live_ltp)

        instrument["live_ltp_updated_at"] = live_ltp_updated_at

        output.append(instrument)

    maximum_instruments = int(config.EMA_ALERT_MAX_ORDER_INSTRUMENTS)

    return output[:maximum_instruments]


def get_suggested_order_option_type(
    instruments: list,
    cross_type: str,
    isolated_instrument_type: str | None,
) -> str | None:
    if isinstance(instruments, list) and instruments:
        first_instrument = instruments[0]

        if isinstance(first_instrument, dict):
            option_type = normalize_option_type(
                first_instrument.get("instrument_type")
                or first_instrument.get("option_type")
            )

            if option_type:
                return option_type

    normalized_isolated_type = normalize_option_type(isolated_instrument_type)

    normalized_cross_type = str(cross_type or "").strip().lower()

    if normalized_isolated_type:
        if "bullish" in normalized_cross_type:
            return normalized_isolated_type

        if "bearish" in normalized_cross_type:
            return "PE" if normalized_isolated_type == "CE" else "CE"

    if "bullish" in normalized_cross_type:
        return normalize_option_type(config.EMA_ALERT_BULLISH_OPTION_TYPE)

    if "bearish" in normalized_cross_type:
        return normalize_option_type(config.EMA_ALERT_BEARISH_OPTION_TYPE)

    return None


def enrich_nearest_instruments(
    instruments: list,
    isolated_instrument_key: str,
    ema_candle: dict,
) -> list:
    output = []

    for item in instruments:
        if not isinstance(item, dict):
            continue

        instrument = deepcopy(item)

        instrument_key = str(instrument.get("instrument_key") or "").strip()

        is_isolated = bool(instrument_key and instrument_key == isolated_instrument_key)

        instrument["is_isolated_instrument"] = is_isolated

        if is_isolated:
            instrument["ema_candle_close"] = ema_candle.get("close")

            instrument["ema_candle_low"] = ema_candle.get("low")

            instrument["close_minus_low_points"] = ema_candle.get(
                "close_minus_low_points"
            )

        output.append(instrument)

    return output


def format_suggested_order_instruments(
    instruments: list,
) -> str:
    if not isinstance(instruments, list) or not instruments:
        return "Nearest Instrument Details:\n" "- not_available"

    decimal_places = int(config.EMA_ISOLATED_ALERT_PRICE_DECIMAL_PLACES)

    lines = ["Nearest Instrument Details:"]

    for item in instruments:
        if not isinstance(item, dict):
            continue

        option_label = _format_option_label(
            item.get("strike_price"),
            item.get("instrument_type") or item.get("option_type"),
        )

        live_ltp = item.get("live_ltp")

        if live_ltp is None:
            price_text = "ltp_not_available"
        else:
            price_text = f"{_format_numeric_value(
                    live_ltp,
                    decimal_places=decimal_places,
                )}rs"

        is_isolated = bool(item.get("is_isolated_instrument"))

        if is_isolated:
            close_value = _format_numeric_value(
                item.get("ema_candle_close"),
                unavailable_text="N/A",
                decimal_places=decimal_places,
            )

            low_value = _format_numeric_value(
                item.get("ema_candle_low"),
                unavailable_text="N/A",
                decimal_places=decimal_places,
            )

            movement_value = _format_numeric_value(
                item.get("close_minus_low_points"),
                unavailable_text="N/A",
                decimal_places=decimal_places,
            )

            lines.append(
                f"- {option_label} "
                f"(Close: {close_value}, "
                f"Low: {low_value}, "
                f"Move: {movement_value} pts) "
                f"- {price_text}"
            )
        else:
            lines.append(f"- {option_label} - {price_text}")

    if len(lines) == 1:
        lines.append("- not_available")

    return "\n".join(lines)


# ============================================================
# Budget Range Instruments
# ============================================================


def get_budget_range_instruments_for_ema(
    order_option_type: str | None,
    nifty_ltp: float | None,
) -> list:
    if not config.EMA_ALERT_BUDGET_RANGE_ENABLED:
        return []

    normalized_option_type = normalize_option_type(order_option_type)

    if not normalized_option_type:
        return []

    minimum_price = float(config.EMA_ALERT_BUDGET_MIN_PRICE)

    maximum_price = float(config.EMA_ALERT_BUDGET_MAX_PRICE)

    maximum_instruments = int(config.EMA_ALERT_BUDGET_MAX_INSTRUMENTS)

    subscribed_only = bool(config.EMA_ALERT_BUDGET_SUBSCRIBED_ONLY)

    require_live_ltp = bool(config.EMA_ALERT_BUDGET_REQUIRE_LIVE_LTP)

    inclusive = bool(config.EMA_ALERT_BUDGET_RANGE_INCLUSIVE)

    subscribed_keys = set(options_cache.get("subscribed_keys", []))

    contracts = options_cache.get("data", [])

    if not isinstance(contracts, list):
        return []

    budget_midpoint = (minimum_price + maximum_price) / 2

    candidates = []

    for contract in contracts:
        if not isinstance(contract, dict):
            continue

        instrument_key = str(contract.get("instrument_key") or "").strip()

        if not instrument_key:
            continue

        if subscribed_only and instrument_key not in subscribed_keys:
            continue

        contract_option_type = normalize_option_type(
            contract.get("instrument_type") or contract.get("option_type")
        )

        if contract_option_type != normalized_option_type:
            continue

        ltp_snapshot = state.get_latest_instrument_ltp_snapshot(instrument_key)

        live_ltp = _safe_float(ltp_snapshot.get("ltp"))

        if require_live_ltp and live_ltp is None:
            continue

        if live_ltp is None:
            continue

        if inclusive:
            within_range = minimum_price <= live_ltp <= maximum_price
        else:
            within_range = minimum_price < live_ltp < maximum_price

        if not within_range:
            continue

        strike_price = _safe_float(contract.get("strike_price"))

        distance_from_nifty = None

        if strike_price is not None and nifty_ltp is not None:
            distance_from_nifty = abs(strike_price - nifty_ltp)

        candidate = {
            "instrument_key": instrument_key,
            "instrument_type": (contract_option_type),
            "option_type": contract_option_type,
            "strike_price": strike_price,
            "trading_symbol": contract.get("trading_symbol"),
            "expiry": contract.get("expiry"),
            "live_ltp": live_ltp,
            "live_ltp_updated_at": (ltp_snapshot.get("updated_at")),
            "minimum_budget_price": minimum_price,
            "maximum_budget_price": maximum_price,
            "within_budget": True,
            "distance_from_budget_midpoint": abs(live_ltp - budget_midpoint),
            "distance_from_nifty": (distance_from_nifty),
        }

        candidates.append(candidate)

    sort_mode = (
        str(config.EMA_ALERT_BUDGET_SORT_MODE or "nearest_to_budget_midpoint")
        .strip()
        .lower()
    )

    if sort_mode == "nearest_to_nifty":
        candidates.sort(
            key=lambda item: (
                (
                    item.get("distance_from_nifty")
                    if item.get("distance_from_nifty") is not None
                    else float("inf")
                ),
                item.get("distance_from_budget_midpoint"),
                (
                    item.get("strike_price")
                    if item.get("strike_price") is not None
                    else float("inf")
                ),
            )
        )

    elif sort_mode == "price_ascending":
        candidates.sort(
            key=lambda item: (
                (
                    item.get("live_ltp")
                    if item.get("live_ltp") is not None
                    else float("inf")
                ),
                (
                    item.get("strike_price")
                    if item.get("strike_price") is not None
                    else float("inf")
                ),
            )
        )

    elif sort_mode == "price_descending":
        candidates.sort(
            key=lambda item: (
                (
                    -item.get("live_ltp")
                    if item.get("live_ltp") is not None
                    else float("inf")
                ),
                (
                    item.get("strike_price")
                    if item.get("strike_price") is not None
                    else float("inf")
                ),
            )
        )

    else:
        candidates.sort(
            key=lambda item: (
                item.get("distance_from_budget_midpoint"),
                (
                    item.get("distance_from_nifty")
                    if item.get("distance_from_nifty") is not None
                    else float("inf")
                ),
                (
                    item.get("strike_price")
                    if item.get("strike_price") is not None
                    else float("inf")
                ),
            )
        )

    return candidates[:maximum_instruments]


def format_budget_range_instruments(
    instruments: list,
    order_option_type: str | None,
) -> str:
    decimal_places = int(config.EMA_ISOLATED_ALERT_PRICE_DECIMAL_PLACES)

    minimum_price = _format_numeric_value(
        config.EMA_ALERT_BUDGET_MIN_PRICE,
        decimal_places=decimal_places,
    )

    maximum_price = _format_numeric_value(
        config.EMA_ALERT_BUDGET_MAX_PRICE,
        decimal_places=decimal_places,
    )

    normalized_option_type = normalize_option_type(order_option_type)

    option_type_text = normalized_option_type if normalized_option_type else "option"

    lines = [
        (
            "Budget Range Instrument Details "
            f"({minimum_price}rs to "
            f"{maximum_price}rs):"
        )
    ]

    if not isinstance(instruments, list) or not instruments:
        lines.append(f"- No matching {option_type_text} instruments")

        return "\n".join(lines)

    for item in instruments:
        if not isinstance(item, dict):
            continue

        option_label = _format_option_label(
            item.get("strike_price"),
            item.get("instrument_type") or item.get("option_type"),
        )

        live_ltp = _format_numeric_value(
            item.get("live_ltp"),
            unavailable_text="ltp_not_available",
            decimal_places=decimal_places,
        )

        lines.append(f"- {option_label} - {live_ltp}rs")

    if len(lines) == 1:
        lines.append(f"- No matching {option_type_text} instruments")

    return "\n".join(lines)


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
    now_market = get_now_market_time()

    instrument_key = str(ema_event.get("instrument_key") or "").strip()

    strike_price = _safe_float(contract_info.get("strike_price"))

    selected_level = selected_state.get("selected_level") or "N/A"

    event_timestamp = ema_candle.get("timestamp") or _format_market_timestamp(
        ema_event.get("timestamp")
    )

    event_id = (
        f"EMA-{instrument_key.replace('|', '-')}-"
        f"{now_market.strftime('%Y%m%dT%H%M%S')}-"
        f"{alert_direction}-"
        f"{uuid4().hex[:8]}"
    )

    nifty_ltp = state.get_latest_main_index_ltp_value()

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

    payload = {
        "schema_version": (config.ALGO_APP_PAYLOAD_SCHEMA_VERSION),
        "event_id": (event_id if config.ALGO_APP_INCLUDE_EVENT_ID else None),
        "event_type": ("isolated_instrument_ema_alert"),
        "source": config.ALGO_APP_SOURCE_NAME,
        "market": "NSE",
        "timezone": config.MARKET_TIMEZONE,
        "created_at": now_market.isoformat(),
        "instrument": {
            "instrument_key": instrument_key,
            "trading_symbol": (contract_info.get("trading_symbol")),
            "underlying_symbol": (
                contract_info.get(
                    "underlying_symbol",
                    "NIFTY",
                )
            ),
            "instrument_type": (isolated_instrument_type),
            "strike_price": strike_price,
            "expiry": contract_info.get("expiry"),
            "isolated": True,
        },
        "opening_range": {
            "selected_level": selected_level,
            "level_value": (
                selected_state.get("level_value")
                or selected_state.get("selected_level_value")
            ),
            "touch_source": (
                selected_state.get("touch_source")
                or selected_state.get("selection_source")
            ),
            "touch_time": (
                selected_state.get("touch_time") or selected_state.get("selected_at")
            ),
            "selection_reason": (selected_state.get("selection_reason")),
        },
        "market_snapshot": {
            "nifty_ltp": nifty_ltp,
            "isolated_instrument_ltp": (ema_candle.get("close")),
        },
        "ema": {
            "cross_type": (ema_event.get("cross_type")),
            "signal": (ema_event.get("current_signal") or alert_direction),
            "calculation_mode": (ema_calculation_mode),
            "fast_period": safe_int(
                ema_event.get(
                    "ema_fast_period",
                    config.LIVE_EMA_FAST_PERIOD,
                ),
                default=config.LIVE_EMA_FAST_PERIOD,
            ),
            "slow_period": safe_int(
                ema_event.get(
                    "ema_slow_period",
                    config.LIVE_EMA_SLOW_PERIOD,
                ),
                default=config.LIVE_EMA_SLOW_PERIOD,
            ),
            "fast_value": _safe_float(ema_event.get("ema_fast")),
            "slow_value": _safe_float(ema_event.get("ema_slow")),
            "previous_fast_value": _safe_float(ema_event.get("previous_ema_fast")),
            "previous_slow_value": _safe_float(ema_event.get("previous_ema_slow")),
            "previous_signal": (ema_event.get("previous_signal")),
            "current_signal": (ema_event.get("current_signal")),
            "source": ema_event.get("source"),
            "timestamp": event_timestamp,
            "candle": deepcopy(ema_candle),
        },
        "order_suggestion": {
            "rule": ("bullish_same_side_" "bearish_opposite_side"),
            "isolated_instrument_type": (isolated_instrument_type),
            "suggested_order_side": (suggested_order_option_type),
            "nearest_instruments": deepcopy(suggested_instruments),
            "budget_filter": {
                "enabled": (config.EMA_ALERT_BUDGET_RANGE_ENABLED),
                "minimum_price": (config.EMA_ALERT_BUDGET_MIN_PRICE),
                "maximum_price": (config.EMA_ALERT_BUDGET_MAX_PRICE),
                "maximum_instruments": (config.EMA_ALERT_BUDGET_MAX_INSTRUMENTS),
                "sort_mode": (config.EMA_ALERT_BUDGET_SORT_MODE),
                "matched_count": len(budget_instruments),
                "instruments": deepcopy(budget_instruments),
            },
        },
        "duplicate_control": {
            "minute_alert_key": minute_alert_key,
            "direction": alert_direction,
        },
        "raw_ema_event": deepcopy(ema_event),
    }

    if not config.EMA_ALGO_PAYLOAD_INCLUDE_OPENING_RANGE:
        payload.pop("opening_range", None)

    if not config.EMA_ALGO_PAYLOAD_INCLUDE_EMA_VALUES:
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

    if not config.EMA_ALGO_PAYLOAD_INCLUDE_CANDLE:
        payload["ema"].pop("candle", None)

    if not config.EMA_ALGO_PAYLOAD_INCLUDE_NEAREST_INSTRUMENTS:
        payload["order_suggestion"]["nearest_instruments"] = []

    if not config.EMA_ALGO_PAYLOAD_INCLUDE_BUDGET_INSTRUMENTS:
        payload["order_suggestion"]["budget_filter"]["instruments"] = []

        payload["order_suggestion"]["budget_filter"]["matched_count"] = 0

    return payload


# ============================================================
# Telegram Message Formatting
# ============================================================


def build_isolated_ema_telegram_message(
    payload: dict,
) -> str:
    decimal_places = int(config.EMA_ISOLATED_ALERT_PRICE_DECIMAL_PLACES)

    instrument = payload.get("instrument") or {}
    opening_range = payload.get("opening_range") or {}
    market_snapshot = payload.get("market_snapshot") or {}
    ema_data = payload.get("ema") or {}
    order_suggestion = payload.get("order_suggestion") or {}
    candle = ema_data.get("candle") or {}

    strike = _format_numeric_value(
        instrument.get("strike_price"),
        unavailable_text="N/A",
    )

    option_type = normalize_option_type(instrument.get("instrument_type")) or "N/A"

    selected_level = opening_range.get("selected_level") or "N/A"

    nifty_ltp = _format_numeric_value(
        market_snapshot.get("nifty_ltp"),
        unavailable_text="N/A",
        decimal_places=decimal_places,
    )

    cross_type = ema_data.get("cross_type") or "N/A"

    signal = str(ema_data.get("signal") or "N/A").lower()

    suggested_order_side = (
        normalize_option_type(order_suggestion.get("suggested_order_side"))
        or "not_available"
    )

    calculation_mode = ema_data.get("calculation_mode") or "not_available"

    candle_close = _format_numeric_value(
        candle.get("close"),
        unavailable_text="N/A",
        decimal_places=decimal_places,
    )

    candle_low = _format_numeric_value(
        candle.get("low"),
        unavailable_text="N/A",
        decimal_places=decimal_places,
    )

    close_low_movement = _format_numeric_value(
        candle.get("close_minus_low_points"),
        unavailable_text="N/A",
        decimal_places=decimal_places,
    )

    candle_time = (
        candle.get("timestamp") or ema_data.get("timestamp") or "not_available"
    )

    instrument_key = instrument.get("instrument_key") or "not_available"

    nearest_text = format_suggested_order_instruments(
        order_suggestion.get("nearest_instruments") or []
    )

    budget_filter = order_suggestion.get("budget_filter") or {}

    budget_text = format_budget_range_instruments(
        budget_filter.get("instruments") or [],
        suggested_order_side,
    )

    lines = [
        (f"{strike} {option_type} " f"- crosses {selected_level} " f"- At {nifty_ltp}"),
        "",
        "EMA Cross Details:",
        f"Cross Type: {cross_type}",
        f"Signal: {signal}",
        ("Isolated Instrument Type: " f"{option_type}"),
        ("Suggested Order Side: " f"{suggested_order_side}"),
        ("EMA Calculation Mode: " f"{calculation_mode}"),
    ]

    if config.EMA_ISOLATED_ALERT_INCLUDE_CANDLE_CLOSE:
        lines.append(f"EMA Candle Close: {candle_close}")

    if config.EMA_ISOLATED_ALERT_INCLUDE_CANDLE_LOW:
        lines.append(f"EMA Candle Low: {candle_low}")

    if config.EMA_ISOLATED_ALERT_INCLUDE_CLOSE_LOW_DIFFERENCE:
        lines.append("EMA Close-Low Movement: " f"{close_low_movement} points")

    if config.EMA_ISOLATED_ALERT_INCLUDE_CANDLE_TIME:
        lines.extend(
            [
                "",
                f"EMA Candle Time: {candle_time}",
            ]
        )

    lines.extend(
        [
            "",
            f"Instrument Key: {instrument_key}",
        ]
    )

    if config.EMA_ISOLATED_ALERT_INCLUDE_NEAREST_ORDER_INSTRUMENTS:
        lines.extend(
            [
                "",
                nearest_text,
            ]
        )

    if config.EMA_ISOLATED_ALERT_INCLUDE_BUDGET_INSTRUMENTS:
        lines.extend(
            [
                "",
                budget_text,
            ]
        )

    return "\n".join(lines)


# ============================================================
# Isolated EMA Alert Processing
# ============================================================


def process_selected_or_ema_cross_alert(
    ema_event: dict,
) -> bool:
    if not isinstance(ema_event, dict):
        logger.warning("Isolated EMA alert skipped because " "ema_event is invalid.")
        return False

    telegram_enabled = bool(DEFAULT_EMA_ISOLATED_TELEGRAM_ENABLED)

    algo_enabled = bool(config.ALGO_APP_ENABLED)

    if not telegram_enabled and not algo_enabled:
        return False

    state.ensure_current_market_day()

    selected_state = get_selected_or_instrument_state()

    if not selected_state.get("selected"):
        return False

    isolated_key = str(selected_state.get("instrument_key") or "").strip()

    event_key = str(ema_event.get("instrument_key") or "").strip()

    if not isolated_key or not event_key or isolated_key != event_key:
        return False

    contract_info = selected_state.get("contract_info") or {}

    if not isinstance(contract_info, dict):
        contract_info = {}

    isolated_instrument_type = get_isolated_instrument_type_from_state(selected_state)

    cross_type = str(ema_event.get("cross_type") or "").strip()

    event_timestamp = ema_event.get("timestamp")

    minute_alert_key = None

    alert_direction = normalize_ema_cross_direction(ema_event)

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

    ema_candle = extract_ema_candle_details(ema_event)

    suggested_instruments = get_suggested_order_instruments_for_ema(
        cross_type=cross_type,
        isolated_instrument_type=(isolated_instrument_type),
    )

    suggested_order_option_type = get_suggested_order_option_type(
        instruments=suggested_instruments,
        cross_type=cross_type,
        isolated_instrument_type=(isolated_instrument_type),
    )

    enriched_nearest_instruments = enrich_nearest_instruments(
        instruments=suggested_instruments,
        isolated_instrument_key=event_key,
        ema_candle=ema_candle,
    )

    nifty_ltp = state.get_latest_main_index_ltp_value()

    budget_instruments = get_budget_range_instruments_for_ema(
        order_option_type=(suggested_order_option_type),
        nifty_ltp=nifty_ltp,
    )

    payload = build_isolated_ema_alert_payload(
        ema_event=ema_event,
        selected_state=selected_state,
        contract_info=contract_info,
        isolated_instrument_type=(isolated_instrument_type),
        suggested_order_option_type=(suggested_order_option_type),
        suggested_instruments=(enriched_nearest_instruments),
        budget_instruments=(budget_instruments),
        ema_candle=ema_candle,
        minute_alert_key=minute_alert_key,
        alert_direction=alert_direction,
    )

    telegram_message = build_isolated_ema_telegram_message(payload)

    logger.info(
        "Processing isolated EMA alert. "
        "event_id=%s, instrument_key=%s, "
        "cross_type=%s, signal=%s, "
        "isolated_type=%s, order_side=%s, "
        "budget_matches=%s",
        payload.get("event_id"),
        event_key,
        cross_type,
        alert_direction,
        isolated_instrument_type,
        suggested_order_option_type,
        len(budget_instruments),
    )

    telegram_sent = False
    algo_dispatched = False

    if telegram_enabled:
        telegram_sent = _send_telegram_message(
            title="Isolated Instrument EMA Alert",
            message=telegram_message,
            level="EMA",
        )

    if algo_enabled:
        algo_dispatched = _dispatch_algo_app_payload(payload)

    if telegram_enabled and not telegram_sent and minute_alert_key:
        state.release_ema_minute_key(minute_alert_key)

    delivery_record = {
        "telegram": {
            "enabled": telegram_enabled,
            "attempted": telegram_enabled,
            "success": telegram_sent,
        },
        "algo_app": {
            "enabled": algo_enabled,
            "attempted": algo_enabled,
            "dispatched": algo_dispatched,
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
        "ema_calculation_mode": (payload.get("ema", {}).get("calculation_mode")),
        "ema_event": deepcopy(ema_event),
        "ema_candle": deepcopy(ema_candle),
        "suggested_order_instruments": deepcopy(enriched_nearest_instruments),
        "budget_range_instruments": deepcopy(budget_instruments),
        "payload": deepcopy(payload),
        "delivery": delivery_record,
        "created_at": (get_now_market_time().isoformat()),
    }

    delivery_accepted = bool(telegram_sent or algo_dispatched)

    if delivery_accepted:
        with state.selected_or_lock:
            state.selected_or_ema_alerts.append(alert_record)

            current_alert_count = safe_int(
                state.selected_or_instrument_state.get(
                    "ema_alerts_count",
                    0,
                ),
                default=0,
            )

            state.selected_or_instrument_state["ema_alerts_count"] = (
                current_alert_count + 1
            )

            state.selected_or_instrument_state["last_ema_alert"] = alert_record

            selected_state_snapshot = deepcopy(state.selected_or_instrument_state)

            isolated_ema_alert_count = len(state.selected_or_ema_alerts)

        with state.opening_range_cache_lock:
            state.opening_range_cache["isolated_ema_alerts_count"] = (
                isolated_ema_alert_count
            )

            state.opening_range_cache["isolated_instrument"] = selected_state_snapshot

            state.opening_range_cache["isolated_instrument_selected"] = bool(
                selected_state_snapshot.get("selected")
            )

            state.opening_range_cache["isolated_instrument_selected_at"] = (
                selected_state_snapshot.get("selected_at")
            )

            state.opening_range_cache["isolated_instrument_selection_reason"] = (
                selected_state_snapshot.get("selection_reason")
            )

    if not delivery_accepted:
        logger.warning(
            "Isolated EMA alert was not accepted by "
            "Telegram or Algo App. "
            "instrument_key=%s, event_id=%s",
            event_key,
            payload.get("event_id"),
        )

    return delivery_accepted


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

__all__ = [
    "is_selected_or_instrument_locked",
    "get_selected_or_instrument_key",
    "get_selected_or_instrument_state",
    "get_selected_or_ema_alerts",
    "get_isolated_instrument_type_from_state",
    "extract_ema_candle_details",
    "get_suggested_order_instruments_for_ema",
    "get_suggested_order_option_type",
    "enrich_nearest_instruments",
    "format_suggested_order_instruments",
    "get_budget_range_instruments_for_ema",
    "format_budget_range_instruments",
    "normalize_ema_cross_direction",
    "get_ema_alert_minute_bucket",
    "should_skip_isolated_ema_alert_for_minute_direction",
    "build_isolated_ema_alert_payload",
    "build_isolated_ema_telegram_message",
    "process_selected_or_ema_cross_alert",
    "get_opening_range_levels_for_ema_event",
]
