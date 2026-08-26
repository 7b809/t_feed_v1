"""
Isolated instrument EMA alert helpers.

Responsibilities:

1. Return the currently isolated Opening Range instrument.
2. Prevent duplicate live EMA Telegram alerts.
3. Resolve suggested order instruments.
4. Send isolated-instrument EMA Telegram alerts.
5. Add Opening Range levels to EMA WebSocket events.

Shared runtime data is accessed through state.py. This module must not
create its own cache, locks, deques, sets, or isolated-instrument state.
"""

from copy import deepcopy
from typing import Any

from core import config
from core.logger import get_logger
from services.option_service import (
    get_nearest_order_instruments_for_ema_cross,
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
# Selected / Isolated Instrument Compatibility Helpers
# ============================================================


def is_selected_or_instrument_locked() -> bool:
    """Returns True when an isolated instrument is selected."""
    with state.selected_or_lock:
        return bool(state.selected_or_instrument_state.get("selected"))


def get_selected_or_instrument_key() -> str | None:
    """Returns the currently isolated instrument key."""
    with state.selected_or_lock:
        instrument_key = state.selected_or_instrument_state.get("instrument_key")

    if instrument_key is None:
        return None

    normalized_key = str(instrument_key).strip()

    return normalized_key if normalized_key else None


def get_selected_or_instrument_state() -> dict:
    """
    Returns a safe copy of the isolated instrument state.

    A deep copy is used so callers cannot accidentally modify nested
    runtime values such as contract_info, levels, or range.
    """
    return state.get_selected_or_state_snapshot()


def get_selected_or_ema_alerts(
    limit: int = 100,
) -> list:
    """Returns the latest isolated instrument EMA alerts."""
    normalized_limit = max(
        1,
        safe_int(limit, default=100),
    )

    return state.get_selected_or_ema_alerts_snapshot(limit=normalized_limit)


# ============================================================
# Isolated Instrument Type Helpers
# ============================================================


def get_isolated_instrument_type_from_state(
    selected_state: dict,
) -> str | None:
    """
    Returns CE or PE from the isolated instrument state.

    Supported source values:
        CE
        CALL
        PE
        PUT
    """
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
# Formatting Helpers
# ============================================================


def _format_numeric_value(
    value: Any,
    unavailable_text: str = "not_available",
) -> str:
    """
    Formats a numeric value without unnecessary trailing zeros.

    Non-numeric values are returned as text.
    """
    if value is None:
        return unavailable_text

    try:
        numeric_value = float(value)
    except (TypeError, ValueError, OverflowError):
        text = str(value).strip()
        return text if text else unavailable_text

    if numeric_value.is_integer():
        return str(int(numeric_value))

    return f"{numeric_value:.4f}".rstrip("0").rstrip(".")


# ============================================================
# Suggested EMA Order Instruments
# ============================================================


def get_suggested_order_instruments_for_ema(
    cross_type: str,
    isolated_instrument_type: str | None = None,
) -> list:
    """
    Returns nearest option instruments around the latest NIFTY LTP.

    Rule:

        bullish_cross:
            Use the same side as the isolated instrument.

        bearish_cross:
            Use the opposite side of the isolated instrument.
    """
    nifty_ltp = state.get_latest_main_index_ltp_value()

    if nifty_ltp is None or nifty_ltp <= 0:
        logger.warning(
            "Suggested EMA order instruments could not be resolved "
            "because the latest NIFTY LTP is not available."
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
            "Failed resolving suggested EMA order instruments. "
            "nifty_ltp=%s, cross_type=%s, "
            "isolated_instrument_type=%s, error=%s: %s",
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

        instrument["live_ltp"] = live_ltp
        instrument["live_ltp_updated_at"] = live_ltp_updated_at

        output.append(instrument)

    return output


def format_suggested_order_instruments(
    instruments: list,
) -> str:
    """Formats suggested instruments for a Telegram message."""
    if not isinstance(instruments, list) or not instruments:
        return "Nearest Instrument Details: not_available"

    lines = ["Nearest Instrument Details:"]

    for item in instruments:
        if not isinstance(item, dict):
            continue

        strike = _format_numeric_value(
            item.get("strike_price"),
            unavailable_text="N/A",
        )

        option_type = normalize_option_type(
            item.get("instrument_type") or item.get("option_type")
        )

        option_type_text = option_type if option_type else "N/A"

        live_ltp = item.get("live_ltp")

        if live_ltp is None:
            price_text = "ltp_not_available"
        else:
            formatted_ltp = _format_numeric_value(live_ltp)
            price_text = f"{formatted_ltp}rs"

        lines.append(f"- {strike}{option_type_text} - {price_text}")

    if len(lines) == 1:
        return "Nearest Instrument Details: not_available"

    return "\n".join(lines)


# ============================================================
# EMA Cross Direction Helpers
# ============================================================


def normalize_ema_cross_direction(
    ema_event: dict,
) -> str:
    """
    Normalizes EMA cross direction for duplicate control.

    Returns:
        bullish
        bearish
        unknown
    """
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
    """
    Returns the minute bucket for EMA Telegram duplicate control.

    Example:

        2026-08-13T09:43:22+05:30

    becomes:

        2026-08-13T09:43

    The current market time is used when the timestamp is unavailable
    or cannot be parsed.
    """
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
    """
    Prevents duplicate Telegram EMA alerts in tick/LTP mode.

    Rule:

        One alert per:
            isolated instrument
            minute
            cross direction

    Examples:

        09:43 bullish -> allowed
        09:43 bullish -> skipped
        09:43 bearish -> allowed
        09:43 bearish -> skipped
        09:44 bullish -> allowed

    The duplicate key is reserved before Telegram delivery. If delivery
    fails, the caller must release the key using:

        state.release_ema_minute_key(alert_key)
    """
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
# Telegram Helper
# ============================================================


def _send_telegram_message(
    title: str,
    message: str,
    level: str,
) -> bool:
    """
    Sends a Telegram message safely.

    Telegram errors are logged and converted to False so they do not
    stop live EMA processing.
    """
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
            "Telegram message delivery failed. " "title=%s, level=%s, error=%s: %s",
            title,
            level,
            type(ex).__name__,
            ex,
        )
        return False


# ============================================================
# Isolated EMA Telegram Alert Processing
# ============================================================


def process_selected_or_ema_cross_alert(
    ema_event: dict,
) -> bool:
    """
    Sends a Telegram EMA alert only for the isolated instrument.

    Order-side rule:

        bullish_cross:
            Use the same option side as the isolated instrument.

        bearish_cross:
            Use the opposite option side.

    The Telegram message is intentionally short. Complete EMA details
    are retained in the in-memory alert record.
    """
    if not DEFAULT_EMA_ISOLATED_TELEGRAM_ENABLED:
        return False

    if not isinstance(ema_event, dict):
        logger.warning("Isolated EMA alert skipped because ema_event is invalid.")
        return False

    # Clear previous trading-day runtime state when necessary.
    state.ensure_current_market_day()

    selected_state = get_selected_or_instrument_state()

    if not selected_state.get("selected"):
        return False

    isolated_key = selected_state.get("instrument_key")
    event_key = ema_event.get("instrument_key")

    if isolated_key is not None:
        isolated_key = str(isolated_key).strip()

    if event_key is not None:
        event_key = str(event_key).strip()

    if not isolated_key or not event_key or isolated_key != event_key:
        return False

    contract_info = selected_state.get("contract_info") or {}

    if not isinstance(contract_info, dict):
        contract_info = {}

    strike = contract_info.get(
        "strike_price",
        "N/A",
    )

    isolated_instrument_type = get_isolated_instrument_type_from_state(selected_state)

    option_type = isolated_instrument_type if isolated_instrument_type else "N/A"

    selected_level = selected_state.get("selected_level") or "N/A"

    nifty_ltp = state.get_latest_main_index_ltp_value()

    cross_type = ema_event.get("cross_type") or "N/A"

    current_signal = ema_event.get("current_signal") or "N/A"

    close_price = ema_event.get("close")
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
                "Skipping duplicate tick EMA Telegram alert for "
                "the same minute and direction. "
                "instrument_key=%s, minute_alert_key=%s, "
                "direction=%s",
                event_key,
                minute_alert_key,
                alert_direction,
            )
            return False

    ema_fast = ema_event.get("ema_fast")
    ema_slow = ema_event.get("ema_slow")

    previous_ema_fast = ema_event.get("previous_ema_fast")

    previous_ema_slow = ema_event.get("previous_ema_slow")

    ema_fast_period = safe_int(
        ema_event.get(
            "ema_fast_period",
            getattr(
                config,
                "LIVE_EMA_FAST_PERIOD",
                9,
            ),
        ),
        default=9,
    )

    ema_slow_period = safe_int(
        ema_event.get(
            "ema_slow_period",
            getattr(
                config,
                "LIVE_EMA_SLOW_PERIOD",
                21,
            ),
        ),
        default=21,
    )

    default_calculation_mode = get_live_ema_calculation_mode_text()

    ema_calculation_mode = (
        str(
            ema_event.get(
                "ema_calculation_mode",
                default_calculation_mode,
            )
            or default_calculation_mode
        )
        .strip()
        .lower()
    )

    source = str(
        ema_event.get(
            "source",
            "live_feed",
        )
        or "live_feed"
    ).strip()

    if ema_calculation_mode == "tick_ltp":
        price_label = "Live Tick LTP"
        mode_description = "Live tick/LTP based EMA cross detection"
    else:
        price_label = "EMA Close"
        mode_description = "Completed candle close based EMA cross detection"

    suggested_instruments = get_suggested_order_instruments_for_ema(
        cross_type=str(cross_type),
        isolated_instrument_type=(isolated_instrument_type),
    )

    suggested_order_option_type = None

    if suggested_instruments:
        first_suggested_instrument = suggested_instruments[0]

        suggested_order_option_type = normalize_option_type(
            first_suggested_instrument.get("instrument_type")
            or first_suggested_instrument.get("option_type")
        )

    suggested_order_type_display = (
        suggested_order_option_type if suggested_order_option_type else "not_available"
    )

    suggested_text = format_suggested_order_instruments(suggested_instruments)

    signal_text = str(current_signal or cross_type or "N/A").strip().upper()

    parsed_event_timestamp = parse_candle_timestamp(event_timestamp)

    if parsed_event_timestamp is not None:
        short_event_time = parsed_event_timestamp.strftime("%H:%M")
    elif event_timestamp:
        short_event_time = str(event_timestamp)
    else:
        short_event_time = "N/A"

    formatted_strike = _format_numeric_value(
        strike,
        unavailable_text="N/A",
    )

    formatted_nifty_ltp = _format_numeric_value(
        nifty_ltp,
        unavailable_text="N/A",
    )

    formatted_close_price = _format_numeric_value(
        close_price,
        unavailable_text="N/A",
    )

    message = (
        f"{formatted_strike} {option_type} | "
        f"{selected_level} | "
        f"NIFTY {formatted_nifty_ltp}\n\n"
        f"Signal: {signal_text}\n"
        f"Order Side: {suggested_order_type_display}\n"
        f"{price_label}: {formatted_close_price}\n"
        f"EMA Time: {short_event_time}\n"
        f"Instrument: {event_key}\n\n"
        f"{suggested_text}"
    )

    alert_record = {
        "type": "isolated_instrument_ema_alert",
        "instrument_key": event_key,
        "contract_info": deepcopy(contract_info),
        "selected_level": selected_level,
        "nifty_ltp": nifty_ltp,
        "isolated_instrument_type": (isolated_instrument_type),
        "suggested_order_option_type": (suggested_order_option_type),
        "order_side_rule": (
            "bullish_cross uses isolated instrument side; "
            "bearish_cross uses opposite side"
        ),
        "minute_alert_key": minute_alert_key,
        "alert_direction": alert_direction,
        "ema_calculation_mode": (ema_calculation_mode),
        "ema_mode_description": mode_description,
        "ema_event": deepcopy(ema_event),
        "suggested_order_instruments": deepcopy(suggested_instruments),
        "debug_details": {
            "cross_type": cross_type,
            "current_signal": current_signal,
            "source": source,
            "close_price": close_price,
            "event_timestamp": event_timestamp,
            "ema_fast_period": ema_fast_period,
            "ema_slow_period": ema_slow_period,
            "ema_fast": ema_fast,
            "ema_slow": ema_slow,
            "previous_ema_fast": previous_ema_fast,
            "previous_ema_slow": previous_ema_slow,
        },
        "created_at": (get_now_market_time().isoformat()),
    }

    logger.info(
        "Processing isolated EMA Telegram alert. "
        "instrument_key=%s, cross_type=%s, signal=%s, "
        "isolated_instrument_type=%s, "
        "suggested_order_option_type=%s",
        event_key,
        cross_type,
        signal_text,
        isolated_instrument_type,
        suggested_order_option_type,
    )

    sent = _send_telegram_message(
        title="Isolated Instrument EMA Alert",
        message=message,
        level="EMA",
    )

    if not sent:
        # Allow a retry during the same minute because the alert was
        # not actually delivered.
        if minute_alert_key:
            state.release_ema_minute_key(minute_alert_key)

        logger.warning(
            "Isolated EMA Telegram alert was not delivered. "
            "instrument_key=%s, minute_alert_key=%s",
            event_key,
            minute_alert_key,
        )

        return False

    with state.selected_or_lock:
        state.selected_or_ema_alerts.append(alert_record)

        current_alert_count = safe_int(
            state.selected_or_instrument_state.get(
                "ema_alerts_count",
                0,
            ),
            default=0,
        )

        state.selected_or_instrument_state["ema_alerts_count"] = current_alert_count + 1

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

    return True


# ============================================================
# Default Opening Range EMA Payload
# ============================================================


def _build_default_touch_status() -> dict:
    """
    Builds an empty touch-status payload.

    This helper is kept inside ema_alerts.py to avoid importing
    live_touch.py, which helps prevent circular imports.
    """
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
    """Builds an empty Opening Range EMA enrichment payload."""
    return {
        "opening_range": {},
        "touch_status": _build_default_touch_status(),
        "latest_intraday_close": None,
        "latest_main_index_ltp": (latest_main_index_ltp),
        "processed_at": None,
        "isolated_instrument": (get_selected_or_instrument_state()),
    }


# ============================================================
# EMA WebSocket Opening Range Enrichment Helper
# ============================================================


def get_opening_range_levels_for_ema_event(
    instrument_key: str,
) -> dict:
    """
    Returns lightweight Opening Range data for an EMA crossover event.

    Returned values:

        opening_range
        touch_status
        latest_intraday_close
        latest_main_index_ltp
        processed_at
        isolated_instrument
    """
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
        "latest_intraday_close": item.get("latest_intraday_close"),
        "latest_main_index_ltp": (latest_main_index_ltp),
        "processed_at": item.get("processed_at"),
        "isolated_instrument": (get_selected_or_instrument_state()),
    }


# ============================================================
# Public API
# ============================================================


__all__ = [
    # Selected instrument compatibility helpers
    "is_selected_or_instrument_locked",
    "get_selected_or_instrument_key",
    "get_selected_or_instrument_state",
    "get_selected_or_ema_alerts",
    # Isolated instrument type
    "get_isolated_instrument_type_from_state",
    # Suggested order instruments
    "get_suggested_order_instruments_for_ema",
    "format_suggested_order_instruments",
    # EMA direction and duplicate control
    "normalize_ema_cross_direction",
    "get_ema_alert_minute_bucket",
    "should_skip_isolated_ema_alert_for_minute_direction",
    # Telegram EMA alert
    "process_selected_or_ema_cross_alert",
    # EMA WebSocket enrichment
    "get_opening_range_levels_for_ema_event",
]
