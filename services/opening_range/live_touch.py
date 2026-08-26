"""
Opening Range touch detection and live-tick processing.

This module handles:

1. Creation of Opening Range touch events.
2. Detection of R2, R3, S2, and S3 touches.
3. Backfill scanning using intraday candles.
4. Live Upstox tick extraction.
5. Main-index and instrument LTP updates.
6. Touch-status updates in the shared cache.
7. Isolated-instrument selection after eligible live touches.

All shared runtime data is owned by state.py.
"""

from copy import deepcopy
from typing import Any

from core.logger import get_logger

from . import state as runtime_state
from .candle_utils import (
    get_contract_info_by_key,
    get_now_market_time,
    is_option_contract,
    parse_candle_timestamp,
    safe_float,
    safe_int,
    select_post_opening_range_candles,
    serialize_candle,
)
from .constants import (
    DEFAULT_BACKFILL_SCAN_ENABLED,
    DEFAULT_BACKFILL_TOUCH_ALERT_ENABLED,
    DEFAULT_ISOLATION_TOUCH_LEVELS,
    DEFAULT_LEGACY_TOUCH_TELEGRAM_ENABLED,
    DEFAULT_LIVE_TOUCH_ALERT_ENABLED,
    DEFAULT_MAIN_INDEX_KEY,
    DEFAULT_TOUCH_ALERT_ENABLED,
    DEFAULT_TOUCH_ALERT_ONCE_PER_LEVEL,
    DEFAULT_TOUCH_ALERT_OPTIONS_ONLY,
    DEFAULT_TOUCH_CHECK_MODE,
)

logger = get_logger(__file__)


# ============================================================
# Touch Event Helpers
# ============================================================


def build_alert_key(
    instrument_key: str,
    level: str,
) -> str:
    """
    Builds the daily duplicate-control key for a touch event.

    Daily state is cleared by state.ensure_current_market_day(), so the
    date does not need to be included directly in this key.
    """
    normalized_instrument_key = str(instrument_key or "").strip()

    normalized_level = str(level or "").strip().upper()

    return f"{normalized_instrument_key}_" f"{normalized_level}"


def calculate_distance_from_index(
    strike_price: Any,
    index_ltp: Any,
) -> float | None:
    """
    Calculates the absolute distance between an option strike and the
    latest NIFTY index LTP.
    """
    if strike_price is None or index_ltp is None:
        return None

    try:
        strike_value = float(strike_price)
        index_value = float(index_ltp)
    except (TypeError, ValueError, OverflowError):
        return None

    if strike_value <= 0 or index_value <= 0:
        return None

    return round(
        abs(strike_value - index_value),
        4,
    )


def update_latest_main_index_ltp(
    ltp: Any,
    source: str = "unknown",
    updated_at: str | None = None,
) -> bool:
    """
    Updates the latest main-index LTP.

    The value is stored in both the centralized runtime state and the
    Opening Range cache.

    Returns True when a valid positive LTP is stored.
    """
    return runtime_state.set_latest_main_index_ltp(
        ltp=ltp,
        source=source,
        updated_at=updated_at,
    )


def get_latest_main_index_ltp() -> float | None:
    """Returns the latest main-index LTP."""
    return runtime_state.get_latest_main_index_ltp_value()


def get_default_touch_status() -> dict:
    """Returns a new default Opening Range touch-status dictionary."""
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


def create_touch_event(
    instrument_key: str,
    level: str,
    level_value: float,
    trigger_price: float,
    trigger_field: str,
    touch_time: str,
    source: str,
    contract_info: dict,
    candle: dict | None = None,
) -> dict:
    """
    Creates a normalized R2, R3, S2, or S3 touch event.
    """
    normalized_instrument_key = str(instrument_key or "").strip()

    normalized_level = str(level or "").strip().upper()

    normalized_contract_info = (
        deepcopy(contract_info) if isinstance(contract_info, dict) else {}
    )

    parsed_touch_time = parse_candle_timestamp(touch_time)

    if parsed_touch_time is not None:
        normalized_touch_time = parsed_touch_time.isoformat()
        event_date = parsed_touch_time.date().isoformat()
    else:
        now_market = get_now_market_time()
        normalized_touch_time = (
            str(touch_time).strip() if touch_time else now_market.isoformat()
        )
        event_date = now_market.date().isoformat()

    index_ltp = get_latest_main_index_ltp()

    strike_price = normalized_contract_info.get("strike_price")

    distance_from_index = calculate_distance_from_index(
        strike_price=strike_price,
        index_ltp=index_ltp,
    )

    return {
        "type": "opening_range_touch",
        "instrument_key": normalized_instrument_key,
        "level": normalized_level,
        "level_value": round(
            safe_float(level_value),
            4,
        ),
        "trigger_price": round(
            safe_float(trigger_price),
            4,
        ),
        "trigger_field": str(trigger_field or "unknown").strip(),
        "touch_time": normalized_touch_time,
        "source": str(source or "unknown").strip(),
        "date": event_date,
        "main_index_ltp": index_ltp,
        "distance_from_index": distance_from_index,
        "alert_key": build_alert_key(
            normalized_instrument_key,
            normalized_level,
        ),
        "contract_info": normalized_contract_info,
        "candle": (serialize_candle(candle) if isinstance(candle, dict) else None),
        "created_at": get_now_market_time().isoformat(),
    }


def should_skip_touch_alert(
    instrument_key: str,
    level: str,
    contract_info: dict | None = None,
) -> bool:
    """
    Checks whether a touch event should be skipped.

    This function controls duplicate touch-event detection. It does not
    indicate whether a Telegram message was successfully delivered.
    """
    if not DEFAULT_TOUCH_ALERT_ENABLED:
        return True

    normalized_instrument_key = str(instrument_key or "").strip()

    if not normalized_instrument_key:
        return True

    if DEFAULT_TOUCH_ALERT_OPTIONS_ONLY and not is_option_contract(contract_info):
        return True

    normalized_level = str(level or "").strip().upper()

    if normalized_level not in DEFAULT_ISOLATION_TOUCH_LEVELS:
        return True

    runtime_state.ensure_current_market_day()

    alert_key = build_alert_key(
        normalized_instrument_key,
        normalized_level,
    )

    with runtime_state.touch_lock:
        if (
            DEFAULT_TOUCH_ALERT_ONCE_PER_LEVEL
            and alert_key in runtime_state.alert_sent_keys
        ):
            return True

    return False


def mark_touch_alert_sent(
    event: dict,
) -> bool:
    """
    Marks a touch key as already tracked.

    The function name is retained for backward compatibility. The key
    represents a tracked touch, not necessarily a successfully delivered
    Telegram notification.
    """
    if not isinstance(event, dict):
        return False

    alert_key = str(event.get("alert_key") or "").strip()

    if not alert_key:
        return False

    with runtime_state.touch_lock:
        runtime_state.alert_sent_keys.add(alert_key)

    return True


def queue_touch_event(
    event: dict,
) -> bool:
    """
    Queues one touch event for internal tracking.

    When the legacy grouped Telegram alert is enabled, the event is
    also added to the pending legacy-alert queue.
    """
    if not isinstance(event, dict):
        return False

    event_snapshot = deepcopy(event)

    with runtime_state.touch_lock:
        runtime_state.touch_events.append(event_snapshot)

        if DEFAULT_LEGACY_TOUCH_TELEGRAM_ENABLED:
            runtime_state.pending_touch_events.append(deepcopy(event_snapshot))

        touch_events_count = len(runtime_state.touch_events)

        pending_events_count = len(runtime_state.pending_touch_events)

        alert_keys_count = len(runtime_state.alert_sent_keys)

        touch_events_snapshot = deepcopy(list(runtime_state.touch_events))

    with runtime_state.opening_range_cache_lock:
        runtime_state.opening_range_cache["touch_events_count"] = touch_events_count

        runtime_state.opening_range_cache["pending_touch_events_count"] = (
            pending_events_count
        )

        runtime_state.opening_range_cache["alert_sent_keys_count"] = alert_keys_count

        runtime_state.opening_range_cache["touch_events"] = touch_events_snapshot

    return True


def build_touch_status_from_events(
    events: list,
) -> dict:
    """Builds an Opening Range touch status from detected events."""
    status = get_default_touch_status()

    if not isinstance(events, list):
        return status

    for event in events:
        if not isinstance(event, dict):
            continue

        level = str(event.get("level") or "").strip().upper()

        if level not in {"R2", "S2", "R3", "S3"}:
            continue

        lower_level = level.lower()

        status[f"{lower_level}_touched"] = True

        status[f"{lower_level}_touch_time"] = event.get("touch_time")

        status[f"{lower_level}_alert_sent"] = bool(
            event.get(
                "telegram_alert_sent",
                event.get("alert_sent", False),
            )
        )

        if not status.get("first_touch_level"):
            status["first_touch_level"] = level
            status["first_touch_source"] = event.get("source")
            status["first_touch_time"] = event.get("touch_time")

        status["events"].append(deepcopy(event))

    return status


def update_touch_status_in_cache(
    instrument_key: str,
    event: dict,
) -> bool:
    """
    Updates touch status for one instrument in opening_range_cache.

    Returns False when the instrument does not yet exist in the cache.
    The service orchestration can then build touch status locally using
    build_touch_status_from_events().
    """
    if not isinstance(event, dict):
        return False

    normalized_instrument_key = str(instrument_key or "").strip()

    if not normalized_instrument_key:
        return False

    level = str(event.get("level") or "").strip().upper()

    if level not in {"R2", "S2", "R3", "S3"}:
        return False

    lower_level = level.lower()

    with runtime_state.opening_range_cache_lock:
        data = runtime_state.opening_range_cache.get(
            "data",
            {},
        )

        if not isinstance(data, dict):
            return False

        item = data.get(normalized_instrument_key)

        if not isinstance(item, dict):
            return False

        touch_status = item.get("touch_status")

        if not isinstance(touch_status, dict):
            touch_status = get_default_touch_status()
        else:
            touch_status = deepcopy(touch_status)

        touch_status[f"{lower_level}_touched"] = True

        touch_status[f"{lower_level}_touch_time"] = event.get("touch_time")

        touch_status[f"{lower_level}_alert_sent"] = bool(
            event.get(
                "telegram_alert_sent",
                event.get("alert_sent", False),
            )
        )

        if not touch_status.get("first_touch_level"):
            touch_status["first_touch_level"] = level
            touch_status["first_touch_source"] = event.get("source")
            touch_status["first_touch_time"] = event.get("touch_time")

        touch_events = touch_status.get("events")

        if not isinstance(touch_events, list):
            touch_events = []

        alert_key = event.get("alert_key")

        duplicate_exists = any(
            isinstance(existing_event, dict)
            and existing_event.get("alert_key") == alert_key
            and existing_event.get("touch_time") == event.get("touch_time")
            for existing_event in touch_events
        )

        if not duplicate_exists:
            touch_events.append(deepcopy(event))

        touch_status["events"] = touch_events

        item["touch_status"] = touch_status
        data[normalized_instrument_key] = item

        runtime_state.opening_range_cache["data"] = data

    return True


# ============================================================
# Touch Detection
# ============================================================


def detect_touch_from_candle(
    instrument_key: str,
    candle: dict,
    levels: dict,
    contract_info: dict,
    source: str,
) -> list:
    """
    Detects configured R2, R3, S2, and S3 touches from a candle.

    Resistance levels use candle high.
    Support levels use candle low.
    Candle close is used only when the preferred field is unavailable.
    """
    if not instrument_key:
        return []

    if not isinstance(candle, dict):
        return []

    if not isinstance(levels, dict):
        return []

    normalized_contract_info = contract_info if isinstance(contract_info, dict) else {}

    level_map = {
        "R2": {
            "value": safe_float(levels.get("r2")),
            "condition_field": "high",
            "trigger": safe_float(candle.get("high")),
            "fallback": safe_float(candle.get("close")),
            "direction": "above",
        },
        "R3": {
            "value": safe_float(levels.get("r3")),
            "condition_field": "high",
            "trigger": safe_float(candle.get("high")),
            "fallback": safe_float(candle.get("close")),
            "direction": "above",
        },
        "S2": {
            "value": safe_float(levels.get("s2")),
            "condition_field": "low",
            "trigger": safe_float(candle.get("low")),
            "fallback": safe_float(candle.get("close")),
            "direction": "below",
        },
        "S3": {
            "value": safe_float(levels.get("s3")),
            "condition_field": "low",
            "trigger": safe_float(candle.get("low")),
            "fallback": safe_float(candle.get("close")),
            "direction": "below",
        },
    }

    candle_time = candle.get("timestamp") or get_now_market_time().isoformat()

    events = []

    for level_name, level_details in level_map.items():
        if level_name not in DEFAULT_ISOLATION_TOUCH_LEVELS:
            continue

        level_value = level_details["value"]

        if level_value <= 0:
            continue

        trigger_price = level_details["trigger"]
        trigger_field = level_details["condition_field"]

        if trigger_price <= 0:
            trigger_price = level_details["fallback"]
            trigger_field = "close"

        if trigger_price <= 0:
            continue

        if level_details["direction"] == "above":
            touched = trigger_price >= level_value
        else:
            touched = trigger_price <= level_value

        if not touched:
            continue

        if should_skip_touch_alert(
            instrument_key=instrument_key,
            level=level_name,
            contract_info=normalized_contract_info,
        ):
            continue

        event = create_touch_event(
            instrument_key=instrument_key,
            level=level_name,
            level_value=level_value,
            trigger_price=trigger_price,
            trigger_field=trigger_field,
            touch_time=candle_time,
            source=source,
            contract_info=normalized_contract_info,
            candle=candle,
        )

        events.append(event)

    return events


def scan_backfill_touches(
    instrument_key: str,
    candles: list,
    levels: dict,
    contract_info: dict,
    candle_count: int,
) -> list:
    """
    Scans post-Opening-Range candles for existing touches.

    Backfill events are returned to the service orchestration. They are
    also added to the global touch-event history.

    If the instrument result already exists in the shared cache, its
    touch status is updated immediately. Otherwise, the orchestration
    service should assign:

        result["touch_status"] =
            build_touch_status_from_events(events)
    """
    if not DEFAULT_BACKFILL_SCAN_ENABLED:
        return []

    if not DEFAULT_BACKFILL_TOUCH_ALERT_ENABLED:
        return []

    if not instrument_key:
        return []

    if not isinstance(candles, list):
        return []

    if not isinstance(levels, dict):
        return []

    runtime_state.ensure_current_market_day()

    post_opening_range_candles = select_post_opening_range_candles(
        candles=candles,
        candle_count=candle_count,
    )

    events = []

    for candle in post_opening_range_candles:
        detected_events = detect_touch_from_candle(
            instrument_key=instrument_key,
            candle=candle,
            levels=levels,
            contract_info=contract_info,
            source="intraday_backfill_scan",
        )

        for event in detected_events:
            events.append(event)

            if DEFAULT_TOUCH_ALERT_ONCE_PER_LEVEL:
                mark_touch_alert_sent(event)

            update_touch_status_in_cache(
                instrument_key=instrument_key,
                event=event,
            )

            queue_touch_event(event)

    return events


# ============================================================
# Upstox Feed Extraction
# ============================================================


def _safe_dict(
    value: Any,
) -> dict:
    """Returns a dictionary or an empty dictionary."""
    return value if isinstance(value, dict) else {}


def _normalize_ohlc_collection(
    value: Any,
) -> list:
    """Returns valid OHLC dictionaries from an Upstox OHLC value."""
    if not isinstance(value, list):
        return []

    return [item for item in value if isinstance(item, dict)]


def extract_feed_values(
    tick_data: dict,
) -> dict:
    """
    Extracts LTP, high, low, close, and timestamp from Upstox feed data.

    The helper supports marketFF and indexFF structures and tolerates
    partially missing feed wrappers.
    """
    default_timestamp = get_now_market_time().isoformat()

    empty_result = {
        "ltp": 0.0,
        "high": 0.0,
        "low": 0.0,
        "close": 0.0,
        "timestamp": default_timestamp,
    }

    if not isinstance(tick_data, dict):
        return empty_result

    raw_feed_object = _safe_dict(tick_data.get("raw_feed", tick_data))

    full_feed = _safe_dict(
        raw_feed_object.get(
            "fullFeed",
            raw_feed_object,
        )
    )

    feed_wrapper = _safe_dict(full_feed.get("ff", full_feed))

    feed = (
        _safe_dict(feed_wrapper.get("marketFF"))
        or _safe_dict(feed_wrapper.get("indexFF"))
        or _safe_dict(full_feed.get("marketFF"))
        or _safe_dict(full_feed.get("indexFF"))
        or full_feed
    )

    ltpc = _safe_dict(feed.get("ltpc"))

    ltp = safe_float(
        ltpc.get("ltp"),
        default=0.0,
    )

    ltpc_timestamp = ltpc.get("ltt") or ltpc.get("ltq") or ltpc.get("ts")

    timestamp = default_timestamp

    parsed_ltpc_timestamp = parse_candle_timestamp(ltpc_timestamp)

    if parsed_ltpc_timestamp is not None:
        timestamp = parsed_ltpc_timestamp.isoformat()

    ohlc_list = []

    market_ohlc = _safe_dict(feed.get("marketOHLC"))

    option_ohlc = _safe_dict(feed.get("optionOHLC"))

    if market_ohlc:
        ohlc_list = _normalize_ohlc_collection(market_ohlc.get("ohlc"))
    elif option_ohlc:
        ohlc_list = _normalize_ohlc_collection(option_ohlc.get("ohlc"))

    matching_i1_candles = [
        item
        for item in ohlc_list
        if str(item.get("interval") or "").strip().upper()
        in {"I1", "1MINUTE", "1_MINUTE"}
    ]

    latest_i1 = None

    if matching_i1_candles:

        def get_ohlc_timestamp(
            item: dict,
        ) -> float:
            raw_timestamp = item.get("ts") or item.get("timestamp") or 0

            parsed_timestamp = parse_candle_timestamp(raw_timestamp)

            if parsed_timestamp is not None:
                return parsed_timestamp.timestamp()

            return float(safe_int(raw_timestamp, default=0))

        try:
            latest_i1 = max(
                matching_i1_candles,
                key=get_ohlc_timestamp,
            )
        except Exception:
            latest_i1 = matching_i1_candles[-1]

    high_value = 0.0
    low_value = 0.0
    close_value = ltp

    if latest_i1 is not None:
        high_value = safe_float(
            latest_i1.get("high"),
            default=0.0,
        )

        low_value = safe_float(
            latest_i1.get("low"),
            default=0.0,
        )

        close_value = safe_float(
            latest_i1.get("close"),
            default=ltp,
        )

        raw_candle_timestamp = latest_i1.get("ts") or latest_i1.get("timestamp")

        parsed_candle_timestamp = parse_candle_timestamp(raw_candle_timestamp)

        if parsed_candle_timestamp is not None:
            timestamp = parsed_candle_timestamp.isoformat()

    if close_value <= 0:
        close_value = ltp

    return {
        "ltp": ltp,
        "high": high_value,
        "low": low_value,
        "close": close_value,
        "timestamp": timestamp,
    }


# ============================================================
# Live Opening Range Processing
# ============================================================


def process_live_tick_for_opening_range(
    instrument_key: str,
    tick_data: dict,
    contract_info: dict | None = None,
) -> list:
    """
    Processes a live tick for Opening Range touch detection.

    Behavior:

        Touches are tracked for all eligible option instruments.
        The best eligible touch can isolate one instrument for the day.
        EMA calculation can continue for all instruments.
        Isolated EMA Telegram alerts are handled separately.
    """
    if not DEFAULT_LIVE_TOUCH_ALERT_ENABLED:
        return []

    normalized_instrument_key = str(instrument_key or "").strip()

    if not normalized_instrument_key:
        return []

    if not isinstance(tick_data, dict):
        return []

    runtime_state.ensure_current_market_day()

    feed_values = extract_feed_values(tick_data)

    ltp = safe_float(
        feed_values.get("ltp"),
        default=0.0,
    )

    updated_at = feed_values.get("timestamp") or get_now_market_time().isoformat()

    runtime_state.set_latest_instrument_ltp(
        instrument_key=normalized_instrument_key,
        ltp=ltp,
        updated_at=updated_at,
    )

    if normalized_instrument_key == DEFAULT_MAIN_INDEX_KEY:
        if ltp > 0:
            update_latest_main_index_ltp(
                ltp=ltp,
                source="live_tick",
                updated_at=updated_at,
            )

        return []

    normalized_contract_info = (
        contract_info if isinstance(contract_info, dict) else None
    )

    if not normalized_contract_info:
        normalized_contract_info = get_contract_info_by_key(normalized_instrument_key)

    if DEFAULT_TOUCH_ALERT_OPTIONS_ONLY and not is_option_contract(
        normalized_contract_info
    ):
        return []

    with runtime_state.opening_range_cache_lock:
        cache_data = runtime_state.opening_range_cache.get(
            "data",
            {},
        )

        if not isinstance(cache_data, dict):
            cache_data = {}

        cached_item = cache_data.get(normalized_instrument_key)

        item = deepcopy(cached_item) if isinstance(cached_item, dict) else None

    if not item:
        return []

    if item.get("status") != "success":
        return []

    levels = item.get("levels") or {}

    if not isinstance(levels, dict) or not levels:
        return []

    pseudo_candle = {
        "timestamp": updated_at,
        "open": 0.0,
        "high": safe_float(
            feed_values.get("high"),
            default=0.0,
        ),
        "low": safe_float(
            feed_values.get("low"),
            default=0.0,
        ),
        "close": safe_float(
            feed_values.get("close"),
            default=ltp,
        ),
        "volume": 0,
        "oi": 0,
    }

    if DEFAULT_TOUCH_CHECK_MODE == "ltp":
        pseudo_candle["high"] = ltp
        pseudo_candle["low"] = ltp
        pseudo_candle["close"] = ltp

    events = detect_touch_from_candle(
        instrument_key=normalized_instrument_key,
        candle=pseudo_candle,
        levels=levels,
        contract_info=normalized_contract_info,
        source="live_tick",
    )

    for event in events:
        if DEFAULT_TOUCH_ALERT_ONCE_PER_LEVEL:
            mark_touch_alert_sent(event)

        update_touch_status_in_cache(
            instrument_key=normalized_instrument_key,
            event=event,
        )

        queue_touch_event(event)

    if events:
        # Local import prevents isolation.py and live_touch.py from
        # becoming a top-level circular-import pair.
        from .isolation import (
            try_isolate_from_touch_events,
        )

        try:
            try_isolate_from_touch_events(events)
        except Exception as ex:
            logger.exception(
                "Opening Range isolation failed after touch "
                "detection. instrument_key=%s, error=%s: %s",
                normalized_instrument_key,
                type(ex).__name__,
                ex,
            )

    return events


# ============================================================
# Public API
# ============================================================


__all__ = [
    # Touch event helpers
    "build_alert_key",
    "calculate_distance_from_index",
    "update_latest_main_index_ltp",
    "get_latest_main_index_ltp",
    "get_default_touch_status",
    "create_touch_event",
    "should_skip_touch_alert",
    "mark_touch_alert_sent",
    "queue_touch_event",
    "build_touch_status_from_events",
    "update_touch_status_in_cache",
    # Touch detection
    "detect_touch_from_candle",
    "scan_backfill_touches",
    # Feed extraction
    "extract_feed_values",
    # Live processing
    "process_live_tick_for_opening_range",
]
