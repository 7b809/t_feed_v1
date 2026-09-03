"""
Isolated Opening Range instrument selection.

This module selects one eligible option instrument after an Opening
Range level touch. Once selected, the instrument remains locked for
the current trading day. A new instrument can be selected only after
the daily runtime-state reset.

All mutable runtime data is stored in state.py.
"""

from copy import deepcopy
from typing import Any

from core.logger import get_logger
from services.telegram_service import telegram_service

from . import state as runtime_state
from .candle_utils import (
    get_contract_info_by_key,
    get_live_ema_calculation_mode_text,
    get_now_market_time,
    is_option_contract,
    normalize_option_type,
    parse_candle_timestamp,
    safe_float,
)
from .constants import (
    DEFAULT_ISOLATED_NOTIFY_ENABLED,
    DEFAULT_ISOLATION_ALLOW_BACKFILL_TOUCH,
    DEFAULT_ISOLATION_ALLOW_LIVE_TOUCH,
    DEFAULT_ISOLATION_ENABLED,
    DEFAULT_ISOLATION_OPTIONS_ONLY,
    DEFAULT_ISOLATION_PRIORITY_LEVELS,
    DEFAULT_ISOLATION_TOUCH_LEVELS,
    DEFAULT_ISOLATION_WINDOW_POINTS,
    DEFAULT_LIVE_EMA_CALCULATION_MODE,
    DEFAULT_MAIN_INDEX_KEY,
    DEFAULT_STRIKE_FROM,
    DEFAULT_STRIKE_TO,
)

logger = get_logger(__file__)


def _format_numeric_value(
    value: Any,
    unavailable_text: str = "not_available",
) -> str:
    """Formats a numeric value without unnecessary trailing zeros."""
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


def _get_short_market_time(
    timestamp_value: Any,
) -> str:
    """Returns a short HH:MM market-time representation."""
    parsed_timestamp = parse_candle_timestamp(timestamp_value)

    if parsed_timestamp is not None:
        return parsed_timestamp.strftime("%H:%M")

    if timestamp_value is None:
        return "not_available"

    text = str(timestamp_value).strip()

    return text if text else "not_available"


def get_level_priority(level: str) -> int:
    """
    Returns the configured priority rank for a touched level.

    A lower numeric value means a higher priority.
    """
    level_upper = str(level or "").strip().upper()

    try:
        return DEFAULT_ISOLATION_PRIORITY_LEVELS.index(
            level_upper
        )
    except ValueError:
        return 999


def get_reference_opening_range_average() -> float | None:
    """
    Returns the reference value used for initial instrument selection.
    """
    with runtime_state.opening_range_cache_lock:
        cache_data = runtime_state.opening_range_cache.get(
            "data",
            {},
        )

        if not isinstance(cache_data, dict):
            cache_data = {}

        main_item = cache_data.get(
            DEFAULT_MAIN_INDEX_KEY
        )

        if isinstance(main_item, dict):
            range_payload = main_item.get("range") or {}

            if isinstance(range_payload, dict):
                average = range_payload.get("average")

                if average is not None:
                    average_value = safe_float(
                        average,
                        default=0.0,
                    )

                    if average_value > 0:
                        return average_value

        cached_latest_ltp = (
            runtime_state.opening_range_cache.get(
                "latest_main_index_ltp"
            )
        )

    if cached_latest_ltp is not None:
        cached_latest_ltp_value = safe_float(
            cached_latest_ltp,
            default=0.0,
        )

        if cached_latest_ltp_value > 0:
            return cached_latest_ltp_value

    latest_ltp = (
        runtime_state.get_latest_main_index_ltp_value()
    )

    if latest_ltp is None or latest_ltp <= 0:
        return None

    return latest_ltp


def build_average_window(
    reference_average: float,
) -> dict:
    """
    Builds the strike-selection window around the reference average.
    """
    normalized_reference_average = safe_float(
        reference_average,
        default=0.0,
    )

    strike_from = safe_float(
        DEFAULT_STRIKE_FROM,
        default=0.0,
    )

    strike_to = safe_float(
        DEFAULT_STRIKE_TO,
        default=999999.0,
    )

    if strike_from > strike_to:
        strike_from, strike_to = (
            strike_to,
            strike_from,
        )

    raw_lower = (
        normalized_reference_average
        - DEFAULT_ISOLATION_WINDOW_POINTS
    )

    raw_upper = (
        normalized_reference_average
        + DEFAULT_ISOLATION_WINDOW_POINTS
    )

    final_lower = max(
        strike_from,
        raw_lower,
    )

    final_upper = min(
        strike_to,
        raw_upper,
    )

    return {
        "reference_average": normalized_reference_average,
        "window_points": DEFAULT_ISOLATION_WINDOW_POINTS,
        "raw_lower": raw_lower,
        "raw_upper": raw_upper,
        "configured_from": strike_from,
        "configured_to": strike_to,
        "final_lower": final_lower,
        "final_upper": final_upper,
        "valid": final_lower <= final_upper,
    }


def is_event_eligible_for_isolation(
    event: dict,
) -> tuple[bool, str]:
    """
    Checks whether a touch event is eligible for initial isolation.
    """
    if not DEFAULT_ISOLATION_ENABLED:
        return False, "isolation_disabled"

    if not isinstance(event, dict):
        return False, "invalid_event"

    instrument_key = str(
        event.get("instrument_key") or ""
    ).strip()

    if not instrument_key:
        return False, "missing_instrument_key"

    level = str(
        event.get("level") or ""
    ).strip().upper()

    if level not in DEFAULT_ISOLATION_TOUCH_LEVELS:
        return False, "level_not_eligible"

    source = str(
        event.get("source") or ""
    ).strip().lower()

    if (
        source == "intraday_backfill_scan"
        and not DEFAULT_ISOLATION_ALLOW_BACKFILL_TOUCH
    ):
        return False, "backfill_selection_disabled"

    if (
        source == "live_tick"
        and not DEFAULT_ISOLATION_ALLOW_LIVE_TOUCH
    ):
        return False, "live_selection_disabled"

    contract_info = event.get("contract_info") or {}

    if not isinstance(contract_info, dict):
        contract_info = {}

    if (
        DEFAULT_ISOLATION_OPTIONS_ONLY
        and not is_option_contract(contract_info)
    ):
        return False, "not_option_contract"

    strike = contract_info.get("strike_price")

    if strike is None:
        return False, "missing_strike"

    strike_value = safe_float(
        strike,
        default=0.0,
    )

    if strike_value <= 0:
        return False, "invalid_strike"

    reference_average = (
        get_reference_opening_range_average()
    )

    if (
        reference_average is None
        or reference_average <= 0
    ):
        return False, "reference_average_not_available"

    average_window = build_average_window(
        reference_average
    )

    if not average_window.get("valid"):
        return False, "invalid_average_window"

    if not (
        average_window["final_lower"]
        <= strike_value
        <= average_window["final_upper"]
    ):
        return False, "strike_outside_average_window"

    return True, "eligible"


def choose_best_isolation_event(
    events: list,
) -> dict | None:
    """
    Chooses the best eligible event for initial isolation.

    Events are ordered by configured level priority, distance from
    the Opening Range average, touch time, and strike.
    """
    if not isinstance(events, list) or not events:
        return None

    reference_average = (
        get_reference_opening_range_average()
    )

    if (
        reference_average is None
        or reference_average <= 0
    ):
        return None

    eligible_events = []

    for event_index, event in enumerate(events):
        eligible, reason = (
            is_event_eligible_for_isolation(event)
        )

        if not eligible:
            logger.debug(
                "Isolation candidate rejected. "
                "reason=%s, instrument_key=%s, level=%s",
                reason,
                (
                    event.get("instrument_key")
                    if isinstance(event, dict)
                    else None
                ),
                (
                    event.get("level")
                    if isinstance(event, dict)
                    else None
                ),
            )
            continue

        contract_info = event.get(
            "contract_info"
        ) or {}

        if not isinstance(contract_info, dict):
            contract_info = {}

        strike = safe_float(
            contract_info.get("strike_price"),
            default=0.0,
        )

        level = str(
            event.get("level") or ""
        ).strip().upper()

        parsed_touch_time = parse_candle_timestamp(
            event.get("touch_time")
        )

        touch_sort_value = (
            parsed_touch_time.timestamp()
            if parsed_touch_time is not None
            else float(event_index)
        )

        eligible_events.append(
            {
                "event": event,
                "priority": get_level_priority(level),
                "distance_to_average": abs(
                    strike - reference_average
                ),
                "touch_sort_value": touch_sort_value,
                "strike": strike,
            }
        )

    if not eligible_events:
        return None

    selected = min(
        eligible_events,
        key=lambda item: (
            item["priority"],
            item["distance_to_average"],
            item["touch_sort_value"],
            item["strike"],
        ),
    )

    return selected["event"]


def should_replace_isolated_instrument(
    new_event: dict,
) -> bool:
    """
    Allows selection only when no instrument is selected for the
    current trading day.

    The function name is retained for compatibility. An existing
    isolated instrument is never replaced during the trading day.
    """
    if not isinstance(new_event, dict):
        return False

    runtime_state.ensure_current_market_day()

    new_instrument_key = str(
        new_event.get("instrument_key") or ""
    ).strip()

    if not new_instrument_key:
        return False

    with runtime_state.selected_or_lock:
        current_state = deepcopy(
            runtime_state.selected_or_instrument_state
        )

    if not current_state.get("selected"):
        logger.info(
            "Isolation selection allowed. "
            "reason=no_existing_selection, "
            "new_instrument_key=%s, new_level=%s",
            new_instrument_key,
            new_event.get("level"),
        )
        return True

    logger.info(
        "Isolation selection blocked. "
        "reason=instrument_already_locked_for_market_day, "
        "current_instrument_key=%s, current_level=%s, "
        "new_instrument_key=%s, new_level=%s",
        current_state.get("instrument_key"),
        current_state.get("selected_level"),
        new_instrument_key,
        new_event.get("level"),
    )

    return False


def format_isolated_instrument_title(
    selected_state: dict,
) -> str:
    """Builds the isolated-instrument notification title."""
    if not isinstance(selected_state, dict):
        return "Opening Range Instrument Isolated"

    contract_info = selected_state.get(
        "contract_info"
    ) or {}

    if not isinstance(contract_info, dict):
        contract_info = {}

    strike = _format_numeric_value(
        contract_info.get("strike_price"),
        unavailable_text="N/A",
    )

    option_type = normalize_option_type(
        contract_info.get("instrument_type")
        or contract_info.get("option_type")
    )

    option_type_text = (
        option_type if option_type else "N/A"
    )

    level = (
        selected_state.get("selected_level")
        or "N/A"
    )

    return (
        f"{strike} {option_type_text} "
        f"isolated after {level} touch"
    )


def send_isolated_instrument_notification(
    selected_state: dict,
) -> bool:
    """
    Sends a Telegram notification after initial isolation.
    """
    if not DEFAULT_ISOLATED_NOTIFY_ENABLED:
        return False

    if not isinstance(selected_state, dict):
        return False

    contract_info = selected_state.get(
        "contract_info"
    ) or {}

    if not isinstance(contract_info, dict):
        contract_info = {}

    strike = _format_numeric_value(
        contract_info.get("strike_price"),
        unavailable_text="N/A",
    )

    option_type = normalize_option_type(
        contract_info.get("instrument_type")
        or contract_info.get("option_type")
    )

    option_type_text = (
        option_type if option_type else "N/A"
    )

    selected_level = (
        selected_state.get("selected_level")
        or "N/A"
    )

    trigger_price = _format_numeric_value(
        selected_state.get("trigger_price"),
        unavailable_text="N/A",
    )

    level_value = _format_numeric_value(
        selected_state.get("level_value"),
        unavailable_text="N/A",
    )

    nifty_ltp = _format_numeric_value(
        selected_state.get(
            "latest_main_index_ltp"
        ),
        unavailable_text="not_available",
    )

    short_touch_time = _get_short_market_time(
        selected_state.get("touch_time")
    )

    message = (
        f"{strike} {option_type_text} selected after "
        f"{selected_level} touch\n\n"
        f"Touch Price: {trigger_price}\n"
        f"Level Value: {level_value}\n"
        f"NIFTY: {nifty_ltp}\n"
        f"Touch Time: {short_touch_time}\n\n"
        f"EMA Telegram alerts will now be sent only "
        f"for this instrument.\n"
        f"The instrument is locked for the market day."
    )

    try:
        return bool(
            telegram_service.send_message(
                title=(
                    "Opening Range Instrument Isolated"
                ),
                message=message,
                level="OPENING_RANGE",
            )
        )
    except Exception as ex:
        logger.error(
            "Failed sending isolated-instrument Telegram "
            "notification. instrument_key=%s, error=%s: %s",
            selected_state.get("instrument_key"),
            type(ex).__name__,
            ex,
        )
        return False


def isolate_instrument_from_event(
    event: dict,
) -> bool:
    """
    Selects and locks one instrument from an eligible touch event.

    Selection is allowed only when no instrument is already selected
    for the current trading day.
    """
    if not isinstance(event, dict) or not event:
        return False

    runtime_state.ensure_current_market_day()

    eligible, reason = (
        is_event_eligible_for_isolation(event)
    )

    if not eligible:
        logger.info(
            "Isolation skipped. reason=%s, "
            "instrument_key=%s, level=%s",
            reason,
            event.get("instrument_key"),
            event.get("level"),
        )
        return False

    if not should_replace_isolated_instrument(event):
        logger.info(
            "Isolation selection skipped. "
            "instrument_key=%s, level=%s, "
            "reason=instrument_already_selected_for_market_day",
            event.get("instrument_key"),
            event.get("level"),
        )
        return False

    instrument_key = str(
        event.get("instrument_key") or ""
    ).strip()

    if not instrument_key:
        return False

    contract_info = event.get("contract_info") or {}

    if not isinstance(contract_info, dict):
        contract_info = {}

    if not contract_info:
        contract_info = get_contract_info_by_key(
            instrument_key
        )

    if not isinstance(contract_info, dict):
        contract_info = {}

    contract_info = deepcopy(contract_info)

    reference_average = (
        get_reference_opening_range_average()
    )

    average_window = (
        build_average_window(reference_average)
        if reference_average is not None
        else None
    )

    with runtime_state.opening_range_cache_lock:
        cache_data = (
            runtime_state.opening_range_cache.get(
                "data",
                {},
            )
        )

        if not isinstance(cache_data, dict):
            cache_data = {}

        cached_item = cache_data.get(
            instrument_key,
            {},
        )

        item = (
            deepcopy(cached_item)
            if isinstance(cached_item, dict)
            else {}
        )

    latest_instrument_snapshot = (
        runtime_state.get_latest_instrument_ltp_snapshot(
            instrument_key
        )
    )

    if not isinstance(
        latest_instrument_snapshot,
        dict,
    ):
        latest_instrument_snapshot = {}

    latest_main_index_ltp = (
        runtime_state.get_latest_main_index_ltp_value()
    )

    selected_at = get_now_market_time().isoformat()

    new_selected_state = {
        "selected": True,
        "instrument_key": instrument_key,
        "selected_level": str(
            event.get("level") or ""
        ).strip().upper(),
        "level_value": event.get("level_value"),
        "trigger_price": event.get("trigger_price"),
        "trigger_field": event.get("trigger_field"),
        "touch_time": event.get("touch_time"),
        "touch_source": event.get("source"),
        "selected_at": selected_at,
        "selection_priority": get_level_priority(
            event.get("level")
        ),
        "selection_reason": (
            "initial_level_priority_nearest_to_"
            "opening_range_average_daily_lock"
        ),
        "locked_for_market_day": True,
        "reference_average": reference_average,
        "average_window": average_window,
        "contract_info": contract_info,
        "range": deepcopy(item.get("range")),
        "levels": deepcopy(item.get("levels")),
        "latest_live_data": {
            "ltp": latest_instrument_snapshot.get(
                "ltp"
            ),
            "updated_at": (
                latest_instrument_snapshot.get(
                    "updated_at"
                )
            ),
        },
        "latest_main_index_ltp": (
            latest_main_index_ltp
        ),
        "live_ema_calculation_mode_flag": (
            DEFAULT_LIVE_EMA_CALCULATION_MODE
        ),
        "live_ema_calculation_mode": (
            get_live_ema_calculation_mode_text()
        ),
        "ema_alerts_count": 0,
        "last_ema_alert": None,
        "disabled": False,
        "message": (
            "Opening Range instrument isolated and locked "
            "for EMA Telegram alerts for the market day."
        ),
    }

    with runtime_state.selected_or_lock:
        if runtime_state.selected_or_instrument_state.get(
            "selected"
        ):
            logger.info(
                "Isolation state update blocked. "
                "reason=instrument_selected_by_another_event, "
                "current_instrument_key=%s, "
                "new_instrument_key=%s, new_level=%s",
                (
                    runtime_state
                    .selected_or_instrument_state
                    .get("instrument_key")
                ),
                instrument_key,
                event.get("level"),
            )
            return False

        runtime_state.selected_or_instrument_state.clear()

        runtime_state.selected_or_instrument_state.update(
            new_selected_state
        )

        selected_state_snapshot = deepcopy(
            runtime_state.selected_or_instrument_state
        )

    with runtime_state.opening_range_cache_lock:
        runtime_state.opening_range_cache[
            "isolated_instrument"
        ] = selected_state_snapshot

        runtime_state.opening_range_cache[
            "isolated_instrument_selected"
        ] = True

        runtime_state.opening_range_cache[
            "isolated_instrument_selected_at"
        ] = selected_at

        runtime_state.opening_range_cache[
            "isolated_instrument_selection_reason"
        ] = new_selected_state.get(
            "selection_reason"
        )

        runtime_state.opening_range_cache[
            "isolated_ema_alerts_count"
        ] = len(
            runtime_state.selected_or_ema_alerts
        )

    logger.info(
        "Opening Range isolated instrument selected "
        "and locked for market day. "
        "instrument_key=%s, level=%s, strike=%s, "
        "type=%s, reference_average=%s",
        instrument_key,
        new_selected_state.get("selected_level"),
        contract_info.get("strike_price"),
        (
            normalize_option_type(
                contract_info.get("instrument_type")
                or contract_info.get("option_type")
            )
            or contract_info.get("instrument_type")
        ),
        reference_average,
    )

    notification_sent = (
        send_isolated_instrument_notification(
            selected_state_snapshot
        )
    )

    if (
        DEFAULT_ISOLATED_NOTIFY_ENABLED
        and not notification_sent
    ):
        logger.warning(
            "Instrument was isolated but its Telegram "
            "notification was not delivered. "
            "instrument_key=%s",
            instrument_key,
        )

    return True


def try_isolate_from_touch_events(
    events: list,
) -> bool:
    """
    Chooses the best eligible event and isolates its instrument.

    Returns True only when an instrument is newly selected.
    """
    if not isinstance(events, list) or not events:
        return False

    runtime_state.ensure_current_market_day()

    with runtime_state.selected_or_lock:
        if runtime_state.selected_or_instrument_state.get(
            "selected"
        ):
            logger.debug(
                "Isolation event processing skipped. "
                "reason=instrument_already_selected_for_market_day, "
                "instrument_key=%s",
                (
                    runtime_state
                    .selected_or_instrument_state
                    .get("instrument_key")
                ),
            )
            return False

    best_event = choose_best_isolation_event(events)

    if not best_event:
        return False

    return isolate_instrument_from_event(best_event)


__all__ = [
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
]