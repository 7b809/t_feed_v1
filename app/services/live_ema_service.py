# app/services/live_ema_service.py

import asyncio
import logging
from datetime import datetime
from typing import Any, Dict, Optional

from app.config import settings
from app.services.feed_cache_service import update_candle
from app.services.indicator_service import indicator_cache
from app.services.options_history_service import options_history_cache
from app.websocket.websocket_manager import websocket_manager

logger = logging.getLogger("uvicorn")


# ---------------------------------------------------------------------
# Runtime Cache
# ---------------------------------------------------------------------

live_ema_cache: Dict[str, Dict[str, Any]] = {}

live_crossover_events = []


class LiveEMAService:
    """
    Real-time EMA Engine.

    Responsibilities:
    -----------------
    1. Consume live Upstox feed
    2. Extract LTP from full / ltpc feed
    3. Build live 1-minute candles
    4. Aggregate 3-minute and 5-minute candles
    5. Maintain EMA short / EMA long state
    6. Detect bullish / bearish crossovers
    7. Store runtime signals
    8. Publish completed candles to custom websocket clients
    """

    def __init__(self):
        self.ema_short_period = settings.EMA_SHORT_PERIOD
        self.ema_long_period = settings.EMA_LONG_PERIOD

        self.ema_short_multiplier = 2 / (self.ema_short_period + 1)
        self.ema_long_multiplier = 2 / (self.ema_long_period + 1)

        # Runtime aggregation cache for 3m and 5m candles.
        #
        # Structure:
        # {
        #     "NSE_FO|63935": {
        #         3: {...},
        #         5: {...}
        #     }
        # }
        self.interval_candles: Dict[str, Dict[int, Optional[Dict[str, Any]]]] = {}

    # -----------------------------------------------------------------
    # Initialization
    # -----------------------------------------------------------------

    def initialize_from_historical_cache(self):
        """
        Called after morning historical pipeline.

        Loads latest EMA values from indicator_cache into
        live runtime cache.
        """

        initialized = 0

        live_ema_cache.clear()
        self.interval_candles.clear()

        for instrument_key, history_data in options_history_cache.items():
            trading_symbol = history_data.get(
                "trading_symbol",
                instrument_key,
            )

            indicator_data = indicator_cache.get(trading_symbol)

            if not indicator_data:
                continue

            ema_short = indicator_data.get("last_ema_short")
            ema_long = indicator_data.get("last_ema_long")
            last_close = indicator_data.get("last_close")
            last_timestamp = indicator_data.get("last_timestamp")

            # Fallback for older indicator cache format.
            if ema_short is None or ema_long is None:
                series = indicator_data.get("indicator_series", [])

                if not series:
                    continue

                latest = series[-1]

                ema_short = latest.get(f"ema_{self.ema_short_period}")
                ema_long = latest.get(f"ema_{self.ema_long_period}")
                last_close = latest.get("close")
                last_timestamp = latest.get("timestamp")

            if ema_short is None or ema_long is None:
                continue

            live_ema_cache[instrument_key] = {
                "instrument_key": instrument_key,
                "trading_symbol": trading_symbol,
                "ema_short_period": self.ema_short_period,
                "ema_long_period": self.ema_long_period,
                # Generic EMA fields
                "ema_short": ema_short,
                "ema_long": ema_long,
                # Backward-compatible fields
                "ema9": ema_short,
                "ema21": ema_long,
                "last_close": last_close,
                "last_timestamp": last_timestamp,
                "last_signal": None,
                "last_crossover": None,
                # Current 1-minute candle
                "candle": None,
                "updated_at": datetime.now().isoformat(),
            }

            self.interval_candles[instrument_key] = {
                3: None,
                5: None,
            }

            initialized += 1

        logger.info(f"Live EMA Cache Initialized for {initialized} instruments")

        return initialized

    # -----------------------------------------------------------------
    # Feed Helpers
    # -----------------------------------------------------------------

    @staticmethod
    def extract_ltp(feed):
        """
        Extract LTP from both supported Upstox feed structures.

        FULL mode:
            feed["fullFeed"]["marketFF"]["ltpc"]["ltp"]

        LTPC mode:
            feed["ltpc"]["ltp"]
        """

        ltp = None

        # FULL mode
        try:
            ltp = (
                feed.get("fullFeed", {}).get("marketFF", {}).get("ltpc", {}).get("ltp")
            )
        except Exception:
            ltp = None

        # LTPC fallback
        if ltp is None:
            try:
                ltp = feed.get("ltpc", {}).get("ltp")
            except Exception:
                ltp = None

        if ltp is None:
            return None

        try:
            return float(ltp)
        except Exception:
            return None

    @staticmethod
    def get_minute_bucket(current_ts):
        """
        Converts Upstox timestamp into minute bucket.

        Example:
            1785134555215 -> 2026-07-28 09:15
        """

        try:
            ts = int(current_ts)
            dt = datetime.fromtimestamp(ts / 1000)
            return dt.strftime("%Y-%m-%d %H:%M")

        except Exception:
            return datetime.now().strftime("%Y-%m-%d %H:%M")

    @staticmethod
    def get_interval_bucket(minute_string, interval):
        """
        Converts a 1-minute bucket into interval bucket.

        Example:
            2026-07-28 09:17 with interval=3
            becomes
            2026-07-28 09:15
        """

        try:
            dt = datetime.strptime(
                minute_string,
                "%Y-%m-%d %H:%M",
            )

            bucket_minute = (dt.minute // interval) * interval

            bucket_dt = dt.replace(
                minute=bucket_minute,
                second=0,
                microsecond=0,
            )

            return bucket_dt.strftime("%Y-%m-%d %H:%M")

        except Exception:
            return minute_string

    # -----------------------------------------------------------------
    # WebSocket Publish Helper
    # -----------------------------------------------------------------

    @staticmethod
    def publish_candle_to_clients(
        instrument_key,
        interval,
        candle,
    ):
        """
        Publishes completed candle to custom websocket subscribers.

        Note:
        If this is called from the Upstox websocket thread and no running
        event loop is available, it safely skips websocket publishing.
        The candle cache is still updated.
        """

        payload = {
            "feed_type": "candle",
            "instrument_key": instrument_key,
            "interval": interval,
            "timestamp": datetime.now().isoformat(),
            "candle": candle,
        }

        try:
            loop = asyncio.get_running_loop()

            loop.create_task(
                websocket_manager.publish_candle(
                    instrument_key=instrument_key,
                    interval=interval,
                    candle=payload,
                )
            )

        except RuntimeError:
            logger.debug(
                f"No running event loop available to publish "
                f"{interval}m candle for {instrument_key}."
            )

        except Exception as ex:
            logger.error(
                f"Failed scheduling candle publish for "
                f"{instrument_key}, interval={interval}: {ex}"
            )

    # -----------------------------------------------------------------
    # EMA Formula
    # -----------------------------------------------------------------

    @staticmethod
    def next_ema(
        close_price,
        previous_ema,
        multiplier,
    ):
        """
        Incremental EMA formula.

        New EMA =
        ((Close - Previous EMA) * Multiplier) + Previous EMA
        """

        return round(
            ((close_price - previous_ema) * multiplier) + previous_ema,
            4,
        )

    # -----------------------------------------------------------------
    # Main Feed Processor
    # -----------------------------------------------------------------

    def process_market_feed(
        self,
        instrument_key,
        feed,
        current_ts,
    ):
        """
        Receives live market feed from Upstox websocket.

        Builds 1-minute candles from ticks.

        On each completed 1-minute candle:
        - Updates 1m candle cache
        - Publishes 1m candle to custom websocket subscribers
        - Updates EMA
        - Detects EMA crossover
        - Aggregates 3m and 5m candles
        """

        if instrument_key not in live_ema_cache:
            return

        try:
            ltp = self.extract_ltp(feed)

            if ltp is None:
                return

            minute_bucket = self.get_minute_bucket(current_ts)

            instrument_state = live_ema_cache[instrument_key]

            candle = instrument_state.get("candle")

            # -----------------------------------------------------
            # First Tick
            # -----------------------------------------------------

            if candle is None:
                instrument_state["candle"] = {
                    "minute": minute_bucket,
                    "open": ltp,
                    "high": ltp,
                    "low": ltp,
                    "close": ltp,
                    "tick_count": 1,
                }

                return

            # -----------------------------------------------------
            # Same 1-Minute Candle
            # -----------------------------------------------------

            if candle["minute"] == minute_bucket:
                candle["high"] = max(candle["high"], ltp)
                candle["low"] = min(candle["low"], ltp)
                candle["close"] = ltp
                candle["tick_count"] = candle.get("tick_count", 0) + 1

                return

            # -----------------------------------------------------
            # Previous 1-Minute Candle Completed
            # -----------------------------------------------------

            completed_candle = candle.copy()

            update_candle(
                instrument_key=instrument_key,
                interval=1,
                candle=completed_candle,
            )

            self.publish_candle_to_clients(
                instrument_key=instrument_key,
                interval=1,
                candle=completed_candle,
            )

            self.process_completed_candle(
                instrument_key=instrument_key,
                candle=completed_candle,
            )

            self.process_interval_aggregation(
                instrument_key=instrument_key,
                completed_1m_candle=completed_candle,
                interval=3,
            )

            self.process_interval_aggregation(
                instrument_key=instrument_key,
                completed_1m_candle=completed_candle,
                interval=5,
            )

            # -----------------------------------------------------
            # Start New 1-Minute Candle
            # -----------------------------------------------------

            instrument_state["candle"] = {
                "minute": minute_bucket,
                "open": ltp,
                "high": ltp,
                "low": ltp,
                "close": ltp,
                "tick_count": 1,
            }

        except Exception as ex:
            logger.error(f"Live EMA Processing Failed for {instrument_key}: {ex}")

    # -----------------------------------------------------------------
    # 3m / 5m Aggregation
    # -----------------------------------------------------------------

    def process_interval_aggregation(
        self,
        instrument_key,
        completed_1m_candle,
        interval,
    ):
        """
        Aggregates completed 1-minute candles into 3-minute and 5-minute candles.
        """

        if interval not in [3, 5]:
            return

        try:
            if instrument_key not in self.interval_candles:
                self.interval_candles[instrument_key] = {
                    3: None,
                    5: None,
                }

            interval_bucket = self.get_interval_bucket(
                completed_1m_candle["minute"],
                interval,
            )

            current_interval_candle = self.interval_candles[instrument_key].get(
                interval
            )

            # -----------------------------------------------------
            # First interval candle
            # -----------------------------------------------------

            if current_interval_candle is None:
                self.interval_candles[instrument_key][interval] = {
                    "minute": interval_bucket,
                    "open": completed_1m_candle["open"],
                    "high": completed_1m_candle["high"],
                    "low": completed_1m_candle["low"],
                    "close": completed_1m_candle["close"],
                    "source_candles": 1,
                }

                return

            # -----------------------------------------------------
            # Same interval bucket
            # -----------------------------------------------------

            if current_interval_candle["minute"] == interval_bucket:
                current_interval_candle["high"] = max(
                    current_interval_candle["high"],
                    completed_1m_candle["high"],
                )

                current_interval_candle["low"] = min(
                    current_interval_candle["low"],
                    completed_1m_candle["low"],
                )

                current_interval_candle["close"] = completed_1m_candle["close"]

                current_interval_candle["source_candles"] = (
                    current_interval_candle.get("source_candles", 0) + 1
                )

                return

            # -----------------------------------------------------
            # Interval candle completed
            # -----------------------------------------------------

            completed_interval_candle = current_interval_candle.copy()

            update_candle(
                instrument_key=instrument_key,
                interval=interval,
                candle=completed_interval_candle,
            )

            self.publish_candle_to_clients(
                instrument_key=instrument_key,
                interval=interval,
                candle=completed_interval_candle,
            )

            self.interval_candles[instrument_key][interval] = {
                "minute": interval_bucket,
                "open": completed_1m_candle["open"],
                "high": completed_1m_candle["high"],
                "low": completed_1m_candle["low"],
                "close": completed_1m_candle["close"],
                "source_candles": 1,
            }

        except Exception as ex:
            logger.error(
                f"Interval aggregation failed for "
                f"{instrument_key}, interval={interval}: {ex}"
            )

    # -----------------------------------------------------------------
    # Completed 1-Minute Candle Processor
    # -----------------------------------------------------------------

    def process_completed_candle(
        self,
        instrument_key,
        candle,
    ):
        """
        Called once for every completed 1-minute candle.
        """

        state = live_ema_cache.get(instrument_key)

        if not state:
            return

        try:
            close_price = float(candle["close"])

            old_ema_short = float(state["ema_short"])
            old_ema_long = float(state["ema_long"])

            new_ema_short = self.next_ema(
                close_price=close_price,
                previous_ema=old_ema_short,
                multiplier=self.ema_short_multiplier,
            )

            new_ema_long = self.next_ema(
                close_price=close_price,
                previous_ema=old_ema_long,
                multiplier=self.ema_long_multiplier,
            )

            bullish_cross = (
                old_ema_short < old_ema_long and new_ema_short >= new_ema_long
            )

            bearish_cross = (
                old_ema_short > old_ema_long and new_ema_short <= new_ema_long
            )

            signal = None

            if bullish_cross:
                signal = "Bullish Cross"

            elif bearish_cross:
                signal = "Bearish Cross"

            state["ema_short"] = new_ema_short
            state["ema_long"] = new_ema_long

            # Backward-compatible fields
            state["ema9"] = new_ema_short
            state["ema21"] = new_ema_long

            state["last_close"] = close_price
            state["last_timestamp"] = candle.get("minute")
            state["updated_at"] = datetime.now().isoformat()

            if signal:
                crossover_event = {
                    "instrument_key": instrument_key,
                    "trading_symbol": state.get("trading_symbol"),
                    "timestamp": datetime.now().isoformat(),
                    "candle_minute": candle.get("minute"),
                    "signal": signal,
                    "close": close_price,
                    "ema_short_period": self.ema_short_period,
                    "ema_long_period": self.ema_long_period,
                    "ema_short": new_ema_short,
                    "ema_long": new_ema_long,
                    "ema9": new_ema_short,
                    "ema21": new_ema_long,
                }

                state["last_signal"] = signal
                state["last_crossover"] = crossover_event

                live_crossover_events.insert(0, crossover_event)

                max_events = getattr(
                    settings,
                    "MAX_CROSSOVER_EVENTS",
                    500,
                )

                if len(live_crossover_events) > max_events:
                    del live_crossover_events[max_events:]

                logger.info(
                    f"{signal} | "
                    f"{state.get('trading_symbol')} | "
                    f"Close={close_price} | "
                    f"EMA{self.ema_short_period}={new_ema_short} | "
                    f"EMA{self.ema_long_period}={new_ema_long}"
                )

        except Exception as ex:
            logger.exception(f"Completed Candle Processing Failed: {ex}")

    # -----------------------------------------------------------------
    # API Helpers
    # -----------------------------------------------------------------

    def get_instrument_state(
        self,
        instrument_key,
    ):
        return live_ema_cache.get(instrument_key)

    def get_all_states(self):
        return live_ema_cache

    def get_crossovers(self):
        return live_crossover_events

    def clear(self):
        live_ema_cache.clear()
        live_crossover_events.clear()
        self.interval_candles.clear()

        logger.info("Live EMA Cache Cleared")


live_ema_service = LiveEMAService()
