"""
Backward-compatible Opening Range service wrapper.

The Opening Range implementation has been moved into the modular
package:

    services/opening_range/

This file remains temporarily so existing application files using:

    from services.opening_range_service import (
        calculate_opening_range_for_all_subscribed,
        process_live_tick_for_opening_range,
    )

continue to work without modification.

Important
---------

Do not define runtime state, locks, caches, configuration constants, or
business logic in this file.

All mutable Opening Range runtime state is owned exclusively by:

    services/opening_range/state.py

All new application code should preferably import from:

    services.opening_range

This compatibility wrapper imports public functions directly from their
owning modules to minimize circular-import risk.
"""

# ============================================================
# Main Opening Range Calculation
# ============================================================

from .opening_range.service import (
    calculate_opening_range_for_all_subscribed,
    calculate_opening_range_for_instrument,
)

# ============================================================
# Candle and Instrument Helpers
# ============================================================

from .opening_range.candle_utils import (
    extract_candles_from_response,
    get_contract_info_by_key,
    get_live_ema_calculation_mode_text,
    get_market_open_datetime,
    get_market_timezone,
    get_now_market_time,
    get_opening_range_end_datetime,
    get_subscribed_instrument_keys,
    is_opening_range_enabled,
    is_option_contract,
    normalize_candle,
    normalize_candles,
    normalize_option_type,
    parse_candle_timestamp,
    response_to_dict,
    safe_float,
    safe_int,
    select_opening_range_candles,
    select_post_opening_range_candles,
    serialize_candle,
)

# ============================================================
# Intraday Candle Fetch
# ============================================================

from .opening_range.intraday import (
    fetch_intraday_candles_for_instrument,
)

# ============================================================
# Opening Range Formula
# ============================================================

from .opening_range.range_calculator import (
    calculate_opening_range_levels,
)

# ============================================================
# Live Touch Processing
# ============================================================

from .opening_range.live_touch import (
    build_alert_key,
    build_touch_status_from_events,
    calculate_distance_from_index,
    create_touch_event,
    detect_touch_from_candle,
    extract_feed_values,
    get_default_touch_status,
    get_latest_main_index_ltp,
    mark_touch_alert_sent,
    process_live_tick_for_opening_range,
    queue_touch_event,
    scan_backfill_touches,
    should_skip_touch_alert,
    update_latest_main_index_ltp,
    update_touch_status_in_cache,
)

# ============================================================
# Latest Instrument LTP and Legacy Touch Alerts
# ============================================================

from .opening_range.touch_events import (
    flush_pending_touch_alerts,
    format_touch_event_line,
    get_latest_ltp_for_instrument,
    get_sorted_touch_events_for_alert,
    send_touch_events_telegram_alert,
    update_latest_ltp_for_instrument,
)

# ============================================================
# Isolated Instrument Selection
# ============================================================

from .opening_range.isolation import (
    build_average_window,
    choose_best_isolation_event,
    format_isolated_instrument_title,
    get_level_priority,
    get_reference_opening_range_average,
    is_event_eligible_for_isolation,
    isolate_instrument_from_event,
    send_isolated_instrument_notification,
    should_replace_isolated_instrument,
    try_isolate_from_touch_events,
)

# ============================================================
# Isolated Instrument EMA Alerts
# ============================================================

from .opening_range.ema_alerts import (
    format_suggested_order_instruments,
    get_ema_alert_minute_bucket,
    get_isolated_instrument_type_from_state,
    get_opening_range_levels_for_ema_event,
    get_selected_or_ema_alerts,
    get_selected_or_instrument_key,
    get_selected_or_instrument_state,
    get_suggested_order_instruments_for_ema,
    is_selected_or_instrument_locked,
    normalize_ema_cross_direction,
    process_selected_or_ema_cross_alert,
    should_skip_isolated_ema_alert_for_minute_direction,
)

# ============================================================
# Opening Range Status and Dashboard
# ============================================================

from .opening_range.status import (
    get_opening_range_cache,
    get_opening_range_dashboard_summary,
    get_opening_range_for_instrument_from_cache,
    get_opening_range_pending_touch_events,
    get_opening_range_status,
    get_opening_range_touch_events,
)

# ============================================================
# Opening Range Storage
# ============================================================

from .opening_range.storage import (
    save_opening_range_results_to_file,
    save_touch_events_to_file_if_enabled,
)

# ============================================================
# Shared Runtime State Management
# ============================================================

from .opening_range.state import (
    ensure_current_market_day,
    get_latest_main_index_ltp_snapshot,
    get_opening_range_cache_snapshot,
    get_selected_or_ema_alerts_snapshot,
    get_selected_or_state_snapshot,
    get_touch_state_snapshot,
    reset_all_opening_range_state,
    synchronize_cache_counters,
)

# ============================================================
# Shared Runtime State Compatibility
# ============================================================

from .opening_range.state import (
    alert_sent_keys,
    opening_range_cache,
    opening_range_cache_lock,
    pending_touch_events,
    selected_or_ema_alerts,
    selected_or_ema_alert_minute_keys,
    selected_or_instrument_state,
    selected_or_lock,
    touch_events,
    touch_lock,
)

# ============================================================
# Old Underscore-Prefixed State Aliases
# ============================================================

"""
These aliases are retained only for older project code that directly
imports the original underscore-prefixed mutable state objects.

New code should use status and state helper functions such as:

    get_opening_range_cache()
    get_opening_range_status()
    get_selected_or_instrument_state()
    get_opening_range_touch_events()

Important:

These aliases are safe for mutable objects such as dictionaries, sets,
deques, and locks because those objects are modified in place.

Reassigned scalar state is intentionally not re-exported here because
directly importing scalar values can produce stale references.
"""

_opening_range_cache_lock = opening_range_cache_lock
_touch_lock = touch_lock
_selected_or_lock = selected_or_lock

_pending_touch_events = pending_touch_events
_touch_events = touch_events
_alert_sent_keys = alert_sent_keys

_selected_or_instrument_state = selected_or_instrument_state

_selected_or_ema_alerts = selected_or_ema_alerts

_selected_or_ema_alert_minute_keys = selected_or_ema_alert_minute_keys


# ============================================================
# Public API
# ============================================================

__all__ = [
    # Main calculation
    "calculate_opening_range_for_instrument",
    "calculate_opening_range_for_all_subscribed",
    # Basic helpers
    "is_opening_range_enabled",
    "get_market_timezone",
    "get_now_market_time",
    "get_live_ema_calculation_mode_text",
    "safe_float",
    "safe_int",
    "response_to_dict",
    "extract_candles_from_response",
    "parse_candle_timestamp",
    "normalize_candle",
    "normalize_candles",
    "serialize_candle",
    # Candle selection
    "get_market_open_datetime",
    "get_opening_range_end_datetime",
    "select_opening_range_candles",
    "select_post_opening_range_candles",
    # Instrument helpers
    "get_subscribed_instrument_keys",
    "get_contract_info_by_key",
    "normalize_option_type",
    "is_option_contract",
    # Intraday fetch
    "fetch_intraday_candles_for_instrument",
    # Range calculation
    "calculate_opening_range_levels",
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
    "extract_feed_values",
    "process_live_tick_for_opening_range",
    # Instrument LTP
    "update_latest_ltp_for_instrument",
    "get_latest_ltp_for_instrument",
    # Legacy Telegram touch alerts
    "get_sorted_touch_events_for_alert",
    "format_touch_event_line",
    "send_touch_events_telegram_alert",
    "flush_pending_touch_alerts",
    # Isolated instrument selection
    "get_level_priority",
    "get_reference_opening_range_average",
    "build_average_window",
    "is_event_eligible_for_isolation",
    "choose_best_isolation_event",
    "should_replace_isolated_instrument",
    "format_isolated_instrument_title",
    "send_isolated_instrument_notification",
    "isolate_instrument_from_event",
    "try_isolate_from_touch_events",
    # Isolated EMA helpers
    "is_selected_or_instrument_locked",
    "get_selected_or_instrument_key",
    "get_selected_or_instrument_state",
    "get_selected_or_ema_alerts",
    "get_isolated_instrument_type_from_state",
    "get_suggested_order_instruments_for_ema",
    "format_suggested_order_instruments",
    "normalize_ema_cross_direction",
    "get_ema_alert_minute_bucket",
    "should_skip_isolated_ema_alert_for_minute_direction",
    "process_selected_or_ema_cross_alert",
    "get_opening_range_levels_for_ema_event",
    # Status and dashboard
    "get_opening_range_status",
    "get_opening_range_cache",
    "get_opening_range_dashboard_summary",
    "get_opening_range_for_instrument_from_cache",
    "get_opening_range_touch_events",
    "get_opening_range_pending_touch_events",
    # Storage
    "save_opening_range_results_to_file",
    "save_touch_events_to_file_if_enabled",
    # Runtime state management
    "ensure_current_market_day",
    "reset_all_opening_range_state",
    "synchronize_cache_counters",
    "get_opening_range_cache_snapshot",
    "get_touch_state_snapshot",
    "get_selected_or_state_snapshot",
    "get_selected_or_ema_alerts_snapshot",
    "get_latest_main_index_ltp_snapshot",
    # Shared mutable compatibility state
    "opening_range_cache",
    "opening_range_cache_lock",
    "touch_lock",
    "touch_events",
    "pending_touch_events",
    "alert_sent_keys",
    "selected_or_lock",
    "selected_or_instrument_state",
    "selected_or_ema_alerts",
    "selected_or_ema_alert_minute_keys",
    # Legacy aliases
    "_opening_range_cache_lock",
    "_touch_lock",
    "_selected_or_lock",
    "_pending_touch_events",
    "_touch_events",
    "_alert_sent_keys",
    "_selected_or_instrument_state",
    "_selected_or_ema_alerts",
    "_selected_or_ema_alert_minute_keys",
]
