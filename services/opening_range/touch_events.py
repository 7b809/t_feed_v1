"""
Latest instrument LTP storage and legacy touch-alert processing.

This module handles:

1. Latest live LTP storage for subscribed instruments.
2. Sorting Opening Range touch events by distance from NIFTY.
3. Formatting touch events for Telegram.
4. Sending grouped legacy Opening Range touch alerts.
5. Flushing and safely restoring pending touch events.

All mutable runtime data is owned by state.py.
"""

import time
from copy import deepcopy
from typing import Any

from core.logger import get_logger
from services.telegram_service import telegram_service

from . import state as runtime_state
from .candle_utils import (
    normalize_option_type,
    parse_candle_timestamp,
    safe_float,
    safe_int,
)
from .constants import (
    DEFAULT_LEGACY_TOUCH_TELEGRAM_ENABLED,
    DEFAULT_SORT_BY_NEAREST_INDEX,
    DEFAULT_TOUCH_ALERT_BATCH_SECONDS,
    DEFAULT_TOUCH_ALERT_ENABLED,
    DEFAULT_TOUCH_ALERT_MAX_INSTRUMENTS,
)

logger = get_logger(__file__)


# ============================================================
# Formatting Helpers
# ============================================================


def _format_numeric_value(
    value: Any,
    unavailable_text: str = "not_available",
) -> str:
    """
    Formats a numeric value without unnecessary trailing zeros.

    Non-numeric values are returned as strings.
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


def _format_touch_time(
    timestamp_value: Any,
) -> str:
    """
    Formats a touch timestamp for Telegram.

    A valid timestamp is shown as YYYY-MM-DD HH:MM:SS. The original
    value is returned when it cannot be parsed.
    """
    if timestamp_value is None:
        return "not_available"

    parsed_timestamp = parse_candle_timestamp(timestamp_value)

    if parsed_timestamp is not None:
        return parsed_timestamp.strftime("%Y-%m-%d %H:%M:%S")

    text = str(timestamp_value).strip()

    return text if text else "not_available"


# ============================================================
# Latest Instrument LTP Helpers
# ============================================================


def update_latest_ltp_for_instrument(
    instrument_key: str,
    ltp: Any,
    updated_at: str | None = None,
) -> bool:
    """
    Stores the latest live LTP for an instrument.

    The value is later used when displaying suggested order instruments
    in isolated EMA Telegram alerts.

    Returns True when a valid positive LTP is stored.
    """
    normalized_instrument_key = str(instrument_key or "").strip()

    if not normalized_instrument_key:
        return False

    value = safe_float(
        ltp,
        default=0.0,
    )

    if value <= 0:
        return False

    return runtime_state.set_latest_instrument_ltp(
        instrument_key=normalized_instrument_key,
        ltp=value,
        updated_at=updated_at,
    )


def get_latest_ltp_for_instrument(
    instrument_key: str,
) -> float | None:
    """Returns the latest cached live LTP for an instrument."""
    normalized_instrument_key = str(instrument_key or "").strip()

    if not normalized_instrument_key:
        return None

    return runtime_state.get_latest_instrument_ltp(normalized_instrument_key)


# ============================================================
# Legacy Telegram Touch Alert Sorting
# ============================================================


def get_sorted_touch_events_for_alert(
    events: list,
) -> list:
    """
    Sorts touch events for a grouped Telegram alert.

    When nearest-index sorting is enabled, events with the smallest
    strike-to-NIFTY distance are shown first.

    Events without a valid distance are placed last.
    """
    if not isinstance(events, list) or not events:
        return []

    valid_events = [deepcopy(event) for event in events if isinstance(event, dict)]

    if not DEFAULT_SORT_BY_NEAREST_INDEX:
        return valid_events

    def sort_key(event: dict) -> tuple:
        distance = event.get("distance_from_index")

        if distance is None:
            distance_value = float("inf")
        else:
            try:
                distance_value = float(distance)
            except (
                TypeError,
                ValueError,
                OverflowError,
            ):
                distance_value = float("inf")

        touch_time = parse_candle_timestamp(event.get("touch_time"))

        touch_timestamp = (
            touch_time.timestamp() if touch_time is not None else float("inf")
        )

        strike = safe_float(
            (event.get("contract_info") or {}).get("strike_price"),
            default=float("inf"),
        )

        return (
            distance_value,
            touch_timestamp,
            strike,
        )

    return sorted(
        valid_events,
        key=sort_key,
    )


# ============================================================
# Legacy Telegram Touch Alert Formatting
# ============================================================


def format_touch_event_line(
    index: int,
    event: dict,
) -> str:
    """Formats one Opening Range touch event for Telegram."""
    if not isinstance(event, dict):
        return f"{index}. Invalid touch event"

    contract_info = event.get("contract_info") or {}

    if not isinstance(contract_info, dict):
        contract_info = {}

    strike = _format_numeric_value(
        contract_info.get("strike_price"),
        unavailable_text="N/A",
    )

    option_type = normalize_option_type(
        contract_info.get("instrument_type") or contract_info.get("option_type")
    )

    if option_type is None:
        raw_instrument_type = (
            str(
                contract_info.get(
                    "instrument_type",
                    "N/A",
                )
                or "N/A"
            )
            .strip()
            .upper()
        )

        option_type = raw_instrument_type

    symbol = (
        contract_info.get("trading_symbol")
        or contract_info.get("tradingsymbol")
        or contract_info.get("instrument_key")
        or event.get("instrument_key")
        or "not_available"
    )

    level = str(event.get("level") or "N/A").strip().upper()

    level_value = _format_numeric_value(
        event.get("level_value"),
        unavailable_text="N/A",
    )

    trigger_price = _format_numeric_value(
        event.get("trigger_price"),
        unavailable_text="N/A",
    )

    trigger_field = str(event.get("trigger_field") or "price").strip()

    touch_time = _format_touch_time(event.get("touch_time"))

    distance = _format_numeric_value(
        event.get("distance_from_index"),
        unavailable_text="not_available",
    )

    return (
        f"{index}. {strike} {option_type}\n"
        f"   Symbol: {symbol}\n"
        f"   Level: {level}\n"
        f"   Level Value: {level_value}\n"
        f"   Trigger {trigger_field}: {trigger_price}\n"
        f"   Touch Time: {touch_time}\n"
        f"   Distance From Index: {distance}"
    )


# ============================================================
# Legacy Telegram Touch Alert Sending
# ============================================================


def send_touch_events_telegram_alert(
    events: list,
    source: str,
    force: bool = False,
) -> bool:
    """
    Sends a grouped legacy Opening Range touch alert.

    The alert is sent only when:

        Legacy touch Telegram alerts are enabled.
        Opening Range touch alerts are enabled.
        At least one valid event is available.
        The configured batching interval has elapsed, unless forced.

    Returns True only when Telegram confirms successful delivery.
    """
    if not DEFAULT_LEGACY_TOUCH_TELEGRAM_ENABLED:
        return False

    if not DEFAULT_TOUCH_ALERT_ENABLED:
        return False

    if not isinstance(events, list) or not events:
        return False

    valid_events = [event for event in events if isinstance(event, dict)]

    if not valid_events:
        return False

    runtime_state.ensure_current_market_day()

    now_timestamp = time.time()

    with runtime_state.touch_lock:
        last_alert_timestamp = runtime_state.last_touch_alert_sent_at

    if not force and last_alert_timestamp is not None:
        elapsed_seconds = now_timestamp - last_alert_timestamp

        if elapsed_seconds < DEFAULT_TOUCH_ALERT_BATCH_SECONDS:
            logger.debug(
                "Legacy Opening Range touch alert deferred. "
                "elapsed_seconds=%s, batch_seconds=%s",
                round(elapsed_seconds, 4),
                DEFAULT_TOUCH_ALERT_BATCH_SECONDS,
            )
            return False

    sorted_events = get_sorted_touch_events_for_alert(valid_events)

    max_instruments = max(
        1,
        safe_int(
            DEFAULT_TOUCH_ALERT_MAX_INSTRUMENTS,
            default=5,
        ),
    )

    selected_events = sorted_events[:max_instruments]

    if not selected_events:
        return False

    index_ltp = runtime_state.get_latest_main_index_ltp_value()

    formatted_index_ltp = _format_numeric_value(
        index_ltp,
        unavailable_text="not_available",
    )

    normalized_source = str(source or "unknown").strip()

    lines = [
        format_touch_event_line(
            index=index + 1,
            event=event,
        )
        for index, event in enumerate(selected_events)
    ]

    message = (
        f"Source: {normalized_source}\n"
        f"NIFTY LTP: {formatted_index_ltp}\n"
        f"Total Touched Instruments: "
        f"{len(valid_events)}\n"
        f"Alerted Instruments: "
        f"{len(selected_events)}\n\n" + "\n\n".join(lines)
    )

    try:
        sent = bool(
            telegram_service.send_message(
                title="Opening Range Touch Alert",
                message=message,
                level="REFRESH",
            )
        )
    except Exception as ex:
        logger.exception(
            "Failed sending legacy Opening Range touch "
            "Telegram alert. source=%s, events_count=%s, "
            "error=%s: %s",
            normalized_source,
            len(valid_events),
            type(ex).__name__,
            ex,
        )
        return False

    if not sent:
        logger.warning(
            "Legacy Opening Range touch Telegram alert "
            "was not delivered. source=%s, events_count=%s",
            normalized_source,
            len(valid_events),
        )
        return False

    with runtime_state.touch_lock:
        runtime_state.last_touch_alert_sent_at = now_timestamp

    logger.info(
        "Legacy Opening Range touch Telegram alert sent. "
        "source=%s, total_events=%s, alerted_events=%s",
        normalized_source,
        len(valid_events),
        len(selected_events),
    )

    return True


# ============================================================
# Pending Touch Alert Queue
# ============================================================


def _restore_pending_events_to_front(
    events: list,
) -> None:
    """
    Restores events to the front of the pending queue.

    Events are added in reverse order because appendleft() reverses
    insertion order.

    Example:

        Original:
            A, B, C

        Correct restoration:
            appendleft(C)
            appendleft(B)
            appendleft(A)

        Final queue:
            A, B, C
    """
    if not isinstance(events, list) or not events:
        return

    with runtime_state.touch_lock:
        for event in reversed(events):
            runtime_state.pending_touch_events.appendleft(event)


def _update_pending_event_count_in_cache() -> None:
    """Synchronizes the pending-event count into the main cache."""
    with runtime_state.touch_lock:
        pending_events_count = len(runtime_state.pending_touch_events)

    with runtime_state.opening_range_cache_lock:
        runtime_state.opening_range_cache["pending_touch_events_count"] = (
            pending_events_count
        )


def flush_pending_touch_alerts(
    force: bool = False,
    source: str = "live_tick",
) -> bool:
    """
    Flushes pending touch events into one legacy Telegram alert.

    Processing:

        1. Pending events are removed from the queue.
        2. Telegram delivery is attempted.
        3. When delivery fails or batching delays the alert, the events
           are restored to the front of the queue in their original
           order.
        4. Newly queued events remain behind the restored events.

    Returns True only when Telegram delivery succeeds.
    """
    if not DEFAULT_LEGACY_TOUCH_TELEGRAM_ENABLED:
        return False

    if not DEFAULT_TOUCH_ALERT_ENABLED:
        return False

    runtime_state.ensure_current_market_day()

    with runtime_state.touch_lock:
        pending_events = list(runtime_state.pending_touch_events)

        runtime_state.pending_touch_events.clear()

    if not pending_events:
        _update_pending_event_count_in_cache()
        return False

    sent = send_touch_events_telegram_alert(
        events=pending_events,
        source=source,
        force=force,
    )

    if not sent:
        # Preserve the original event order. The old implementation
        # used appendleft() in forward order, which reversed the queue.
        _restore_pending_events_to_front(pending_events)

    _update_pending_event_count_in_cache()

    return sent


# ============================================================
# Public API
# ============================================================


__all__ = [
    # Latest instrument LTP
    "update_latest_ltp_for_instrument",
    "get_latest_ltp_for_instrument",
    # Legacy alert helpers
    "get_sorted_touch_events_for_alert",
    "format_touch_event_line",
    "send_touch_events_telegram_alert",
    "flush_pending_touch_alerts",
]
