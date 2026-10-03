"""
upstox_app/market_streamer.py

MarketDataStreamerV3 service with:
    - idempotent subscribe / unsubscribe / change-mode
    - fanout to downstream WebSocket clients
    - wait-for-open handshake
    - graceful handling of socket-closed during out-of-market-hours

Security:
    The access token is never logged or returned.
"""
import threading
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

import upstox_client

try:
    from websocket import WebSocketConnectionClosedException
except Exception:  # noqa: BLE001
    # Fallback if the transitive `websocket-client` package structure changes
    class WebSocketConnectionClosedException(Exception):  # type: ignore[no-redef]
        pass

from core.logger import get_logger
from token_tasks.service import token_service
from upstox_app.common.config import upstox_config
from upstox_app.common.market_hours import is_market_open, market_state_text
from upstox_app.streamer.ws_manager import fanout_market_message

logger = get_logger(__name__)

VALID_MODES = ("ltpc", "full", "option_greeks", "full_d30")


class MarketStreamerService:
    """Singleton wrapper around a single MarketDataStreamerV3 instance."""

    def __init__(self, buffer_size: Optional[int] = None) -> None:
        self._lock = threading.RLock()
        self._streamer: Optional[Any] = None
        self._messages: Deque[Any] = deque(maxlen=buffer_size or upstox_config.MESSAGE_BUFFER_SIZE)
        self._subscriptions: Dict[str, str] = {}   # key -> mode
        self._connected: bool = False
        self._on_open_hook: Optional[Callable[[], None]] = None

        # Set inside on_open; connect() waits on this before returning.
        self._opened_event: threading.Event = threading.Event()

    # ── helpers ──────────────────────────────────────────────────
    def _build_config(self):
        token = token_service.get_access_token()
        if not token or not token.strip():
            raise RuntimeError("No access token available in cache")

        config = upstox_client.Configuration()
        config.access_token = token.strip()
        return config

    @staticmethod
    def _normalize_mode(mode: str) -> str:
        normalized = (mode or "").strip().lower()
        if normalized not in VALID_MODES:
            raise ValueError(f"Invalid mode '{mode}'. Allowed: {VALID_MODES}")
        return normalized

    def _mark_disconnected(self) -> None:
        """Internal: flip local state to disconnected (caller holds lock)."""
        self._connected = False
        self._opened_event.clear()

    def _apply_all_subscriptions(self) -> None:
        """Push the full subscription map to the SDK (used on open/reconnect)."""
        if not self._streamer or not self._subscriptions:
            return

        grouped: Dict[str, List[str]] = {}
        for key, mode in self._subscriptions.items():
            grouped.setdefault(mode, []).append(key)

        for mode, keys in grouped.items():
            try:
                self._streamer.subscribe(keys, mode)
                logger.info("Re-subscribed on open | mode=%s | count=%d", mode, len(keys))
            except WebSocketConnectionClosedException:
                self._handle_socket_closed("re-subscribe")
                return
            except Exception as exc:  # noqa: BLE001
                logger.exception("Re-subscribe failed | mode=%s | err=%s", mode, exc)

    def _handle_socket_closed(self, context: str) -> None:
        """
        Called when the SDK socket is closed.
        Downgrades to WARNING outside market hours, ERROR during session.
        Subscription intent stays in self._subscriptions for the next on_open.
        """
        self._mark_disconnected()
        if is_market_open():
            logger.error(
                "Upstream socket closed during market hours | context=%s | "
                "will re-apply %d subscription(s) on reconnect",
                context, len(self._subscriptions),
            )
        else:
            logger.warning(
                "Upstream socket closed outside market hours | context=%s | "
                "state=%s | deferred %d subscription(s) until next open",
                context, market_state_text(), len(self._subscriptions),
            )

    def is_connected(self) -> bool:
        """True only after on_open has fired AND the socket hasn't been closed."""
        return self._connected and self._opened_event.is_set()

    # ── lifecycle ────────────────────────────────────────────────
    def connect(
        self,
        on_open_hook: Optional[Callable[[], None]] = None,
        wait_timeout: Optional[float] = None,
    ) -> bool:
        """
        Build and start the streamer. Blocks until `on_open` fires (up to
        wait_timeout, default from config). Safe to subscribe immediately
        after this returns True.
        """
        effective_timeout = (
            wait_timeout if wait_timeout is not None
            else upstox_config.CONNECT_TIMEOUT_SEC
        )

        with self._lock:
            if self.is_connected():
                logger.warning("Market streamer already connected")
                return True

            self._opened_event = threading.Event()

            try:
                config = self._build_config()
            except Exception as exc:  # noqa: BLE001
                logger.error("Market streamer cannot start: %s", exc)
                return False

            try:
                streamer = upstox_client.MarketDataStreamerV3(
                    upstox_client.ApiClient(config)
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("Failed to instantiate market streamer: %s", exc)
                return False

            self._on_open_hook = on_open_hook

            try:
                streamer.auto_reconnect(
                    upstox_config.AUTO_RECONNECT_ENABLED,
                    upstox_config.AUTO_RECONNECT_INTERVAL_SEC,
                    upstox_config.AUTO_RECONNECT_RETRY_COUNT,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("auto_reconnect() failed: %s", exc)

            def on_open():
                logger.info(
                    "Market streamer opened | re-subscribing %d keys",
                    len(self._subscriptions),
                )
                self._connected = True
                self._opened_event.set()
                self._apply_all_subscriptions()
                if self._on_open_hook:
                    try:
                        self._on_open_hook()
                    except Exception:  # noqa: BLE001
                        logger.exception("on_open hook failed")

            def on_message(message):
                with self._lock:
                    self._messages.append(message)
                fanout_market_message(message)

            def on_error(err):
                logger.error("Market streamer error: %s", err)

            def on_close(*_args, **_kwargs):
                if is_market_open():
                    logger.warning("Market streamer closed during market hours")
                else:
                    logger.info(
                        "Market streamer closed outside market hours | state=%s",
                        market_state_text(),
                    )
                self._mark_disconnected()

            def on_reconnect_halt(message):
                logger.error("Market streamer auto-reconnect halted: %s", message)
                self._mark_disconnected()

            streamer.on("open", on_open)
            streamer.on("message", on_message)
            streamer.on("error", on_error)
            streamer.on("close", on_close)
            streamer.on("autoReconnectStopped", on_reconnect_halt)

            try:
                streamer.connect()
            except Exception as exc:  # noqa: BLE001
                logger.exception("Market streamer.connect() failed: %s", exc)
                return False

            self._streamer = streamer

        # Wait for on_open OUTSIDE the lock (on_open takes self._lock).
        if not self._opened_event.wait(timeout=effective_timeout):
            logger.error(
                "Market streamer did not open within %.1fs — "
                "subscribe/unsubscribe will be queued until on_open fires",
                effective_timeout,
            )
            return False

        logger.info(
            "Market streamer started and opened | buffer=%d",
            self._messages.maxlen or 0,
        )
        return True

    def disconnect(self) -> bool:
        with self._lock:
            if not self._streamer:
                logger.info("Market streamer disconnect: nothing to do")
                self._mark_disconnected()
                return True

            try:
                self._streamer.disconnect()
            except Exception as exc:  # noqa: BLE001
                logger.exception("Market streamer.disconnect() failed: %s", exc)
                return False
            finally:
                self._streamer = None
                self._mark_disconnected()

            logger.info("Market streamer disconnected")
            return True

    def reconnect_with_latest_token(self) -> bool:
        logger.info("Market streamer reconnecting with latest cached token")
        self.disconnect()
        return self.connect()

    # ── operations (idempotent + safe before open + market-hours aware) ──
    def subscribe(
        self, instrument_keys: List[str], mode: str = "full"
    ) -> Tuple[List[str], List[str]]:
        """
        Subscribe keys to the given mode. Idempotent.

        If the socket is not open (not yet connected OR closed out-of-hours),
        the intent is stored in `_subscriptions` and applied on next `on_open`.
        Never raises `WebSocketConnectionClosedException` — it is caught and
        logged at the appropriate level for the current market state.
        """
        mode = self._normalize_mode(mode)
        keys = [k.strip() for k in instrument_keys if k and k.strip()]
        if not keys:
            raise ValueError("No valid instrument keys provided")

        new_keys: List[str] = []
        changed_keys: List[str] = []
        skipped: List[str] = []

        with self._lock:
            for k in keys:
                existing = self._subscriptions.get(k)
                if existing is None:
                    new_keys.append(k)
                    self._subscriptions[k] = mode
                elif existing == mode:
                    skipped.append(k)
                else:
                    changed_keys.append(k)
                    self._subscriptions[k] = mode

            ready = self._streamer is not None and self.is_connected()

            if not ready:
                if is_market_open():
                    logger.info(
                        "Subscribe queued (streamer not open) | mode=%s | new=%d | changed=%d | skipped=%d",
                        mode, len(new_keys), len(changed_keys), len(skipped),
                    )
                else:
                    logger.warning(
                        "Subscribe deferred — market closed | state=%s | mode=%s | new=%d | "
                        "will apply on next market open",
                        market_state_text(), mode, len(new_keys),
                    )
                return new_keys + changed_keys, skipped

            try:
                if new_keys:
                    self._streamer.subscribe(new_keys, mode)
                    logger.info("Subscribe issued | mode=%s | new=%d", mode, len(new_keys))
                if changed_keys:
                    self._streamer.change_mode(changed_keys, mode)
                    logger.info("Mode change issued | mode=%s | changed=%d", mode, len(changed_keys))
                if skipped:
                    logger.info("Subscribe no-op | mode=%s | skipped=%d", mode, len(skipped))

            except WebSocketConnectionClosedException:
                self._handle_socket_closed("subscribe")
                # Intent is already stored in _subscriptions — safe to return as applied.

            except Exception as exc:  # noqa: BLE001
                logger.exception("Subscribe SDK call failed | mode=%s | err=%s", mode, exc)

        return new_keys + changed_keys, skipped

    def unsubscribe(
        self, instrument_keys: List[str]
    ) -> Tuple[List[str], List[str]]:
        """
        Unsubscribe keys. Idempotent. Queued safely if socket not open.
        """
        keys = [k.strip() for k in instrument_keys if k and k.strip()]
        if not keys:
            raise ValueError("No valid instrument keys provided")

        applied: List[str] = []
        skipped: List[str] = []

        with self._lock:
            for k in keys:
                if k in self._subscriptions:
                    del self._subscriptions[k]
                    applied.append(k)
                else:
                    skipped.append(k)

            ready = self._streamer is not None and self.is_connected()

            if not ready:
                logger.info(
                    "Unsubscribe queued (streamer not open) | applied=%d | skipped=%d",
                    len(applied), len(skipped),
                )
                return applied, skipped

            try:
                if applied:
                    self._streamer.unsubscribe(applied)
                    logger.info("Unsubscribe issued | applied=%d", len(applied))
                if skipped:
                    logger.info("Unsubscribe no-op | skipped=%d", len(skipped))
            except WebSocketConnectionClosedException:
                self._handle_socket_closed("unsubscribe")
            except Exception as exc:  # noqa: BLE001
                logger.exception("Unsubscribe SDK call failed | err=%s", exc)

        return applied, skipped

    def change_mode(
        self, instrument_keys: List[str], mode: str
    ) -> Tuple[List[str], List[str]]:
        """
        Change mode for keys. Idempotent. Queued safely if socket not open.
        """
        mode = self._normalize_mode(mode)
        keys = [k.strip() for k in instrument_keys if k and k.strip()]
        if not keys:
            raise ValueError("No valid instrument keys provided")

        applied: List[str] = []
        skipped: List[str] = []

        with self._lock:
            for k in keys:
                if k not in self._subscriptions:
                    skipped.append(k)
                    continue
                if self._subscriptions[k] == mode:
                    skipped.append(k)
                    continue
                applied.append(k)
                self._subscriptions[k] = mode

            ready = self._streamer is not None and self.is_connected()

            if not ready:
                logger.info(
                    "Change-mode queued (streamer not open) | mode=%s | applied=%d | skipped=%d",
                    mode, len(applied), len(skipped),
                )
                return applied, skipped

            try:
                if applied:
                    self._streamer.change_mode(applied, mode)
                    logger.info("Change-mode issued | mode=%s | applied=%d", mode, len(applied))
                if skipped:
                    logger.info("Change-mode no-op | mode=%s | skipped=%d", mode, len(skipped))
            except WebSocketConnectionClosedException:
                self._handle_socket_closed("change-mode")
            except Exception as exc:  # noqa: BLE001
                logger.exception("Change-mode SDK call failed | mode=%s | err=%s", mode, exc)

        return applied, skipped

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
        logger.info("Market streamer buffer cleared | removed=%d", count)
        return count

    def status(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "name": "market",
                "connected": self.is_connected(),
                "subscribed_count": len(self._subscriptions),
                "subscribed_keys": sorted(self._subscriptions.keys()),
                "message_buffer_size": self._messages.maxlen or 0,
                "message_count": len(self._messages),
                "market_open": is_market_open(),
                "market_state": market_state_text(),
            }


market_streamer = MarketStreamerService()