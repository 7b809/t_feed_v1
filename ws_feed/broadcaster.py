import asyncio
import json
import os
import logging
from collections import deque
from threading import Lock
from typing import Dict, Any, Set

from fastapi import WebSocket

from core.logger import get_logger

logger = get_logger(__file__)

# ------------------------------------------------------------
# Disable console printing for this logger only.
# Logs will still be written to logs/broadcaster.log.
# ------------------------------------------------------------
for handler in list(logger.handlers):
    if type(handler) is logging.StreamHandler:
        logger.removeHandler(handler)

# Configure raw feeds log path and max line limit
LOG_DIR = "logs"
FEEDS_LOG_PATH = os.path.join(LOG_DIR, "feeds.log")
MAX_LOG_LINES = 2000

# Thread-safe in-memory feed log buffer
_feed_log_lock = Lock()
_feed_log_buffer = deque(maxlen=MAX_LOG_LINES)
_feed_log_initialized = False


def _initialize_feed_log_buffer():
    """Loads existing feeds.log into memory once, capped to MAX_LOG_LINES."""
    global _feed_log_initialized

    if _feed_log_initialized:
        return

    os.makedirs(LOG_DIR, exist_ok=True)

    if os.path.exists(FEEDS_LOG_PATH):
        try:
            with open(FEEDS_LOG_PATH, "r", encoding="utf-8") as f:
                existing_lines = f.readlines()

            for line in existing_lines[-MAX_LOG_LINES:]:
                _feed_log_buffer.append(line)

        except Exception as ex:
            logger.error(
                f"Failed initializing feed log buffer from {FEEDS_LOG_PATH}: "
                f"{type(ex).__name__}: {ex}"
            )

    _feed_log_initialized = True


def append_raw_feed_log(instrument_key: str, tick_raw: Dict[str, Any]):
    """
    Appends raw feed tick JSON to logs/feeds.log and caps the file at MAX_LOG_LINES.

    This writes raw ticks to file only.
    It does not print raw ticks to console.
    """

    try:
        with _feed_log_lock:
            _initialize_feed_log_buffer()

            log_entry = json.dumps(
                {
                    "instrument_key": instrument_key,
                    "raw_feed": tick_raw,
                },
                default=str,
            ) + "\n"

            _feed_log_buffer.append(log_entry)

            with open(FEEDS_LOG_PATH, "w", encoding="utf-8") as f:
                f.writelines(_feed_log_buffer)

    except Exception as e:
        logger.error(
            f"Failed writing raw feed to {FEEDS_LOG_PATH}: "
            f"{type(e).__name__}: {e}"
        )


# --- Safe Helper Functions ---
def safe_float(val: Any, default: float = 0.0) -> float:
    """Safely converts a value to float without throwing exceptions."""
    if val is None:
        return default

    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def safe_int(val: Any, default: int = 0) -> int:
    """Safely converts a value to integer without throwing exceptions."""
    if val is None:
        return default

    try:
        return int(val)
    except (ValueError, TypeError):
        return default


def normalize_interval(interval: Any) -> int:
    """Normalizes interval value. 0 means live tick."""
    try:
        return int(interval)
    except (ValueError, TypeError):
        return 0


def build_option_key(strike_price: float, instrument_type: str, interval: int = 0) -> str:
    """
    Builds interval-aware option connection key.

    Examples:
        24500.0_CE_0
        24500.0_CE_1
        24500.0_CE_5
    """

    return f"{float(strike_price)}_{str(instrument_type).upper()}_{normalize_interval(interval)}"


class Broadcaster:
    """Manages FastAPI WebSocket client connections and broadcasts parsed market ticks."""

    def __init__(self):
        # Generic live connection pool
        self.active_connections: Set[WebSocket] = set()

        # Dedicated EMA crossover event connections pool
        self.ema_crossover_connections: Set[WebSocket] = set()

        # Interval-aware all-feeds connections
        # Example:
        # {
        #   0: {websocket1, websocket2},
        #   1: {websocket3},
        #   5: {websocket4}
        # }
        self.all_feeds_connections: Dict[int, Set[WebSocket]] = {}

        # Interval-aware option connections
        # Example:
        # {
        #   "24500.0_CE_0": {websocket1},
        #   "24500.0_CE_1": {websocket2},
        #   "24500.0_CE_5": {websocket3}
        # }
        self.option_connections: Dict[str, Set[WebSocket]] = {}

        # Counters
        self.broadcast_count = 0
        self.candle_broadcast_count = 0
        self.ema_cross_broadcast_count = 0
        self.sent_count = 0
        self.failed_send_count = 0

        logger.info("Broadcaster initialized")

    def get_active_connections_count(self) -> int:
        """Returns total active WebSocket clients across all route types."""

        return (
            len(self.active_connections)
            + len(self.ema_crossover_connections)
            + sum(len(s) for s in self.all_feeds_connections.values())
            + sum(len(s) for s in self.option_connections.values())
        )

    # ========================================================
    # Basic Connection Handlers
    # ========================================================
    async def connect(self, websocket: WebSocket):
        """Tracks a generic live WebSocket connection."""

        self.active_connections.add(websocket)

        logger.info(
            f"Generic WebSocket client connected. "
            f"active_connections={len(self.active_connections)}, "
            f"total_clients={self.get_active_connections_count()}"
        )

    def disconnect(self, websocket: WebSocket):
        """Removes a generic WebSocket connection."""

        self.active_connections.discard(websocket)

        logger.info(
            f"Generic WebSocket client disconnected. "
            f"active_connections={len(self.active_connections)}, "
            f"total_clients={self.get_active_connections_count()}"
        )

    # ========================================================
    # EMA Crossover Connection Handlers
    # ========================================================
    async def connect_ema_crossover(self, websocket: WebSocket):
        """Tracks connection for /ws/ema-crossover endpoint."""

        self.ema_crossover_connections.add(websocket)

        logger.info(
            f"Client connected to /ws/ema-crossover. "
            f"ema_clients={len(self.ema_crossover_connections)}, "
            f"total_clients={self.get_active_connections_count()}"
        )

    def disconnect_ema_crossover(self, websocket: WebSocket):
        """Removes connection for /ws/ema-crossover endpoint."""

        self.ema_crossover_connections.discard(websocket)

        logger.info(
            f"Client disconnected from /ws/ema-crossover. "
            f"remaining_ema_clients={len(self.ema_crossover_connections)}, "
            f"total_clients={self.get_active_connections_count()}"
        )

    # ========================================================
    # All Feeds Connection Handlers
    # ========================================================
    async def connect_all_feeds(self, websocket: WebSocket, interval: int = 0):
        """Tracks connection for /all-feeds endpoint by interval."""

        interval = normalize_interval(interval)

        if interval not in self.all_feeds_connections:
            self.all_feeds_connections[interval] = set()

        self.all_feeds_connections[interval].add(websocket)

        logger.info(
            f"Client connected to /all-feeds. "
            f"interval={interval}, "
            f"clients_for_interval={len(self.all_feeds_connections[interval])}, "
            f"total_clients={self.get_active_connections_count()}"
        )

    def disconnect_all_feeds(self, websocket: WebSocket, interval: int = 0):
        """Removes connection for /all-feeds endpoint."""

        interval = normalize_interval(interval)

        if interval in self.all_feeds_connections:
            self.all_feeds_connections[interval].discard(websocket)

            logger.info(
                f"Client disconnected from /all-feeds. "
                f"interval={interval}, "
                f"remaining_for_interval={len(self.all_feeds_connections[interval])}, "
                f"total_clients={self.get_active_connections_count()}"
            )

    # ========================================================
    # Option Connection Handlers
    # ========================================================
    async def connect_option(
        self,
        websocket: WebSocket,
        strike_price: float,
        instrument_type: str,
        interval: int = 0,
    ):
        """Tracks connection for /option endpoint filtered by strike, CE/PE and interval."""

        interval = normalize_interval(interval)
        key = build_option_key(strike_price, instrument_type, interval)

        if key not in self.option_connections:
            self.option_connections[key] = set()
            logger.info(f"Created new option connection pool for key: {key}")

        self.option_connections[key].add(websocket)

        logger.info(
            f"Client connected to /option. "
            f"key={key}, "
            f"clients_for_key={len(self.option_connections[key])}, "
            f"total_clients={self.get_active_connections_count()}"
        )

    def disconnect_option(
        self,
        websocket: WebSocket,
        strike_price: float,
        instrument_type: str,
        interval: int = 0,
    ):
        """Removes connection for /option endpoint."""

        interval = normalize_interval(interval)
        key = build_option_key(strike_price, instrument_type, interval)

        if key in self.option_connections:
            self.option_connections[key].discard(websocket)

            logger.info(
                f"Client disconnected from /option. "
                f"key={key}, "
                f"remaining_for_key={len(self.option_connections[key])}, "
                f"total_clients={self.get_active_connections_count()}"
            )

            if len(self.option_connections[key]) == 0:
                logger.info(f"Option connection pool empty for key: {key}")

        else:
            logger.warning(
                f"disconnect_option called, but key not found: {key}. "
                f"Available keys: {list(self.option_connections.keys())}"
            )

    # ========================================================
    # Tick Parsing Helper
    # ========================================================
    def build_live_payload(
        self,
        instrument_key: str,
        tick_raw: Dict[str, Any],
        contract_info: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Builds normalized live tick payload from Upstox raw feed."""

        raw_feed_obj = tick_raw.get("raw_feed", tick_raw)
        full_feed = raw_feed_obj.get("fullFeed", raw_feed_obj)
        ff_wrapper = full_feed.get("ff", full_feed)

        ff = (
            ff_wrapper.get("marketFF")
            or ff_wrapper.get("indexFF")
            or full_feed.get("marketFF")
            or full_feed.get("indexFF")
            or full_feed
        )

        ltpc = ff.get("ltpc") or {}
        ltp = safe_float(ltpc.get("ltp"))
        close = safe_float(ltpc.get("cp"))
        ltt = safe_int(ltpc.get("ltt"))
        ltq = safe_int(ltpc.get("ltq"))

        if "marketOHLC" in ff:
            ohlc_list = ff.get("marketOHLC", {}).get("ohlc", [])
        else:
            ohlc_list = ff.get("optionOHLC", {}).get("ohlc", [])

        daily_ohlc = next(
            (item for item in ohlc_list if item.get("interval") == "1d"),
            {},
        )

        open_price = safe_float(daily_ohlc.get("open"))
        high_price = safe_float(daily_ohlc.get("high"))
        low_price = safe_float(daily_ohlc.get("low"))
        day_volume = safe_int(daily_ohlc.get("vol")) or safe_int(
            daily_ohlc.get("volume")
        )

        atp = safe_float(ff.get("atp"))
        vtt = safe_int(ff.get("vtt"))
        oi = safe_int(ff.get("oi"))
        iv = safe_float(ff.get("iv"))
        upper_circuit = safe_float(ff.get("uc"))
        lower_circuit = safe_float(ff.get("lc"))

        greeks_raw = ff.get("optionGreeks")
        greeks_data = None

        if isinstance(greeks_raw, dict) and greeks_raw:
            greeks_data = {
                "iv": iv,
                "delta": safe_float(greeks_raw.get("delta")),
                "theta": safe_float(greeks_raw.get("theta")),
                "gamma": safe_float(greeks_raw.get("gamma")),
                "vega": safe_float(greeks_raw.get("vega")),
                "rho": safe_float(greeks_raw.get("rho")),
            }

        bid_ask = ff.get("marketLevel", {}).get("bidAskQuote", [])
        market_depth = []

        if isinstance(bid_ask, list):
            for level in bid_ask[:5]:
                market_depth.append(
                    {
                        "bid_qty": safe_int(level.get("bidQ") or level.get("bq")),
                        "bid_price": safe_float(level.get("bidP") or level.get("bp")),
                        "ask_qty": safe_int(level.get("askQ") or level.get("aq")),
                        "ask_price": safe_float(level.get("askP") or level.get("ap")),
                    }
                )

        price_change = round(ltp - close, 2) if (ltp > 0 and close > 0) else 0.0

        p_change_pct = (
            round((price_change / close) * 100, 2)
            if (ltp > 0 and close > 0)
            else 0.0
        )

        return {
            "type": "live_tick",
            "interval": 0,
            "instrument_key": instrument_key,
            "ltp": ltp,
            "close": close,
            "change": price_change,
            "change_pct": p_change_pct,
            "ltt": ltt,
            "ltq": ltq,
            "open": open_price,
            "high": high_price,
            "low": low_price,
            "volume": day_volume or vtt,
            "atp": atp,
            "oi": oi,
            "upper_circuit": upper_circuit,
            "lower_circuit": lower_circuit,
            "greeks": greeks_data,
            "depth": market_depth,
            "info": contract_info or {},
        }

    # ========================================================
    # Core Live Tick Broadcast Engine
    # ========================================================
    async def broadcast_tick(
        self,
        instrument_key: str,
        tick_raw: Dict[str, Any],
        contract_info: Dict[str, Any],
    ):
        """
        Saves raw feed to logs/feeds.log and broadcasts live tick to interval=0 clients.
        """

        self.broadcast_count += 1

        # Save all raw feeds to logs/feeds.log, latest 2000 lines only.
        # Done in a worker thread to avoid blocking FastAPI's event loop.
        await asyncio.to_thread(append_raw_feed_log, instrument_key, tick_raw)

        total_clients = self.get_active_connections_count()

        if total_clients == 0:
            return

        try:
            payload = self.build_live_payload(
                instrument_key=instrument_key,
                tick_raw=tick_raw,
                contract_info=contract_info,
            )

            message_str = json.dumps(payload, default=str)

            # interval=0 means live tick
            target_connections = set(self.active_connections)

            # All-feeds live clients
            target_connections |= set(self.all_feeds_connections.get(0, set()))

            # Option live clients
            c_info = contract_info or {}
            strike = c_info.get("strike_price")
            itype = c_info.get("instrument_type")

            if strike is not None and itype:
                option_key = build_option_key(strike, itype, interval=0)

                if option_key in self.option_connections:
                    target_connections |= set(self.option_connections[option_key])

            if len(target_connections) == 0:
                return

            await self._send_to_connections(target_connections, message_str)

        except Exception as ex:
            logger.error(
                f"Exception inside broadcast_tick for instrument_key={instrument_key}: "
                f"{type(ex).__name__}: {ex}"
            )

    # ========================================================
    # Live EMA Cross Broadcast Engine
    # ========================================================
    async def broadcast_ema_cross(self, ema_cross_event: Dict[str, Any]):
        """
        Broadcasts live EMA crossover event to connected clients.

        Payload example:
            {
                "type": "live_ema_cross",
                "instrument_key": "NSE_INDEX|Nifty 50",
                "timestamp": "2026-08-05T09:16:00+05:30",
                "cross_type": "bullish_cross",
                "interval_minutes": 1,
                "close": 24774.3,
                "ema_fast": 24613.6098,
                "ema_slow": 24593.3994,
                "info": {...}
            }
        """

        self.ema_cross_broadcast_count += 1

        total_clients = self.get_active_connections_count()

        if total_clients == 0:
            return

        try:
            if not isinstance(ema_cross_event, dict):
                return

            event = dict(ema_cross_event)
            event["type"] = event.get("type", "live_ema_cross")

            message_str = json.dumps(event, default=str)

            target_connections = set(self.active_connections)

            # Send EMA cross to dedicated EMA crossover pool
            target_connections |= set(self.ema_crossover_connections)

            # Send EMA cross to all /all-feeds clients.
            for conn_set in self.all_feeds_connections.values():
                target_connections |= set(conn_set)

            # Send to matching /option clients if option info exists.
            info = event.get("info") or {}
            strike = info.get("strike_price")
            itype = info.get("instrument_type")
            interval_minutes = normalize_interval(event.get("interval_minutes", 0))

            if strike is not None and itype:
                # Match exact interval pool first, e.g. 24500.0_CE_1
                option_key = build_option_key(strike, itype, interval=interval_minutes)

                if option_key in self.option_connections:
                    target_connections |= set(self.option_connections[option_key])

                # Also send to live interval 0 pool for compatibility.
                live_option_key = build_option_key(strike, itype, interval=0)

                if live_option_key in self.option_connections:
                    target_connections |= set(self.option_connections[live_option_key])

            if len(target_connections) == 0:
                return

            await self._send_to_connections(target_connections, message_str)

        except Exception as ex:
            logger.error(
                f"Exception inside broadcast_ema_cross: "
                f"{type(ex).__name__}: {ex}"
            )

    # ========================================================
    # Candle Broadcast Engine
    # ========================================================
    async def broadcast_candle(
        self,
        instrument_key: str,
        candle_payload: Dict[str, Any],
        contract_info: Dict[str, Any],
        interval: int,
    ):
        """
        Broadcasts candle payload to interval-based clients.

        Expected interval:
            1, 3, or 5
        """

        self.candle_broadcast_count += 1
        interval = normalize_interval(interval)

        total_clients = self.get_active_connections_count()

        if total_clients == 0:
            return

        try:
            payload = {
                "type": "candle",
                "interval": interval,
                "instrument_key": instrument_key,
                **candle_payload,
                "info": contract_info or {},
            }

            message_str = json.dumps(payload, default=str)

            target_connections = set()

            # All-feeds candle clients for this interval
            target_connections |= set(self.all_feeds_connections.get(interval, set()))

            # Option candle clients for this interval
            c_info = contract_info or {}
            strike = c_info.get("strike_price")
            itype = c_info.get("instrument_type")

            if strike is not None and itype:
                option_key = build_option_key(strike, itype, interval=interval)

                if option_key in self.option_connections:
                    target_connections |= set(self.option_connections[option_key])

            if len(target_connections) == 0:
                return

            await self._send_to_connections(target_connections, message_str)

        except Exception as ex:
            logger.error(
                f"Exception inside broadcast_candle for instrument_key={instrument_key}, "
                f"interval={interval}: {type(ex).__name__}: {ex}"
            )

    # ========================================================
    # Send Helper
    # ========================================================
    async def _send_to_connections(self, target_connections: Set[WebSocket], message_str: str):
        """Sends message to resolved WebSocket connections and cleans dead clients."""

        dead_connections = set()

        for connection in target_connections:
            try:
                await connection.send_text(message_str)
                self.sent_count += 1

            except Exception as send_ex:
                self.failed_send_count += 1

                logger.error(
                    f"Failed sending payload to WebSocket client: "
                    f"{type(send_ex).__name__}: {send_ex}"
                )

                dead_connections.add(connection)

        for dead in dead_connections:
            if dead in self.active_connections:
                self.disconnect(dead)

            if dead in self.ema_crossover_connections:
                self.disconnect_ema_crossover(dead)

            for interval, conn_set in list(self.all_feeds_connections.items()):
                if dead in conn_set:
                    conn_set.discard(dead)

                    logger.info(
                        f"Dead client removed from /all-feeds pool. "
                        f"interval={interval}, remaining={len(conn_set)}"
                    )

            for key, conn_set in list(self.option_connections.items()):
                if dead in conn_set:
                    conn_set.discard(dead)

                    logger.info(
                        f"Dead client removed from /option pool. "
                        f"key={key}, remaining={len(conn_set)}"
                    )


broadcaster = Broadcaster()