"""
Shared runtime state for the Opening Range package.

This module is the single owner of all mutable, process-level Opening
Range state.

Important usage rule
--------------------

Internal modules should import the state module itself when reading or
updating scalar values:

    from . import state

    value = state.latest_main_index_ltp

    with state.touch_lock:
        state.latest_main_index_ltp = 22500.50

Avoid importing mutable scalar values directly:

    from .state import latest_main_index_ltp

Directly imported scalar values do not stay synchronized when another
module reassigns the value.

Shared dictionaries, lists, deques, sets, and locks may be imported
directly, but importing the state module itself is still the preferred
and safest pattern.

This module must not import from other Opening Range business modules.
That keeps the dependency direction clean and helps prevent circular
imports.
"""

from collections import deque
from copy import deepcopy
from datetime import date, datetime
from threading import RLock
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .constants import (
    DEFAULT_MAIN_INDEX_KEY,
    DEFAULT_MARKET_TIMEZONE,
    DEFAULT_MAX_EVENTS_IN_MEMORY,
)

# ============================================================
# Lock Ordering
# ============================================================

"""
When a function needs more than one lock, use this order:

    1. touch_lock
    2. selected_or_lock
    3. opening_range_cache_lock

Most functions should create snapshots under their individual locks
instead of holding multiple locks simultaneously.
"""


# ============================================================
# Locks
# ============================================================


opening_range_cache_lock = RLock()
touch_lock = RLock()
selected_or_lock = RLock()


# Backward-compatible aliases for code migrated from the original
# monolithic opening_range_service.py file.
_opening_range_cache_lock = opening_range_cache_lock
_touch_lock = touch_lock
_selected_or_lock = selected_or_lock


# ============================================================
# Internal Date Helpers
# ============================================================


def _get_market_timezone() -> ZoneInfo:
    """Returns the configured market timezone safely."""
    try:
        return ZoneInfo(DEFAULT_MARKET_TIMEZONE)
    except ZoneInfoNotFoundError:
        return ZoneInfo("Asia/Kolkata")


def get_state_market_datetime() -> datetime:
    """
    Returns current datetime using the configured market timezone.

    This helper is intentionally kept inside state.py so this module
    does not need to import candle_utils.py.
    """
    return datetime.now(_get_market_timezone())


def get_state_market_date() -> str:
    """Returns the current market date as an ISO-formatted string."""
    return get_state_market_datetime().date().isoformat()


def normalize_state_date(value: Any = None) -> str:
    """
    Normalizes a date-like value to YYYY-MM-DD.

    Supported input:
        None
        datetime
        date
        ISO date string
        ISO datetime string
    """
    if value is None:
        return get_state_market_date()

    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=_get_market_timezone())
        else:
            value = value.astimezone(_get_market_timezone())

        return value.date().isoformat()

    if isinstance(value, date):
        return value.isoformat()

    text = str(value).strip()

    if not text:
        return get_state_market_date()

    try:
        return date.fromisoformat(text[:10]).isoformat()
    except (TypeError, ValueError):
        return get_state_market_date()


# ============================================================
# Default State Builders
# ============================================================


def build_default_opening_range_cache(
    state_date: str | None = None,
) -> dict:
    """Builds a fresh Opening Range cache."""
    normalized_date = (
        normalize_state_date(state_date) if state_date is not None else None
    )

    return {
        "last_run_at": None,
        "date": normalized_date,
        "status": "not_started",
        "message": "Opening range calculation has not run yet.",
        "source": "intraday_api",
        "interval": None,
        "opening_range_candle_count": 0,
        "market_open_time": None,
        "fetch_time": None,
        "total_instruments": 0,
        "success_count": 0,
        "failed_count": 0,
        "empty_count": 0,
        "insufficient_data_count": 0,
        "output_file_path": None,
        "latest_main_index_ltp": None,
        "latest_main_index_ltp_source": None,
        "latest_main_index_ltp_updated_at": None,
        "touch_events_count": 0,
        "pending_touch_events_count": 0,
        "alert_sent_keys_count": 0,
        "isolated_instrument": None,
        "isolated_instrument_selected": False,
        "isolated_instrument_selected_at": None,
        "isolated_instrument_selection_reason": None,
        "isolated_ema_alerts_count": 0,
        "data": {},
        "touch_events": [],
        "errors": {},
    }


def build_default_selected_or_instrument_state() -> dict:
    """Builds a fresh isolated Opening Range instrument state."""
    return {
        "selected": False,
        "instrument_key": None,
        "selected_level": None,
        "level_value": None,
        "trigger_price": None,
        "trigger_field": None,
        "touch_time": None,
        "touch_source": None,
        "selected_at": None,
        "selection_priority": None,
        "selection_reason": None,
        "reference_average": None,
        "average_window": None,
        "contract_info": None,
        "range": None,
        "levels": None,
        "latest_live_data": None,
        "latest_main_index_ltp": None,
        "live_ema_calculation_mode_flag": None,
        "live_ema_calculation_mode": None,
        "ema_alerts_count": 0,
        "last_ema_alert": None,
        "disabled": False,
        "message": "No isolated Opening Range instrument selected yet.",
    }


# ============================================================
# Main Opening Range Cache
# ============================================================


opening_range_cache = build_default_opening_range_cache()


# ============================================================
# Touch Event State
# ============================================================


pending_touch_events = deque()

touch_events = deque(maxlen=DEFAULT_MAX_EVENTS_IN_MEMORY)

alert_sent_keys: set[str] = set()

last_touch_alert_sent_at: float | None = None


# Backward-compatible aliases.
#
# These objects are mutated in place and therefore safely remain shared
# across modules.
_pending_touch_events = pending_touch_events
_touch_events = touch_events
_alert_sent_keys = alert_sent_keys


# ============================================================
# Main Index LTP State
# ============================================================


latest_main_index_ltp: float | None = None
latest_main_index_ltp_source: str | None = None
latest_main_index_ltp_updated_at: str | None = None


# Backward-compatibility scalar names.
#
# New modular files should use:
#
#     from . import state
#     state.latest_main_index_ltp
#
# These underscore names are retained temporarily for migration.
_latest_main_index_ltp: float | None = None
_latest_main_index_ltp_source: str | None = None
_latest_main_index_ltp_updated_at: str | None = None


# ============================================================
# Latest Instrument LTP State
# ============================================================


latest_ltp_by_instrument: dict[str, float] = {}

latest_ltp_updated_at_by_instrument: dict[str, str] = {}


# Backward-compatible aliases.
_latest_ltp_by_instrument = latest_ltp_by_instrument
_latest_ltp_updated_at_by_instrument = latest_ltp_updated_at_by_instrument


# ============================================================
# Isolated Instrument State
# ============================================================


selected_or_instrument_state = build_default_selected_or_instrument_state()

selected_or_ema_alerts = deque(maxlen=DEFAULT_MAX_EVENTS_IN_MEMORY)

selected_or_ema_alert_minute_keys: set[str] = set()

selected_or_ema_alert_minute_date: str | None = None


# Backward-compatible aliases for mutable objects.
_selected_or_instrument_state = selected_or_instrument_state
_selected_or_ema_alerts = selected_or_ema_alerts
_selected_or_ema_alert_minute_keys = selected_or_ema_alert_minute_keys


# Backward-compatibility scalar name.
#
# New modules should use state.selected_or_ema_alert_minute_date.
_selected_or_ema_alert_minute_date: str | None = None


# ============================================================
# Active Runtime Date
# ============================================================


runtime_state_date = get_state_market_date()


# ============================================================
# Main Index LTP Helpers
# ============================================================


def set_latest_main_index_ltp(
    ltp: Any,
    source: str = "unknown",
    updated_at: str | None = None,
) -> bool:
    """
    Updates the latest main-index LTP in shared state and cache.

    Returns True when a valid positive LTP is stored.
    """
    global latest_main_index_ltp
    global latest_main_index_ltp_source
    global latest_main_index_ltp_updated_at
    global _latest_main_index_ltp
    global _latest_main_index_ltp_source
    global _latest_main_index_ltp_updated_at

    try:
        value = float(ltp)
    except (TypeError, ValueError, OverflowError):
        return False

    if value <= 0:
        return False

    normalized_source = str(source or "unknown").strip() or "unknown"
    normalized_updated_at = (
        str(updated_at).strip()
        if updated_at
        else get_state_market_datetime().isoformat()
    )

    with touch_lock:
        latest_main_index_ltp = value
        latest_main_index_ltp_source = normalized_source
        latest_main_index_ltp_updated_at = normalized_updated_at

        # Keep legacy scalar names synchronized.
        _latest_main_index_ltp = value
        _latest_main_index_ltp_source = normalized_source
        _latest_main_index_ltp_updated_at = normalized_updated_at

    with opening_range_cache_lock:
        opening_range_cache["latest_main_index_ltp"] = value
        opening_range_cache["latest_main_index_ltp_source"] = normalized_source
        opening_range_cache["latest_main_index_ltp_updated_at"] = normalized_updated_at

    return True


def get_latest_main_index_ltp_value() -> float | None:
    """Returns the latest shared main-index LTP."""
    with touch_lock:
        return latest_main_index_ltp


def get_latest_main_index_ltp_snapshot() -> dict:
    """Returns the latest main-index LTP metadata."""
    with touch_lock:
        return {
            "instrument_key": DEFAULT_MAIN_INDEX_KEY,
            "ltp": latest_main_index_ltp,
            "source": latest_main_index_ltp_source,
            "updated_at": latest_main_index_ltp_updated_at,
        }


# ============================================================
# Instrument LTP Helpers
# ============================================================


def set_latest_instrument_ltp(
    instrument_key: str,
    ltp: Any,
    updated_at: str | None = None,
) -> bool:
    """
    Stores the latest positive LTP for an instrument.

    Returns True when the value is stored successfully.
    """
    if not instrument_key:
        return False

    normalized_key = str(instrument_key).strip()

    if not normalized_key:
        return False

    try:
        value = float(ltp)
    except (TypeError, ValueError, OverflowError):
        return False

    if value <= 0:
        return False

    normalized_updated_at = (
        str(updated_at).strip()
        if updated_at
        else get_state_market_datetime().isoformat()
    )

    with touch_lock:
        latest_ltp_by_instrument[normalized_key] = value
        latest_ltp_updated_at_by_instrument[normalized_key] = normalized_updated_at

    return True


def get_latest_instrument_ltp(
    instrument_key: str,
) -> float | None:
    """Returns the latest cached LTP for one instrument."""
    if not instrument_key:
        return None

    normalized_key = str(instrument_key).strip()

    if not normalized_key:
        return None

    with touch_lock:
        return latest_ltp_by_instrument.get(normalized_key)


def get_latest_instrument_ltp_snapshot(
    instrument_key: str,
) -> dict:
    """Returns LTP and update-time information for one instrument."""
    if not instrument_key:
        return {
            "instrument_key": None,
            "ltp": None,
            "updated_at": None,
        }

    normalized_key = str(instrument_key).strip()

    with touch_lock:
        return {
            "instrument_key": normalized_key,
            "ltp": latest_ltp_by_instrument.get(normalized_key),
            "updated_at": (latest_ltp_updated_at_by_instrument.get(normalized_key)),
        }


# ============================================================
# Snapshot Helpers
# ============================================================


def get_opening_range_cache_snapshot() -> dict:
    """
    Returns a deep copy of the Opening Range cache.

    A deep copy prevents API callers from accidentally modifying the
    live runtime cache.
    """
    with opening_range_cache_lock:
        return deepcopy(opening_range_cache)


def get_touch_state_snapshot(
    limit: int | None = None,
) -> dict:
    """Returns a consistent snapshot of touch-related shared state."""
    with touch_lock:
        touch_event_list = list(touch_events)

        if limit is not None:
            try:
                normalized_limit = max(1, int(limit))
                touch_event_list = touch_event_list[-normalized_limit:]
            except (TypeError, ValueError, OverflowError):
                pass

        return {
            "events": deepcopy(touch_event_list),
            "events_count": len(touch_events),
            "pending_events": deepcopy(list(pending_touch_events)),
            "pending_events_count": len(pending_touch_events),
            "alert_sent_keys": list(alert_sent_keys),
            "alert_sent_keys_count": len(alert_sent_keys),
            "latest_main_index_ltp": latest_main_index_ltp,
            "latest_main_index_ltp_source": (latest_main_index_ltp_source),
            "latest_main_index_ltp_updated_at": (latest_main_index_ltp_updated_at),
            "last_touch_alert_sent_at": last_touch_alert_sent_at,
        }


def get_selected_or_state_snapshot() -> dict:
    """Returns a deep copy of the isolated instrument state."""
    with selected_or_lock:
        return deepcopy(selected_or_instrument_state)


def get_selected_or_ema_alerts_snapshot(
    limit: int | None = None,
) -> list:
    """Returns a snapshot of isolated EMA alerts."""
    with selected_or_lock:
        alerts = list(selected_or_ema_alerts)

        if limit is not None:
            try:
                normalized_limit = max(1, int(limit))
                alerts = alerts[-normalized_limit:]
            except (TypeError, ValueError, OverflowError):
                pass

        return deepcopy(alerts)


# ============================================================
# Cache Synchronization
# ============================================================


def synchronize_cache_counters() -> None:
    """
    Synchronizes top-level cache counters with shared state.

    Snapshots are created independently so multiple locks do not need
    to remain held at the same time.
    """
    with touch_lock:
        touch_events_count = len(touch_events)
        pending_events_count = len(pending_touch_events)
        alert_keys_count = len(alert_sent_keys)
        touch_events_snapshot = deepcopy(list(touch_events))
        main_index_ltp = latest_main_index_ltp
        main_index_ltp_source = latest_main_index_ltp_source
        main_index_ltp_updated_at = latest_main_index_ltp_updated_at

    with selected_or_lock:
        selected_state_snapshot = deepcopy(selected_or_instrument_state)
        isolated_alerts_count = len(selected_or_ema_alerts)

    with opening_range_cache_lock:
        opening_range_cache["touch_events_count"] = touch_events_count

        opening_range_cache["pending_touch_events_count"] = pending_events_count

        opening_range_cache["alert_sent_keys_count"] = alert_keys_count

        opening_range_cache["touch_events"] = touch_events_snapshot

        opening_range_cache["latest_main_index_ltp"] = main_index_ltp

        opening_range_cache["latest_main_index_ltp_source"] = main_index_ltp_source

        opening_range_cache["latest_main_index_ltp_updated_at"] = (
            main_index_ltp_updated_at
        )

        opening_range_cache["isolated_instrument"] = selected_state_snapshot

        opening_range_cache["isolated_instrument_selected"] = bool(
            selected_state_snapshot.get("selected")
        )

        opening_range_cache["isolated_instrument_selected_at"] = (
            selected_state_snapshot.get("selected_at")
        )

        opening_range_cache["isolated_instrument_selection_reason"] = (
            selected_state_snapshot.get("selection_reason")
        )

        opening_range_cache["isolated_ema_alerts_count"] = isolated_alerts_count


# ============================================================
# Reset Helpers
# ============================================================


def reset_touch_state() -> None:
    """Clears all touch-event and latest-LTP state."""
    global last_touch_alert_sent_at
    global latest_main_index_ltp
    global latest_main_index_ltp_source
    global latest_main_index_ltp_updated_at
    global _latest_main_index_ltp
    global _latest_main_index_ltp_source
    global _latest_main_index_ltp_updated_at

    with touch_lock:
        pending_touch_events.clear()
        touch_events.clear()
        alert_sent_keys.clear()

        latest_ltp_by_instrument.clear()
        latest_ltp_updated_at_by_instrument.clear()

        latest_main_index_ltp = None
        latest_main_index_ltp_source = None
        latest_main_index_ltp_updated_at = None

        _latest_main_index_ltp = None
        _latest_main_index_ltp_source = None
        _latest_main_index_ltp_updated_at = None

        last_touch_alert_sent_at = None


def reset_selected_or_state() -> None:
    """Clears isolated-instrument and isolated-EMA state."""
    global selected_or_ema_alert_minute_date
    global _selected_or_ema_alert_minute_date

    default_state = build_default_selected_or_instrument_state()

    with selected_or_lock:
        selected_or_instrument_state.clear()
        selected_or_instrument_state.update(default_state)

        selected_or_ema_alerts.clear()
        selected_or_ema_alert_minute_keys.clear()

        selected_or_ema_alert_minute_date = None
        _selected_or_ema_alert_minute_date = None


def reset_opening_range_cache(
    state_date: Any = None,
) -> None:
    """Clears and recreates the main Opening Range cache."""
    normalized_date = normalize_state_date(state_date)
    default_cache = build_default_opening_range_cache(state_date=normalized_date)

    with opening_range_cache_lock:
        opening_range_cache.clear()
        opening_range_cache.update(default_cache)


def reset_all_opening_range_state(
    state_date: Any = None,
) -> str:
    """
    Resets all Opening Range runtime state.

    Returns the normalized active market date.
    """
    global runtime_state_date

    normalized_date = normalize_state_date(state_date)

    reset_touch_state()
    reset_selected_or_state()
    reset_opening_range_cache(normalized_date)

    runtime_state_date = normalized_date

    synchronize_cache_counters()

    return normalized_date


def ensure_current_market_day(
    state_date: Any = None,
) -> bool:
    """
    Resets runtime state when the market date changes.

    Returns:
        True when a day-change reset occurred.
        False when the active state already belongs to the date.
    """
    normalized_date = normalize_state_date(state_date)

    if runtime_state_date == normalized_date:
        return False

    reset_all_opening_range_state(state_date=normalized_date)

    return True


# ============================================================
# Selected EMA Minute-Key Helpers
# ============================================================


def check_and_reserve_ema_minute_key(
    alert_key: str,
    state_date: Any = None,
) -> bool:
    """
    Checks and reserves an isolated EMA minute duplicate key.

    Returns:
        True when the key already exists and should be skipped.
        False when the key is newly reserved.

    If Telegram delivery fails, the caller should release the key using
    release_ema_minute_key().
    """
    global selected_or_ema_alert_minute_date
    global _selected_or_ema_alert_minute_date

    if not alert_key:
        return False

    normalized_date = normalize_state_date(state_date)
    normalized_key = str(alert_key).strip()

    if not normalized_key:
        return False

    with selected_or_lock:
        if selected_or_ema_alert_minute_date != normalized_date:
            selected_or_ema_alert_minute_keys.clear()

            selected_or_ema_alert_minute_date = normalized_date

            _selected_or_ema_alert_minute_date = normalized_date

        if normalized_key in selected_or_ema_alert_minute_keys:
            return True

        selected_or_ema_alert_minute_keys.add(normalized_key)

    return False


def release_ema_minute_key(alert_key: str) -> None:
    """
    Releases a reserved EMA minute key.

    This should be called when Telegram delivery fails so another
    attempt within the same minute is not incorrectly blocked.
    """
    if not alert_key:
        return

    normalized_key = str(alert_key).strip()

    if not normalized_key:
        return

    with selected_or_lock:
        selected_or_ema_alert_minute_keys.discard(normalized_key)


# ============================================================
# Initialization
# ============================================================


synchronize_cache_counters()


# ============================================================
# Public State API
# ============================================================


__all__ = [
    # Locks
    "opening_range_cache_lock",
    "touch_lock",
    "selected_or_lock",
    # Main cache
    "opening_range_cache",
    # Touch state
    "pending_touch_events",
    "touch_events",
    "alert_sent_keys",
    "last_touch_alert_sent_at",
    # Main-index LTP
    "latest_main_index_ltp",
    "latest_main_index_ltp_source",
    "latest_main_index_ltp_updated_at",
    # Instrument LTP
    "latest_ltp_by_instrument",
    "latest_ltp_updated_at_by_instrument",
    # Isolated instrument
    "selected_or_instrument_state",
    "selected_or_ema_alerts",
    "selected_or_ema_alert_minute_keys",
    "selected_or_ema_alert_minute_date",
    # Active date
    "runtime_state_date",
    # Date helpers
    "get_state_market_datetime",
    "get_state_market_date",
    "normalize_state_date",
    # Default builders
    "build_default_opening_range_cache",
    "build_default_selected_or_instrument_state",
    # Main-index LTP helpers
    "set_latest_main_index_ltp",
    "get_latest_main_index_ltp_value",
    "get_latest_main_index_ltp_snapshot",
    # Instrument LTP helpers
    "set_latest_instrument_ltp",
    "get_latest_instrument_ltp",
    "get_latest_instrument_ltp_snapshot",
    # Snapshots
    "get_opening_range_cache_snapshot",
    "get_touch_state_snapshot",
    "get_selected_or_state_snapshot",
    "get_selected_or_ema_alerts_snapshot",
    # Synchronization
    "synchronize_cache_counters",
    # Reset handling
    "reset_touch_state",
    "reset_selected_or_state",
    "reset_opening_range_cache",
    "reset_all_opening_range_state",
    "ensure_current_market_day",
    # EMA duplicate key handling
    "check_and_reserve_ema_minute_key",
    "release_ema_minute_key",
]
