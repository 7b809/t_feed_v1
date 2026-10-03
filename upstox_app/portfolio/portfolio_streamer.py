"""
upstox_app/portfolio_streamer.py

PortfolioDataStreamer service with wait-for-open handshake.

Security:
    The access token is never logged or returned.
"""
import threading
from collections import deque
from typing import Any, Deque, Dict, List, Optional

import upstox_client

from core.logger import get_logger
from token_tasks.service import token_service
from upstox_app.common.config import upstox_config

logger = get_logger(__name__)

class PortfolioStreamerService:
    """Singleton wrapper around a single PortfolioDataStreamer instance."""

    def __init__(self, buffer_size: Optional[int] = None) -> None:
        self._lock = threading.RLock()
        self._streamer: Optional[Any] = None
        self._messages: Deque[Any] = deque(maxlen=buffer_size or upstox_config.MESSAGE_BUFFER_SIZE)
        self._connected: bool = False
        self._opened_event: threading.Event = threading.Event()

    # ── helpers ──────────────────────────────────────────────────
    def _build_config(self):
        token = token_service.get_access_token()
        if not token or not token.strip():
            raise RuntimeError("No access token available in cache")

        config = upstox_client.Configuration()
        config.access_token = token.strip()
        return config

    def is_connected(self) -> bool:
        return self._connected and self._opened_event.is_set()

    # ── lifecycle ────────────────────────────────────────────────
    def connect(self, wait_timeout: Optional[float] = None) -> bool:
        """
        Build and start the portfolio streamer.

        Blocks until on_open fires (default from config), so callers can
        safely assume the socket is ready when this returns True.
        """
        effective_timeout = (
            wait_timeout if wait_timeout is not None
            else upstox_config.CONNECT_TIMEOUT_SEC
        )

        with self._lock:
            if self.is_connected():
                logger.warning("Portfolio streamer already connected")
                return True

            self._opened_event = threading.Event()

            try:
                config = self._build_config()
            except Exception as exc:  # noqa: BLE001
                logger.error("Portfolio streamer cannot start: %s", exc)
                return False

            try:
                streamer = upstox_client.PortfolioDataStreamer(
                    upstox_client.ApiClient(config)
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("Failed to instantiate portfolio streamer: %s", exc)
                return False

            def on_message(message):
                with self._lock:
                    self._messages.append(message)

            def on_open():
                logger.info("Portfolio streamer opened")
                self._connected = True
                self._opened_event.set()

            def on_error(err):
                logger.error("Portfolio streamer error: %s", err)

            def on_close(*_args, **_kwargs):
                logger.warning("Portfolio streamer closed")
                self._connected = False
                self._opened_event.clear()

            streamer.on("message", on_message)
            streamer.on("open", on_open)
            streamer.on("error", on_error)
            streamer.on("close", on_close)

            try:
                streamer.connect()
            except Exception as exc:  # noqa: BLE001
                logger.exception("Portfolio streamer.connect() failed: %s", exc)
                return False

            self._streamer = streamer

        if not self._opened_event.wait(timeout=effective_timeout):
            logger.error(
                "Portfolio streamer did not open within %.1fs",
                effective_timeout,
            )
            return False

        logger.info(
            "Portfolio streamer started and opened | buffer=%d",
            self._messages.maxlen or 0,
        )
        return True

    def disconnect(self) -> bool:
        with self._lock:
            if not self._streamer:
                logger.info("Portfolio streamer disconnect: nothing to do")
                self._connected = False
                self._opened_event.clear()
                return True

            try:
                self._streamer.disconnect()
            except Exception as exc:  # noqa: BLE001
                logger.exception("Portfolio streamer.disconnect() failed: %s", exc)
                return False
            finally:
                self._streamer = None
                self._connected = False
                self._opened_event.clear()

            logger.info("Portfolio streamer disconnected")
            return True

    def reconnect_with_latest_token(self) -> bool:
        logger.info("Portfolio streamer reconnecting with latest cached token")
        self.disconnect()
        return self.connect()

    # ── reads ────────────────────────────────────────────────────
    def get_messages(self, limit: int = 100, clear: bool = False) -> List[Any]:
        with self._lock:
            snapshot = list(self._messages)
            if clear:
                self._messages.clear()
        if limit > 0:
            snapshot = snapshot[-limit:]
        return snapshot

    def clear_messages(self) -> int:
        with self._lock:
            count = len(self._messages)
            self._messages.clear()
        logger.info("Portfolio streamer buffer cleared | removed=%d", count)
        return count

    def status(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "name": "portfolio",
                "connected": self.is_connected(),
                "subscribed_count": 0,
                "subscribed_keys": [],
                "message_buffer_size": self._messages.maxlen or 0,
                "message_count": len(self._messages),
            }

portfolio_streamer = PortfolioStreamerService()