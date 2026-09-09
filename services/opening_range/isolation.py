from collections import Counter
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
    DEFAULT_LIVE_EMA_CALCULATION_MODE,
    DEFAULT_MAIN_INDEX_KEY,
    DEFAULT_STRIKE_FROM,
    DEFAULT_STRIKE_TO,
    get_isolated_notify_enabled,
    get_isolation_allow_backfill_touch,
    get_isolation_allow_live_touch,
    get_isolation_enabled,
    get_isolation_options_only,
    get_isolation_priority_levels,
    get_isolation_touch_levels,
    get_isolation_window_points,
)

logger = get_logger(__file__)


def _format_numeric_value(
    value: Any,
    unavailable_text: str = "not_available",
) -> str:
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


def _get_short_market_time(timestamp_value: Any) -> str:
    parsed_timestamp = parse_candle_timestamp(timestamp_value)

    if parsed_timestamp is not None:
        return parsed_timestamp.strftime("%H:%M")

    if timestamp_value is None:
        return "not_available"

    text = str(timestamp_value).strip()
    return text if text else "not_available"


def get_level_priority(level: str) -> int:
    level_upper = str(level or "").strip().upper()
    priority_levels = get_isolation_priority_levels()

    try:
        return priority_levels.index(level_upper)
    except ValueError:
        logger.debug(
            "Isolation level does not exist in configured priority list. "
            "level=%s, configured_levels=%s",
            level_upper,
            priority_levels,
        )
        return 999


def get_reference_opening_range_average() -> float | None:
    try:
        with runtime_state.opening_range_cache_lock:
            cache_data = runtime_state.opening_range_cache.get("data", {})

            if not isinstance(cache_data, dict):
                logger.warning(
                    "Opening Range cache data has an invalid type. " "payload_type=%s",
                    type(cache_data).__name__,
                )
                cache_data = {}

            main_item = cache_data.get(DEFAULT_MAIN_INDEX_KEY)

            if isinstance(main_item, dict):
                range_payload = main_item.get("range") or {}

                if isinstance(range_payload, dict):
                    average = range_payload.get("average")

                    if average is not None:
                        average_value = safe_float(average, default=0.0)

                        if average_value > 0:
                            logger.debug(
                                "Reference average resolved from Opening Range "
                                "cache. reference_average=%s",
                                average_value,
                            )
                            return average_value

            cached_latest_ltp = runtime_state.opening_range_cache.get(
                "latest_main_index_ltp"
            )

        if cached_latest_ltp is not None:
            cached_latest_ltp_value = safe_float(
                cached_latest_ltp,
                default=0.0,
            )

            if cached_latest_ltp_value > 0:
                logger.debug(
                    "Reference average resolved from cached main-index LTP. "
                    "reference_average=%s",
                    cached_latest_ltp_value,
                )
                return cached_latest_ltp_value

        latest_ltp = runtime_state.get_latest_main_index_ltp_value()

        if latest_ltp is None or latest_ltp <= 0:
            logger.info(
                "Reference Opening Range average is unavailable. "
                "reason=opening_range_average_and_main_index_ltp_missing"
            )
            return None

        logger.debug(
            "Reference average resolved from latest main-index LTP. "
            "reference_average=%s",
            latest_ltp,
        )
        return latest_ltp

    except Exception:
        logger.exception(
            "Unexpected error while resolving the reference Opening Range " "average."
        )
        return None


def build_average_window(reference_average: float) -> dict:
    normalized_reference_average = safe_float(
        reference_average,
        default=0.0,
    )
    strike_from = safe_float(DEFAULT_STRIKE_FROM, default=0.0)
    strike_to = safe_float(DEFAULT_STRIKE_TO, default=999999.0)

    if strike_from > strike_to:
        logger.warning(
            "Configured strike boundaries are reversed and will be corrected. "
            "configured_from=%s, configured_to=%s",
            strike_from,
            strike_to,
        )
        strike_from, strike_to = strike_to, strike_from

    window_points = safe_float(
        get_isolation_window_points(),
        default=0.0,
    )
    raw_lower = normalized_reference_average - window_points
    raw_upper = normalized_reference_average + window_points
    final_lower = max(strike_from, raw_lower)
    final_upper = min(strike_to, raw_upper)
    valid = final_lower <= final_upper

    average_window = {
        "reference_average": normalized_reference_average,
        "window_points": window_points,
        "raw_lower": raw_lower,
        "raw_upper": raw_upper,
        "configured_from": strike_from,
        "configured_to": strike_to,
        "final_lower": final_lower,
        "final_upper": final_upper,
        "valid": valid,
    }

    if not valid:
        logger.warning(
            "Calculated isolation average window is invalid. "
            "reference_average=%s, window_points=%s, configured_from=%s, "
            "configured_to=%s, final_lower=%s, final_upper=%s",
            normalized_reference_average,
            window_points,
            strike_from,
            strike_to,
            final_lower,
            final_upper,
        )
    else:
        logger.debug(
            "Isolation average window calculated. reference_average=%s, "
            "window_points=%s, final_lower=%s, final_upper=%s",
            normalized_reference_average,
            window_points,
            final_lower,
            final_upper,
        )

    return average_window


def is_event_eligible_for_isolation(event: dict) -> tuple[bool, str]:
    if not get_isolation_enabled():
        return False, "isolation_disabled"

    if not isinstance(event, dict):
        return False, "invalid_event"

    instrument_key = str(event.get("instrument_key") or "").strip()

    if not instrument_key:
        return False, "missing_instrument_key"

    level = str(event.get("level") or "").strip().upper()
    touch_levels = get_isolation_touch_levels()

    if level not in touch_levels:
        return False, "level_not_eligible"

    source = str(event.get("source") or "").strip().lower()

    if source == "intraday_backfill_scan" and not get_isolation_allow_backfill_touch():
        return False, "backfill_selection_disabled"

    if source == "live_tick" and not get_isolation_allow_live_touch():
        return False, "live_selection_disabled"

    contract_info = event.get("contract_info") or {}

    if not isinstance(contract_info, dict):
        return False, "invalid_contract_info"

    if get_isolation_options_only() and not is_option_contract(contract_info):
        return False, "not_option_contract"

    strike = contract_info.get("strike_price")

    if strike is None:
        return False, "missing_strike"

    strike_value = safe_float(strike, default=0.0)

    if strike_value <= 0:
        return False, "invalid_strike"

    reference_average = get_reference_opening_range_average()

    if reference_average is None or reference_average <= 0:
        return False, "reference_average_not_available"

    average_window = build_average_window(reference_average)

    if not average_window.get("valid"):
        return False, "invalid_average_window"

    if not (
        average_window["final_lower"] <= strike_value <= average_window["final_upper"]
    ):
        return False, "strike_outside_average_window"

    return True, "eligible"


def choose_best_isolation_event(events: list) -> dict | None:
    if not isinstance(events, list):
        logger.warning(
            "Isolation candidate selection skipped. "
            "reason=invalid_events_payload, payload_type=%s",
            type(events).__name__,
        )
        return None

    if not events:
        logger.info("Isolation candidate selection skipped. reason=no_touch_events")
        return None

    reference_average = get_reference_opening_range_average()

    if reference_average is None or reference_average <= 0:
        logger.info(
            "Isolation candidate selection skipped. "
            "reason=reference_average_not_available, candidate_count=%s",
            len(events),
        )
        return None

    average_window = build_average_window(reference_average)

    if not average_window.get("valid"):
        logger.warning(
            "Isolation candidate selection skipped. "
            "reason=invalid_average_window, candidate_count=%s, "
            "reference_average=%s, final_lower=%s, final_upper=%s",
            len(events),
            reference_average,
            average_window.get("final_lower"),
            average_window.get("final_upper"),
        )
        return None

    eligible_events = []
    rejection_reasons = Counter()

    for event_index, event in enumerate(events):
        try:
            eligible, reason = is_event_eligible_for_isolation(event)

            if not eligible:
                rejection_reasons[reason] += 1

                event_data = event if isinstance(event, dict) else {}
                contract_info = event_data.get("contract_info") or {}

                if not isinstance(contract_info, dict):
                    contract_info = {}

                logger.debug(
                    "Isolation candidate rejected. reason=%s, "
                    "instrument_key=%s, level=%s, source=%s, strike=%s, "
                    "reference_average=%s, final_lower=%s, final_upper=%s",
                    reason,
                    event_data.get("instrument_key"),
                    event_data.get("level"),
                    event_data.get("source"),
                    contract_info.get("strike_price"),
                    reference_average,
                    average_window.get("final_lower"),
                    average_window.get("final_upper"),
                )
                continue

            contract_info = event.get("contract_info") or {}

            if not isinstance(contract_info, dict):
                contract_info = {}

            strike = safe_float(
                contract_info.get("strike_price"),
                default=0.0,
            )
            level = str(event.get("level") or "").strip().upper()
            parsed_touch_time = parse_candle_timestamp(event.get("touch_time"))
            touch_sort_value = (
                parsed_touch_time.timestamp()
                if parsed_touch_time is not None
                else float(event_index)
            )

            eligible_events.append(
                {
                    "event": event,
                    "priority": get_level_priority(level),
                    "distance_to_average": abs(strike - reference_average),
                    "touch_sort_value": touch_sort_value,
                    "strike": strike,
                }
            )

        except Exception:
            rejection_reasons["candidate_processing_error"] += 1

            logger.exception(
                "Unexpected error while evaluating isolation candidate. "
                "event_index=%s, instrument_key=%s, level=%s",
                event_index,
                event.get("instrument_key") if isinstance(event, dict) else None,
                event.get("level") if isinstance(event, dict) else None,
            )

    if not eligible_events:
        logger.info(
            "No eligible isolation candidate found. candidate_count=%s, "
            "reference_average=%s, final_lower=%s, final_upper=%s, "
            "rejection_summary=%s",
            len(events),
            reference_average,
            average_window.get("final_lower"),
            average_window.get("final_upper"),
            dict(rejection_reasons),
        )
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
    selected_event = selected["event"]

    logger.info(
        "Best isolation candidate selected. instrument_key=%s, level=%s, "
        "strike=%s, priority=%s, distance_to_average=%s, "
        "reference_average=%s, eligible_count=%s, candidate_count=%s, "
        "rejection_summary=%s",
        selected_event.get("instrument_key"),
        selected_event.get("level"),
        selected.get("strike"),
        selected.get("priority"),
        selected.get("distance_to_average"),
        reference_average,
        len(eligible_events),
        len(events),
        dict(rejection_reasons),
    )

    return selected_event


def should_replace_isolated_instrument(new_event: dict) -> bool:
    if not isinstance(new_event, dict):
        logger.warning(
            "Isolation replacement check failed. "
            "reason=invalid_event, payload_type=%s",
            type(new_event).__name__,
        )
        return False

    runtime_state.ensure_current_market_day()

    new_instrument_key = str(new_event.get("instrument_key") or "").strip()

    if not new_instrument_key:
        logger.warning(
            "Isolation replacement check failed. "
            "reason=missing_instrument_key, level=%s",
            new_event.get("level"),
        )
        return False

    with runtime_state.selected_or_lock:
        current_state = deepcopy(runtime_state.selected_or_instrument_state)

    if not current_state.get("selected"):
        logger.info(
            "Isolation selection allowed. reason=no_existing_selection, "
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


def format_isolated_instrument_title(selected_state: dict) -> str:
    if not isinstance(selected_state, dict):
        return "Opening Range Instrument Isolated"

    contract_info = selected_state.get("contract_info") or {}

    if not isinstance(contract_info, dict):
        contract_info = {}

    strike = _format_numeric_value(
        contract_info.get("strike_price"),
        unavailable_text="N/A",
    )
    option_type = normalize_option_type(
        contract_info.get("instrument_type") or contract_info.get("option_type")
    )
    option_type_text = option_type if option_type else "N/A"
    level = selected_state.get("selected_level") or "N/A"

    return f"{strike} {option_type_text} isolated after {level} touch"


def send_isolated_instrument_notification(
    selected_state: dict,
) -> bool:
    if not isinstance(selected_state, dict):
        logger.warning(
            "Isolated-instrument Telegram notification skipped. "
            "reason=invalid_selected_state, payload_type=%s",
            type(selected_state).__name__,
        )
        return False

    instrument_key = selected_state.get("instrument_key")

    if not get_isolated_notify_enabled():
        logger.info(
            "Isolated-instrument Telegram notification skipped. "
            "reason=notification_disabled, instrument_key=%s",
            instrument_key,
        )
        return False

    contract_info = selected_state.get("contract_info") or {}

    if not isinstance(contract_info, dict):
        logger.warning(
            "Selected instrument contains invalid contract information. "
            "instrument_key=%s, payload_type=%s",
            instrument_key,
            type(contract_info).__name__,
        )
        contract_info = {}

    strike = _format_numeric_value(
        contract_info.get("strike_price"),
        unavailable_text="N/A",
    )
    option_type = normalize_option_type(
        contract_info.get("instrument_type") or contract_info.get("option_type")
    )
    option_type_text = option_type if option_type else "N/A"
    selected_level = selected_state.get("selected_level") or "N/A"
    trigger_price = _format_numeric_value(
        selected_state.get("trigger_price"),
        unavailable_text="N/A",
    )
    level_value = _format_numeric_value(
        selected_state.get("level_value"),
        unavailable_text="N/A",
    )
    nifty_ltp = _format_numeric_value(
        selected_state.get("latest_main_index_ltp"),
        unavailable_text="not_available",
    )
    short_touch_time = _get_short_market_time(selected_state.get("touch_time"))

    message = (
        f"{strike} {option_type_text} selected after "
        f"{selected_level} touch\n\n"
        f"Touch Price: {trigger_price}\n"
        f"Level Value: {level_value}\n"
        f"NIFTY: {nifty_ltp}\n"
        f"Touch Time: {short_touch_time}\n\n"
        "EMA Telegram alerts will now be sent only for this instrument.\n"
        "The instrument is locked for the market day."
    )

    try:
        notification_sent = bool(
            telegram_service.send_message(
                title="Opening Range Instrument Isolated",
                message=message,
                level="OPENING_RANGE",
            )
        )

        if notification_sent:
            logger.info(
                "Isolated-instrument Telegram notification delivered. "
                "instrument_key=%s, level=%s, strike=%s, option_type=%s",
                instrument_key,
                selected_level,
                strike,
                option_type_text,
            )
        else:
            logger.warning(
                "Isolated-instrument Telegram notification was not "
                "delivered. instrument_key=%s, level=%s",
                instrument_key,
                selected_level,
            )

        return notification_sent

    except Exception:
        logger.exception(
            "Failed sending isolated-instrument Telegram notification. "
            "instrument_key=%s, level=%s",
            instrument_key,
            selected_level,
        )
        return False


def isolate_instrument_from_event(event: dict) -> bool:
    if not isinstance(event, dict):
        logger.warning(
            "Isolation commit skipped. reason=invalid_event, " "payload_type=%s",
            type(event).__name__,
        )
        return False

    if not event:
        logger.info("Isolation commit skipped. reason=empty_event")
        return False

    instrument_key = str(event.get("instrument_key") or "").strip()
    level = str(event.get("level") or "").strip().upper()

    logger.info(
        "Isolation commit started. instrument_key=%s, level=%s, " "source=%s",
        instrument_key or None,
        level or None,
        event.get("source"),
    )

    try:
        runtime_state.ensure_current_market_day()

        eligible, reason = is_event_eligible_for_isolation(event)

        if not eligible:
            logger.info(
                "Isolation commit skipped. reason=%s, " "instrument_key=%s, level=%s",
                reason,
                instrument_key or None,
                level or None,
            )
            return False

        if not should_replace_isolated_instrument(event):
            logger.info(
                "Isolation commit skipped. "
                "reason=instrument_already_selected_for_market_day, "
                "instrument_key=%s, level=%s",
                instrument_key,
                level,
            )
            return False

        if not instrument_key:
            logger.warning(
                "Isolation commit skipped. " "reason=missing_instrument_key, level=%s",
                level or None,
            )
            return False

        contract_info = event.get("contract_info") or {}

        if not isinstance(contract_info, dict):
            logger.warning(
                "Event contract information has an invalid type. "
                "instrument_key=%s, payload_type=%s",
                instrument_key,
                type(contract_info).__name__,
            )
            contract_info = {}

        if not contract_info:
            logger.info(
                "Contract information missing from touch event. "
                "Resolving from runtime cache. instrument_key=%s",
                instrument_key,
            )
            contract_info = get_contract_info_by_key(instrument_key)

        if not isinstance(contract_info, dict):
            logger.warning(
                "Contract information lookup returned an invalid type. "
                "instrument_key=%s, payload_type=%s",
                instrument_key,
                type(contract_info).__name__,
            )
            contract_info = {}

        contract_info = deepcopy(contract_info)
        reference_average = get_reference_opening_range_average()

        if reference_average is None or reference_average <= 0:
            logger.info(
                "Isolation commit skipped. "
                "reason=reference_average_not_available, "
                "instrument_key=%s, level=%s",
                instrument_key,
                level,
            )
            return False

        average_window = build_average_window(reference_average)

        if not average_window.get("valid"):
            logger.warning(
                "Isolation commit skipped. "
                "reason=invalid_average_window, instrument_key=%s, "
                "level=%s, reference_average=%s",
                instrument_key,
                level,
                reference_average,
            )
            return False

        with runtime_state.opening_range_cache_lock:
            cache_data = runtime_state.opening_range_cache.get(
                "data",
                {},
            )

            if not isinstance(cache_data, dict):
                logger.warning(
                    "Opening Range cache data has an invalid type during "
                    "isolation commit. payload_type=%s",
                    type(cache_data).__name__,
                )
                cache_data = {}

            cached_item = cache_data.get(instrument_key, {})
            item = deepcopy(cached_item) if isinstance(cached_item, dict) else {}

        latest_instrument_snapshot = runtime_state.get_latest_instrument_ltp_snapshot(
            instrument_key
        )

        if not isinstance(latest_instrument_snapshot, dict):
            logger.warning(
                "Latest instrument snapshot has an invalid type. "
                "instrument_key=%s, payload_type=%s",
                instrument_key,
                type(latest_instrument_snapshot).__name__,
            )
            latest_instrument_snapshot = {}

        latest_main_index_ltp = runtime_state.get_latest_main_index_ltp_value()
        selected_at = get_now_market_time().isoformat()

        new_selected_state = {
            "selected": True,
            "instrument_key": instrument_key,
            "selected_level": level,
            "level_value": event.get("level_value"),
            "trigger_price": event.get("trigger_price"),
            "trigger_field": event.get("trigger_field"),
            "touch_time": event.get("touch_time"),
            "touch_source": event.get("source"),
            "selected_at": selected_at,
            "selection_priority": get_level_priority(level),
            "selection_reason": (
                "initial_level_priority_nearest_to_" "opening_range_average_daily_lock"
            ),
            "locked_for_market_day": True,
            "reference_average": reference_average,
            "average_window": average_window,
            "contract_info": contract_info,
            "range": deepcopy(item.get("range")),
            "levels": deepcopy(item.get("levels")),
            "latest_live_data": {
                "ltp": latest_instrument_snapshot.get("ltp"),
                "updated_at": latest_instrument_snapshot.get("updated_at"),
            },
            "latest_main_index_ltp": latest_main_index_ltp,
            "live_ema_calculation_mode_flag": (DEFAULT_LIVE_EMA_CALCULATION_MODE),
            "live_ema_calculation_mode": (get_live_ema_calculation_mode_text()),
            "ema_alerts_count": 0,
            "last_ema_alert": None,
            "disabled": False,
            "message": (
                "Opening Range instrument isolated and locked for EMA "
                "Telegram alerts for the market day."
            ),
        }

        with runtime_state.selected_or_lock:
            if runtime_state.selected_or_instrument_state.get("selected"):
                logger.info(
                    "Isolation state update blocked. "
                    "reason=instrument_selected_by_another_event, "
                    "current_instrument_key=%s, "
                    "current_level=%s, new_instrument_key=%s, "
                    "new_level=%s",
                    runtime_state.selected_or_instrument_state.get("instrument_key"),
                    runtime_state.selected_or_instrument_state.get("selected_level"),
                    instrument_key,
                    level,
                )
                return False

            runtime_state.selected_or_instrument_state.clear()
            runtime_state.selected_or_instrument_state.update(new_selected_state)
            selected_state_snapshot = deepcopy(
                runtime_state.selected_or_instrument_state
            )

        with runtime_state.opening_range_cache_lock:
            runtime_state.opening_range_cache["isolated_instrument"] = (
                selected_state_snapshot
            )
            runtime_state.opening_range_cache["isolated_instrument_selected"] = True
            runtime_state.opening_range_cache["isolated_instrument_selected_at"] = (
                selected_at
            )
            runtime_state.opening_range_cache[
                "isolated_instrument_selection_reason"
            ] = new_selected_state.get("selection_reason")
            runtime_state.opening_range_cache["isolated_ema_alerts_count"] = len(
                runtime_state.selected_or_ema_alerts
            )

        option_type = normalize_option_type(
            contract_info.get("instrument_type") or contract_info.get("option_type")
        ) or contract_info.get("instrument_type")

        logger.info(
            "Opening Range instrument isolated and locked for market day. "
            "instrument_key=%s, level=%s, strike=%s, type=%s, "
            "reference_average=%s, window_points=%s, "
            "final_lower=%s, final_upper=%s, selected_at=%s",
            instrument_key,
            level,
            contract_info.get("strike_price"),
            option_type,
            reference_average,
            average_window.get("window_points"),
            average_window.get("final_lower"),
            average_window.get("final_upper"),
            selected_at,
        )

        notification_enabled = get_isolated_notify_enabled()
        notification_sent = send_isolated_instrument_notification(
            selected_state_snapshot
        )

        if notification_enabled and not notification_sent:
            logger.warning(
                "Instrument was isolated but its Telegram notification "
                "was not delivered. instrument_key=%s, level=%s",
                instrument_key,
                level,
            )

        logger.info(
            "Isolation commit completed. instrument_key=%s, level=%s, "
            "isolated=true, notification_enabled=%s, "
            "notification_sent=%s",
            instrument_key,
            level,
            notification_enabled,
            notification_sent,
        )

        return True

    except Exception:
        logger.exception(
            "Unexpected isolation commit failure. instrument_key=%s, "
            "level=%s, source=%s",
            instrument_key or None,
            level or None,
            event.get("source"),
        )
        return False


def try_isolate_from_touch_events(events: list) -> bool:
    event_count = len(events) if isinstance(events, list) else None

    logger.info(
        "Isolation event processing started. payload_type=%s, "
        "event_count=%s, isolation_enabled=%s",
        type(events).__name__,
        event_count,
        get_isolation_enabled(),
    )

    if not isinstance(events, list):
        logger.warning(
            "Isolation event processing stopped. "
            "reason=invalid_events_payload, payload_type=%s",
            type(events).__name__,
        )
        return False

    if not events:
        logger.info("Isolation event processing stopped. reason=no_touch_events")
        return False

    if not get_isolation_enabled():
        logger.info(
            "Isolation event processing stopped. "
            "reason=isolation_disabled, event_count=%s",
            len(events),
        )
        return False

    try:
        runtime_state.ensure_current_market_day()

        with runtime_state.selected_or_lock:
            current_selected = bool(
                runtime_state.selected_or_instrument_state.get("selected")
            )
            current_instrument_key = runtime_state.selected_or_instrument_state.get(
                "instrument_key"
            )
            current_level = runtime_state.selected_or_instrument_state.get(
                "selected_level"
            )

        if current_selected:
            logger.info(
                "Isolation event processing stopped. "
                "reason=instrument_already_selected_for_market_day, "
                "instrument_key=%s, level=%s, event_count=%s",
                current_instrument_key,
                current_level,
                len(events),
            )
            return False

        best_event = choose_best_isolation_event(events)

        if not best_event:
            logger.info(
                "Isolation event processing completed. "
                "result=no_selection, reason=no_eligible_touch_event, "
                "event_count=%s",
                len(events),
            )
            return False

        logger.info(
            "Isolation candidate will be committed. instrument_key=%s, "
            "level=%s, source=%s",
            best_event.get("instrument_key"),
            best_event.get("level"),
            best_event.get("source"),
        )

        isolated = isolate_instrument_from_event(best_event)

        logger.info(
            "Isolation event processing completed. result=%s, "
            "instrument_key=%s, level=%s, event_count=%s",
            "isolated" if isolated else "not_isolated",
            best_event.get("instrument_key"),
            best_event.get("level"),
            len(events),
        )

        return isolated

    except Exception:
        logger.exception(
            "Unexpected isolation event processing failure. " "event_count=%s",
            len(events),
        )
        return False


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
