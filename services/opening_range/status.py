"""
Opening Range status, cache, and dashboard helpers.

This module provides read-only access to Opening Range runtime state.
Returned values are deep copies so callers cannot modify live state.
"""

from copy import deepcopy

from core.logger import get_logger

from . import state as runtime_state
from .candle_utils import (
    get_live_ema_calculation_mode_text,
    get_now_market_time,
    safe_int,
)
from .constants import (
    DEFAULT_EMA_CROSS_INCLUDE_OPENING_RANGE_LEVELS,
    DEFAULT_ISOLATION_ENABLED,
    DEFAULT_ISOLATION_PRIORITY_LEVELS,
    DEFAULT_ISOLATION_TOUCH_LEVELS,
    DEFAULT_ISOLATION_WINDOW_POINTS,
    DEFAULT_LIVE_EMA_CALCULATION_MODE,
)
from .ema_alerts import (
    get_opening_range_levels_for_ema_event,
    get_selected_or_ema_alerts,
    get_selected_or_instrument_state,
)

logger = get_logger(__file__)


def _normalize_limit(
    value,
    default: int = 100,
) -> int:
    """Returns a safe positive result limit."""
    return max(
        1,
        safe_int(
            value,
            default=default,
        ),
    )


def _get_live_ema_calculation_payload() -> dict:
    """Builds the live EMA calculation status payload."""
    mode = get_live_ema_calculation_mode_text()

    return {
        "flag": DEFAULT_LIVE_EMA_CALCULATION_MODE,
        "mode": mode,
        "description": (
            "live tick/LTP based EMA calculation"
            if DEFAULT_LIVE_EMA_CALCULATION_MODE
            else "completed candle close based EMA calculation"
        ),
    }


def _get_isolation_config_payload(
    isolated_state: dict | None = None,
) -> dict:
    """Builds the isolated-instrument policy payload."""
    if not isinstance(isolated_state, dict):
        isolated_state = runtime_state.get_selected_or_state_snapshot()

    selected = bool(isolated_state.get("selected"))

    locked_for_market_day = bool(
        isolated_state.get(
            "locked_for_market_day",
            selected,
        )
    )

    return {
        "enabled": DEFAULT_ISOLATION_ENABLED,
        "window_points": DEFAULT_ISOLATION_WINDOW_POINTS,
        "touch_levels": list(DEFAULT_ISOLATION_TOUCH_LEVELS),
        "priority_levels": list(DEFAULT_ISOLATION_PRIORITY_LEVELS),
        "selection_policy": ("first_eligible_instrument_locked_for_market_day"),
        "replacement_enabled": False,
        "distance_reselection_enabled": False,
        "priority_upgrade_enabled": False,
        "selected": selected,
        "locked_for_market_day": (locked_for_market_day),
        "reset_policy": "next_market_day",
    }


def _get_runtime_snapshots() -> tuple[
    dict,
    dict,
    dict,
]:
    """
    Returns cache, touch, and isolated-instrument snapshots.
    """
    cache_snapshot = runtime_state.get_opening_range_cache_snapshot()

    touch_snapshot = runtime_state.get_touch_state_snapshot()

    isolated_snapshot = runtime_state.get_selected_or_state_snapshot()

    return (
        cache_snapshot,
        touch_snapshot,
        isolated_snapshot,
    )


def get_opening_range_status() -> dict:
    """
    Returns the latest Opening Range status summary.
    """
    runtime_state.ensure_current_market_day()

    (
        cache_snapshot,
        touch_snapshot,
        isolated_state,
    ) = _get_runtime_snapshots()

    isolated_ema_alerts_count = len(runtime_state.get_selected_or_ema_alerts_snapshot())

    isolated_selected = bool(isolated_state.get("selected"))

    isolated_locked = bool(
        isolated_state.get(
            "locked_for_market_day",
            isolated_selected,
        )
    )

    return {
        "last_run_at": cache_snapshot.get("last_run_at"),
        "date": cache_snapshot.get("date"),
        "status": cache_snapshot.get("status"),
        "message": cache_snapshot.get("message"),
        "source": cache_snapshot.get("source"),
        "interval": cache_snapshot.get("interval"),
        "opening_range_candle_count": (
            cache_snapshot.get("opening_range_candle_count")
        ),
        "market_open_time": cache_snapshot.get("market_open_time"),
        "fetch_time": cache_snapshot.get("fetch_time"),
        "total_instruments": cache_snapshot.get("total_instruments"),
        "success_count": cache_snapshot.get("success_count"),
        "failed_count": cache_snapshot.get("failed_count"),
        "empty_count": cache_snapshot.get("empty_count"),
        "insufficient_data_count": (cache_snapshot.get("insufficient_data_count")),
        "output_file_path": cache_snapshot.get("output_file_path"),
        "latest_main_index_ltp": (touch_snapshot.get("latest_main_index_ltp")),
        "latest_main_index_ltp_source": (
            touch_snapshot.get("latest_main_index_ltp_source")
        ),
        "latest_main_index_ltp_updated_at": (
            touch_snapshot.get("latest_main_index_ltp_updated_at")
        ),
        "touch_events_count": touch_snapshot.get(
            "events_count",
            0,
        ),
        "pending_touch_events_count": (
            touch_snapshot.get(
                "pending_events_count",
                0,
            )
        ),
        "alert_sent_keys_count": (
            touch_snapshot.get(
                "alert_sent_keys_count",
                0,
            )
        ),
        "isolated_instrument": isolated_state,
        "isolated_instrument_selected": (isolated_selected),
        "isolated_instrument_locked_for_market_day": (isolated_locked),
        "isolated_instrument_replacement_enabled": False,
        "isolated_ema_alerts_count": (isolated_ema_alerts_count),
        "ema_cross_include_opening_range_levels": (
            DEFAULT_EMA_CROSS_INCLUDE_OPENING_RANGE_LEVELS
        ),
        "live_ema_calculation": (_get_live_ema_calculation_payload()),
        "isolation_config": (_get_isolation_config_payload(isolated_state)),
        "errors": deepcopy(
            cache_snapshot.get(
                "errors",
                {},
            )
        ),
    }


def get_opening_range_cache() -> dict:
    """
    Returns a deep copy of the complete Opening Range cache.
    """
    runtime_state.ensure_current_market_day()

    runtime_state.synchronize_cache_counters()

    cache_copy = runtime_state.get_opening_range_cache_snapshot()

    isolated_state = runtime_state.get_selected_or_state_snapshot()

    isolated_ema_alerts = runtime_state.get_selected_or_ema_alerts_snapshot()

    live_ema_payload = _get_live_ema_calculation_payload()

    isolated_selected = bool(isolated_state.get("selected"))

    isolated_locked = bool(
        isolated_state.get(
            "locked_for_market_day",
            isolated_selected,
        )
    )

    cache_copy["isolated_instrument"] = isolated_state

    cache_copy["isolated_instrument_selected"] = isolated_selected

    cache_copy["isolated_instrument_locked_for_market_day"] = isolated_locked

    cache_copy["isolated_instrument_replacement_enabled"] = False

    cache_copy["isolated_ema_alerts_count"] = len(isolated_ema_alerts)

    cache_copy["live_ema_calculation_mode_flag"] = DEFAULT_LIVE_EMA_CALCULATION_MODE

    cache_copy["live_ema_calculation_mode"] = live_ema_payload["mode"]

    cache_copy["live_ema_calculation"] = live_ema_payload

    cache_copy["ema_cross_include_opening_range_levels"] = (
        DEFAULT_EMA_CROSS_INCLUDE_OPENING_RANGE_LEVELS
    )

    cache_copy["isolation_config"] = _get_isolation_config_payload(isolated_state)

    return cache_copy


def get_opening_range_dashboard_summary(
    touch_limit: int = 100,
    alert_limit: int = 100,
) -> dict:
    """
    Returns compact data for the isolated EMA dashboard.
    """
    normalized_touch_limit = _normalize_limit(
        touch_limit,
        default=100,
    )

    normalized_alert_limit = _normalize_limit(
        alert_limit,
        default=100,
    )

    runtime_state.ensure_current_market_day()

    generated_at = get_now_market_time()

    cache_snapshot = runtime_state.get_opening_range_cache_snapshot()

    touch_snapshot = runtime_state.get_touch_state_snapshot(
        limit=normalized_touch_limit
    )

    isolated_state = get_selected_or_instrument_state()

    if not isinstance(isolated_state, dict):
        isolated_state = {}

    isolated_ema_alerts = get_selected_or_ema_alerts(limit=normalized_alert_limit)

    if not isinstance(isolated_ema_alerts, list):
        isolated_ema_alerts = []

    isolated_key = isolated_state.get("instrument_key")

    isolated_opening_range_context = {}

    if isolated_key:
        try:
            isolated_opening_range_context = get_opening_range_levels_for_ema_event(
                isolated_key
            )
        except Exception as ex:
            error_message = f"{type(ex).__name__}: {ex}"

            logger.exception(
                "Failed building isolated Opening Range "
                "dashboard context. instrument_key=%s, "
                "error=%s",
                isolated_key,
                error_message,
            )

            isolated_opening_range_context = {
                "status": "error",
                "error": error_message,
            }

    recent_touch_events = deepcopy(
        touch_snapshot.get(
            "events",
            [],
        )
    )

    latest_main_index_ltp = touch_snapshot.get("latest_main_index_ltp")

    latest_main_index_ltp_source = touch_snapshot.get("latest_main_index_ltp_source")

    latest_main_index_ltp_updated_at = touch_snapshot.get(
        "latest_main_index_ltp_updated_at"
    )

    isolated_selected = bool(isolated_state.get("selected"))

    isolated_locked = bool(
        isolated_state.get(
            "locked_for_market_day",
            isolated_selected,
        )
    )

    return {
        "status": "success",
        "generated_at": generated_at.isoformat(),
        "date": generated_at.date().isoformat(),
        "live_ema_calculation": (_get_live_ema_calculation_payload()),
        "opening_range_status": (get_opening_range_status()),
        "isolated_instrument": isolated_state,
        "selected_or_instrument": deepcopy(isolated_state),
        "isolated_instrument_selected": (isolated_selected),
        "isolated_instrument_locked_for_market_day": (isolated_locked),
        "isolated_instrument_replacement_enabled": False,
        "isolation_config": (_get_isolation_config_payload(isolated_state)),
        "isolated_opening_range_context": (isolated_opening_range_context),
        "isolated_ema_alerts_count": len(isolated_ema_alerts),
        "isolated_ema_alerts": (isolated_ema_alerts),
        "recent_touch_events_count": len(recent_touch_events),
        "recent_touch_events": (recent_touch_events),
        "latest_main_index_ltp": (latest_main_index_ltp),
        "latest_main_index_ltp_source": (latest_main_index_ltp_source),
        "latest_main_index_ltp_updated_at": (latest_main_index_ltp_updated_at),
        "cache_summary": {
            "last_run_at": cache_snapshot.get("last_run_at"),
            "date": cache_snapshot.get("date"),
            "status": cache_snapshot.get("status"),
            "message": cache_snapshot.get("message"),
            "source": cache_snapshot.get("source"),
            "interval": cache_snapshot.get("interval"),
            "opening_range_candle_count": (
                cache_snapshot.get("opening_range_candle_count")
            ),
            "total_instruments": (cache_snapshot.get("total_instruments")),
            "success_count": (cache_snapshot.get("success_count")),
            "failed_count": (cache_snapshot.get("failed_count")),
            "empty_count": (cache_snapshot.get("empty_count")),
            "insufficient_data_count": (cache_snapshot.get("insufficient_data_count")),
            "touch_events_count": (
                touch_snapshot.get(
                    "events_count",
                    0,
                )
            ),
            "pending_touch_events_count": (
                touch_snapshot.get(
                    "pending_events_count",
                    0,
                )
            ),
            "alert_sent_keys_count": (
                touch_snapshot.get(
                    "alert_sent_keys_count",
                    0,
                )
            ),
            "output_file_path": (cache_snapshot.get("output_file_path")),
            "isolated_instrument_selected": (isolated_selected),
            "isolated_instrument_locked_for_market_day": (isolated_locked),
            "isolated_instrument_replacement_enabled": False,
        },
    }


def get_opening_range_for_instrument_from_cache(
    instrument_key: str,
) -> dict | None:
    """
    Returns one instrument's cached Opening Range result.
    """
    if instrument_key is None:
        return None

    normalized_instrument_key = str(instrument_key).strip()

    if not normalized_instrument_key:
        return None

    runtime_state.ensure_current_market_day()

    with runtime_state.opening_range_cache_lock:
        cache_data = runtime_state.opening_range_cache.get(
            "data",
            {},
        )

        if not isinstance(cache_data, dict):
            return None

        instrument_result = cache_data.get(normalized_instrument_key)

        if not isinstance(instrument_result, dict):
            return None

        return deepcopy(instrument_result)


def get_opening_range_touch_events(
    limit: int = 100,
) -> list:
    """
    Returns the latest Opening Range touch events.
    """
    normalized_limit = _normalize_limit(
        limit,
        default=100,
    )

    runtime_state.ensure_current_market_day()

    touch_snapshot = runtime_state.get_touch_state_snapshot(limit=normalized_limit)

    return deepcopy(
        touch_snapshot.get(
            "events",
            [],
        )
    )


def get_opening_range_pending_touch_events() -> list:
    """
    Returns pending legacy Telegram touch events.
    """
    runtime_state.ensure_current_market_day()

    touch_snapshot = runtime_state.get_touch_state_snapshot()

    return deepcopy(
        touch_snapshot.get(
            "pending_events",
            [],
        )
    )


__all__ = [
    "get_opening_range_status",
    "get_opening_range_cache",
    "get_opening_range_dashboard_summary",
    "get_opening_range_for_instrument_from_cache",
    "get_opening_range_touch_events",
    "get_opening_range_pending_touch_events",
]
