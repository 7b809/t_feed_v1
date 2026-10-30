# services/live_feed_service.py
"""Upstox full-mode WebSocket manager.

Subscriptions are owned by ``feed_subscription_manager`` (2026 capacity
policy). The feed no longer subscribes to every registry key on
startup — it subscribes to the mandatory set (four index rows +
health-feed + isolated/promoted) plus the per-index ATM strike band
(seeded from cached LTPs and updated every
``LIVE_FEED_ATM_RECALC_SECONDS`` from live ticks). Any other key is
pinned on demand (chart page, layout page, /api/upstox/subscribe,
dummy-order service). The manager caps the total at
``LIVE_FEED_MAX_TOTAL_INSTRUMENTS`` so reconnects never briefly exceed
the 2,000 hard cap.

Setting ``LIVE_FEED_ATM_BAND_ENABLED=false`` restores the pre-2026
"subscribe every registry key, capped at MAX_TOTAL" behaviour.

SR integration + level isolation
--------------------------------
* On start, every subscribed instrument is registered with
  ``live_sr_levels_service``.
* A mid-session SR backfill runs BEFORE the streamer thread is spawned,
  so the dashboard has OR/SR levels immediately — BUT ONLY when the
  current market-local time is inside [09:15, 23:59] IST on a
  Mon–Fri, non-holiday trading day.
* Each tick is forwarded to ``live_sr_levels_service.on_tick`` so the
  SR engine builds 1-min candles and detects pivots.
* Every NIFTY / BANKNIFTY / MIDCPNIFTY / SENSEX index tick is forwarded
  to ``level_isolated_instrument_service.on_tick`` so the
  level-isolation engine can watch for R3 / S3 touches.
* Every index tick is also forwarded to
  ``feed_subscription_manager.on_index_tick`` so the ATM band can slide.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any

from core import config
from core.logger import get_logger
from services.instrument_registry import instrument_registry
from services.level_isolated_instrument_service import (
    CORE_INDEX_KEYS as LEVEL_ISOLATION_INDEX_KEYS,
    level_isolated_instrument_service,
)
from services.live_broadcast import live_broadcast
from services.live_sr_levels_service import live_sr_levels_service
from services.token_service import token_service

logger = get_logger(__file__)

DEFAULT_CORE_INSTRUMENTS = [
    ("NSE_INDEX|Nifty 50", "NIFTY 50"),
    ("NSE_INDEX|Nifty Bank", "NIFTY BANK"),
    ("NSE_INDEX|NIFTY MID SELECT", "NIFTY MID SELECT"),
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
        self._latest_ltp: dict[str, float] = {}
        self._lifecycle_lock = threading.Lock()
        self._subscribe_lock = threading.RLock()
        self._generation: int = 0

    def set_loop(self, loop) -> None:
        self._loop = loop

    # ------------------------------------------------------------------ #
    # Subscription helpers                                               #
    # ------------------------------------------------------------------ #
    def _ensure_core_subscriptions(self) -> None:
        targets = [k for k, _ in DEFAULT_CORE_INSTRUMENTS]
        if config.HEALTH_FEED_INSTRUMENT:
            targets.append(config.HEALTH_FEED_INSTRUMENT.strip())
        for target in targets:
            if not target:
                continue
            if not instrument_registry.get(target):
                symbol = target.split("|", 1)[-1]
                instrument_registry.register_adhoc(
                    target, trading_symbol=symbol,
                )
            if target not in self._subscribed:
                self._subscribed.append(target)
                logger.info(
                    "Core index feed [%s] ensured in subscription list.",
                    target,
                )

    def _register_all_with_sr_engine(self) -> None:
        if not config.LIVE_SR_ENABLED:
            return
        if not config.LIVE_SR_TRACK_ALL_INSTRUMENTS:
            logger.info(
                "[live-feed] SR track-all disabled — only level-filtered "
                "instruments will be tracked by live_sr_levels_service"
            )
            return
        try:
            entries: list[dict[str, Any]] = []
            for key in list(self._subscribed):
                entry = instrument_registry.get(key)
                if entry is not None:
                    entries.append(entry)
            live_sr_levels_service.register_all(entries)
            logger.info(
                "[live-feed] SR track-all: %d instrument(s) registered",
                len(entries),
            )
        except Exception:
            logger.exception("[live-feed] SR register_all failed")

    def _run_sr_backfill(self, reason: str) -> None:
        if not config.SR_BACKFILL_ENABLED:
            logger.info("[live-feed] SR backfill disabled by config")
            return

        try:
            from services.sr_backfill_service import sr_backfill_service
        except Exception:
            logger.exception(
                "[live-feed] sr_backfill_service import failed"
            )
            return

        should_run, skip_reason = sr_backfill_service.should_run()
        if not should_run:
            logger.info(
                "[live-feed] SR backfill skipped at feed-start — %s",
                skip_reason,
            )
            return

        try:
            entries: list[dict[str, Any]] = []
            for key in list(self._subscribed):
                entry = instrument_registry.get(key)
                if entry is not None:
                    entries.append(entry)
            sr_backfill_service.run(reason=reason, entries=entries)
        except Exception:
            logger.exception(
                "[live-feed] SR backfill failed; continuing without it"
            )

    # ------------------------------------------------------------------ #
    # NEW: subscription-budget integration                               #
    # ------------------------------------------------------------------ #
    def _build_initial_subscription_set(self) -> list[str]:
        """Determine the initial subscription list via the manager."""
        from services.feed_subscription_manager import (
            feed_subscription_manager,
        )

        mandatory: list[str] = [k for k, _ in DEFAULT_CORE_INSTRUMENTS]
        if config.HEALTH_FEED_INSTRUMENT:
            hf = config.HEALTH_FEED_INSTRUMENT.strip()
            if hf and hf not in mandatory:
                mandatory.append(hf)

        # Isolated instrument
        try:
            iso_status = level_isolated_instrument_service.last_status()
            iso = (iso_status or {}).get("isolated") or {}
            iso_key = str(iso.get("instrument_key") or "").strip()
            if iso_key and iso_key not in mandatory:
                mandatory.append(iso_key)
                feed_subscription_manager.set_isolated(iso_key)
        except Exception:
            logger.exception(
                "[live-feed] isolated-instrument lookup failed during start"
            )

        # Promoted instrument
        try:
            from services.isolated_instrument_promotion_service import (
                isolated_instrument_promotion_service,
            )
            active = isolated_instrument_promotion_service.read_active()
            if isinstance(active, dict):
                promo_key = str(
                    active.get("to_instrument_key") or ""
                ).strip()
                if promo_key and promo_key not in mandatory:
                    mandatory.append(promo_key)
                    feed_subscription_manager.set_promoted(promo_key)
        except Exception:
            logger.exception(
                "[live-feed] promotion lookup failed during start"
            )

        feed_subscription_manager.set_mandatory(mandatory)
        feed_subscription_manager.set_apply_callback(
            self._apply_desired_subscriptions
        )

        if not getattr(config, "LIVE_FEED_ATM_BAND_ENABLED", True):
            # Legacy behaviour: everything in the registry, capped at
            # LIVE_FEED_MAX_TOTAL_INSTRUMENTS.
            all_keys = list(instrument_registry.subscribe_keys())
            max_total = int(
                getattr(config, "LIVE_FEED_MAX_TOTAL_INSTRUMENTS", 1800)
            )
            ordered: list[str] = []
            seen: set[str] = set()
            for k in mandatory + all_keys:
                if k and k not in seen:
                    seen.add(k)
                    ordered.append(k)
                    if max_total > 0 and len(ordered) >= max_total:
                        break
            logger.info(
                "[live-feed] legacy mode — subscribing %d key(s) "
                "(max=%d, registry=%d)",
                len(ordered), max_total, len(all_keys),
            )
            return ordered

        # Manager mode: seed ATM bands from cached LTPs (may be empty on
        # a fresh process — subsequent index ticks will fill them in).
        try:
            feed_subscription_manager.recompute_atm_bands_from_registry(
                instrument_registry
            )
        except Exception:
            logger.exception(
                "[live-feed] initial ATM band seeding failed"
            )

        return feed_subscription_manager.desired_keys()

    def _apply_desired_subscriptions(self, desired: set[str]) -> None:
        """Callback invoked by ``feed_subscription_manager.apply()``.

        Diffs the desired set against what we currently have,
        batch-subscribes / unsubscribes on the streamer, and keeps
        ``self._subscribed`` and the SR engine in sync.
        """
        with self._subscribe_lock:
            current = set(self._subscribed)
            to_add = desired - current
            to_remove = current - desired
            if not to_add and not to_remove:
                return

            streamer = self._streamer
            running = bool(
                self._thread
                and self._thread.is_alive()
                and streamer is not None
            )

            # SR registration for newly-added instruments.
            if (
                config.LIVE_SR_ENABLED
                and config.LIVE_SR_TRACK_ALL_INSTRUMENTS
                and to_add
            ):
                for k in to_add:
                    try:
                        entry = instrument_registry.get(k)
                        if entry is not None:
                            live_sr_levels_service.register_instrument(
                                entry
                            )
                    except Exception:
                        logger.exception(
                            "[live-feed] SR register failed for %s", k
                        )

            BATCH = 50

            if running:
                add_list = list(to_add)
                for i in range(0, len(add_list), BATCH):
                    chunk = add_list[i:i + BATCH]
                    try:
                        streamer.subscribe(
                            chunk, config.UPSTOX_WS_MODE
                        )
                    except Exception:
                        logger.exception(
                            "[live-feed] streamer.subscribe batch "
                            "failed (size=%d)", len(chunk),
                        )
                        continue

                remove_list = list(to_remove)
                for i in range(0, len(remove_list), BATCH):
                    chunk = remove_list[i:i + BATCH]
                    try:
                        streamer.unsubscribe(chunk)
                    except Exception:
                        logger.exception(
                            "[live-feed] streamer.unsubscribe batch "
                            "failed (size=%d)", len(chunk),
                        )
                        continue

            # Update local tracking.
            for k in to_add:
                if k not in self._subscribed:
                    self._subscribed.append(k)
            if to_remove:
                self._subscribed = [
                    k for k in self._subscribed if k not in to_remove
                ]

            logger.info(
                "[live-feed] subscription delta applied — +%d -%d "
                "(total=%d)",
                len(to_add), len(to_remove), len(self._subscribed),
            )

    # ------------------------------------------------------------------ #
    # Lifecycle                                                          #
    # ------------------------------------------------------------------ #
    def start(self) -> bool:
        with self._lifecycle_lock:
            if self._thread and self._thread.is_alive():
                return True
            if not config.UPSTOX_WS_AUTO_START:
                logger.info("Live feed auto-start disabled by config")
                return False

            self._stop.clear()
            self._subscribed = list(
                self._build_initial_subscription_set()
            )

            self._register_all_with_sr_engine()
            self._run_sr_backfill(reason="feed_start")

            self._generation += 1
            generation = self._generation

            self._thread = threading.Thread(
                target=self._run,
                args=(generation,),
                name="upstox-live-feed",
                daemon=True,
            )
            self._thread.start()
            self._started_at = datetime.now(timezone.utc)
            logger.info(
                "Live feed starting with %d instruments (generation=%d)",
                len(self._subscribed), generation,
            )
            return True

    def stop(self) -> None:
        # Set the stop flag and bump the generation without holding
        # the lifecycle lock during join() — the WS thread may be
        # inside a subscribe callback (through the manager) that
        # acquires _subscribe_lock, and we must not deadlock.
        self._stop.set()
        self._generation += 1
        old_generation = self._generation - 1

        with self._lifecycle_lock:
            streamer = self._streamer
            thread = self._thread
            self._thread = None

        try:
            if streamer is not None:
                streamer.disconnect()
        except Exception:
            logger.exception("Error disconnecting Upstox streamer")

        if thread and thread.is_alive():
            thread.join(timeout=10)
            if thread.is_alive():
                logger.warning(
                    "Live feed worker (generation=%d) did not exit "
                    "within 10s; orphaned.",
                    old_generation,
                )
        logger.info("Live feed stopped")

    def restart(self, skip_sr_backfill: bool = False) -> bool:
        self.stop()
        time.sleep(1)
        if skip_sr_backfill:
            original = config.SR_BACKFILL_ENABLED
            config.SR_BACKFILL_ENABLED = False
            try:
                return self.start()
            finally:
                config.SR_BACKFILL_ENABLED = original
        return self.start()

    def is_subscribed(self, instrument_key: str) -> bool:
        key = str(instrument_key or "").strip()
        return key in self._subscribed

    # ------------------------------------------------------------------ #
    # LTP cache                                                          #
    # ------------------------------------------------------------------ #
    def get_latest_ltp(self, instrument_key: str) -> float | None:
        return self._latest_ltp.get(str(instrument_key or "").strip())

    def get_latest_ltp_snapshot(self, limit: int = 50) -> dict[str, float]:
        if not self._latest_ltp:
            return {}
        if limit and limit > 0:
            items = list(self._latest_ltp.items())[:limit]
            return dict(items)
        return dict(self._latest_ltp)

    # ------------------------------------------------------------------ #
    # On-demand subscription                                             #
    # ------------------------------------------------------------------ #
    def subscribe_instrument(self, instrument_key: str) -> bool:
        """Pin + subscribe one instrument on demand.

        Adds the key to the feed-subscription manager's LRU (so it is
        protected from eviction until the LRU is drained) and then
        ensures the streamer is actually subscribed to it.
        """
        key = str(instrument_key or "").strip()
        if not key:
            return False

        # Pin through the manager for LRU accounting.
        try:
            from services.feed_subscription_manager import (
                feed_subscription_manager,
            )
            feed_subscription_manager.pin(key)
        except Exception:
            logger.exception(
                "[live-feed] feed-sub manager pin failed for %s", key
            )

        if key in self._subscribed:
            return True

        with self._subscribe_lock:
            if key in self._subscribed:
                return True
            self._subscribed.append(key)

            if (
                config.LIVE_SR_ENABLED
                and config.LIVE_SR_TRACK_ALL_INSTRUMENTS
            ):
                try:
                    entry = instrument_registry.get(key)
                    if entry is not None:
                        live_sr_levels_service.register_instrument(entry)
                except Exception:
                    logger.exception(
                        "[live-feed] SR register on subscribe failed "
                        "for %s", key,
                    )

            streamer = self._streamer
            if (
                streamer is None
                or not (self._thread and self._thread.is_alive())
            ):
                self._stop.clear()
                self._generation += 1
                generation = self._generation
                self._thread = threading.Thread(
                    target=self._run,
                    args=(generation,),
                    name="upstox-live-feed",
                    daemon=True,
                )
                self._thread.start()
                self._started_at = datetime.now(timezone.utc)
                logger.info(
                    "Live feed starting with %d instruments "
                    "(generation=%d, via subscribe_instrument)",
                    len(self._subscribed), generation,
                )
                return True
            try:
                streamer.subscribe([key], config.UPSTOX_WS_MODE)
                logger.info("Subscribed on demand: %s", key)
                return True
            except Exception:
                logger.exception("Streamer.subscribe failed for %s", key)
                return False

    def unsubscribe_instrument(self, instrument_key: str) -> bool:
        """Remove a key from the on-demand LRU and unsubscribe it."""
        key = str(instrument_key or "").strip()
        if not key:
            return False

        try:
            from services.feed_subscription_manager import (
                feed_subscription_manager,
            )
            feed_subscription_manager.unpin(key)
        except Exception:
            logger.exception(
                "[live-feed] feed-sub manager unpin failed for %s", key
            )

        with self._subscribe_lock:
            if key not in self._subscribed:
                return True
            streamer = self._streamer
            if streamer is not None and self._thread and self._thread.is_alive():
                try:
                    streamer.unsubscribe([key])
                except Exception:
                    logger.exception(
                        "[live-feed] streamer.unsubscribe failed for %s",
                        key,
                    )
            self._subscribed = [
                k for k in self._subscribed if k != key
            ]
            logger.info("Unsubscribed on demand: %s", key)
            return True

    # ------------------------------------------------------------------ #
    # Worker loop                                                        #
    # ------------------------------------------------------------------ #
    def _run(self, generation: int) -> None:
        sdk = _load_sdk()
        if sdk is None:
            logger.error("upstox_client is not installed; live feed disabled")
            return
        while not self._stop.is_set() and generation == self._generation:
            try:
                self._connect_and_listen(sdk, generation)
            except Exception:
                logger.exception("Live feed crashed; will retry")
            if self._stop.is_set() or generation != self._generation:
                break
            logger.info(
                "Reconnecting live feed in %ds",
                config.UPSTOX_WS_RECONNECT_INTERVAL,
            )
            self._stop.wait(config.UPSTOX_WS_RECONNECT_INTERVAL)

    def _connect_and_listen(self, sdk, generation: int) -> None:
        token = token_service.get_access_token()
        if not token:
            raise RuntimeError(
                "No Upstox access token available for live feed"
            )
        self._ensure_core_subscriptions()
        configuration = sdk.Configuration()
        configuration.access_token = token
        client = sdk.ApiClient(configuration)
        streamer = sdk.MarketDataStreamerV3(
            client, list(self._subscribed), config.UPSTOX_WS_MODE,
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
            logger.info(
                "Upstox feed closed: args=%s kwargs=%s", args, kwargs,
            )

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
            "Connecting live feed mode=%s instruments=%d generation=%d",
            config.UPSTOX_WS_MODE, len(self._subscribed), generation,
        )
        streamer.connect()
        while not self._stop.is_set() and generation == self._generation:
            time.sleep(0.5)
        try:
            streamer.disconnect()
        except Exception:
            logger.exception("Error disconnecting live feed")

    # ------------------------------------------------------------------ #
    # Feed handling                                                      #
    # ------------------------------------------------------------------ #
    def _handle_feed(self, message: Any) -> None:
        payload = (
            message if isinstance(message, dict)
            else getattr(message, "data", None)
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
            ltp = tick.get("ltp")
            if ltp is not None:
                try:
                    self._latest_ltp[instrument_key] = float(ltp)
                except (TypeError, ValueError):
                    pass

            if (
                config.HEALTH_FEED_LOG
                and instrument_key == config.HEALTH_FEED_INSTRUMENT
            ):
                now = time.monotonic()
                interval_seconds = (
                    config.HEALTH_FEED_INSTRUMENT_INTERVAL * 60
                )
                if (now - self._last_health_log_time) >= interval_seconds:
                    self._last_health_log_time = now
                    candle_1m = tick.get("candle_1m") or {}
                    logger.info(
                        "[HEALTH FEED] instrument=%s | ltp=%s | "
                        "prev_close=%s | 1m_ohlc=(O:%s H:%s L:%s C:%s) | "
                        "vol=%s (interval=%dm)",
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

            if config.LIVE_SR_ENABLED:
                try:
                    live_sr_levels_service.on_tick(
                        instrument_key, entry, tick,
                    )
                except Exception:
                    logger.exception(
                        "[live-feed] SR on_tick failed for %s",
                        instrument_key,
                    )

            # --- LEVEL ISOLATION HOOK --------------------------------- #
            if (
                config.LEVEL_ISOLATION_ENABLED
                and instrument_key in LEVEL_ISOLATION_INDEX_KEYS
                and ltp is not None
            ):
                try:
                    level_isolated_instrument_service.on_tick(
                        instrument_key, ltp,
                    )
                except Exception:
                    logger.exception(
                        "[live-feed] level isolation on_tick failed "
                        "for %s", instrument_key,
                    )

                # --- FEED SUBSCRIPTION MANAGER HOOK ------------------ #
                # Recompute the ATM band for this index (throttled inside
                # the manager via LIVE_FEED_ATM_RECALC_SECONDS).
                try:
                    from services.feed_subscription_manager import (
                        feed_subscription_manager,
                    )
                    feed_subscription_manager.on_index_tick(
                        instrument_key, float(ltp),
                    )
                except Exception:
                    logger.exception(
                        "[live-feed] feed-sub manager tick failed for %s",
                        instrument_key,
                    )
            # --------------------------------------------------------- #

            self._publish_feed_snapshot(entry, tick)

    def _extract_tick(self, feed: dict[str, Any]) -> dict[str, Any] | None:
        if not isinstance(feed, dict):
            return None
        ff = feed.get("fullFeed") or feed.get("ff") or {}
        oc = feed.get("oc") or {}
        market_ff = feed.get("marketFF") or ff.get("marketFF") or {}
        index_ff = feed.get("indexFF") or ff.get("indexFF") or {}
        market_level = feed.get("marketLevel") or ff.get("marketLevel") or {}

        ltpc = (
            feed.get("ltpc") or ff.get("ltpc") or index_ff.get("ltpc")
            or market_ff.get("ltpc") or oc.get("ltpc") or {}
        )
        ltp = self._flt(
            ltpc.get("ltp") or feed.get("ltp") or market_ff.get("ltp")
        )
        prev_close = self._flt(
            ltpc.get("cp") or feed.get("cp") or feed.get("prev_close")
        )

        candle_1m = None
        market_ohlc = (
            index_ff.get("marketOHLC") or market_ff.get("marketOHLC")
            or ff.get("marketOHLC") or feed.get("marketOHLC") or {}
        )
        ohlc_list = market_ohlc.get("ohlc")
        ohlc_vol = None
        if isinstance(ohlc_list, list):
            for item in ohlc_list:
                if isinstance(item, dict):
                    if str(item.get("interval")) == "I1":
                        candle_1m = item
                    elif str(item.get("interval")).lower() in ("1d", "d"):
                        ohlc_vol = self._flt(
                            item.get("volume") or item.get("vol")
                        )

        efd = (
            feed.get("eFeedDetails") or ff.get("eFeedDetails")
            or market_ff.get("eFeedDetails") or oc.get("eFeedDetails") or {}
        )
        volume = self._flt(
            efd.get("vtt") or market_level.get("vtt")
            or feed.get("vtt") or feed.get("volume")
            or market_ff.get("volume") or ohlc_vol
            or (
                candle_1m.get("volume")
                if isinstance(candle_1m, dict) else None
            )
        )

        if ltp is None and candle_1m is None:
            return None

        return {
            "ltp": ltp,
            "volume": volume,
            "candle_1m": candle_1m,
            "prev_close": prev_close,
            "ltt": ltpc.get("ltt"),
        }

    @staticmethod
    def _flt(value: Any) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _publish_feed_snapshot(
        self, entry: dict[str, Any], tick: dict[str, Any],
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
            "ltp_cached": len(self._latest_ltp),
            "generation": self._generation,
            "started_at": (
                self._started_at.isoformat() if self._started_at else None
            ),
            "last_tick_at": {
                k: v.isoformat()
                for k, v in list(self._last_tick_at.items())[:50]
            },
        }


live_feed_service = LiveFeedService()