"""
upstox_app/ws_manager.py

Manages downstream WebSocket clients that subscribe to our market feed.

- Keeps a registry of connected clients, each with an optional filter set
  of instrument keys (None = receive everything).
- Provides a THREAD-SAFE fanout entrypoint for SDK callbacks
  (which run outside the asyncio event loop).
- Broadcasts system events (upstream subscribe / unsubscribe) so clients
  can react to feed topology changes.

Security:
    The access token is never included in any outbound message.
"""
import asyncio
import uuid
from typing import Any, Dict, Optional, Set

from fastapi import WebSocket

from core.logger import get_logger

logger = get_logger(__name__)

# Event loop reference captured from lifespan; used for thread-safe fanout
_loop: Optional[asyncio.AbstractEventLoop] = None

def set_event_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Register the running asyncio loop (called from core/lifespan.py)."""
    global _loop
    _loop = loop
    logger.info("ws_manager: event loop registered")

def _jsonable(message: Any) -> dict:
    """Coerce an SDK message into a JSON-safe dict."""
    try:
        if hasattr(message, "to_dict"):
            return message.to_dict()
        if isinstance(message, dict):
            return message
        return {"raw": str(message)}
    except Exception:  # noqa: BLE001
        return {"raw": str(message)}

def _extract_instrument_key(message: dict) -> Optional[str]:
    """Best-effort extraction of an instrument key from an Upstox market message."""
    if not isinstance(message, dict):
        return None

    if isinstance(message.get("instrument_key"), str):
        return message["instrument_key"]

    feeds = message.get("feeds")
    if isinstance(feeds, dict) and feeds:
        for k in feeds.keys():
            if isinstance(k, str):
                return k

    # Some payloads nest under "data"
    data = message.get("data")
    if isinstance(data, dict):
        return _extract_instrument_key(data)

    return None

class WSClient:
    """A single downstream WebSocket client."""

    def __init__(self, ws: WebSocket, filter_keys: Optional[Set[str]] = None) -> None:
        self.id: str = uuid.uuid4().hex
        self.ws: WebSocket = ws
        self.filter_keys: Optional[Set[str]] = filter_keys  # None = receive all
        self._send_lock = asyncio.Lock()

    async def send_json(self, data: dict) -> None:
        async with self._send_lock:
            await self.ws.send_json(data)

    def wants(self, instrument_key: Optional[str]) -> bool:
        if self.filter_keys is None:
            return True
        if instrument_key is None:
            return False
        return instrument_key in self.filter_keys

class WSManager:
    """Registry + fanout for downstream WebSocket clients."""

    def __init__(self) -> None:
        self._clients: Dict[str, WSClient] = {}
        self._lock = asyncio.Lock()

    # ── lifecycle ────────────────────────────────────────────────
    async def connect(self, ws: WebSocket, filter_keys: Optional[Set[str]] = None) -> WSClient:
        client = WSClient(ws, filter_keys)
        async with self._lock:
            self._clients[client.id] = client
        logger.info(
            "WS client connected | id=%s | filter=%s",
            client.id,
            sorted(filter_keys) if filter_keys else "ALL",
        )
        return client

    async def disconnect(self, client_id: str) -> None:
        async with self._lock:
            self._clients.pop(client_id, None)
        logger.info("WS client disconnected | id=%s", client_id)

    def client_count(self) -> int:
        return len(self._clients)

    # ── broadcast ────────────────────────────────────────────────
    async def broadcast_market(self, message: dict) -> None:
        key = _extract_instrument_key(message)
        payload = {"type": "market", "instrument_key": key, "data": message}
        snapshot = list(self._clients.values())
        for c in snapshot:
            if c.wants(key):
                try:
                    await c.send_json(payload)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("WS market send failed | id=%s | err=%s", c.id, exc)

    async def broadcast_event(self, event: str, payload: dict) -> None:
        msg = {"type": "event", "event": event, "payload": payload}
        snapshot = list(self._clients.values())
        for c in snapshot:
            try:
                await c.send_json(msg)
            except Exception as exc:  # noqa: BLE001
                logger.warning("WS event send failed | id=%s | err=%s", c.id, exc)

ws_manager = WSManager()

# ── Thread-safe fanout (called from SDK callback threads) ────────
def fanout_market_message(message: Any) -> None:
    """
    Called from the Upstox SDK's message callback (non-async thread).
    Schedules the broadcast on the main event loop.
    """
    if _loop is None or _loop.is_closed():
        return
    try:
        asyncio.run_coroutine_threadsafe(
            ws_manager.broadcast_market(_jsonable(message)),
            _loop,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("fanout_market_message failed: %s", exc)

def fanout_event(event: str, payload: dict) -> None:
    """Thread-safe system-event broadcast."""
    if _loop is None or _loop.is_closed():
        return
    try:
        asyncio.run_coroutine_threadsafe(
            ws_manager.broadcast_event(event, payload),
            _loop,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("fanout_event failed: %s", exc)