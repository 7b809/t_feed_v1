"""Upstox full-mode WebSocket manager.

Subscribes to every instrument in the registry and, for each incoming
feed, extracts the best available 1-min candle (index feeds carry
`marketOHLC.ohlc[I1]`; option feeds don't, so we synthesise one from
`ltpc` + `eFeedDetails.vtt`) and hands it to the EMA engine.

Also publishes an ephemeral "last tick" snapshot on the feed topic so
UI clients can show live LTP / OHLC without touching the raw feed.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any

from core import config
from core.logger import get_logger
from services.instrument_registry import instrument_registry
from services.live_broadcast import live_broadcast
from services.live_ema_engine import live_ema_engine
from services.token_service import token_service

logger = get_logger(__file__)


def _load_sdk():
    try:
        import upstox_client  # type: ignore

        return upstox_client
    except ImportError:
        return None


class LiveFeedService:
    def __init__(self) -> None:
        self._streamer = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._started_at: datetime | None = None
        self._last_tick_at: dict[str, datetime] = {}
        self._subscribed: list[str] = []
        self._loop = None  # asyncio loop for broadcast

    # ------------------------------------------------------------------ #
    # Lifecycle                                                          #
    # ------------------------------------------------------------------ #
    def set_loop(self, loop) -> None:
        """Give the service a handle to the running asyncio loop."""
        self._loop = loop

    def start(self) -> bool:
        if self._thread and self._thread.is_alive():
            return True
        if not config.UPSTOX_WS_AUTO_START:
            logger.info("Live feed auto-start disabled by config")
            return False

        keys = instrument_registry.subscribe_keys()
        if not keys:
            logger.warning("Live feed: no instruments in registry; not starting")
            return False

        self._stop.clear()
        self._subscribed = list(keys)
        self._thread = threading.Thread(
            target=self._run, name="upstox-live-feed", daemon=True
        )
        self._thread.start()
        self._started_at = datetime.now(timezone.utc)
        logger.info("Live feed starting with %d instruments", len(keys))
        return True

    def stop(self) -> None:
        self._stop.set()
        try:
            if self._streamer is not None:
                self._streamer.disconnect()
        except Exception:
            logger.exception("Error disconnecting Upstox streamer")
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=10)
        logger.info("Live feed stopped")

    def restart(self) -> bool:
        self.stop()
        time.sleep(1)
        return self.start()

    # ------------------------------------------------------------------ #
    # Internal run                                                       #
    # ------------------------------------------------------------------ #
    def _run(self) -> None:
        sdk = _load_sdk()
        if sdk is None:
            logger.error("upstox_client is not installed; live feed disabled")
            return

        while not self._stop.is_set():
            try:
                self._connect_and_listen(sdk)
            except Exception:
                logger.exception("Live feed crashed; will retry")

            if self._stop.is_set():
                break
            logger.info(
                "Reconnecting live feed in %ds", config.UPSTOX_WS_RECONNECT_INTERVAL
            )
            self._stop.wait(config.UPSTOX_WS_RECONNECT_INTERVAL)

    def _connect_and_listen(self, sdk) -> None:
        token = token_service.get_access_token()
        if not token:
            raise RuntimeError("No Upstox access token available for live feed")

        configuration = sdk.Configuration()
        configuration.access_token = token
        client = sdk.ApiClient(configuration)

        streamer = sdk.MarketDataStreamerV3(
            client, list(self._subscribed), config.UPSTOX_WS_MODE
        )
        self._streamer = streamer

        def on_message(message: Any) -> None:
            try:
                self._handle_feed(message)
            except Exception:
                logger.exception("Error handling live feed message")

        def on_error(message: Any) -> None:
            logger.warning("Upstox feed error: %s", message)

        def on_close() -> None:
            logger.info("Upstox feed closed")

        streamer.on("message", on_message)
        streamer.on("error", on_error)
        streamer.on("close", on_close)

        try:
            streamer.auto_reconnect(
                True,
                config.UPSTOX_WS_RECONNECT_INTERVAL,
                config.UPSTOX_WS_MAX_RETRIES or 0,
            )
        except Exception:
            logger.exception("auto_reconnect configuration failed")

        logger.info(
            "Connecting live feed mode=%s instruments=%d",
            config.UPSTOX_WS_MODE,
            len(self._subscribed),
        )
        streamer.connect()

        # Block until stop is requested or the streamer dies.
        while not self._stop.is_set():
            time.sleep(0.5)

        try:
            streamer.disconnect()
        except Exception:
            logger.exception("Error disconnecting live feed")

    # ------------------------------------------------------------------ #
    # Feed parsing                                                       #
    # ------------------------------------------------------------------ #
    def _handle_feed(self, message: Any) -> None:
        payload = (
            message if isinstance(message, dict) else getattr(message, "data", None)
        )
        if not isinstance(payload, dict):
            return

        feeds = payload.get("feeds") or {}
        if not isinstance(feeds, dict):
            return

        for instrument_key, feed in feeds.items():
            entry = instrument_registry.get(instrument_key)
            if entry is None:
                continue

            tick = self._extract_tick(feed)
            if tick is None:
                continue

            self._last_tick_at[instrument_key] = datetime.now(timezone.utc)

            # Feed the EMA engine first so crosses are computed before
            # the ephemeral snapshot goes out.
            live_ema_engine.on_tick(
                instrument_key,
                candle_1m=tick.get("candle_1m"),
                ltp=tick.get("ltp"),
                volume=tick.get("volume"),
            )

            # Ephemeral feed snapshot for UI clients.
            self._publish_feed_snapshot(entry, tick)

    def _extract_tick(self, feed: dict[str, Any]) -> dict[str, Any] | None:
        """Best-effort extraction of (ltp, volume, candle_1m) from a feed."""
        if not isinstance(feed, dict):
            return None

        # Index feeds carry the FF block. Option feeds carry OC.
        ff = feed.get("ff") or {}
        oc = feed.get("oc") or {}
        block = ff or oc
        if not isinstance(block, dict):
            return None

        index_ff = block.get("indexFF") or {}
        ltpc = block.get("ltpc") or index_ff.get("ltpc") or {}
        ltp = self._flt(ltpc.get("ltp"))

        candle_1m = None
        m_ohlc = (index_ff.get("marketOHLC") or {}).get("ohlc") if index_ff else None
        if isinstance(m_ohlc, list):
            # Last I1 entry wins; feed may include several.
            for item in m_ohlc:
                if not isinstance(item, dict):
                    continue
                if str(item.get("interval")) == "I1":
                    candle_1m = item

        volume = None
        efd = block.get("eFeedDetails") or {}
        if isinstance(efd, dict):
            volume = self._flt(efd.get("vtt"))

        return {"ltp": ltp, "volume": volume, "candle_1m": candle_1m}

    @staticmethod
    def _flt(value: Any) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _publish_feed_snapshot(
        self, entry: dict[str, Any], tick: dict[str, Any]
    ) -> None:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "ltp": tick.get("ltp"),
            "volume": tick.get("volume"),
            "candle_1m": tick.get("candle_1m"),
            "underlying": entry.get("underlying"),
            "strike_price": entry.get("strike_price"),
            "strike_type": entry.get("strike_type"),
        }
        try:
            live_broadcast.publish_sync("feed", entry, payload)
        except Exception:
            logger.exception("Failed to publish feed snapshot")

    # ------------------------------------------------------------------ #
    # Introspection                                                      #
    # ------------------------------------------------------------------ #
    def status(self) -> dict[str, Any]:
        return {
            "running": bool(self._thread and self._thread.is_alive()),
            "mode": config.UPSTOX_WS_MODE,
            "instrument_count": len(self._subscribed),
            "started_at": (self._started_at.isoformat() if self._started_at else None),
            "last_tick_at": {
                k: v.isoformat() for k, v in list(self._last_tick_at.items())[:50]
            },
        }


live_feed_service = LiveFeedService()
