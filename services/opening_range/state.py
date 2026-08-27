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
# Locks
# ============================================================

opening_range_cache_lock = RLock()
touch_lock = RLock()
selected_or_lock = RLock()

_opening_range_cache_lock = opening_range_cache_lock
_touch_lock = touch_lock
_selected_or_lock = selected_or_lock


# ============================================================
# Date Helpers
# ============================================================


def _get_market_timezone() -> ZoneInfo:
    try:
        return ZoneInfo(DEFAULT_MARKET_TIMEZONE)
    except ZoneInfoNotFoundError:
        return ZoneInfo("Asia/Kolkata")


def get_state_market_datetime() -> datetime:
    return datetime.now(_get_market_timezone())


def get_state_market_date() -> str:
    return get_state_market_datetime().date().isoformat()


def normalize_state_date(
    value: Any = None,
) -> str:
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
    normalized_date = (
        normalize_state_date(state_date) if state_date is not None else None
    )

    return {
        "last_run_at": None,
        "date": normalized_date,
        "status": "not_started",
        "message": ("Opening range calculation has not run yet."),
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
        "isolated_ema_telegram_attempts_count": 0,
        "isolated_ema_telegram_success_count": 0,
        "isolated_ema_telegram_failed_count": 0,
        "isolated_ema_algo_attempts_count": 0,
        "isolated_ema_algo_dispatch_count": 0,
        "isolated_ema_algo_failed_count": 0,
        "last_isolated_ema_alert": None,
        "last_telegram_delivery": None,
        "last_algo_app_delivery": None,
        "data": {},
        "touch_events": [],
        "errors": {},
    }


def build_default_selected_or_instrument_state() -> dict:
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
        "telegram_attempts_count": 0,
        "telegram_success_count": 0,
        "telegram_failed_count": 0,
        "algo_app_attempts_count": 0,
        "algo_app_dispatch_count": 0,
        "algo_app_failed_count": 0,
        "last_ema_alert": None,
        "last_telegram_delivery": None,
        "last_algo_app_delivery": None,
        "disabled": False,
        "message": ("No isolated Opening Range instrument " "selected yet."),
    }


# ============================================================
# Opening Range Cache
# ============================================================

opening_range_cache = build_default_opening_range_cache()


# ============================================================
# Touch State
# ============================================================

pending_touch_events = deque()

touch_events = deque(maxlen=DEFAULT_MAX_EVENTS_IN_MEMORY)

alert_sent_keys: set[str] = set()

last_touch_alert_sent_at: float | None = None

_pending_touch_events = pending_touch_events
_touch_events = touch_events
_alert_sent_keys = alert_sent_keys


# ============================================================
# Main Index LTP State
# ============================================================

latest_main_index_ltp: float | None = None
latest_main_index_ltp_source: str | None = None
latest_main_index_ltp_updated_at: str | None = None

_latest_main_index_ltp: float | None = None
_latest_main_index_ltp_source: str | None = None
_latest_main_index_ltp_updated_at: str | None = None


# ============================================================
# Instrument LTP State
# ============================================================

latest_ltp_by_instrument: dict[str, float] = {}

latest_ltp_updated_at_by_instrument: dict[
    str,
    str,
] = {}

_latest_ltp_by_instrument = latest_ltp_by_instrument

_latest_ltp_updated_at_by_instrument = latest_ltp_updated_at_by_instrument


# ============================================================
# Isolated Instrument State
# ============================================================

selected_or_instrument_state = build_default_selected_or_instrument_state()

selected_or_ema_alerts = deque(maxlen=DEFAULT_MAX_EVENTS_IN_MEMORY)

selected_or_ema_alert_minute_keys: set[str] = set()

selected_or_ema_alert_minute_date: str | None = None

_selected_or_instrument_state = selected_or_instrument_state

_selected_or_ema_alerts = selected_or_ema_alerts

_selected_or_ema_alert_minute_keys = selected_or_ema_alert_minute_keys

_selected_or_ema_alert_minute_date: str | None = None


# ============================================================
# Runtime State
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

        _latest_main_index_ltp = value
        _latest_main_index_ltp_source = normalized_source
        _latest_main_index_ltp_updated_at = normalized_updated_at

    with opening_range_cache_lock:
        opening_range_cache["latest_main_index_ltp"] = value

        opening_range_cache["latest_main_index_ltp_source"] = normalized_source

        opening_range_cache["latest_main_index_ltp_updated_at"] = normalized_updated_at

    return True


def get_latest_main_index_ltp_value() -> float | None:
    with touch_lock:
        return latest_main_index_ltp


def get_latest_main_index_ltp_snapshot() -> dict:
    with touch_lock:
        return {
            "instrument_key": DEFAULT_MAIN_INDEX_KEY,
            "ltp": latest_main_index_ltp,
            "source": latest_main_index_ltp_source,
            "updated_at": (latest_main_index_ltp_updated_at),
        }


# ============================================================
# Instrument LTP Helpers
# ============================================================


def set_latest_instrument_ltp(
    instrument_key: str,
    ltp: Any,
    updated_at: str | None = None,
) -> bool:
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
    if not instrument_key:
        return {
            "instrument_key": None,
            "ltp": None,
            "updated_at": None,
        }

    normalized_key = str(instrument_key).strip()

    if not normalized_key:
        return {
            "instrument_key": None,
            "ltp": None,
            "updated_at": None,
        }

    with touch_lock:
        return {
            "instrument_key": normalized_key,
            "ltp": latest_ltp_by_instrument.get(normalized_key),
            "updated_at": (latest_ltp_updated_at_by_instrument.get(normalized_key)),
        }


def get_latest_instrument_ltp_state_snapshot() -> dict:
    with touch_lock:
        return {
            "ltp": deepcopy(latest_ltp_by_instrument),
            "updated_at": deepcopy(latest_ltp_updated_at_by_instrument),
            "count": len(latest_ltp_by_instrument),
        }


# ============================================================
# Snapshot Helpers
# ============================================================


def get_opening_range_cache_snapshot() -> dict:
    with opening_range_cache_lock:
        return deepcopy(opening_range_cache)


def get_touch_state_snapshot(
    limit: int | None = None,
) -> dict:
    with touch_lock:
        touch_event_list = list(touch_events)

        if limit is not None:
            try:
                normalized_limit = max(
                    1,
                    int(limit),
                )

                touch_event_list = touch_event_list[-normalized_limit:]
            except (
                TypeError,
                ValueError,
                OverflowError,
            ):
                pass

        return {
            "events": deepcopy(touch_event_list),
            "events_count": len(touch_events),
            "pending_events": deepcopy(list(pending_touch_events)),
            "pending_events_count": len(pending_touch_events),
            "alert_sent_keys": list(alert_sent_keys),
            "alert_sent_keys_count": len(alert_sent_keys),
            "latest_main_index_ltp": (latest_main_index_ltp),
            "latest_main_index_ltp_source": (latest_main_index_ltp_source),
            "latest_main_index_ltp_updated_at": (latest_main_index_ltp_updated_at),
            "last_touch_alert_sent_at": (last_touch_alert_sent_at),
        }


def get_selected_or_state_snapshot() -> dict:
    with selected_or_lock:
        return deepcopy(selected_or_instrument_state)


def get_selected_or_ema_alerts_snapshot(
    limit: int | None = None,
) -> list:
    with selected_or_lock:
        alerts = list(selected_or_ema_alerts)

        if limit is not None:
            try:
                normalized_limit = max(
                    1,
                    int(limit),
                )

                alerts = alerts[-normalized_limit:]
            except (
                TypeError,
                ValueError,
                OverflowError,
            ):
                pass

        return deepcopy(alerts)


# ============================================================
# EMA Alert State Helpers
# ============================================================


def append_selected_or_ema_alert(
    alert_record: dict,
) -> dict:
    if not isinstance(alert_record, dict):
        return {}

    record = deepcopy(alert_record)

    delivery = record.get("delivery") or {}

    if not isinstance(delivery, dict):
        delivery = {}

    telegram_delivery = delivery.get("telegram") or {}

    algo_delivery = delivery.get("algo_app") or {}

    if not isinstance(
        telegram_delivery,
        dict,
    ):
        telegram_delivery = {}

    if not isinstance(
        algo_delivery,
        dict,
    ):
        algo_delivery = {}

    telegram_attempted = bool(telegram_delivery.get("attempted"))

    telegram_success = bool(telegram_delivery.get("success"))

    algo_attempted = bool(algo_delivery.get("attempted"))

    algo_dispatched = bool(
        algo_delivery.get("dispatched") or algo_delivery.get("success")
    )

    with selected_or_lock:
        selected_or_ema_alerts.append(record)

        current_alert_count = int(
            selected_or_instrument_state.get(
                "ema_alerts_count",
                0,
            )
            or 0
        )

        selected_or_instrument_state["ema_alerts_count"] = current_alert_count + 1

        if telegram_attempted:
            current_count = int(
                selected_or_instrument_state.get(
                    "telegram_attempts_count",
                    0,
                )
                or 0
            )

            selected_or_instrument_state["telegram_attempts_count"] = current_count + 1

            if telegram_success:
                current_count = int(
                    selected_or_instrument_state.get(
                        "telegram_success_count",
                        0,
                    )
                    or 0
                )

                selected_or_instrument_state["telegram_success_count"] = (
                    current_count + 1
                )
            else:
                current_count = int(
                    selected_or_instrument_state.get(
                        "telegram_failed_count",
                        0,
                    )
                    or 0
                )

                selected_or_instrument_state["telegram_failed_count"] = (
                    current_count + 1
                )

            selected_or_instrument_state["last_telegram_delivery"] = deepcopy(
                telegram_delivery
            )

        if algo_attempted:
            current_count = int(
                selected_or_instrument_state.get(
                    "algo_app_attempts_count",
                    0,
                )
                or 0
            )

            selected_or_instrument_state["algo_app_attempts_count"] = current_count + 1

            if algo_dispatched:
                current_count = int(
                    selected_or_instrument_state.get(
                        "algo_app_dispatch_count",
                        0,
                    )
                    or 0
                )

                selected_or_instrument_state["algo_app_dispatch_count"] = (
                    current_count + 1
                )
            else:
                current_count = int(
                    selected_or_instrument_state.get(
                        "algo_app_failed_count",
                        0,
                    )
                    or 0
                )

                selected_or_instrument_state["algo_app_failed_count"] = (
                    current_count + 1
                )

            selected_or_instrument_state["last_algo_app_delivery"] = deepcopy(
                algo_delivery
            )

        selected_or_instrument_state["last_ema_alert"] = record

        selected_state_snapshot = deepcopy(selected_or_instrument_state)

        alert_count = len(selected_or_ema_alerts)

    with opening_range_cache_lock:
        opening_range_cache["isolated_ema_alerts_count"] = alert_count

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

        opening_range_cache["isolated_ema_telegram_attempts_count"] = (
            selected_state_snapshot.get(
                "telegram_attempts_count",
                0,
            )
        )

        opening_range_cache["isolated_ema_telegram_success_count"] = (
            selected_state_snapshot.get(
                "telegram_success_count",
                0,
            )
        )

        opening_range_cache["isolated_ema_telegram_failed_count"] = (
            selected_state_snapshot.get(
                "telegram_failed_count",
                0,
            )
        )

        opening_range_cache["isolated_ema_algo_attempts_count"] = (
            selected_state_snapshot.get(
                "algo_app_attempts_count",
                0,
            )
        )

        opening_range_cache["isolated_ema_algo_dispatch_count"] = (
            selected_state_snapshot.get(
                "algo_app_dispatch_count",
                0,
            )
        )

        opening_range_cache["isolated_ema_algo_failed_count"] = (
            selected_state_snapshot.get(
                "algo_app_failed_count",
                0,
            )
        )

        opening_range_cache["last_isolated_ema_alert"] = record

        opening_range_cache["last_telegram_delivery"] = deepcopy(telegram_delivery)

        opening_range_cache["last_algo_app_delivery"] = deepcopy(algo_delivery)

    return deepcopy(record)


def update_last_algo_app_delivery(
    event_id: str | None,
    delivery_result: dict,
) -> bool:
    if not isinstance(
        delivery_result,
        dict,
    ):
        return False

    normalized_event_id = str(event_id or "").strip()

    delivery_snapshot = deepcopy(delivery_result)

    matched = False

    with selected_or_lock:
        for alert_record in reversed(selected_or_ema_alerts):
            if not isinstance(
                alert_record,
                dict,
            ):
                continue

            record_event_id = str(alert_record.get("event_id") or "").strip()

            if normalized_event_id and record_event_id != normalized_event_id:
                continue

            delivery = alert_record.setdefault(
                "delivery",
                {},
            )

            delivery["algo_app"] = delivery_snapshot

            matched = True
            break

        selected_or_instrument_state["last_algo_app_delivery"] = delivery_snapshot

        last_alert = selected_or_instrument_state.get("last_ema_alert")

        if isinstance(last_alert, dict):
            last_event_id = str(last_alert.get("event_id") or "").strip()

            if not normalized_event_id or last_event_id == normalized_event_id:
                delivery = last_alert.setdefault(
                    "delivery",
                    {},
                )

                delivery["algo_app"] = delivery_snapshot

                matched = True

        selected_state_snapshot = deepcopy(selected_or_instrument_state)

    with opening_range_cache_lock:
        opening_range_cache["last_algo_app_delivery"] = delivery_snapshot

        opening_range_cache["isolated_instrument"] = selected_state_snapshot

        last_cache_alert = opening_range_cache.get("last_isolated_ema_alert")

        if isinstance(
            last_cache_alert,
            dict,
        ):
            last_event_id = str(last_cache_alert.get("event_id") or "").strip()

            if not normalized_event_id or last_event_id == normalized_event_id:
                delivery = last_cache_alert.setdefault(
                    "delivery",
                    {},
                )

                delivery["algo_app"] = delivery_snapshot

    return matched


# ============================================================
# Cache Synchronization
# ============================================================


def synchronize_cache_counters() -> None:
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

        last_alert = (
            deepcopy(selected_or_ema_alerts[-1]) if selected_or_ema_alerts else None
        )

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

        opening_range_cache["isolated_ema_telegram_attempts_count"] = (
            selected_state_snapshot.get(
                "telegram_attempts_count",
                0,
            )
        )

        opening_range_cache["isolated_ema_telegram_success_count"] = (
            selected_state_snapshot.get(
                "telegram_success_count",
                0,
            )
        )

        opening_range_cache["isolated_ema_telegram_failed_count"] = (
            selected_state_snapshot.get(
                "telegram_failed_count",
                0,
            )
        )

        opening_range_cache["isolated_ema_algo_attempts_count"] = (
            selected_state_snapshot.get(
                "algo_app_attempts_count",
                0,
            )
        )

        opening_range_cache["isolated_ema_algo_dispatch_count"] = (
            selected_state_snapshot.get(
                "algo_app_dispatch_count",
                0,
            )
        )

        opening_range_cache["isolated_ema_algo_failed_count"] = (
            selected_state_snapshot.get(
                "algo_app_failed_count",
                0,
            )
        )

        opening_range_cache["last_isolated_ema_alert"] = last_alert

        opening_range_cache["last_telegram_delivery"] = selected_state_snapshot.get(
            "last_telegram_delivery"
        )

        opening_range_cache["last_algo_app_delivery"] = selected_state_snapshot.get(
            "last_algo_app_delivery"
        )


# ============================================================
# Reset Helpers
# ============================================================


def reset_touch_state() -> None:
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
    normalized_date = normalize_state_date(state_date)

    default_cache = build_default_opening_range_cache(state_date=normalized_date)

    with opening_range_cache_lock:
        opening_range_cache.clear()

        opening_range_cache.update(default_cache)


def reset_all_opening_range_state(
    state_date: Any = None,
) -> str:
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
    normalized_date = normalize_state_date(state_date)

    if runtime_state_date == normalized_date:
        return False

    reset_all_opening_range_state(state_date=normalized_date)

    return True


# ============================================================
# EMA Duplicate Key Helpers
# ============================================================


def check_and_reserve_ema_minute_key(
    alert_key: str,
    state_date: Any = None,
) -> bool:
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


def release_ema_minute_key(
    alert_key: str,
) -> None:
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
# Public API
# ============================================================

__all__ = [
    "opening_range_cache_lock",
    "touch_lock",
    "selected_or_lock",
    "opening_range_cache",
    "pending_touch_events",
    "touch_events",
    "alert_sent_keys",
    "last_touch_alert_sent_at",
    "latest_main_index_ltp",
    "latest_main_index_ltp_source",
    "latest_main_index_ltp_updated_at",
    "latest_ltp_by_instrument",
    "latest_ltp_updated_at_by_instrument",
    "selected_or_instrument_state",
    "selected_or_ema_alerts",
    "selected_or_ema_alert_minute_keys",
    "selected_or_ema_alert_minute_date",
    "runtime_state_date",
    "get_state_market_datetime",
    "get_state_market_date",
    "normalize_state_date",
    "build_default_opening_range_cache",
    "build_default_selected_or_instrument_state",
    "set_latest_main_index_ltp",
    "get_latest_main_index_ltp_value",
    "get_latest_main_index_ltp_snapshot",
    "set_latest_instrument_ltp",
    "get_latest_instrument_ltp",
    "get_latest_instrument_ltp_snapshot",
    "get_latest_instrument_ltp_state_snapshot",
    "get_opening_range_cache_snapshot",
    "get_touch_state_snapshot",
    "get_selected_or_state_snapshot",
    "get_selected_or_ema_alerts_snapshot",
    "append_selected_or_ema_alert",
    "update_last_algo_app_delivery",
    "synchronize_cache_counters",
    "reset_touch_state",
    "reset_selected_or_state",
    "reset_opening_range_cache",
    "reset_all_opening_range_state",
    "ensure_current_market_day",
    "check_and_reserve_ema_minute_key",
    "release_ema_minute_key",
]
