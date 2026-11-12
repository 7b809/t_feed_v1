# core/market_feed_service.py
"""Upstox live market feed service (MarketDataStreamerV3).

Subscription **mode** is configurable via the ``FEED_MODE`` env var
(read once at import time in ``core.config``), with ``full`` as the
default. Valid values are ``ltpc``, ``full``, ``full_d30`` and
``option_greeks``. Invalid values fall back to ``full`` with a warning.

The active mode is written to the logs at three points so you can
always tell which feed the process is subscribed to:

  * ``connect()``   -> "Market feed thread started (mode=full, ...)"
  * ``_on_open()``  -> "Market feed connected (mode=full) ..."
  * every health summary header -> "[HEALTH] feed summary (..., mode=full)"

Per-message DEBUG logging of the raw feed payload is **disabled by
default**; the compact [HEALTH] line per instrument is emitted once
after the first batch, then every ``FEED_HEALTH_INTERVAL_SECONDS``
(default 300 s = 5 min), plus once on disconnect.

Set ``LOG_RAW_FEED=true`` to restore the old per-message DEBUG dump
for troubleshooting (not recommended in production).

Dynamic subscription
--------------------
The isolation service calls ``subscribe([instrument_key])`` at isolation
time to add the isolated option leg (and, if available, the counter-side
leg) to the live feed. ``subscribe()`` is idempotent, thread-safe, and
never raises — a failed subscription is logged and the isolation flow
continues with ``isolated_option_ltp_source = "unavailable"``.
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
        self.instrument_keys = list(INSTRUMENT_KEYS if instrument_keys is None else instrument_keys)
        self.mode = _resolve_mode(mode)

        self.streamer = None
        self._thread = None
        self._health_thread = None
        self._stop_event = threading.Event()
        self._running = False

        # Guards ``instrument_keys`` and ``_ltp_cache`` mutation across
        # the feed thread, the health thread, and ``subscribe()`` callers.
        self._lock = threading.RLock()

        # Per-instrument cache:
        # {key: {ltp, ltt, cp, recv_ts, recv_iso, count}}
        self._ltp_cache: dict[str, dict] = {}
        self._first_batch_logged = False
        self._market_status_logged = False
        self._connect_ts: float | None = None
        self._message_listeners: set = set()

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

        with self._lock:
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
                should_log = True
            else:
                should_log = False

        if should_log:
            self._log_health(reason="first_batch")

        with self._lock:
            listeners = list(self._message_listeners)
        for listener in listeners:
            try:
                listener(dict(feeds))
            except Exception:
                logger.exception("Market-feed message listener failed.")

    def add_message_listener(self, callback) -> None:
        """Register a thread-safe consumer of raw instrument feed payloads."""
        with self._lock:
            self._message_listeners.add(callback)

    def remove_message_listener(self, callback) -> None:
        """Remove a previously registered feed consumer."""
        with self._lock:
            self._message_listeners.discard(callback)

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

    # ------------------------------------------------------------------ #
    # SDK event handlers                                                 #
    #                                                                    #
    # The Upstox SDK dispatches error / close handlers with additional   #
    # positional arguments (observed in production logs as              #
    # "TypeError: _on_close() takes 1 positional argument but 3 were      #
    # given"). Accept *args / **kwargs so future SDK additions don't     #
    # break the handlers.                                                #
    # ------------------------------------------------------------------ #
    def _on_error(self, *args, **kwargs):
        err = args[0] if args else kwargs.get("error")
        extra = args[1:] if len(args) > 1 else ()
        logger.error(
            "Market feed error (mode=%s): %s%s",
            self.mode,
            err,
            f" extra={extra}" if extra else "",
        )

    def _on_close(self, *args, **kwargs):
        logger.info(
            "Market feed connection closed (mode=%s)%s.",
            self.mode,
            f" args={args}" if args else "",
        )

    # ------------------------------------------------------------------ #
    # Health logging                                                     #
    # ------------------------------------------------------------------ #
    def _log_health(self, reason: str = "interval"):
        now = time.time()
        uptime = (now - self._connect_ts) if self._connect_ts else 0.0

        with self._lock:
            instrument_keys = list(self.instrument_keys)
            cache_snapshot = {
                k: dict(v) for k, v in self._ltp_cache.items()
            }

        logger.info(
            "[HEALTH] feed summary (reason=%s, mode=%s, uptime=%.0fs, instruments=%d)",
            reason,
            self.mode,
            uptime,
            len(instrument_keys),
        )
        for key in instrument_keys:
            entry = cache_snapshot.get(key)
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

    def subscribe(self, instrument_keys: list[str]) -> list[str]:
        """Subscribe to additional instrument keys (idempotent).

        Used by the isolation service to add the isolated option leg
        (and, if available, the counter-side leg) to the live feed
        after a touch has been detected.

        Returns the list of keys that were actually **newly** subscribed.
        Never raises — subscription failures are logged and skipped so
        the caller can continue with a degraded snapshot.
        """
        if not instrument_keys:
            return []

        if not self.streamer or not self._running:
            logger.warning(
                "subscribe() called before feed is connected; skipping %s",
                instrument_keys,
            )
            return []

        with self._lock:
            new_keys = [
                k for k in instrument_keys
                if k and k not in self.instrument_keys
            ]

        if not new_keys:
            logger.debug(
                "subscribe(): no new keys (already subscribed) — requested=%s",
                instrument_keys,
            )
            return []

        try:
            self.streamer.subscribe(new_keys, self.mode)
        except Exception:
            logger.exception(
                "Feed subscribe failed for %s (mode=%s)",
                new_keys, self.mode,
            )
            return []

        with self._lock:
            for key in new_keys:
                if key not in self.instrument_keys:
                    self.instrument_keys.append(key)
                self._ltp_cache.setdefault(key, {"count": 0})

        logger.info(
            "Subscribed to %d new instrument(s): %s",
            len(new_keys), new_keys,
        )
        return new_keys

    def unsubscribe(self, instrument_keys: list[str]) -> list[str]:
        """Unsubscribe from instrument keys (idempotent). Never raises."""
        if not instrument_keys or not self.streamer:
            return []

        with self._lock:
            targets = [k for k in instrument_keys if k in self.instrument_keys]

        if not targets:
            return []

        try:
            self.streamer.unsubscribe(targets)
        except Exception:
            logger.exception(
                "Feed unsubscribe failed for %s (mode=%s)", targets, self.mode,
            )
            return []

        with self._lock:
            for key in targets:
                try:
                    self.instrument_keys.remove(key)
                except ValueError:
                    pass
                self._ltp_cache.pop(key, None)

        logger.info(
            "Unsubscribed from %d instrument(s): %s", len(targets), targets,
        )
        return targets

    def get_ltp(self, instrument_key: str) -> float | None:
        """Return the latest LTP for a single instrument, or ``None``.

        Convenience wrapper around ``get_health()`` used by the isolation
        service during the option-LTP snapshot loop.
        """
        with self._lock:
            entry = self._ltp_cache.get(instrument_key)
            if not entry:
                return None
            ltp = entry.get("ltp")
            return float(ltp) if ltp is not None and ltp > 0 else None

    def get_health(self) -> dict:
        """Return a JSON-friendly health snapshot (for the /status API)."""
        now = time.time()

        with self._lock:
            instrument_keys = list(self.instrument_keys)
            cache_snapshot = {
                k: dict(v) for k, v in self._ltp_cache.items()
            }

        instruments = {}
        for key in instrument_keys:
            entry = cache_snapshot.get(key)
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