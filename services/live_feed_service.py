"""Upstox full-mode WebSocket manager.

Subscribes to every instrument in the registry and, for each incoming
feed, extracts the best available 1-min candle and tick snapshot, then
hands it to the EMA engine and broadcasts to connected UI clients.
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

DEFAULT_CORE_INSTRUMENTS = [
    ("NSE_INDEX|Nifty 50", "NIFTY 50"),
    ("BSE_INDEX|SENSEX", "SENSEX"),
]

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
        self._loop = None
        self._last_health_log_time: float = 0.0

    def set_loop(self, loop) -> None:
        self._loop = loop

    def _ensure_core_subscriptions(self) -> None:
        """Ensure critical index feeds (Nifty, Sensex, and health feed) are present."""
        targets = [k for k, _ in DEFAULT_CORE_INSTRUMENTS]
        if config.HEALTH_FEED_INSTRUMENT:
            targets.append(config.HEALTH_FEED_INSTRUMENT.strip())

        for target in targets:
            if not target:
                continue
            if not instrument_registry.get(target):
                symbol = target.split("|", 1)[-1]
                instrument_registry.register_adhoc(target, trading_symbol=symbol)
            if target not in self._subscribed:
                self._subscribed.append(target)
                logger.info("Core index feed [%s] ensured in subscription list.", target)

    def start(self) -> bool:
        if self._thread and self._thread.is_alive():
            return True
        if not config.UPSTOX_WS_AUTO_START:
            logger.info("Live feed auto-start disabled by config")
            return False

        keys = instrument_registry.subscribe_keys()
        self._stop.clear()
        self._subscribed = list(keys)

        self._ensure_core_subscriptions()

        self._thread = threading.Thread(
            target=self._run, name="upstox-live-feed", daemon=True
        )
        self._thread.start()
        self._started_at = datetime.now(timezone.utc)
        logger.info("Live feed starting with %d instruments", len(self._subscribed))
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

    def is_subscribed(self, instrument_key: str) -> bool:
        key = str(instrument_key or "").strip()
        return key in self._subscribed

    def subscribe_instrument(self, instrument_key: str) -> bool:
        key = str(instrument_key or "").strip()
        if not key:
            return False
        if key in self._subscribed:
            return True

        self._subscribed.append(key)

        streamer = self._streamer
        if streamer is None or not (self._thread and self._thread.is_alive()):
            self.start()
            return True

        try:
            streamer.subscribe([key], config.UPSTOX_WS_MODE)
            logger.info("Subscribed on demand: %s", key)
            return True
        except Exception:
            logger.exception("Streamer.subscribe failed for %s", key)
            return False

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

        self._ensure_core_subscriptions()

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

        def on_close(*args, **kwargs) -> None:
            logger.info("Upstox feed closed: args=%s kwargs=%s", args, kwargs)

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

        while not self._stop.is_set():
            time.sleep(0.5)

        try:
            streamer.disconnect()
        except Exception:
            logger.exception("Error disconnecting live feed")

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
                entry = instrument_registry.register_adhoc(instrument_key)

            tick = self._extract_tick(feed)
            if tick is None:
                continue

            self._last_tick_at[instrument_key] = datetime.now(timezone.utc)

            # Health feed log (e.g., every 5 minutes)
            if (
                config.HEALTH_FEED_LOG
                and instrument_key == config.HEALTH_FEED_INSTRUMENT
            ):
                now = time.monotonic()
                interval_seconds = config.HEALTH_FEED_INSTRUMENT_INTERVAL * 60
                if (now - self._last_health_log_time) >= interval_seconds:
                    self._last_health_log_time = now
                    candle_1m = tick.get("candle_1m") or {}
                    logger.info(
                        "[HEALTH FEED] instrument=%s | ltp=%s | prev_close=%s | 1m_ohlc=(O:%s H:%s L:%s C:%s) | vol=%s (interval=%dm)",
                        instrument_key,
                        tick.get("ltp"),
                        tick.get("prev_close"),
                        candle_1m.get("open"),
                        candle_1m.get("high"),
                        candle_1m.get("low"),
                        candle_1m.get("close"),
                        tick.get("volume"),
                        config.HEALTH_FEED_INSTRUMENT_INTERVAL,
                    )

            live_ema_engine.on_tick(
                instrument_key,
                candle_1m=tick.get("candle_1m"),
                ltp=tick.get("ltp"),
                volume=tick.get("volume"),
            )

            self._publish_feed_snapshot(entry, tick)

    def _extract_tick(self, feed: dict[str, Any]) -> dict[str, Any] | None:
        """Comprehensive extractor supporting Index FullFeed, Option Chain, and MarketFF."""
        if not isinstance(feed, dict):
            return None

        # Look in fullFeed, ff, oc, or directly in root
        ff = feed.get("fullFeed") or feed.get("ff") or {}
        oc = feed.get("oc") or {}
        market_ff = feed.get("marketFF") or ff.get("marketFF") or {}
        index_ff = feed.get("indexFF") or ff.get("indexFF") or {}
        market_level = feed.get("marketLevel") or ff.get("marketLevel") or {}

        # 1. LTP & Prev Close
        ltpc = (
            feed.get("ltpc")
            or ff.get("ltpc")
            or index_ff.get("ltpc")
            or market_ff.get("ltpc")
            or oc.get("ltpc")
            or {}
        )
        ltp = self._flt(ltpc.get("ltp") or feed.get("ltp") or market_ff.get("ltp"))
        prev_close = self._flt(ltpc.get("cp") or feed.get("cp") or feed.get("prev_close"))

        # 2. 1-minute candle (I1) and Day OHLC
        candle_1m = None
        market_ohlc = (
            index_ff.get("marketOHLC")
            or market_ff.get("marketOHLC")
            or ff.get("marketOHLC")
            or feed.get("marketOHLC")
            or {}
        )
        ohlc_list = market_ohlc.get("ohlc")
        ohlc_vol = None
        if isinstance(ohlc_list, list):
            for item in ohlc_list:
                if isinstance(item, dict):
                    if str(item.get("interval")) == "I1":
                        candle_1m = item
                    elif str(item.get("interval")).lower() in ("1d", "d"):
                        ohlc_vol = self._flt(item.get("volume") or item.get("vol"))

        # 3. Volume extraction (checking vtt, vol, volume across eFeedDetails and market structures)
        efd = (
            feed.get("eFeedDetails")
            or ff.get("eFeedDetails")
            or market_ff.get("eFeedDetails")
            or oc.get("eFeedDetails")
            or {}
        )
        volume = self._flt(
            efd.get("vtt")
            or market_level.get("vtt")
            or feed.get("vtt")
            or feed.get("volume")
            or market_ff.get("volume")
            or ohlc_vol
            or (candle_1m.get("volume") if isinstance(candle_1m, dict) else None)
        )

        if ltp is None and candle_1m is None:
            return None

        return {
            "ltp": ltp,
            "volume": volume,
            "candle_1m": candle_1m,
            "prev_close": prev_close,
        }

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
            "prev_close": tick.get("prev_close"),
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