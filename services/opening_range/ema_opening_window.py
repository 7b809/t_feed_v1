"""Opening-minute EMA cross qualification and deterministic isolation."""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, time as dt_time
from threading import RLock

from core import config
from core.logger import get_logger
from services.option_service import get_budget_range_order_instruments, options_cache
from services.runtime_config_service import (
    get_budget_max_price,
    get_budget_min_price,
    get_budget_range_enabled,
)
from services.telegram_service import telegram_service

from .candle_utils import get_contract_info_by_key, get_now_market_time, parse_candle_timestamp
from .isolation import (
    build_average_window,
    choose_best_isolation_event,
    get_reference_opening_range_average,
    is_event_eligible_for_isolation,
    isolate_instrument_from_event,
)
from .state import selected_or_instrument_state, selected_or_lock

logger = get_logger(__file__)


class OpeningEmaCrossSelector:
    """Selects at most one bullish EMA candidate during the first 3 candles."""

    WINDOW_START = dt_time(9, 15)
    WINDOW_END = dt_time(9, 18)
    FINAL_CANDLE_START = dt_time(9, 17)

    def __init__(self):
        self.lock = RLock()
        self.market_date: date | None = None
        self.active = False
        self.candidate_selected = False
        self.no_cross_sent = False
        self.reset_for_day(get_now_market_time().date())

    def reset_for_day(self, market_date: date) -> None:
        with self.lock:
            if self.market_date == market_date:
                return
            self.market_date = market_date
            self.active = market_date.weekday() < 5
            self.candidate_selected = False
            self.no_cross_sent = False
        logger.info("Opening EMA selection window reset. date=%s active=%s", market_date, self.active)

    def is_opening_window_event(self, event: dict) -> bool:
        parsed = parse_candle_timestamp(event.get("candle_timestamp") or event.get("timestamp"))
        if parsed is None or parsed.weekday() >= 5:
            return False
        self._ensure_day(parsed.date())
        with self.lock:
            return bool(
                self.active
                and not self.candidate_selected
                and self.WINDOW_START <= parsed.time().replace(tzinfo=None) < self.WINDOW_END
            )

    def _ensure_day(self, market_date: date) -> None:
        if self.market_date != market_date:
            self.reset_for_day(market_date)

    def _budget_qualified_keys(self, candidates: list[dict], reference: float) -> set[str]:
        candidate_keys = {str(item.get("instrument_key") or "") for item in candidates}
        if not get_budget_range_enabled():
            return candidate_keys

        market_data = {
            str(item["instrument_key"]): {
                "live_ltp": item.get("close"),
                "timestamp": item.get("candle_timestamp") or item.get("timestamp"),
            }
            for item in candidates
            if item.get("instrument_key") and item.get("close") is not None
        }
        contract_data = options_cache.get("data", [])
        maximum = max(1, len(contract_data) if isinstance(contract_data, list) else len(candidates))
        qualified: set[str] = set()
        for option_type in ("CE", "PE"):
            budget_items = get_budget_range_order_instruments(
                option_type=option_type,
                ltp_by_instrument=market_data,
                market_data_by_instrument=market_data,
                current_nifty_ltp=reference,
                minimum_price=get_budget_min_price(),
                maximum_price=get_budget_max_price(),
                maximum_instruments=maximum,
                subscribed_only=True,
                sort_mode=getattr(config, "EMA_ALERT_BUDGET_SORT_MODE", "nearest_to_budget_midpoint"),
                inclusive=getattr(config, "EMA_ALERT_BUDGET_RANGE_INCLUSIVE", True),
                enabled=True,
            )
            qualified.update(
                str(item.get("instrument_key"))
                for item in budget_items
                if isinstance(item, dict) and item.get("instrument_key")
            )
        return candidate_keys & qualified

    def process_candle_crosses(self, events: list[dict], dispatch) -> bool:
        """Filter a completed candle's full cross set, isolate, then dispatch."""
        bullish = [
            deepcopy(event)
            for event in events
            if str(event.get("cross_type") or "").lower() == "bullish"
        ]
        if not bullish:
            logger.info("Opening EMA candle had no bullish cross candidates.")
            return False

        reference = get_reference_opening_range_average()
        if reference is None or reference <= 0:
            logger.warning("Opening EMA candidates rejected: OR reference average unavailable.")
            return False
        average_window = build_average_window(reference)
        if not average_window.get("valid"):
            logger.warning("Opening EMA candidates rejected: OR average window invalid.")
            return False

        eligibility_events = []
        original_by_key = {}
        for event in bullish:
            key = str(event.get("instrument_key") or "")
            contract = event.get("contract_info") or get_contract_info_by_key(key)
            candidate = {
                **event,
                "instrument_key": key,
                "level": "EMA_BULLISH_CROSS",
                "level_value": event.get("ema_9"),
                "trigger_price": event.get("close"),
                "touch_time": event.get("candle_timestamp") or event.get("timestamp"),
                "source": "opening_ema_cross",
                "contract_info": contract,
            }
            eligible, reason = is_event_eligible_for_isolation(candidate)
            if eligible:
                eligibility_events.append(candidate)
                original_by_key[key] = event
            else:
                logger.info("Opening EMA OR filter rejected candidate. key=%s reason=%s", key, reason)

        budget_keys = self._budget_qualified_keys(eligibility_events, reference)
        budget_events = [item for item in eligibility_events if item["instrument_key"] in budget_keys]
        logger.info(
            "Opening EMA candidate filters completed. crosses=%s or_eligible=%s budget_eligible=%s window=%s",
            len(bullish), len(eligibility_events), len(budget_events), average_window,
        )
        if not budget_events:
            return False

        best = choose_best_isolation_event(budget_events)
        if not best:
            return False

        key = str(best.get("instrument_key") or "")
        with self.lock:
            if not self.active or self.candidate_selected:
                return self.candidate_selected
            with selected_or_lock:
                current = deepcopy(selected_or_instrument_state)
            if current.get("selected"):
                if str(current.get("instrument_key") or "") != key:
                    logger.info(
                        "Opening EMA selection blocked by existing daily isolation. current=%s candidate=%s",
                        current.get("instrument_key"), key,
                    )
                    return False
                isolated = True
            else:
                isolated = isolate_instrument_from_event(best)
            if not isolated:
                logger.warning("Opening EMA candidate isolation failed. key=%s", key)
                return False

            selected_event = original_by_key.get(key)
            if not selected_event:
                return False
            logger.info("Opening EMA final candidate isolated. key=%s strike=%s", key,
                        (best.get("contract_info") or {}).get("strike_price"))
            dispatch(selected_event)
            self.candidate_selected = True
            self.active = False
            logger.info("Opening EMA candidate dispatched. key=%s", key)
            return True

    def finalize_if_caught_up(self, states: dict, subscribed_keys: list[str]) -> bool:
        """Send one no-qualifier Telegram after all instruments reach candle 3."""
        now = get_now_market_time()
        self._ensure_day(now.date())
        with self.lock:
            if not self.active or self.candidate_selected or self.no_cross_sent:
                return False
            cutoff = datetime.combine(now.date(), self.FINAL_CANDLE_START).replace(tzinfo=now.tzinfo)
            keys = list(dict.fromkeys(str(key) for key in subscribed_keys if key))
            if not keys or any(
                (parse_candle_timestamp((states.get(key) or {}).get("last_processed_timestamp")) or datetime.min.replace(tzinfo=now.tzinfo)) < cutoff
                for key in keys
            ):
                return False
            self.no_cross_sent = True
            self.active = False

        message = (
            "The initial opening EMA search window (09:15–09:18) is complete.\n"
            "No qualifying bullish EMA cross was identified after applying the "
            "configured Opening Range isolation window and budget-range criteria."
        )
        try:
            telegram_service.send_message(
                title="Opening EMA Search Complete",
                message=message,
                level="INFO",
                notification_context=f"opening_ema_no_candidate|date={now.date().isoformat()}",
            )
            logger.info("Opening EMA search expired without qualifying candidate. date=%s", now.date())
        except Exception:
            logger.exception("Final opening EMA no-candidate Telegram failed.")
        return True


opening_ema_cross_selector = OpeningEmaCrossSelector()

