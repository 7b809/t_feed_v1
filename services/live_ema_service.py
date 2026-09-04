import asyncio
import json
from collections import deque
from datetime import datetime
from pathlib import Path
from threading import Lock
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from core import config
from core.logger import get_logger

logger = get_logger(__file__)

class LiveEMAService:
    """
    Live EMA crossover continuation service.

    Purpose:
    1. Initialize EMA state from historical EMA summary.
    2. Continue EMA 9/21 from historical latest EMA values.
    3. Support two live EMA calculation modes:
       - LIVE_EMA_CALCULATION_MODE=False:
         completed 1-minute candle close based EMA calculation.
       - LIVE_EMA_CALCULATION_MODE=True:
         live tick/LTP based EMA calculation.
    4. Detect live bullish/bearish EMA crossovers for all initialized instruments.
    5. Store crossover events in memory.
    6. Broadcast crossover events via optional registered callback.
    7. Optionally save crossover events to data/live_ema_cross_results.json.

    Important:
    - Raw live ticks are not stored.
    - Raw historical candles are not needed here.
    - This service works from EMA state only.
    - Live EMA continues for all initialized instruments.
    - This service does not isolate instruments.
    - This service does not send Telegram alerts.
    - Opening Range enrichment and isolated instrument Telegram alerting
      are handled outside this service in services/upstox_websocket.py and
      services/opening_range_service.py.
    """

    def __init__(self):
        self.enabled = bool(getattr(config, "LIVE_EMA_ENABLED", True))
        self.tick_based_mode = bool(getattr(config, "LIVE_EMA_CALCULATION_MODE", False))
        self.calculation_mode = "tick_ltp" if self.tick_based_mode else "candle_close"
        self.interval_minutes = int(getattr(config, "LIVE_EMA_INTERVAL_MINUTES", 1))
        self.fast_period = int(getattr(config, "LIVE_EMA_FAST_PERIOD", getattr(config, "EMA_FAST_PERIOD", 9)))
        self.slow_period = int(getattr(config, "LIVE_EMA_SLOW_PERIOD", getattr(config, "EMA_SLOW_PERIOD", 21)))
        self.output_file = getattr(config, "LIVE_EMA_OUTPUT_FILE", "data/live_ema_cross_results.json")
        self.save_test_file = bool(getattr(config, "LIVE_EMA_SAVE_TEST_FILE", True))
        self.max_events_in_memory = int(getattr(config, "LIVE_EMA_MAX_EVENTS_IN_MEMORY", 5000))
        self.tick_alert_once_per_direction = bool(getattr(config, "LIVE_EMA_TICK_ALERT_ONCE_PER_DIRECTION", True))
        self.tick_min_price_change = float(getattr(config, "LIVE_EMA_TICK_MIN_PRICE_CHANGE", 0.0))
        self.market_timezone = self._load_market_timezone()
        self._lock = Lock()
        self.crossover_callback = None
        self.state = {}
        self.cross_events = deque(maxlen=self.max_events_in_memory)
        logger.info(f"LiveEMAService initialized. enabled={self.enabled}, calculation_mode={self.calculation_mode}, tick_based_mode={self.tick_based_mode}, interval_minutes={self.interval_minutes}, fast_period={self.fast_period}, slow_period={self.slow_period}, tick_alert_once_per_direction={self.tick_alert_once_per_direction}, tick_min_price_change={self.tick_min_price_change}")

    # ========================================================
    # Callback Registration
    # ========================================================

    def register_crossover_callback(self, callback):
        """Registers an async callback function to handle crossover events in real time."""
        self.crossover_callback = callback
        logger.info("Registered crossover callback in LiveEMAService.")

    # ========================================================
    # Time Helpers
    # ========================================================

    def _load_market_timezone(self):
        """Loads market timezone from config, defaulting to Asia/Kolkata."""
        timezone_name = getattr(config, "MARKET_TIMEZONE", "Asia/Kolkata")
        try:
            return ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            logger.error(f"Invalid MARKET_TIMEZONE configured: {timezone_name}. Falling back to Asia/Kolkata.")
            return ZoneInfo("Asia/Kolkata")

    def _now_market_time(self) -> str:
        """Returns current market time as ISO string."""
        return datetime.now(self.market_timezone).isoformat()

    def _epoch_ms_to_iso(self, ts_value) -> str | None:
        """Converts epoch milliseconds to ISO datetime string in market timezone."""
        try:
            ts_ms = int(ts_value)
            dt_obj = datetime.fromtimestamp(ts_ms / 1000, tz=self.market_timezone)
            return dt_obj.isoformat()
        except Exception:
            return None

    def _epoch_ms_to_datetime(self, ts_value) -> datetime | None:
        """Converts epoch milliseconds to datetime object in market timezone."""
        try:
            ts_ms = int(ts_value)
            return datetime.fromtimestamp(ts_ms / 1000, tz=self.market_timezone)
        except Exception:
            return None

    # ========================================================
    # Safe Helpers
    # ========================================================

    def _safe_float(self, value, default: float = 0.0) -> float:
        """Safely converts value to float."""
        try:
            if value is None: return default
            return float(value)
        except Exception: return default

    def _safe_int(self, value, default: int = 0) -> int:
        """Safely converts value to int."""
        try:
            if value is None: return default
            return int(value)
        except Exception: return default

    # ========================================================
    # Historical Initialization
    # ========================================================

    def initialize_from_history_summary(self, summary: dict) -> dict:
        """Initializes live EMA state from historical EMA summary."""
        initialized_count = 0
        skipped_count = 0
        if not self.enabled:
            logger.info("Live EMA initialization skipped because LIVE_EMA_ENABLED=False.")
            return {"initialized": False, "initialized_count": 0, "skipped_count": 0, "reason": "disabled"}
        results = (summary or {}).get("results", {})
        if not results:
            logger.warning("Live EMA initialization skipped. No historical results found.")
            return {"initialized": False, "initialized_count": 0, "skipped_count": 0, "reason": "no_historical_results"}
        with self._lock:
            self.state = {}
            for instrument_key, item in results.items():
                ema_result = item.get("ema_result") or {}
                latest_ema_fast = ema_result.get("latest_ema_fast")
                latest_ema_slow = ema_result.get("latest_ema_slow")
                if latest_ema_fast is None or latest_ema_slow is None:
                    skipped_count += 1
                    continue
                latest_close = self._safe_float(ema_result.get("latest_close"))
                self.state[instrument_key] = {
                    "instrument_key": instrument_key,
                    "initialized": True,
                    "source": "historical_ema",
                    "calculation_mode": self.calculation_mode,
                    "tick_based_mode": self.tick_based_mode,
                    "interval_minutes": self.interval_minutes,
                    "fast_period": self.fast_period,
                    "slow_period": self.slow_period,
                    "previous_ema_fast": self._safe_float(latest_ema_fast),
                    "previous_ema_slow": self._safe_float(latest_ema_slow),
                    "previous_signal": ema_result.get("latest_signal"),
                    "latest_close": latest_close,
                    "last_historical_timestamp": ema_result.get("latest_timestamp"),
                    "last_processed_candle_ts": ema_result.get("latest_timestamp"),
                    "last_processed_tick_ts": None,
                    "last_processed_tick_ltp": None,
                    "last_tick_cross_type": None,
                    "last_tick_signal": ema_result.get("latest_signal"),
                    "last_crossover": ema_result.get("last_crossover"),
                    "pending_live_candle": None,
                    "pending_live_candle_ts": None,
                    "aggregation_bucket": None,
                    "aggregation_bucket_start_ts": None,
                    "aggregation_bucket_start_ms": None,
                    "crossovers": [],
                    "contract_info": item.get("contract_info", {}),
                    "updated_at": self._now_market_time(),
                }
                initialized_count += 1
        logger.info(f"Live EMA state initialized from historical summary. initialized_count={initialized_count}, skipped_count={skipped_count}, calculation_mode={self.calculation_mode}")
        return {"initialized": initialized_count > 0, "initialized_count": initialized_count, "skipped_count": skipped_count, "calculation_mode": self.calculation_mode, "tick_based_mode": self.tick_based_mode}

    # ========================================================
    # Feed Extraction Helpers
    # ========================================================

    def _get_feed_container(self, tick_data: dict) -> dict:
        """Extracts the feed container from Upstox full feed."""
        if not isinstance(tick_data, dict): return {}
        raw_feed_obj = tick_data.get("raw_feed", tick_data)
        full_feed = raw_feed_obj.get("fullFeed", raw_feed_obj)
        ff_wrapper = full_feed.get("ff", full_feed)
        ff = ff_wrapper.get("marketFF") or ff_wrapper.get("indexFF") or full_feed.get("marketFF") or full_feed.get("indexFF") or full_feed
        return ff if isinstance(ff, dict) else {}

    def _get_ohlc_list(self, tick_data: dict) -> list:
        """Extracts OHLC list from Upstox full feed."""
        ff = self._get_feed_container(tick_data)
        if "marketOHLC" in ff:
            ohlc_list = ff.get("marketOHLC", {}).get("ohlc", [])
        elif "optionOHLC" in ff:
            ohlc_list = ff.get("optionOHLC", {}).get("ohlc", [])
        else:
            ohlc_list = []
        return ohlc_list if isinstance(ohlc_list, list) else []

    def _find_latest_feed_candle(self, tick_data: dict, interval_tag: str) -> dict | None:
        """Returns the latest candle from OHLC list for requested interval tag."""
        ohlc_list = self._get_ohlc_list(tick_data)
        matching = [item for item in ohlc_list if str(item.get("interval")).upper() == interval_tag.upper() and item.get("ts") is not None]
        if not matching: return None
        try:
            return max(matching, key=lambda item: int(item.get("ts")))
        except Exception:
            return matching[-1]

    def _normalize_feed_candle(self, candle: dict) -> dict | None:
        """Converts Upstox OHLC candle dictionary to internal normalized candle."""
        if not isinstance(candle, dict): return None
        ts_raw = candle.get("ts")
        timestamp = self._epoch_ms_to_iso(ts_raw)
        if not timestamp: return None
        return {
            "timestamp": timestamp,
            "timestamp_ms": self._safe_int(ts_raw),
            "open": self._safe_float(candle.get("open")),
            "high": self._safe_float(candle.get("high")),
            "low": self._safe_float(candle.get("low")),
            "close": self._safe_float(candle.get("close")),
            "volume": self._safe_int(candle.get("volume") or candle.get("vol")),
        }

    def _extract_ltp_tick(self, tick_data: dict) -> dict | None:
        """Extracts live LTP from Upstox full feed for tick-based EMA."""
        ff = self._get_feed_container(tick_data)
        if not ff: return None
        ltpc = ff.get("ltpc") or {}
        ltp = self._safe_float(ltpc.get("ltp"))
        ltq = self._safe_int(ltpc.get("ltq"))
        ltt = self._safe_int(ltpc.get("ltt"))
        if ltp <= 0: return None
        timestamp_ms = ltt if ltt > 0 else None
        timestamp = self._epoch_ms_to_iso(timestamp_ms) if timestamp_ms else None
        if not timestamp: timestamp = self._now_market_time()
        return {"timestamp": timestamp, "timestamp_ms": timestamp_ms, "ltp": ltp, "ltq": ltq, "ltt": ltt}

    # ========================================================
    # EMA Calculation Helpers
    # ========================================================

    def _calculate_next_ema(self, close_price: float, previous_ema: float, period: int) -> float:
        """Calculates next EMA value from previous EMA and current close."""
        multiplier = 2 / (period + 1)
        next_ema = close_price * multiplier + previous_ema * (1 - multiplier)
        return round(next_ema, 4)

    def _detect_cross(self, previous_fast: float, previous_slow: float, current_fast: float, current_slow: float) -> str | None:
        """Detects EMA crossover type."""
        if previous_fast <= previous_slow and current_fast > current_slow: return "bullish_cross"
        if previous_fast >= previous_slow and current_fast < current_slow: return "bearish_cross"
        return None

    def _get_signal(self, ema_fast: float, ema_slow: float) -> str:
        """Returns current EMA signal."""
        if ema_fast > ema_slow: return "bullish"
        if ema_fast < ema_slow: return "bearish"
        return "neutral"

    # ========================================================
    # Candle Completion Logic
    # ========================================================

    def _accept_live_candle_and_get_completed(self, state: dict, latest_candle: dict) -> dict | None:
        """Uses candle timestamp change as candle completion rule."""
        latest_ts = latest_candle.get("timestamp")
        if not latest_ts: return None
        pending_ts = state.get("pending_live_candle_ts")
        pending_candle = state.get("pending_live_candle")
        if pending_ts is None:
            state["pending_live_candle"] = latest_candle
            state["pending_live_candle_ts"] = latest_ts
            return None
        if latest_ts == pending_ts:
            state["pending_live_candle"] = latest_candle
            return None
        completed_candle = pending_candle
        state["pending_live_candle"] = latest_candle
        state["pending_live_candle_ts"] = latest_ts
        return completed_candle

    # ========================================================
    # Aggregation for Custom Intervals
    # ========================================================

    def _get_bucket_start_ms(self, timestamp_ms: int, interval_minutes: int) -> int:
        """Floors timestamp to interval bucket start."""
        interval_ms = interval_minutes * 60 * 1000
        return (timestamp_ms // interval_ms) * interval_ms

    def _process_aggregation_bucket(self, state: dict, one_minute_candle: dict) -> dict | None:
        """Aggregates completed I1 candles into configured interval candle."""
        interval_minutes = int(self.interval_minutes)
        if interval_minutes <= 1: return one_minute_candle
        timestamp_ms = one_minute_candle.get("timestamp_ms")
        if not timestamp_ms: return None
        bucket_start_ms = self._get_bucket_start_ms(timestamp_ms, interval_minutes)
        bucket_start_iso = self._epoch_ms_to_iso(bucket_start_ms)
        current_bucket = state.get("aggregation_bucket")
        current_bucket_start_ms = state.get("aggregation_bucket_start_ms")
        if current_bucket is None:
            state["aggregation_bucket_start_ts"] = bucket_start_iso
            state["aggregation_bucket_start_ms"] = bucket_start_ms
            state["aggregation_bucket"] = {
                "timestamp": bucket_start_iso,
                "timestamp_ms": bucket_start_ms,
                "open": one_minute_candle.get("open"),
                "high": one_minute_candle.get("high"),
                "low": one_minute_candle.get("low"),
                "close": one_minute_candle.get("close"),
                "volume": one_minute_candle.get("volume", 0),
            }
            return None
        if bucket_start_ms == current_bucket_start_ms:
            current_bucket["high"] = max(self._safe_float(current_bucket.get("high")), self._safe_float(one_minute_candle.get("high")))
            current_bucket["low"] = min(self._safe_float(current_bucket.get("low")), self._safe_float(one_minute_candle.get("low")))
            current_bucket["close"] = one_minute_candle.get("close")
            current_bucket["volume"] = self._safe_int(current_bucket.get("volume")) + self._safe_int(one_minute_candle.get("volume"))
            state["aggregation_bucket"] = current_bucket
            return None
        completed_bucket = current_bucket
        state["aggregation_bucket_start_ts"] = bucket_start_iso
        state["aggregation_bucket_start_ms"] = bucket_start_ms
        state["aggregation_bucket"] = {
            "timestamp": bucket_start_iso,
            "timestamp_ms": bucket_start_ms,
            "open": one_minute_candle.get("open"),
            "high": one_minute_candle.get("high"),
            "low": one_minute_candle.get("low"),
            "close": one_minute_candle.get("close"),
            "volume": one_minute_candle.get("volume", 0),
        }
        return completed_bucket

    # ========================================================
    # Main Live Processing
    # ========================================================

    def process_live_feed(self, instrument_key: str, tick_data: dict, contract_info: dict | None = None) -> dict | None:
        """Processes one live Upstox full-mode feed tick."""
        if not self.enabled or not instrument_key or not isinstance(tick_data, dict): return None
        with self._lock:
            state = self.state.get(instrument_key)
            if not state: return None
            previous_ema_fast = state.get("previous_ema_fast")
            previous_ema_slow = state.get("previous_ema_slow")
            if previous_ema_fast is None or previous_ema_slow is None: return None
            if self.tick_based_mode:
                return self._process_tick_mode_locked(instrument_key=instrument_key, state=state, tick_data=tick_data, contract_info=contract_info)
            return self._process_candle_mode_locked(instrument_key=instrument_key, state=state, tick_data=tick_data, contract_info=contract_info)

    def _process_candle_mode_locked(self, instrument_key: str, state: dict, tick_data: dict, contract_info: dict | None = None) -> dict | None:
        """Processes completed 1-minute candle close based EMA. Lock must already be held by caller."""
        latest_i1_raw = self._find_latest_feed_candle(tick_data, "I1")
        if not latest_i1_raw: return None
        latest_i1_candle = self._normalize_feed_candle(latest_i1_raw)
        if not latest_i1_candle: return None
        completed_i1_candle = self._accept_live_candle_and_get_completed(state, latest_i1_candle)
        if not completed_i1_candle: return None
        completed_target_candle = self._process_aggregation_bucket(state, completed_i1_candle)
        if not completed_target_candle: return None
        return self._process_completed_candle_locked(instrument_key=instrument_key, state=state, completed_candle=completed_target_candle, contract_info=contract_info)

    def _process_tick_mode_locked(self, instrument_key: str, state: dict, tick_data: dict, contract_info: dict | None = None) -> dict | None:
        """Processes live tick/LTP based EMA. Lock must already be held by caller."""
        tick = self._extract_ltp_tick(tick_data)
        if not tick: return None
        ltp = self._safe_float(tick.get("ltp"))
        tick_ts = tick.get("timestamp")
        tick_ts_ms = tick.get("timestamp_ms")
        if not tick_ts or ltp <= 0: return None
        last_tick_ltp = state.get("last_processed_tick_ltp")
        if last_tick_ltp is not None and self.tick_min_price_change > 0 and abs(ltp - self._safe_float(last_tick_ltp)) < self.tick_min_price_change:
            return None
        previous_fast = self._safe_float(state.get("previous_ema_fast"))
        previous_slow = self._safe_float(state.get("previous_ema_slow"))
        previous_signal = state.get("previous_signal")
        current_fast = self._calculate_next_ema(close_price=ltp, previous_ema=previous_fast, period=self.fast_period)
        current_slow = self._calculate_next_ema(close_price=ltp, previous_ema=previous_slow, period=self.slow_period)
        cross_type = self._detect_cross(previous_fast=previous_fast, previous_slow=previous_slow, current_fast=current_fast, current_slow=current_slow)
        current_signal = self._get_signal(current_fast, current_slow)
        state["previous_ema_fast"] = current_fast
        state["previous_ema_slow"] = current_slow
        state["previous_signal"] = current_signal
        state["latest_close"] = ltp
        state["last_processed_tick_ts"] = tick_ts
        state["last_processed_tick_ltp"] = ltp
        state["updated_at"] = self._now_market_time()
        if not cross_type: return None
        if self.tick_alert_once_per_direction:
            last_tick_cross_type = state.get("last_tick_cross_type")
            if last_tick_cross_type == cross_type: return None
            state["last_tick_cross_type"] = cross_type
        resolved_contract_info = contract_info or state.get("contract_info", {})
        event = {
            "type": "live_ema_cross",
            "instrument_key": instrument_key,
            "timestamp": tick_ts,
            "timestamp_ms": tick_ts_ms,
            "cross_type": cross_type,
            "interval_minutes": 0,
            "close": ltp,
            "ltp": ltp,
            "ema_fast_period": self.fast_period,
            "ema_slow_period": self.slow_period,
            "ema_fast": current_fast,
            "ema_slow": current_slow,
            "previous_ema_fast": previous_fast,
            "previous_ema_slow": previous_slow,
            "previous_signal": previous_signal,
            "current_signal": current_signal,
            "source": "live_tick",
            "ema_calculation_mode": "tick_ltp",
            "created_at": self._now_market_time(),
            "tick": {"timestamp": tick_ts, "timestamp_ms": tick_ts_ms, "ltp": ltp, "ltq": tick.get("ltq"), "ltt": tick.get("ltt")},
            "candle": None,
            "contract_info": resolved_contract_info,
            "info": resolved_contract_info,
            "telegram_alert_scope": "isolated_instrument_only",
        }
        return self._record_and_emit_event_locked(instrument_key=instrument_key, state=state, event=event)

    def _process_completed_candle_locked(self, instrument_key: str, state: dict, completed_candle: dict, contract_info: dict | None = None) -> dict | None:
        """Processes completed candle and checks EMA crossover. Lock must already be held by caller."""
        candle_ts = completed_candle.get("timestamp")
        close_price = self._safe_float(completed_candle.get("close"))
        if not candle_ts or close_price <= 0: return None
        last_processed_ts = state.get("last_processed_candle_ts")
        if last_processed_ts == candle_ts: return None
        previous_fast = self._safe_float(state.get("previous_ema_fast"))
        previous_slow = self._safe_float(state.get("previous_ema_slow"))
        previous_signal = state.get("previous_signal")
        current_fast = self._calculate_next_ema(close_price=close_price, previous_ema=previous_fast, period=self.fast_period)
        current_slow = self._calculate_next_ema(close_price=close_price, previous_ema=previous_slow, period=self.slow_period)
        cross_type = self._detect_cross(previous_fast=previous_fast, previous_slow=previous_slow, current_fast=current_fast, current_slow=current_slow)
        current_signal = self._get_signal(current_fast, current_slow)
        state["previous_ema_fast"] = current_fast
        state["previous_ema_slow"] = current_slow
        state["previous_signal"] = current_signal
        state["latest_close"] = close_price
        state["last_processed_candle_ts"] = candle_ts
        state["updated_at"] = self._now_market_time()
        if not cross_type: return None
        resolved_contract_info = contract_info or state.get("contract_info", {})
        event = {
            "type": "live_ema_cross",
            "instrument_key": instrument_key,
            "timestamp": candle_ts,
            "timestamp_ms": completed_candle.get("timestamp_ms"),
            "cross_type": cross_type,
            "interval_minutes": self.interval_minutes,
            "close": close_price,
            "ema_fast_period": self.fast_period,
            "ema_slow_period": self.slow_period,
            "ema_fast": current_fast,
            "ema_slow": current_slow,
            "previous_ema_fast": previous_fast,
            "previous_ema_slow": previous_slow,
            "previous_signal": previous_signal,
            "current_signal": current_signal,
            "source": "live_feed",
            "ema_calculation_mode": "candle_close",
            "created_at": self._now_market_time(),
            "candle": {
                "timestamp": completed_candle.get("timestamp"),
                "timestamp_ms": completed_candle.get("timestamp_ms"),
                "open": completed_candle.get("open"),
                "high": completed_candle.get("high"),
                "low": completed_candle.get("low"),
                "close": completed_candle.get("close"),
                "volume": completed_candle.get("volume"),
            },
            "tick": None,
            "contract_info": resolved_contract_info,
            "info": resolved_contract_info,
            "telegram_alert_scope": "isolated_instrument_only",
        }
        return self._record_and_emit_event_locked(instrument_key=instrument_key, state=state, event=event)

    def _record_and_emit_event_locked(self, instrument_key: str, state: dict, event: dict) -> dict:
        """Stores and emits EMA crossover event. Lock must already be held by caller."""
        state["last_crossover"] = event
        state.setdefault("crossovers", []).append(event)
        self.cross_events.append(event)
        logger.info(f"Live EMA crossover detected. instrument_key={instrument_key}, cross_type={event.get('cross_type')}, timestamp={event.get('timestamp')}, close={event.get('close')}, ema_fast={event.get('ema_fast')}, ema_slow={event.get('ema_slow')}, current_signal={event.get('current_signal')}, ema_calculation_mode={event.get('ema_calculation_mode')}")
        self._save_live_events_if_enabled_locked()
        if self.crossover_callback:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self.crossover_callback(event))
            except RuntimeError:
                asyncio.run(self.crossover_callback(event))
            except Exception as ex:
                logger.error(f"Failed to execute crossover callback: {ex}")
        return event

    # ========================================================
    # Storage
    # ========================================================

    def _save_live_events_if_enabled_locked(self):
        """Saves live EMA cross events to file if enabled. Lock must already be held by caller."""
        if not bool(getattr(config, "TEST_FLAG", False)): return
        if not self.save_test_file: return
        try:
            file_path = Path(self.output_file)
            file_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "generated_at": self._now_market_time(),
                "calculation_mode": self.calculation_mode,
                "tick_based_mode": self.tick_based_mode,
                "interval_minutes": self.interval_minutes,
                "fast_period": self.fast_period,
                "slow_period": self.slow_period,
                "events_count": len(self.cross_events),
                "events": list(self.cross_events),
                "telegram_alert_scope": "isolated_instrument_only",
            }
            with open(file_path, "w", encoding="utf-8") as file:
                json.dump(payload, file, indent=4, default=str)
        except Exception as ex:
            logger.error(f"Failed saving live EMA crossover events: {type(ex).__name__}: {ex}")

    # ========================================================
    # Read APIs
    # ========================================================

    def get_status(self) -> dict:
        """Returns live EMA service status."""
        with self._lock:
            return {
                "enabled": self.enabled,
                "live_ema_calculation_mode_flag": self.tick_based_mode,
                "calculation_mode": self.calculation_mode,
                "mode_description": "tick_ltp based EMA calculation" if self.tick_based_mode else "completed candle close based EMA calculation",
                "interval_minutes": self.interval_minutes,
                "fast_period": self.fast_period,
                "slow_period": self.slow_period,
                "tracked_instruments": len(self.state),
                "events_count": len(self.cross_events),
                "output_file": self.output_file,
                "save_test_file": self.save_test_file,
                "max_events_in_memory": self.max_events_in_memory,
                "tick_alert_once_per_direction": self.tick_alert_once_per_direction,
                "tick_min_price_change": self.tick_min_price_change,
                "payload_mode": "enriched_ema_details",
                "opening_range_enrichment": "handled_in_upstox_websocket",
                "isolated_instrument_selection": "handled_in_opening_range_service",
                "telegram_alerts": "isolated_instrument_only",
                "all_instruments_ema_processing": True,
                "updated_at": self._now_market_time(),
            }

    def get_events(self, limit: int = 100) -> list:
        """Returns latest live EMA crossover events."""
        limit = max(1, int(limit or 100))
        with self._lock:
            return list(self.cross_events)[-limit:]

    def get_instrument_state(self, instrument_key: str) -> dict | None:
        """Returns live EMA state for one instrument."""
        with self._lock:
            state = self.state.get(instrument_key)
            if not state: return None
            copied = dict(state)
            crossovers = copied.get("crossovers", [])
            copied["crossovers_count"] = len(crossovers)
            copied["recent_crossovers"] = crossovers[-20:]
            copied.pop("crossovers", None)
            return copied

    def get_all_instrument_summaries(self) -> dict:
        """Returns lightweight state summary for all instruments."""
        with self._lock:
            output = {}
            for instrument_key, state in self.state.items():
                output[instrument_key] = {
                    "instrument_key": instrument_key,
                    "initialized": state.get("initialized"),
                    "calculation_mode": state.get("calculation_mode", self.calculation_mode),
                    "tick_based_mode": state.get("tick_based_mode", self.tick_based_mode),
                    "interval_minutes": state.get("interval_minutes"),
                    "previous_ema_fast": state.get("previous_ema_fast"),
                    "previous_ema_slow": state.get("previous_ema_slow"),
                    "previous_signal": state.get("previous_signal"),
                    "latest_close": state.get("latest_close"),
                    "last_historical_timestamp": state.get("last_historical_timestamp"),
                    "last_processed_candle_ts": state.get("last_processed_candle_ts"),
                    "last_processed_tick_ts": state.get("last_processed_tick_ts"),
                    "last_processed_tick_ltp": state.get("last_processed_tick_ltp"),
                    "last_crossover": state.get("last_crossover"),
                    "crossovers_count": len(state.get("crossovers", [])),
                    "updated_at": state.get("updated_at"),
                    "contract_info": state.get("contract_info", {}),
                }
            return output

live_ema_service = LiveEMAService()