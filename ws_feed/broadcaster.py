import json
import os
import logging
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


def append_raw_feed_log(instrument_key: str, tick_raw: Dict[str, Any]):
    """
    Appends raw feed tick JSON to logs/feeds.log and caps the file at MAX_LOG_LINES.

    This writes raw ticks to file only.
    It does not print raw ticks to console because live feed volume is high.
    """
    try:
        os.makedirs(LOG_DIR, exist_ok=True)

        lines = []
        if os.path.exists(FEEDS_LOG_PATH):
            with open(FEEDS_LOG_PATH, "r", encoding="utf-8") as f:
                lines = f.readlines()

        log_entry = json.dumps(
            {
                "instrument_key": instrument_key,
                "raw_feed": tick_raw,
            },
            default=str,
        ) + "\n"

        lines.append(log_entry)

        if len(lines) > MAX_LOG_LINES:
            lines = lines[-MAX_LOG_LINES:]

        with open(FEEDS_LOG_PATH, "w", encoding="utf-8") as f:
            f.writelines(lines)

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


class Broadcaster:
    """Manages FastAPI WebSocket client connections and broadcasts parsed market ticks."""

    def __init__(self):
        # Global connection pool
        self.active_connections: Set[WebSocket] = set()

        # Categorized connection pools for specialized routes
        self.all_feeds_connections: Set[WebSocket] = set()
        self.option_connections: Dict[str, Set[WebSocket]] = {}

        # Counters
        self.broadcast_count = 0
        self.sent_count = 0
        self.failed_send_count = 0

        logger.info("Broadcaster initialized")

    def get_active_connections_count(self) -> int:
        """Returns total active WebSocket clients across all route types."""
        return (
            len(self.active_connections)
            + len(self.all_feeds_connections)
            + sum(len(s) for s in self.option_connections.values())
        )

    # --- Basic Connection Handlers ---
    async def connect(self, websocket: WebSocket):
        """Tracks a generic client WebSocket connection."""
        self.active_connections.add(websocket)

        logger.info(
            f"Generic WebSocket client connected. "
            f"active_connections={len(self.active_connections)}, "
            f"total_clients={self.get_active_connections_count()}"
        )

    def disconnect(self, websocket: WebSocket):
        """Removes a generic client WebSocket connection on disconnect."""
        self.active_connections.discard(websocket)

        logger.info(
            f"Generic WebSocket client disconnected. "
            f"active_connections={len(self.active_connections)}, "
            f"total_clients={self.get_active_connections_count()}"
        )

    # --- Specialized Connection Handlers ---
    async def connect_all_feeds(self, websocket: WebSocket):
        """Tracks connection for /all-feeds endpoint."""
        self.all_feeds_connections.add(websocket)

        logger.info(
            f"Client connected to /all-feeds. "
            f"all_feeds_connections={len(self.all_feeds_connections)}, "
            f"total_clients={self.get_active_connections_count()}"
        )

    def disconnect_all_feeds(self, websocket: WebSocket):
        """Removes connection for /all-feeds endpoint."""
        self.all_feeds_connections.discard(websocket)

        logger.info(
            f"Client disconnected from /all-feeds. "
            f"all_feeds_connections={len(self.all_feeds_connections)}, "
            f"total_clients={self.get_active_connections_count()}"
        )

    async def connect_option(
        self,
        websocket: WebSocket,
        strike_price: float,
        instrument_type: str,
    ):
        """Tracks connection for /option endpoint filtered by strike and CE/PE."""
        key = f"{float(strike_price)}_{instrument_type.upper()}"

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
    ):
        """Removes connection for /option endpoint."""
        key = f"{float(strike_price)}_{instrument_type.upper()}"

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

    # --- Core Broadcast Engine ---
    async def broadcast_tick(
        self,
        instrument_key: str,
        tick_raw: Dict[str, Any],
        contract_info: Dict[str, Any],
    ):
        """Broadcasts parsed market ticks to matching clients."""

        self.broadcast_count += 1

        # ------------------------------------------------------------------
        # Very important:
        # If no local WebSocket clients are connected, do not write logs,
        # do not parse ticks, and do not process anything.
        #
        # This prevents FastAPI event-loop overload and allows /option
        # WebSocket handshakes to complete quickly.
        # ------------------------------------------------------------------
        total_clients = self.get_active_connections_count()

        if total_clients == 0:
            return

        # Write raw ticks only when at least one local client is connected.
        # If this still causes slowness, comment this line also.
        append_raw_feed_log(instrument_key, tick_raw)

        try:
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

            # 1. Last Traded Price and Close Price
            ltpc = ff.get("ltpc") or {}
            ltp = safe_float(ltpc.get("ltp"))
            close = safe_float(ltpc.get("cp"))
            ltt = safe_int(ltpc.get("ltt"))
            ltq = safe_int(ltpc.get("ltq"))

            # 2. Daily OHLC
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

            # 3. Extended Feed Details
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
                    "rho": safe_float(greeks_raw.get("rho")),
                }

            # 5. Market Depth Parsing
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

            # Calculations
            price_change = round(ltp - close, 2) if (ltp > 0 and close > 0) else 0.0
            p_change_pct = (
                round((price_change / close) * 100, 2)
                if (ltp > 0 and close > 0)
                else 0.0
            )

            # Construct normalized output payload
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
                "info": contract_info or {},
            }

            message_str = json.dumps(payload, default=str)

            # A. Dispatch to generic clients and all-feeds clients
            target_connections = set(self.active_connections) | set(
                self.all_feeds_connections
            )

            # B. Dispatch to filtered option clients
            c_info = contract_info or {}
            strike = c_info.get("strike_price")
            itype = c_info.get("instrument_type")

            if strike is not None and itype:
                option_key = f"{float(strike)}_{str(itype).upper()}"

                if option_key in self.option_connections:
                    matching_option_clients = self.option_connections[option_key]
                    target_connections |= set(matching_option_clients)

            if len(target_connections) == 0:
                return

            # Send payload to resolved connections
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

            # Cleanup disconnected clients accurately from their respective pools
            for dead in dead_connections:
                if dead in self.active_connections:
                    self.disconnect(dead)

                if dead in self.all_feeds_connections:
                    self.disconnect_all_feeds(dead)

                for key, conn_set in list(self.option_connections.items()):
                    if dead in conn_set:
                        conn_set.discard(dead)

                        logger.info(
                            f"Dead client removed from /option pool. "
                            f"key={key}, remaining={len(conn_set)}"
                        )

        except Exception as ex:
            logger.error(
                f"Exception inside broadcast_tick for instrument_key={instrument_key}: "
                f"{type(ex).__name__}: {ex}"
            )


broadcaster = Broadcaster()