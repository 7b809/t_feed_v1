# core/market_feed_service.py
"""Upstox live market feed service (MarketDataStreamerV3).

Subscription **mode** is configurable via the ``FEED_MODE`` env var
(read once at import time in ``core.config``), with ``full`` as the
default. Valid values are ``ltpc``, ``full``, ``full_d30`` and
``option_greeks``. Invalid values fall back to ``full`` with a warning.

The active mode is written to the logs at three points so you can
always tell which feed the process is subscribed to:

  * ``connect()``   -> "Market feed starting (mode=full, ...)"
  * ``_on_open()``  -> "Market feed connected (mode=full) ..."
  * every health summary header -> "[HEALTH] feed summary (..., mode=full)"

Per-message DEBUG logging of the raw feed payload is **disabled by
default**; the compact [HEALTH] line per instrument is emitted once
after the first batch, then every ``FEED_HEALTH_INTERVAL_SECONDS``
(default 300 s = 5 min), plus once on disconnect.

Set ``LOG_RAW_FEED=true`` to restore the old per-message DEBUG dump
for troubleshooting (not recommended in production).
"""

from __future__ import annotations

import threading
import time
from datetime import datetime

import upstox_client

from core.config import (
    FEED_HEALTH_INTERVAL_SECONDS,
    FEED_MODE,
    FEED_STALE_AFTER_SECONDS,
    INSTRUMENT_KEYS,
    LOG_RAW_FEED,
    VALID_FEED_MODES,
)
from core.logger import get_logger

logger = get_logger(__file__)


def _resolve_mode(explicit: str | None) -> str:
    """Resolve the effective feed mode.

    Priority:
      1) explicit ``mode`` argument (from the constructor)
      2) ``FEED_MODE`` config value
      3) hard fallback: ``"full"``

    An invalid explicit argument or invalid ``FEED_MODE`` value logs a
    warning and falls back to ``"full"``.
    """
    candidate = (explicit or FEED_MODE or "full").strip().lower()
    if candidate not in VALID_FEED_MODES:
        logger.warning(
            "Invalid feed mode %r; valid modes are %s. Falling back to 'full'.",
            candidate,
            list(VALID_FEED_MODES),
        )
        return "full"
    return candidate


class MarketFeedService:
    """Manages the Upstox ``MarketDataStreamerV3`` connection."""

    def __init__(self, access_token: str, instrument_keys=None, mode: str | None = None):
        self.access_token = access_token
        self.instrument_keys = list(instrument_keys or INSTRUMENT_KEYS)
        self.mode = _resolve_mode(mode)

        self.streamer = None
        self._thread = None
        self._health_thread = None
        self._stop_event = threading.Event()
        self._running = False

        # Per-instrument cache:
        # {key: {ltp, ltt, cp, recv_ts, recv_iso, count}}
        self._ltp_cache: dict[str, dict] = {}
        self._first_batch_logged = False
        self._market_status_logged = False
        self._connect_ts: float | None = None

    # ------------------------------------------------------------------ #
    # Streamer setup                                                     #
    # ------------------------------------------------------------------ #
    def _create_streamer(self):
        configuration = upstox_client.Configuration()
        configuration.access_token = self.access_token
        api_client = upstox_client.ApiClient(configuration)

        self.streamer = upstox_client.MarketDataStreamerV3(
            api_client, self.instrument_keys, self.mode
        )

        self.streamer.on("open", self._on_open)
        self.streamer.on("message", self._on_message)
        self.streamer.on("error", self._on_error)
        self.streamer.on("close", self._on_close)

    def _on_open(self):
        self._connect_ts = time.time()
        logger.info(
            "Market feed connected (mode=%s). Subscribed to: %s",
            self.mode,
            self.instrument_keys,
        )
        logger.info(
            "[HEALTH] awaiting first feed batch "
            "(mode=%s, interval=%ds, stale_threshold=%ds, raw_log=%s)",
            self.mode,
            FEED_HEALTH_INTERVAL_SECONDS,
            FEED_STALE_AFTER_SECONDS,
            LOG_RAW_FEED,
        )

    def _on_message(self, message):
        if not isinstance(message, dict):
            return

        # Raw dump only when explicitly enabled.
        if LOG_RAW_FEED:
            logger.debug("Raw feed message (mode=%s): %s", self.mode, message)

        # Market status message — log once, then ignore.
        market_info = message.get("marketInfo")
        if market_info and not self._market_status_logged:
            self._market_status_logged = True
            seg = market_info.get("segmentStatus", {})
            logger.info(
                "[HEALTH] market status: %s",
                ", ".join(f"{k}={v}" for k, v in sorted(seg.items())),
            )
            return

        feeds = message.get("feeds")
        if not feeds:
            return

        now = time.time()
        now_iso = datetime.now().isoformat(timespec="seconds")

        for key, payload in feeds.items():
            ltpc = self._extract_ltpc(payload)
            if not ltpc:
                continue
            entry = self._ltp_cache.setdefault(key, {"count": 0})
            entry["ltp"] = ltpc.get("ltp")
            entry["ltt"] = ltpc.get("ltt")
            entry["cp"] = ltpc.get("cp")
            entry["recv_ts"] = now
            entry["recv_iso"] = now_iso
            entry["count"] = entry.get("count", 0) + 1

        # Log health once, immediately after the first real batch arrives.
        if self._ltp_cache and not self._first_batch_logged:
            self._first_batch_logged = True
            self._log_health(reason="first_batch")

    @staticmethod
    def _extract_ltpc(payload: dict) -> dict | None:
        """Pull ``ltpc`` out of any of the known feed shapes."""
        if not isinstance(payload, dict):
            return None

        for wrapper in ("fullFeed",):
            inner = payload.get(wrapper)
            if isinstance(inner, dict):
                ltpc = inner.get("ltpc")
                if isinstance(ltpc, dict):
                    return ltpc
                for deeper in ("indexFF", "marketFF"):
                    deeper_val = inner.get(deeper)
                    if isinstance(deeper_val, dict):
                        ltpc = deeper_val.get("ltpc")
                        if isinstance(ltpc, dict):
                            return ltpc

        for wrapper in ("indexFF", "marketFF"):
            inner = payload.get(wrapper)
            if isinstance(inner, dict):
                ltpc = inner.get("ltpc")
                if isinstance(ltpc, dict):
                    return ltpc

        ltpc = payload.get("ltpc")
        if isinstance(ltpc, dict):
            return ltpc

        return None

    def _on_error(self, error):
        logger.error("Market feed error (mode=%s): %s", self.mode, error)

    def _on_close(self):
        logger.info("Market feed connection closed (mode=%s).", self.mode)

    # ------------------------------------------------------------------ #
    # Health logging                                                     #
    # ------------------------------------------------------------------ #
    def _log_health(self, reason: str = "interval"):
        now = time.time()
        uptime = (now - self._connect_ts) if self._connect_ts else 0.0
        logger.info(
            "[HEALTH] feed summary (reason=%s, mode=%s, uptime=%.0fs, instruments=%d)",
            reason,
            self.mode,
            uptime,
            len(self.instrument_keys),
        )
        for key in self.instrument_keys:
            entry = self._ltp_cache.get(key)
            if not entry:
                logger.info("[HEALTH] %s | no data yet", key)
                continue
            age = now - entry.get("recv_ts", now)
            stale = " STALE" if age > FEED_STALE_AFTER_SECONDS else ""
            logger.info(
                "[HEALTH] %s | ltp=%s | ltt=%s | cp=%s | age=%.1fs%s | msgs=%d",
                key,
                entry.get("ltp"),
                entry.get("ltt"),
                entry.get("cp"),
                age,
                stale,
                entry.get("count", 0),
            )

    def _health_loop(self):
        while not self._stop_event.wait(FEED_HEALTH_INTERVAL_SECONDS):
            try:
                self._log_health(reason="interval")
            except Exception:
                logger.exception("Health log failed.")

    # ------------------------------------------------------------------ #
    # Public API                                                         #
    # ------------------------------------------------------------------ #
    def connect(self):
        """Start the feed and the health-log thread."""
        if self._running:
            logger.warning("Feed already running (mode=%s).", self.mode)
            return

        self._create_streamer()
        self._running = True
        self._stop_event.clear()

        self._thread = threading.Thread(
            target=self._run, name="market-feed", daemon=True
        )
        self._thread.start()

        self._health_thread = threading.Thread(
            target=self._health_loop, name="market-feed-health", daemon=True
        )
        self._health_thread.start()

        logger.info(
            "Market feed thread started (mode=%s). Health interval=%ds, stale=%ds.",
            self.mode,
            FEED_HEALTH_INTERVAL_SECONDS,
            FEED_STALE_AFTER_SECONDS,
        )

    def _run(self):
        try:
            self.streamer.connect()  # blocks until disconnect()
        except Exception:
            logger.exception("Market feed connection failed (mode=%s).", self.mode)
            self._running = False

    def disconnect(self):
        """Disconnect the feed and stop the health thread."""
        self._running = False
        self._stop_event.set()

        if self.streamer:
            try:
                self.streamer.disconnect()
                logger.info("Market feed disconnected (mode=%s).", self.mode)
            except Exception:
                logger.exception("Error while disconnecting feed (mode=%s).", self.mode)

        try:
            self._log_health(reason="shutdown")
        except Exception:
            logger.exception("Final health log failed.")

    def get_health(self) -> dict:
        """Return a JSON-friendly health snapshot (for the /status API)."""
        now = time.time()
        instruments = {}
        for key in self.instrument_keys:
            entry = self._ltp_cache.get(key)
            if not entry:
                instruments[key] = {"status": "no_data"}
                continue
            age = now - entry.get("recv_ts", now)
            instruments[key] = {
                "ltp": entry.get("ltp"),
                "ltt": entry.get("ltt"),
                "cp": entry.get("cp"),
                "last_recv": entry.get("recv_iso"),
                "age_seconds": round(age, 1),
                "stale": age > FEED_STALE_AFTER_SECONDS,
                "message_count": entry.get("count", 0),
            }
        return {
            "running": self._running,
            "mode": self.mode,
            "uptime_seconds": round(
                (now - self._connect_ts) if self._connect_ts else 0.0, 1
            ),
            "health_interval_seconds": FEED_HEALTH_INTERVAL_SECONDS,
            "stale_after_seconds": FEED_STALE_AFTER_SECONDS,
            "instruments": instruments,
        }