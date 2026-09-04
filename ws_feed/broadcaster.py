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

LOG_DIR = "logs"
FEEDS_LOG_PATH = os.path.join(LOG_DIR, "feeds.log")
MAX_LOG_LINES = 2000

_feed_log_lock = Lock()
_feed_log_buffer = deque(maxlen=MAX_LOG_LINES)
_feed_log_initialized = False


def _initialize_feed_log_buffer():
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
            logger.error(f"Failed initializing feed log buffer from {FEEDS_LOG_PATH}: {type(ex).__name__}: {ex}")
    _feed_log_initialized = True


def append_raw_feed_log(instrument_key: str, tick_raw: Dict[str, Any]):
    try:
        with _feed_log_lock:
            _initialize_feed_log_buffer()
            log_entry = json.dumps({"instrument_key": instrument_key, "raw_feed": tick_raw}, default=str) + "\n"
            _feed_log_buffer.append(log_entry)
            with open(FEEDS_LOG_PATH, "w", encoding="utf-8") as f:
                f.writelines(_feed_log_buffer)
    except Exception as e:
        logger.error(f"Failed writing raw feed to {FEEDS_LOG_PATH}: {type(e).__name__}: {e}")


# ============================================================
# Safe Helper Functions
# ============================================================


def safe_float(val: Any, default: float = 0.0) -> float:
    if val is None:
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def safe_int(val: Any, default: int = 0) -> int:
    if val is None:
        return default
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


def normalize_interval(interval: Any) -> int:
    try:
        return int(interval)
    except (ValueError, TypeError):
        return 0


def build_option_key(strike_price: float, instrument_type: str, interval: int = 0) -> str:
    return f"{float(strike_price)}_{str(instrument_type).upper()}_{normalize_interval(interval)}"


def resolve_contract_info_for_event(event: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(event, dict):
        return {}
    info = event.get("info") or event.get("contract_info") or {}
    if isinstance(info, dict) and info.get("strike_price") is not None:
        return info
    instrument_key = event.get("instrument_key")
    if not instrument_key:
        return info if isinstance(info, dict) else {}
    try:
        from services.option_service import get_feed_by_instrument_key
        resolved_info = get_feed_by_instrument_key(instrument_key)
        if isinstance(resolved_info, dict):
            return resolved_info
    except Exception as ex:
        logger.error(f"Failed resolving contract info for event. instrument_key={instrument_key}, error={type(ex).__name__}: {ex}")
    return info if isinstance(info, dict) else {}


class Broadcaster:
    """
    Manages FastAPI WebSocket client connections and broadcasts parsed market ticks.

    Current flow:
    - Live ticks are broadcast to generic, all-feeds, and matching option clients.
    - EMA crossover events are broadcast for all instruments.
    - Opening Range touch events are broadcast for all instruments.
    - No EMA event is filtered by isolated instrument here.
    - Isolated instrument Telegram alert filtering is handled outside this broadcaster.
    - EMA payload may include:
        opening_range
        touch_status
        latest_intraday_close
        latest_main_index_ltp
        isolated_instrument
        EMA details
        contract_info
    - Broadcaster preserves and forwards the EMA event as-is.
    - Broadcaster internally resolves contract info only for routing to /option clients.
    """

    def __init__(self):
        self.active_connections: Set[WebSocket] = set()
        self.ema_crossover_connections: Set[WebSocket] = set()
        self.ema_instrument_connections: Dict[str, Set[WebSocket]] = {}
        self.opening_range_connections: Set[WebSocket] = set()
        self.opening_range_instrument_connections: Dict[str, Set[WebSocket]] = {}
        self.all_feeds_connections: Dict[int, Set[WebSocket]] = {}
        self.option_connections: Dict[str, Set[WebSocket]] = {}
        self.broadcast_count = 0
        self.candle_broadcast_count = 0
        self.ema_cross_broadcast_count = 0
        self.opening_range_broadcast_count = 0
        self.sent_count = 0
        self.failed_send_count = 0
        logger.info("Broadcaster initialized")

    def get_active_connections_count(self) -> int:
        return (len(self.active_connections) + len(self.ema_crossover_connections) +
                sum(len(s) for s in self.ema_instrument_connections.values()) +
                len(self.opening_range_connections) +
                sum(len(s) for s in self.opening_range_instrument_connections.values()) +
                sum(len(s) for s in self.all_feeds_connections.values()) +
                sum(len(s) for s in self.option_connections.values()))

    # ========================================================
    # Basic Connection Handlers
    # ========================================================

    async def connect(self, websocket: WebSocket):
        self.active_connections.add(websocket)
        logger.info(f"Generic WebSocket client connected. active_connections={len(self.active_connections)}, total_clients={self.get_active_connections_count()}")

    def disconnect(self, websocket: WebSocket):
        self.active_connections.discard(websocket)
        logger.info(f"Generic WebSocket client disconnected. active_connections={len(self.active_connections)}, total_clients={self.get_active_connections_count()}")

    # ========================================================
    # Global EMA Crossover Connection Handlers
    # ========================================================

    async def connect_ema_crossover(self, websocket: WebSocket):
        self.ema_crossover_connections.add(websocket)
        logger.info(f"Client connected to /ws/ema-crossover. ema_clients={len(self.ema_crossover_connections)}, total_clients={self.get_active_connections_count()}")

    def disconnect_ema_crossover(self, websocket: WebSocket):
        self.ema_crossover_connections.discard(websocket)
        logger.info(f"Client disconnected from /ws/ema-crossover. remaining_ema_clients={len(self.ema_crossover_connections)}, total_clients={self.get_active_connections_count()}")

    # ========================================================
    # Instrument-Specific EMA Crossover Connection Handlers
    # ========================================================

    async def connect_ema_instrument(self, websocket: WebSocket, instrument_key: str):
        if not instrument_key:
            logger.warning("connect_ema_instrument called without instrument_key.")
            return
        if instrument_key not in self.ema_instrument_connections:
            self.ema_instrument_connections[instrument_key] = set()
        self.ema_instrument_connections[instrument_key].add(websocket)
        logger.info(f"Client connected to instrument-specific EMA crossover feed. instrument_key={instrument_key}, clients_for_instrument={len(self.ema_instrument_connections[instrument_key])}, total_clients={self.get_active_connections_count()}")

    def disconnect_ema_instrument(self, websocket: WebSocket, instrument_key: str):
        if not instrument_key:
            return
        if instrument_key in self.ema_instrument_connections:
            self.ema_instrument_connections[instrument_key].discard(websocket)
            logger.info(f"Client disconnected from instrument-specific EMA crossover feed. instrument_key={instrument_key}, remaining={len(self.ema_instrument_connections[instrument_key])}, total_clients={self.get_active_connections_count()}")
            if len(self.ema_instrument_connections[instrument_key]) == 0:
                self.ema_instrument_connections.pop(instrument_key, None)

    # ========================================================
    # Global Opening Range Connection Handlers
    # ========================================================

    async def connect_opening_range(self, websocket: WebSocket):
        self.opening_range_connections.add(websocket)
        logger.info(f"Client connected to /ws/opening-range. opening_range_clients={len(self.opening_range_connections)}, total_clients={self.get_active_connections_count()}")

    def disconnect_opening_range(self, websocket: WebSocket):
        self.opening_range_connections.discard(websocket)
        logger.info(f"Client disconnected from /ws/opening-range. remaining_opening_range_clients={len(self.opening_range_connections)}, total_clients={self.get_active_connections_count()}")

    # ========================================================
    # Instrument-Specific Opening Range Connection Handlers
    # ========================================================

    async def connect_opening_range_instrument(self, websocket: WebSocket, instrument_key: str):
        if not instrument_key:
            logger.warning("connect_opening_range_instrument called without instrument_key.")
            return
        if instrument_key not in self.opening_range_instrument_connections:
            self.opening_range_instrument_connections[instrument_key] = set()
        self.opening_range_instrument_connections[instrument_key].add(websocket)
        logger.info(f"Client connected to instrument-specific opening range feed. instrument_key={instrument_key}, clients_for_instrument={len(self.opening_range_instrument_connections[instrument_key])}, total_clients={self.get_active_connections_count()}")

    def disconnect_opening_range_instrument(self, websocket: WebSocket, instrument_key: str):
        if not instrument_key:
            return
        if instrument_key in self.opening_range_instrument_connections:
            self.opening_range_instrument_connections[instrument_key].discard(websocket)
            logger.info(f"Client disconnected from instrument-specific opening range feed. instrument_key={instrument_key}, remaining={len(self.opening_range_instrument_connections[instrument_key])}, total_clients={self.get_active_connections_count()}")
            if len(self.opening_range_instrument_connections[instrument_key]) == 0:
                self.opening_range_instrument_connections.pop(instrument_key, None)

    # ========================================================
    # All Feeds Connection Handlers
    # ========================================================

    async def connect_all_feeds(self, websocket: WebSocket, interval: int = 0):
        interval = normalize_interval(interval)
        if interval not in self.all_feeds_connections:
            self.all_feeds_connections[interval] = set()
        self.all_feeds_connections[interval].add(websocket)
        logger.info(f"Client connected to /all-feeds. interval={interval}, clients_for_interval={len(self.all_feeds_connections[interval])}, total_clients={self.get_active_connections_count()}")

    def disconnect_all_feeds(self, websocket: WebSocket, interval: int = 0):
        interval = normalize_interval(interval)
        if interval in self.all_feeds_connections:
            self.all_feeds_connections[interval].discard(websocket)
            logger.info(f"Client disconnected from /all-feeds. interval={interval}, remaining_for_interval={len(self.all_feeds_connections[interval])}, total_clients={self.get_active_connections_count()}")
            if len(self.all_feeds_connections[interval]) == 0:
                self.all_feeds_connections.pop(interval, None)

    # ========================================================
    # Option Connection Handlers
    # ========================================================

    async def connect_option(self, websocket: WebSocket, strike_price: float, instrument_type: str, interval: int = 0):
        interval = normalize_interval(interval)
        key = build_option_key(strike_price, instrument_type, interval)
        if key not in self.option_connections:
            self.option_connections[key] = set()
            logger.info(f"Created new option connection pool for key: {key}")
        self.option_connections[key].add(websocket)
        logger.info(f"Client connected to /option. key={key}, clients_for_key={len(self.option_connections[key])}, total_clients={self.get_active_connections_count()}")

    def disconnect_option(self, websocket: WebSocket, strike_price: float, instrument_type: str, interval: int = 0):
        interval = normalize_interval(interval)
        key = build_option_key(strike_price, instrument_type, interval)
        if key in self.option_connections:
            self.option_connections[key].discard(websocket)
            logger.info(f"Client disconnected from /option. key={key}, remaining_for_key={len(self.option_connections[key])}, total_clients={self.get_active_connections_count()}")
            if len(self.option_connections[key]) == 0:
                logger.info(f"Option connection pool empty for key: {key}")
                self.option_connections.pop(key, None)
        else:
            logger.warning(f"disconnect_option called, but key not found: {key}. Available keys: {list(self.option_connections.keys())}")

    # ========================================================
    # Tick Parsing Helper
    # ========================================================

    def build_live_payload(self, instrument_key: str, tick_raw: Dict[str, Any], contract_info: Dict[str, Any]) -> Dict[str, Any]:
        raw_feed_obj = tick_raw.get("raw_feed", tick_raw)
        full_feed = raw_feed_obj.get("fullFeed", raw_feed_obj)
        ff_wrapper = full_feed.get("ff", full_feed)
        ff = ff_wrapper.get("marketFF") or ff_wrapper.get("indexFF") or full_feed.get("marketFF") or full_feed.get("indexFF") or full_feed
        ltpc = ff.get("ltpc") or {}
        ltp = safe_float(ltpc.get("ltp"))
        close = safe_float(ltpc.get("cp"))
        ltt = safe_int(ltpc.get("ltt"))
        ltq = safe_int(ltpc.get("ltq"))
        if "marketOHLC" in ff:
            ohlc_list = ff.get("marketOHLC", {}).get("ohlc", [])
        else:
            ohlc_list = ff.get("optionOHLC", {}).get("ohlc", [])
        daily_ohlc = next((item for item in ohlc_list if item.get("interval") == "1d"), {})
        open_price = safe_float(daily_ohlc.get("open"))
        high_price = safe_float(daily_ohlc.get("high"))
        low_price = safe_float(daily_ohlc.get("low"))
        day_volume = safe_int(daily_ohlc.get("vol")) or safe_int(daily_ohlc.get("volume"))
        atp = safe_float(ff.get("atp"))
        vtt = safe_int(ff.get("vtt"))
        oi = safe_int(ff.get("oi"))
        iv = safe_float(ff.get("iv"))
        upper_circuit = safe_float(ff.get("uc"))
        lower_circuit = safe_float(ff.get("lc"))
        greeks_raw = ff.get("optionGreeks")
        greeks_data = None
        if isinstance(greeks_raw, dict) and greeks_raw:
            greeks_data = {"iv": iv, "delta": safe_float(greeks_raw.get("delta")), "theta": safe_float(greeks_raw.get("theta")), "gamma": safe_float(greeks_raw.get("gamma")), "vega": safe_float(greeks_raw.get("vega")), "rho": safe_float(greeks_raw.get("rho"))}
        bid_ask = ff.get("marketLevel", {}).get("bidAskQuote", [])
        market_depth = []
        if isinstance(bid_ask, list):
            for level in bid_ask[:5]:
                market_depth.append({"bid_qty": safe_int(level.get("bidQ") or level.get("bq")), "bid_price": safe_float(level.get("bidP") or level.get("bp")), "ask_qty": safe_int(level.get("askQ") or level.get("aq")), "ask_price": safe_float(level.get("askP") or level.get("ap"))})
        price_change = round(ltp - close, 2) if (ltp > 0 and close > 0) else 0.0
        p_change_pct = round((price_change / close) * 100, 2) if (ltp > 0 and close > 0) else 0.0
        return {"type": "live_tick", "interval": 0, "instrument_key": instrument_key, "ltp": ltp, "close": close, "change": price_change, "change_pct": p_change_pct, "ltt": ltt, "ltq": ltq, "open": open_price, "high": high_price, "low": low_price, "volume": day_volume or vtt, "atp": atp, "oi": oi, "upper_circuit": upper_circuit, "lower_circuit": lower_circuit, "greeks": greeks_data, "depth": market_depth, "info": contract_info or {}}

    # ========================================================
    # Core Live Tick Broadcast Engine
    # ========================================================

    async def broadcast_tick(self, instrument_key: str, tick_raw: Dict[str, Any], contract_info: Dict[str, Any]):
        self.broadcast_count += 1
        await asyncio.to_thread(append_raw_feed_log, instrument_key, tick_raw)
        total_clients = self.get_active_connections_count()
        if total_clients == 0:
            return
        try:
            payload = self.build_live_payload(instrument_key=instrument_key, tick_raw=tick_raw, contract_info=contract_info)
            message_str = json.dumps(payload, default=str)
            target_connections = set(self.active_connections)
            target_connections |= set(self.all_feeds_connections.get(0, set()))
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
            logger.error(f"Exception inside broadcast_tick for instrument_key={instrument_key}: {type(ex).__name__}: {ex}")

    # ========================================================
    # Live EMA Cross Broadcast Engine
    # ========================================================

    async def broadcast_ema_cross(self, ema_cross_event: Dict[str, Any]):
        self.ema_cross_broadcast_count += 1
        total_clients = self.get_active_connections_count()
        if total_clients == 0:
            return
        try:
            if not isinstance(ema_cross_event, dict):
                return
            event = dict(ema_cross_event)
            event["type"] = event.get("type", "live_ema_cross")
            event["broadcast_scope"] = event.get("broadcast_scope", "all_instruments")
            event["telegram_alert_scope"] = event.get("telegram_alert_scope", "isolated_instrument_only")
            message_str = json.dumps(event, default=str)
            target_connections = set(self.active_connections)
            target_connections |= set(self.ema_crossover_connections)
            instrument_key = event.get("instrument_key")
            if instrument_key:
                target_connections |= set(self.ema_instrument_connections.get(instrument_key, set()))
            for conn_set in self.all_feeds_connections.values():
                target_connections |= set(conn_set)
            resolved_info = resolve_contract_info_for_event(event)
            strike = resolved_info.get("strike_price")
            itype = resolved_info.get("instrument_type")
            interval_minutes = normalize_interval(event.get("interval_minutes", 0))
            if strike is not None and itype:
                option_key = build_option_key(strike, itype, interval=interval_minutes)
                if option_key in self.option_connections:
                    target_connections |= set(self.option_connections[option_key])
                live_option_key = build_option_key(strike, itype, interval=0)
                if live_option_key in self.option_connections:
                    target_connections |= set(self.option_connections[live_option_key])
            if len(target_connections) == 0:
                return
            await self._send_to_connections(target_connections, message_str)
        except Exception as ex:
            logger.error(f"Exception inside broadcast_ema_cross: {type(ex).__name__}: {ex}")

    # ========================================================
    # Opening Range Broadcast Engine
    # ========================================================

    async def broadcast_opening_range(self, opening_range_event: Dict[str, Any]):
        self.opening_range_broadcast_count += 1
        total_clients = self.get_active_connections_count()
        if total_clients == 0:
            return
        try:
            if not isinstance(opening_range_event, dict):
                return
            event = dict(opening_range_event)
            event["type"] = event.get("type", "opening_range_levels")
            event["broadcast_scope"] = event.get("broadcast_scope", "all_instruments")
            message_str = json.dumps(event, default=str)
            target_connections = set(self.active_connections)
            target_connections |= set(self.opening_range_connections)
            instrument_key = event.get("instrument_key")
            if instrument_key:
                target_connections |= set(self.opening_range_instrument_connections.get(instrument_key, set()))
            for conn_set in self.all_feeds_connections.values():
                target_connections |= set(conn_set)
            resolved_info = resolve_contract_info_for_event(event)
            strike = resolved_info.get("strike_price")
            itype = resolved_info.get("instrument_type")
            interval = normalize_interval(event.get("interval_minutes", 0))
            if strike is not None and itype:
                option_key = build_option_key(strike, itype, interval=interval)
                if option_key in self.option_connections:
                    target_connections |= set(self.option_connections[option_key])
                live_option_key = build_option_key(strike, itype, interval=0)
                if live_option_key in self.option_connections:
                    target_connections |= set(self.option_connections[live_option_key])
            if len(target_connections) == 0:
                return
            await self._send_to_connections(target_connections, message_str)
        except Exception as ex:
            logger.error(f"Exception inside broadcast_opening_range: {type(ex).__name__}: {ex}")

    # ========================================================
    # Candle Broadcast Engine
    # ========================================================

    async def broadcast_candle(self, instrument_key: str, candle_payload: Dict[str, Any], contract_info: Dict[str, Any], interval: int):
        self.candle_broadcast_count += 1
        interval = normalize_interval(interval)
        total_clients = self.get_active_connections_count()
        if total_clients == 0:
            return
        try:
            payload = {"type": "candle", "interval": interval, "instrument_key": instrument_key, **candle_payload, "info": contract_info or {}}
            message_str = json.dumps(payload, default=str)
            target_connections = set()
            target_connections |= set(self.all_feeds_connections.get(interval, set()))
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
            logger.error(f"Exception inside broadcast_candle for instrument_key={instrument_key}, interval={interval}: {type(ex).__name__}: {ex}")

    # ========================================================
    # Send Helper
    # ========================================================

    async def _send_to_connections(self, target_connections: Set[WebSocket], message_str: str):
        dead_connections = set()
        for connection in target_connections:
            try:
                await connection.send_text(message_str)
                self.sent_count += 1
            except Exception as send_ex:
                self.failed_send_count += 1
                logger.error(f"Failed sending payload to WebSocket client: {type(send_ex).__name__}: {send_ex}")
                dead_connections.add(connection)
        for dead in dead_connections:
            if dead in self.active_connections:
                self.disconnect(dead)
            if dead in self.ema_crossover_connections:
                self.disconnect_ema_crossover(dead)
            for instrument_key, conn_set in list(self.ema_instrument_connections.items()):
                if dead in conn_set:
                    conn_set.discard(dead)
                    logger.info(f"Dead client removed from instrument-specific EMA pool. instrument_key={instrument_key}, remaining={len(conn_set)}")
                    if len(conn_set) == 0:
                        self.ema_instrument_connections.pop(instrument_key, None)
            if dead in self.opening_range_connections:
                self.disconnect_opening_range(dead)
            for instrument_key, conn_set in list(self.opening_range_instrument_connections.items()):
                if dead in conn_set:
                    conn_set.discard(dead)
                    logger.info(f"Dead client removed from instrument-specific opening range pool. instrument_key={instrument_key}, remaining={len(conn_set)}")
                    if len(conn_set) == 0:
                        self.opening_range_instrument_connections.pop(instrument_key, None)
            for interval, conn_set in list(self.all_feeds_connections.items()):
                if dead in conn_set:
                    conn_set.discard(dead)
                    logger.info(f"Dead client removed from /all-feeds pool. interval={interval}, remaining={len(conn_set)}")
                    if len(conn_set) == 0:
                        self.all_feeds_connections.pop(interval, None)
            for key, conn_set in list(self.option_connections.items()):
                if dead in conn_set:
                    conn_set.discard(dead)
                    logger.info(f"Dead client removed from /option pool. key={key}, remaining={len(conn_set)}")
                    if len(conn_set) == 0:
                        self.option_connections.pop(key, None)


broadcaster = Broadcaster()