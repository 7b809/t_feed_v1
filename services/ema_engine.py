"""Internal completed-candle EMA engine for the option feed application."""

from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, time as dt_time, timedelta
from pathlib import Path
from threading import RLock

from core import config
from core.logger import get_logger
from services.history_service import (
    fetch_intraday_candles_for_instrument,
    get_market_timezone,
)
from services.opening_range_service import (
    get_opening_range_levels_for_ema_event,
    process_selected_or_ema_cross_alert,
)
from services.opening_range.ema_opening_window import get_opening_ema_cross_selector
from services.option_service import get_contract_info_by_instrument_key
from ws_feed.broadcaster import broadcaster

logger = get_logger(__file__)


class InternalEmaEngine:
    """Owns EMA state and processes each completed candle at most once."""

    def __init__(self):
        self.states: dict[str, dict] = {}
        self._instrument_locks: dict[str, RLock] = {}
        self.events: list[dict] = []
        self.lock = RLock()
        self.loop = None
        self.is_running = False
        self.processed_count = 0
        self.skipped_count = 0
        self.crossovers_count = 0
        self.last_error = None
        self.last_poll_minute = None
        self._poll_lock = RLock()

    def initialize_from_history(self, summary: dict | None) -> bool:
        if not isinstance(summary, dict):
            return False
        from services.opening_range.candle_utils import parse_candle_timestamp

        initialized = 0
        with self.lock:
            self.states = {}
            for key, result in (summary.get("results") or {}).items():
                ema = (result or {}).get("ema_result") or {}
                fast, slow = ema.get("latest_ema_fast"), ema.get("latest_ema_slow")
                if fast is None or slow is None:
                    continue
                old = self.states.get(str(key), {})
                last_timestamp = ema.get("latest_timestamp")
                parsed_timestamp = parse_candle_timestamp(last_timestamp)
                if parsed_timestamp is not None:
                    last_timestamp = parsed_timestamp.isoformat()
                self.states[str(key)] = {
                    **old,
                    "instrument_key": str(key),
                    "ema_9": float(fast),
                    "ema_21": float(slow),
                    "ema_difference": float(fast) - float(slow),
                    "last_processed_timestamp": last_timestamp,
                    "last_processed_candle_timestamp": last_timestamp,
                    "last_price": ema.get("latest_close"),
                    "current_trend": ema.get("latest_signal"),
                    "valid_candle_count": int(result.get("candles_count") or 0),
                    "initialized": True,
                    "live_processing": True,
                }
                initialized += 1
        from services.strategy_context import find_context_for_instrument
        for key, value in self.get_states_snapshot().items():
            context = find_context_for_instrument(key)
            if context:
                context.ema_state[key] = dict(value)
        self.is_running = initialized > 0
        from services.strategy_context import get_strategy_contexts
        for context in get_strategy_contexts().values():
            get_opening_ema_cross_selector(context.underlying).reset_for_day(
                datetime.now(get_market_timezone()).date()
            )
        logger.info("Internal EMA warmup complete initialized=%s", initialized)
        return bool(initialized)

    def get_instrument_state(self, instrument_key):
        with self.lock:
            value = self.states.get(str(instrument_key or ""))
            return dict(value) if value else None

    def get_states_snapshot(self):
        with self.lock:
            return {key: dict(value) for key, value in self.states.items()}

    def get_events_snapshot(self, instrument_key=None, limit=100):
        with self.lock:
            events = list(self.events)
        if instrument_key:
            events = [event for event in events if event.get("instrument_key") == instrument_key]
        return events[-max(1, int(limit)):]

    def get_status(self):
        with self.lock:
            return {
                "enabled": bool(getattr(config, "LIVE_EMA_ENABLED", True)),
                "is_running": self.is_running,
                "connected": self.is_running,
                "instrument_count": len(self.states),
                "messages_received": self.processed_count + self.skipped_count,
                "candles_processed": self.processed_count,
                "duplicate_candles_skipped": self.skipped_count,
                "crossovers_received": self.crossovers_count,
                "last_error": self.last_error,
                "mode": "internal_completed_candle",
            }

    def poll_completed_candles(self):
        """Fetch and incrementally process completed 1-minute candles."""
        if not getattr(config, "LIVE_EMA_ENABLED", True):
            return
        if not self.states:
            return
        from services.strategy_context import get_active_strategy_instrument_keys, get_strategy_contexts, persist_strategy_context
        keys = get_active_strategy_instrument_keys()
        if not keys:
            return
        # Prevent overlapping scheduled/manual cycles from fetching and handling
        # the same instruments concurrently. Per-candle state checks below are
        # still the final duplicate guard.
        if not self._poll_lock.acquire(blocking=False):
            return
        self.is_running = True
        now = datetime.now(get_market_timezone())
        market_open = dt_time(config.MARKET_OPEN_HOUR, config.MARKET_OPEN_MINUTE)
        market_close = dt_time(config.MARKET_CLOSE_HOUR, config.MARKET_CLOSE_MINUTE)
        last_poll_deadline = (
            datetime.combine(now.date(), market_close) + timedelta(minutes=1)
        ).time()
        if (
            now.weekday() >= 5
            or now.time() < market_open
            or now.time() > last_poll_deadline
        ):
            self.is_running = False
            self._poll_lock.release()
            return
        self.is_running = True
        poll_minute = now.strftime("%Y-%m-%dT%H:%M")
        if poll_minute == self.last_poll_minute or now.second < 5:
            self._poll_lock.release()
            return
        self.last_poll_minute = poll_minute
        cutoff = now.replace(second=0, microsecond=0)

        def poll_one(key):
            response = fetch_intraday_candles_for_instrument(key, "1minute")
            candles = response.get("candles", [])
            from services.opening_range.candle_utils import parse_candle_timestamp

            completed = []
            for candle in candles:
                if not isinstance(candle, (list, tuple)) or len(candle) < 5:
                    continue
                candle_dt = parse_candle_timestamp(candle[0])
                if candle_dt is None or candle_dt.replace(second=0, microsecond=0) >= cutoff:
                    continue
                completed.append((candle_dt, candle))
            emitted = []
            for candle_dt, candle in sorted(completed, key=lambda item: item[0]):
                result = self.process_candle(key, {
                    "timestamp": candle_dt.isoformat(),
                    "open": candle[1], "high": candle[2], "low": candle[3],
                    "close": candle[4],
                    "volume": candle[5] if len(candle) > 5 else 0,
                    "open_interest": candle[6] if len(candle) > 6 else 0,
                })
                if isinstance(result, dict):
                    try:
                        from services.opening_range.live_touch import (
                            process_completed_candle_for_opening_range,
                        )
                        process_completed_candle_for_opening_range(
                            key,
                            {
                                "timestamp": candle_dt.isoformat(),
                                "open": candle[1], "high": candle[2],
                                "low": candle[3], "close": candle[4],
                                "volume": candle[5] if len(candle) > 5 else 0,
                                "oi": candle[6] if len(candle) > 6 else 0,
                            },
                            get_contract_info_by_instrument_key(key),
                        )
                    except Exception:
                        logger.exception(
                            "Completed candle Opening Range touch processing failed instrument=%s",
                            key,
                        )
                if isinstance(result, dict) and result.get("type") == "live_ema_cross":
                    emitted.append(result)
            return emitted

        workers = min(
            max(1, int(getattr(config, "HISTORICAL_CANDLE_MAX_WORKERS", 8))),
            len(keys),
        )
        try:
            cycle_crosses = []
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ema-candle") as pool:
                futures = {pool.submit(poll_one, key): key for key in keys}
                for future in as_completed(futures):
                    try:
                        cycle_crosses.extend(future.result())
                    except Exception as exc:
                        self.last_error = f"{type(exc).__name__}: {exc}"
                        logger.exception("Internal EMA candle poll failed instrument=%s", futures[future])
            # Decide after bounded workers finish so opening-window candidates
            # are selected deterministically across instruments.
            grouped = {}
            for event in cycle_crosses:
                stamp = event.get("candle_timestamp") or event.get("timestamp")
                underlying = str(event.get("underlying") or "NIFTY").upper()
                grouped.setdefault((stamp, underlying), []).append(event)
            for stamp, underlying in sorted(grouped):
                candle_events = grouped[(stamp, underlying)]
                selector = get_opening_ema_cross_selector(underlying)
                opening_events = [
                    event for event in candle_events
                    if selector.is_opening_window_event(event)
                ]
                if opening_events:
                    selector.process_candle_crosses(
                        opening_events, self._dispatch_cross_event
                    )
                    continue
                for event in sorted(
                    candle_events,
                    key=lambda item: str(item.get("instrument_key") or ""),
                ):
                    self._dispatch_cross_event(event)
            state_snapshot = self.get_states_snapshot()
            for context in get_strategy_contexts().values():
                scoped_keys = [key for key in keys if key == context.index_instrument_key or key in context.option_universe]
                scoped_state = {key: state_snapshot[key] for key in scoped_keys if key in state_snapshot}
                get_opening_ema_cross_selector(context.underlying).finalize_if_caught_up(scoped_state, scoped_keys)
            for context in get_strategy_contexts().values():
                context.ema_state = {
                    key: dict(value) for key, value in self.get_states_snapshot().items()
                    if key == context.index_instrument_key or key in context.option_universe
                }
                persist_strategy_context(context, event_type="ema_candle_batch")
        finally:
            self._poll_lock.release()

    def process_candle(self, instrument_key: str, candle: dict) -> dict | None:
        key = str(instrument_key)
        with self.lock:
            instrument_lock = self._instrument_locks.setdefault(key, RLock())
        with instrument_lock:
            return self._process_candle(key, candle)

    def _process_candle(self, instrument_key: str, candle: dict) -> dict | None:
        stamp = str(candle.get("timestamp") or "")
        close = float(candle.get("close"))
        with self.lock:
            previous = dict(self.states.get(instrument_key) or {})
            if not previous.get("initialized"):
                return None
            if stamp <= str(previous.get("last_processed_timestamp") or ""):
                self.skipped_count += 1
                return None
            old_fast, old_slow = float(previous["ema_9"]), float(previous["ema_21"])
            fast = old_fast + (2 / 10) * (close - old_fast)
            slow = old_slow + (2 / 22) * (close - old_slow)
            diff = fast - slow
            old_diff = old_fast - old_slow
            cross = "bullish" if old_diff <= 0 < diff else "bearish" if old_diff >= 0 > diff else None
            updated = {
                **previous,
                "ema_9": fast, "ema_21": slow, "ema_difference": diff,
                "last_processed_timestamp": stamp, "last_price": close,
                "last_processed_candle_timestamp": stamp,
                "current_trend": "bullish" if diff > 0 else "bearish" if diff < 0 else "neutral",
                "valid_candle_count": int(previous.get("valid_candle_count", 0)) + 1,
            }
            self.states[instrument_key] = updated
            self.processed_count += 1
        from services.strategy_context import find_context_for_instrument
        context = find_context_for_instrument(instrument_key)
        if context:
            context.ema_state[instrument_key] = dict(updated)
        if not cross:
            return updated
        contract = get_contract_info_by_instrument_key(instrument_key) or {}
        event = {
            "type": "live_ema_cross", "event_type": "ema.crossover",
            "source": "internal_ema_engine", "instrument_key": instrument_key,
            "trading_symbol": contract.get("trading_symbol") or contract.get("symbol"),
            "contract_info": contract, "cross_type": cross,
            "signal": cross.upper(), "timestamp": stamp, "candle_timestamp": stamp,
            "ema_fast": round(fast, 6), "ema_slow": round(slow, 6),
            "ema_9": round(fast, 6), "ema_21": round(slow, 6),
            "previous_ema_fast": old_fast, "previous_ema_slow": old_slow,
            "price": close, "close": close, "ema_calculation_mode": "internal_completed_candle",
        }
        if context:
            event.update({
                "underlying": context.underlying,
                "underlying_instrument_key": context.index_instrument_key,
            })
        try:
            from services.history_service import save_runtime_intraday_ema_cross

            save_runtime_intraday_ema_cross(
                instrument_key,
                {
                    "timestamp": stamp,
                    "type": f"{cross}_cross",
                    "close": close,
                    "ema_fast": fast,
                    "ema_slow": slow,
                },
                contract_info=contract,
            )
        except Exception:
            logger.exception("EMA intraday cross persistence failed instrument=%s", instrument_key)
            with self.lock:
                current = self.states.get(instrument_key) or {}
                if current.get("last_processed_timestamp") == stamp:
                    self.states[instrument_key] = previous
                    self.processed_count = max(0, self.processed_count - 1)
            return None
        try:
            event.update(get_opening_range_levels_for_ema_event(instrument_key) or {})
        except Exception:
            logger.exception("EMA Opening Range enrichment failed instrument=%s", instrument_key)
        with self.lock:
            self.events.append(dict(event))
            max_events = max(1, int(getattr(config, "LIVE_EMA_MAX_EVENTS_IN_MEMORY", 5000)))
            del self.events[:-max_events]
            try:
                event_log = Path("logs/live_ema_events.jsonl")
                event_log.parent.mkdir(parents=True, exist_ok=True)
                with event_log.open("a", encoding="utf-8") as output:
                    output.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
            except OSError:
                logger.exception("Could not persist internal EMA event instrument=%s", instrument_key)
        if context:
            context.append_event(event, kind="ema")
        self.crossovers_count += 1
        try:
            if self.loop and self.loop.is_running():
                asyncio.run_coroutine_threadsafe(broadcaster.broadcast_ema_cross(event), self.loop)
        except Exception:
            logger.exception("EMA WebSocket broadcast scheduling failed instrument=%s", instrument_key)
        return event

    def _dispatch_cross_event(self, event: dict) -> bool:
        try:
            return bool(process_selected_or_ema_cross_alert(event))
        except Exception:
            logger.exception(
                "EMA isolation dispatch failed instrument=%s",
                event.get("instrument_key"),
            )
            return False

internal_ema_engine = InternalEmaEngine()
