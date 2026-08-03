import asyncio
import json
import os
from typing import Dict, Any, Set
from fastapi import WebSocket
from core.logger import get_logger

logger = get_logger(__file__)

# Configure raw feeds log path and max line limit
LOG_DIR = "logs"
FEEDS_LOG_PATH = os.path.join(LOG_DIR, "feeds.log")
MAX_LOG_LINES = 2000


def append_raw_feed_log(instrument_key: str, tick_raw: Dict[str, Any]):
    """Appends raw feed tick JSON to logs/feeds.log and caps the file at MAX_LOG_LINES."""
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        
        # Read existing lines if log file exists
        lines = []
        if os.path.exists(FEEDS_LOG_PATH):
            with open(FEEDS_LOG_PATH, "r", encoding="utf-8") as f:
                lines = f.readlines()

        # Build raw log line
        log_entry = json.dumps({"instrument_key": instrument_key, "raw_feed": tick_raw}) + "\n"
        lines.append(log_entry)

        # Truncate to keep only the last MAX_LOG_LINES (2000 lines)
        if len(lines) > MAX_LOG_LINES:
            lines = lines[-MAX_LOG_LINES:]

        # Overwrite file with updated lines
        with open(FEEDS_LOG_PATH, "w", encoding="utf-8") as f:
            f.writelines(lines)

    except Exception as e:
        logger.error(f"Failed writing raw feed to {FEEDS_LOG_PATH}: {e}")


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


class Broadcaster:
    """Manages FastAPI WebSocket client connections and broadcasts parsed market ticks."""

    def __init__(self):
        # Global connection pool
        self.active_connections: Set[WebSocket] = set()
        
        # Categorized connection pools for specialized routes
        self.all_feeds_connections: Set[WebSocket] = set()
        self.option_connections: Dict[str, Set[WebSocket]] = {}  # key e.g. "23450_CE"

    # --- Basic Connection Handlers ---
    async def connect(self, websocket: WebSocket):
        """Tracks a generic client WebSocket connection."""
        self.active_connections.add(websocket)
        logger.info(f"New generic WebSocket client connected. Active connections: {len(self.active_connections)}")

    def disconnect(self, websocket: WebSocket):
        """Removes a generic client WebSocket connection on disconnect."""
        self.active_connections.discard(websocket)
        logger.info(f"Generic WebSocket client disconnected. Active connections: {len(self.active_connections)}")

    # --- Specialized Connection Handlers ---
    async def connect_all_feeds(self, websocket: WebSocket):
        """Tracks connection for /all-feeds endpoint."""
        self.all_feeds_connections.add(websocket)
        logger.info(f"New client connected to /all-feeds. Total: {len(self.all_feeds_connections)}")

    def disconnect_all_feeds(self, websocket: WebSocket):
        """Removes connection for /all-feeds endpoint."""
        self.all_feeds_connections.discard(websocket)
        logger.info(f"Client disconnected from /all-feeds. Total: {len(self.all_feeds_connections)}")

    async def connect_option(self, websocket: WebSocket, strike_price: float, instrument_type: str):
        """Tracks connection for /option endpoint filtered by strike and CE/PE."""
        key = f"{float(strike_price)}_{instrument_type.upper()}"
        if key not in self.option_connections:
            self.option_connections[key] = set()
        self.option_connections[key].add(websocket)
        logger.info(f"New client connected to /option ({key}). Total for key: {len(self.option_connections[key])}")

    def disconnect_option(self, websocket: WebSocket, strike_price: float, instrument_type: str):
        """Removes connection for /option endpoint."""
        key = f"{float(strike_price)}_{instrument_type.upper()}"
        if key in self.option_connections:
            self.option_connections[key].discard(websocket)
            logger.info(f"Client disconnected from /option ({key}). Remaining: {len(self.option_connections[key])}")

    # --- Core Broadcast Engine ---
    async def broadcast_tick(self, instrument_key: str, tick_raw: Dict[str, Any], contract_info: Dict[str, Any]):
        """Logs raw feed ticks to logs/feeds.log (capped at 2,000 lines) and broadcasts to clients."""
        
        # 1. Log direct raw response into logs/feeds.log
        append_raw_feed_log(instrument_key, tick_raw)

        # Early exit if no active WebSocket clients exist across any route
        total_clients = len(self.active_connections) + len(self.all_feeds_connections) + sum(len(s) for s in self.option_connections.values())
        if total_clients == 0:
            return

        # Upstox V3 structural fallback handling:
        # Handles raw_feed -> fullFeed -> marketFF / indexFF wrapper structure
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

        # 1. Last Traded Price (LTP) & Close Price (ltpc)
        ltpc = ff.get("ltpc") or {}
        ltp = safe_float(ltpc.get("ltp"))
        close = safe_float(ltpc.get("cp"))  # Previous close
        ltt = safe_int(ltpc.get("ltt"))
        ltq = safe_int(ltpc.get("ltq"))

        # 2. Daily OHLC (Filtering array for interval '1d')
        ohlc_list = ff.get("marketOHLC", {}).get("ohlc", []) if "marketOHLC" in ff else ff.get("optionOHLC", {}).get("ohlc", [])
        daily_ohlc = next((item for item in ohlc_list if item.get("interval") == "1d"), {})
        
        open_price = safe_float(daily_ohlc.get("open"))
        high_price = safe_float(daily_ohlc.get("high"))
        low_price = safe_float(daily_ohlc.get("low"))
        day_volume = safe_int(daily_ohlc.get("vol")) or safe_int(daily_ohlc.get("volume"))

        # 3. Extended Feed Details (atp, vtt, oi, iv)
        atp = safe_float(ff.get("atp"))
        vtt = safe_int(ff.get("vtt"))
        oi = safe_int(ff.get("oi"))
        iv = safe_float(ff.get("iv"))
        upper_circuit = safe_float(ff.get("uc"))
        lower_circuit = safe_float(ff.get("lc"))

        # 4. Option Greeks Extraction
        greeks_raw = ff.get("optionGreeks")
        greeks_data = None
        if isinstance(greeks_raw, dict) and greeks_raw:
            greeks_data = {
                "iv": iv,
                "delta": safe_float(greeks_raw.get("delta")),
                "theta": safe_float(greeks_raw.get("theta")),
                "gamma": safe_float(greeks_raw.get("gamma")),
                "vega": safe_float(greeks_raw.get("vega")),
                "rho": safe_float(greeks_raw.get("rho"))
            }

        # 5. Market Depth Parsing (using bidQ, bidP, askQ, askP)
        bid_ask = ff.get("marketLevel", {}).get("bidAskQuote", [])
        market_depth = []
        if isinstance(bid_ask, list):
            for level in bid_ask[:5]:
                market_depth.append({
                    "bid_qty": safe_int(level.get("bidQ") or level.get("bq")),
                    "bid_price": safe_float(level.get("bidP") or level.get("bp")),
                    "ask_qty": safe_int(level.get("askQ") or level.get("aq")),
                    "ask_price": safe_float(level.get("askP") or level.get("ap"))
                })

        # Calculations
        price_change = round(ltp - close, 2) if (ltp > 0 and close > 0) else 0.0
        p_change_pct = round((price_change / close) * 100, 2) if (ltp > 0 and close > 0) else 0.0

        # Construct Normalized Clean Output Payload
        payload = {
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
            "info": contract_info or {}
        }

        message_str = json.dumps(payload)

        # A. Dispatch to generic clients (/ws) & all-feeds clients (/all-feeds)
        target_connections = set(self.active_connections) | set(self.all_feeds_connections)
        
        # B. Dispatch to filtered option clients (/option)
        c_info = contract_info or {}
        strike = c_info.get("strike_price")
        itype = c_info.get("instrument_type")
        if strike is not None and itype:
            option_key = f"{float(strike)}_{itype.upper()}"
            if option_key in self.option_connections:
                target_connections |= set(self.option_connections[option_key])

        # Send payload to resolved connections
        dead_connections = set()
        for connection in target_connections:
            try:
                await connection.send_text(message_str)
            except Exception:
                dead_connections.add(connection)

        # Cleanup disconnected clients accurately from their respective pools
        for dead in dead_connections:
            if dead in self.active_connections:
                self.disconnect(dead)
            if dead in self.all_feeds_connections:
                self.disconnect_all_feeds(dead)
            
            # Check and cleanup option pool connections
            for key, conn_set in list(self.option_connections.items()):
                if dead in conn_set:
                    conn_set.discard(dead)
                    logger.info(f"Client disconnected from /option ({key}). Remaining: {len(conn_set)}")


broadcaster = Broadcaster()