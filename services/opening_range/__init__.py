"""
Public compatibility layer for the modular Opening Range service.

Application code can import Opening Range functions directly from
this package. Internal modules should import from the specific module
they depend on to avoid circular imports.
"""

from .service import (
    calculate_opening_range_for_all_subscribed,
    calculate_opening_range_for_instrument,
)

from .candle_utils import (
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

from .intraday import (
    fetch_intraday_candles_for_instrument,
)

from .range_calculator import (
    calculate_opening_range_levels,
)

from .live_touch import (
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

from .touch_events import (
    flush_pending_touch_alerts,
    format_touch_event_line,
    get_latest_ltp_for_instrument,
    get_sorted_touch_events_for_alert,
    send_touch_events_telegram_alert,
    update_latest_ltp_for_instrument,
)

from .isolation import (
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

from .ema_alerts import (
    enrich_option_chain_instruments,
    format_budget_range_instruments,
    format_suggested_order_instruments,
    get_ema_alert_minute_bucket,
    get_isolated_instrument_type_from_state,
    get_opening_range_levels_for_ema_event,
    get_option_chain_instruments_for_ema,
    get_selected_or_ema_alerts,
    get_selected_or_instrument_key,
    get_selected_or_instrument_state,
    get_suggested_order_option_type,
    is_selected_or_instrument_locked,
    normalize_ema_cross_direction,
    process_selected_or_ema_cross_alert,
    should_skip_isolated_ema_alert_for_minute_direction,
)

from .status import (
    get_opening_range_cache,
    get_opening_range_dashboard_summary,
    get_opening_range_for_instrument_from_cache,
    get_opening_range_pending_touch_events,
    get_opening_range_status,
    get_opening_range_touch_events,
)

from .storage import (
    save_opening_range_results_to_file,
    save_touch_events_to_file_if_enabled,
)

from .state import (
    ensure_current_market_day,
    get_latest_main_index_ltp_snapshot,
    get_opening_range_cache_snapshot,
    get_selected_or_ema_alerts_snapshot,
    get_selected_or_state_snapshot,
    get_touch_state_snapshot,
    reset_all_opening_range_state,
    synchronize_cache_counters,
)

__all__ = [
    "calculate_opening_range_for_instrument",
    "calculate_opening_range_for_all_subscribed",
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
    "get_market_open_datetime",
    "get_opening_range_end_datetime",
    "select_opening_range_candles",
    "select_post_opening_range_candles",
    "get_subscribed_instrument_keys",
    "get_contract_info_by_key",
    "normalize_option_type",
    "is_option_contract",
    "fetch_intraday_candles_for_instrument",
    "calculate_opening_range_levels",
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
    "detect_touch_from_candle",
    "scan_backfill_touches",
    "extract_feed_values",
    "process_live_tick_for_opening_range",
    "update_latest_ltp_for_instrument",
    "get_latest_ltp_for_instrument",
    "get_sorted_touch_events_for_alert",
    "format_touch_event_line",
    "send_touch_events_telegram_alert",
    "flush_pending_touch_alerts",
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
    "is_selected_or_instrument_locked",
    "get_selected_or_instrument_key",
    "get_selected_or_instrument_state",
    "get_selected_or_ema_alerts",
    "get_isolated_instrument_type_from_state",
    "get_suggested_order_option_type",
    "get_option_chain_instruments_for_ema",
    "enrich_option_chain_instruments",
    "format_suggested_order_instruments",
    "format_budget_range_instruments",
    "normalize_ema_cross_direction",
    "get_ema_alert_minute_bucket",
    "should_skip_isolated_ema_alert_for_minute_direction",
    "process_selected_or_ema_cross_alert",
    "get_opening_range_levels_for_ema_event",
    "get_opening_range_status",
    "get_opening_range_cache",
    "get_opening_range_dashboard_summary",
    "get_opening_range_for_instrument_from_cache",
    "get_opening_range_touch_events",
    "get_opening_range_pending_touch_events",
    "save_opening_range_results_to_file",
    "save_touch_events_to_file_if_enabled",
    "ensure_current_market_day",
    "reset_all_opening_range_state",
    "synchronize_cache_counters",
    "get_opening_range_cache_snapshot",
    "get_touch_state_snapshot",
    "get_selected_or_state_snapshot",
    "get_selected_or_ema_alerts_snapshot",
    "get_latest_main_index_ltp_snapshot",
]
