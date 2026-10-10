"""Sixth job: live EMA from the upstream LTP WebSocket feed.

When ``settings.use_live_ltp_feed`` is True this replaces the REST-polling
live EMA job (``LiveEmaCrossStore``). It:

1. Opens one upstream WebSocket per subscribed instrument
   (``{LIVE_FEED_URL}?instrument_key=...``).
2. Feeds each LTP tick into a ``CandleAggregator`` aligned to
   ``settings.live_ema_interval_seconds`` (default 60s -> 1-minute buckets).
3. When a bucket closes, merges the completed candle into the in-memory
   series and recomputes fast/slow EMA.
4. Detects a bullish/bearish cross on the last **completed** candle and, if
   new, appends it to ``ema_crosses.json`` and broadcasts an
   ``ema_cross`` event on the hub.
5. Broadcasts every tick as a ``tick`` event and every closed candle as a
   ``candle_completed`` event on the hub.

Alerts are only ever produced by step 4 — which runs from the closed-bucket
callback. A partial candle can never raise an alert.

The upstream feed itself suppresses non-market-hours data, so no
market-hours gate is needed here.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from app.core.config import settings
from app.core.logger import get_logger
from app.services.candle_aggregator import CandleAggregator
from app.services.ema_utils import classify_cross, compute_ema, to_float
from app.services.instrument_paths import (
    build_candle_path,
    build_ema_cross_path,
    get_file_write_lock,
)
from app.services.live_ema_ws_hub import live_ema_ws_hub
from app.services.live_feed_client import LiveFeedClient

logger = get_logger(__name__)

class LiveLtpEmaStore:
    def __init__(self) -> None:
        self.running = False
        self.started_at: str | None = None
        self.stopped_at: str | None = None

        self.total_ticks_received = 0
        self.total_candles_closed = 0
        self.total_crosses_detected = 0

        self.last_tick_at: str | None = None
        self.last_candle_at: str | None = None
        self.last_error: str | None = None
        self.last_error_at: str | None = None

        self._instruments_by_key: dict[str, dict[str, Any]] = {}
        self._feed_clients: dict[str, LiveFeedClient] = {}
        self._aggregators: dict[str, CandleAggregator] = {}
        self._series: dict[str, list[dict[str, Any]]] = {}
        self._file_mtime: dict[str, float] = {}

    # ------------------------------------------------------------------ #
    # Lifecycle                                                          #
    # ------------------------------------------------------------------ #
    async def start(
        self,
        instruments_provider: Callable[[], list[dict[str, Any]]],
    ) -> dict[str, Any]:
        if self.running:
            return {"started": False, "reason": "already-running"}

        instruments = [i for i in instruments_provider() if isinstance(i, dict)]
        cap = settings.live_feed_max_connections
        if cap and cap > 0:
            instruments = instruments[:cap]

        self._instruments_by_key = {
            str(i.get("instrument_key")): i
            for i in instruments
            if isinstance(i.get("instrument_key"), str)
        }

        # Prime the in-memory series + mtimes from disk.
        for key, item in self._instruments_by_key.items():
            path = build_candle_path(item)
            self._series[key] = self._load_series(path)
            self._file_mtime[key] = self._stat_mtime(path)

        # Spin up one feed client + aggregator per instrument.
        for key in self._instruments_by_key:
            aggregator = CandleAggregator(
                interval_seconds=settings.live_ema_interval_seconds,
                on_candle_completed=self._make_candle_handler(key),
            )
            self._aggregators[key] = aggregator

            client = LiveFeedClient(
                instrument_key=key,
                on_tick=self._on_upstream_tick,
                on_status=self._on_upstream_status,
            )
            self._feed_clients[key] = client
            await client.start()

        self.running = True
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.stopped_at = None
        logger.info(
            "Live LTP EMA job starting; instruments=%d interval=%ds feed_url=%s",
            len(self._feed_clients),
            settings.live_ema_interval_seconds,
            settings.live_feed_url,
        )
        return {"started": True, "instruments": len(self._feed_clients)}

    async def stop(self) -> dict[str, Any]:
        if not self.running:
            return {"stopped": False, "reason": "not-running"}

        await asyncio.gather(
            *(c.stop() for c in self._feed_clients.values()),
            return_exceptions=True,
        )
        self._feed_clients.clear()
        self._aggregators.clear()
        self._instruments_by_key.clear()
        self._series.clear()
        self._file_mtime.clear()

        self.running = False
        self.stopped_at = datetime.now(timezone.utc).isoformat()
        logger.info("Live LTP EMA job stopped")
        return {"stopped": True}

    # ------------------------------------------------------------------ #
    # Upstream callbacks                                                 #
    # ------------------------------------------------------------------ #
    async def _on_upstream_status(
        self, instrument_key: str, status: str, detail: str | None
    ) -> None:
        if status in ("connected", "closed", "stopped"):
            logger.info(
                "Live LTP feed status; instrument=%s status=%s",
                instrument_key,
                status,
            )
        else:
            logger.warning(
                "Live LTP feed status; instrument=%s status=%s detail=%s",
                instrument_key,
                status,
                detail,
            )
        if status == "error":
            self.last_error = detail
            self.last_error_at = datetime.now(timezone.utc).isoformat()

    async def _on_upstream_tick(
        self, instrument_key: str, tick: dict[str, Any]
    ) -> None:
        self.total_ticks_received += 1
        self.last_tick_at = datetime.now(timezone.utc).isoformat()

        aggregator = self._aggregators.get(instrument_key)
        if aggregator is None:
            return

        aggregator.on_tick(
            ltp=tick.get("ltp"),
            ltt_ms=tick.get("ltt"),
            received_at=datetime.now(timezone.utc),
        )

        await live_ema_ws_hub.broadcast(
            instrument_key,
            {
                "type": "tick",
                "server_time": datetime.now(timezone.utc).isoformat(),
                "instrument_key": instrument_key,
                "data": {
                    "ltp": tick.get("ltp"),
                    "ltt": tick.get("ltt"),
                    "cp": tick.get("cp"),
                    "forming_candle": aggregator.current_bucket(),
                },
            },
        )

    # ------------------------------------------------------------------ #
    # Completed candle handling                                          #
    # ------------------------------------------------------------------ #
    def _make_candle_handler(self, instrument_key: str):
        def handler(candle: dict[str, Any]) -> None:
            asyncio.create_task(
                self._handle_completed_candle(instrument_key, candle)
            )
        return handler

    async def _handle_completed_candle(
        self, instrument_key: str, candle: dict[str, Any]
    ) -> None:
        try:
            await self._handle_completed_candle_inner(instrument_key, candle)
        except Exception:
            self.last_error = "completed-candle handler failed"
            self.last_error_at = datetime.now(timezone.utc).isoformat()
            logger.exception(
                "Live LTP EMA: completed-candle handler failed; instrument=%s",
                instrument_key,
            )

    async def _handle_completed_candle_inner(
        self, instrument_key: str, candle: dict[str, Any]
    ) -> None:
        self.total_candles_closed += 1
        self.last_candle_at = datetime.now(timezone.utc).isoformat()

        item = self._instruments_by_key.get(instrument_key)
        if item is None:
            return

        # Reconcile with disk if the file changed since our last read
        # (e.g., job 3 wrote today's intraday data).
        try:
            self._refresh_series_if_needed(instrument_key, item)
        except Exception:
            logger.exception(
                "Live LTP EMA: unable to refresh series; instrument=%s",
                instrument_key,
            )

        series = self._merge_candle(
            self._series.get(instrument_key, []), candle
        )
        self._series[instrument_key] = series

        fast = max(1, settings.ema_fast_period)
        slow = max(fast + 1, settings.ema_slow_period)

        # Not enough history yet: emit a candle_completed event with no EMA.
        if len(series) < max(fast, slow) + 1:
            await self._broadcast_candle_completed(instrument_key, candle, None, None)
            return

        closes = [to_float(c.get("close")) or 0.0 for c in series]
        fast_emas = compute_ema(closes, fast)
        slow_emas = compute_ema(closes, slow)

        i = len(series) - 1
        fp, sp = fast_emas[i - 1], slow_emas[i - 1]
        fc, sc = fast_emas[i], slow_emas[i]

        if None in (fp, sp, fc, sc):
            await self._broadcast_candle_completed(instrument_key, candle, None, None)
            return

        ema_payload = {
            "fast_period": fast,
            "slow_period": slow,
            "ema_fast": fc,
            "ema_slow": sc,
            "ema_fast_prev": fp,
            "ema_slow_prev": sp,
        }

        cross_type = classify_cross(float(fp) - float(sp), float(fc) - float(sc))
        cross_payload: dict[str, Any] | None = None

        if cross_type is not None:
            last_ts = series[i].get("timestamp")
            persisted = await self._persist_cross(
                instrument_key,
                item,
                series,
                i,
                cross_type,
                fp,
                sp,
                fc,
                sc,
            )
            if persisted:
                self.total_crosses_detected += 1
                cross_payload = {
                    "timestamp": last_ts,
                    "type": cross_type,
                    "close": series[i].get("close"),
                    "ema_fast": fc,
                    "ema_slow": sc,
                }
                logger.info(
                    "Live EMA cross (LTP); instrument=%s type=%s ts=%s "
                    "close=%s fast=%.4f slow=%.4f interval=%ds",
                    instrument_key,
                    cross_type,
                    last_ts,
                    series[i].get("close"),
                    fc,
                    sc,
                    settings.live_ema_interval_seconds,
                )
                await live_ema_ws_hub.broadcast(
                    instrument_key,
                    {
                        "type": "ema_cross",
                        "server_time": datetime.now(timezone.utc).isoformat(),
                        "instrument_key": instrument_key,
                        "data": cross_payload,
                    },
                )

        await self._broadcast_candle_completed(
            instrument_key, candle, ema_payload, cross_payload
        )

    async def _broadcast_candle_completed(
        self,
        instrument_key: str,
        candle: dict[str, Any],
        ema_payload: dict[str, Any] | None,
        cross_payload: dict[str, Any] | None,
    ) -> None:
        await live_ema_ws_hub.broadcast(
            instrument_key,
            {
                "type": "candle_completed",
                "server_time": datetime.now(timezone.utc).isoformat(),
                "instrument_key": instrument_key,
                "data": {
                    "candle": candle,
                    "ema": ema_payload,
                    "cross": cross_payload,
                },
            },
        )

    # ------------------------------------------------------------------ #
    # Series handling                                                    #
    # ------------------------------------------------------------------ #
    def _load_series(self, path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("Live LTP EMA: unable to read candles: %s", path)
            return []
        candles = payload.get("candles") if isinstance(payload, dict) else payload
        if not isinstance(candles, list):
            return []
        return [c for c in candles if isinstance(c, dict)]

    @staticmethod
    def _stat_mtime(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except Exception:
            return 0.0

    def _refresh_series_if_needed(
        self, instrument_key: str, item: dict[str, Any]
    ) -> None:
        path = build_candle_path(item)
        current = self._stat_mtime(path)
        if current and current != self._file_mtime.get(instrument_key):
            self._file_mtime[instrument_key] = current
            self._series[instrument_key] = self._load_series(path)

    @staticmethod
    def _merge_candle(
        series: list[dict[str, Any]], candle: dict[str, Any]
    ) -> list[dict[str, Any]]:
        ts = candle.get("timestamp")
        if ts is None:
            return series
        merged: dict[str, dict[str, Any]] = {}
        for c in series:
            t = c.get("timestamp")
            if t is not None:
                merged[str(t)] = c
        merged[str(ts)] = candle
        return sorted(merged.values(), key=lambda c: str(c.get("timestamp", "")))

    # ------------------------------------------------------------------ #
    # Cross persistence                                                  #
    # ------------------------------------------------------------------ #
    async def _persist_cross(
        self,
        instrument_key: str,
        item: dict[str, Any],
        series: list[dict[str, Any]],
        index: int,
        cross_type: str,
        fp: float,
        sp: float,
        fc: float,
        sc: float,
    ) -> bool:
        candle = series[index]
        last_ts = candle.get("timestamp")
        cross_path = build_ema_cross_path(item)
        lock = get_file_write_lock(cross_path)

        def _write() -> bool:
            with lock:
                payload = self._load_crosses(cross_path)
                if any(
                    c.get("timestamp") == last_ts for c in payload.get("crosses", [])
                ):
                    return False

                event = {
                    "index": index,
                    "timestamp": last_ts,
                    "type": cross_type,
                    "close": candle.get("close"),
                    "ema_fast": fc,
                    "ema_slow": sc,
                    "ema_fast_prev": fp,
                    "ema_slow_prev": sp,
                    "candle": candle,
                    "detected_by": "live_ltp",
                    "interval_seconds": settings.live_ema_interval_seconds,
                    "detected_at": datetime.now(timezone.utc).isoformat(),
                }
                payload.setdefault("crosses", []).append(event)
                payload["instrument_key"] = instrument_key
                payload["fast_period"] = settings.ema_fast_period
                payload["slow_period"] = settings.ema_slow_period
                payload["cross_count"] = len(payload["crosses"])
                payload["updated_at"] = datetime.now(timezone.utc).isoformat()

                cross_path.parent.mkdir(parents=True, exist_ok=True)
                tmp = cross_path.with_suffix(cross_path.suffix + ".tmp")
                tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
                tmp.replace(cross_path)
                return True

        return await asyncio.to_thread(_write)

    @staticmethod
    def _load_crosses(path: Path) -> dict[str, Any]:
        if not path.exists():
            return {"crosses": []}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {"crosses": []}
        if not isinstance(payload, dict):
            return {"crosses": []}
        if not isinstance(payload.get("crosses"), list):
            payload["crosses"] = []
        return payload

    # ------------------------------------------------------------------ #
    # Introspection                                                      #
    # ------------------------------------------------------------------ #
    def status(self) -> dict[str, Any]:
        return {
            "backend": "ltp",
            "running": self.running,
            "started_at": self.started_at,
            "stopped_at": self.stopped_at,
            "interval_seconds": settings.live_ema_interval_seconds,
            "feed_url": settings.live_feed_url,
            "instruments": len(self._feed_clients),
            "total_ticks_received": self.total_ticks_received,
            "total_candles_closed": self.total_candles_closed,
            "total_crosses_detected": self.total_crosses_detected,
            "last_tick_at": self.last_tick_at,
            "last_candle_at": self.last_candle_at,
            "last_error": self.last_error,
            "last_error_at": self.last_error_at,
            "ws_subscriptions": live_ema_ws_hub.subscription_counts(),
        }

live_ltp_ema_store = LiveLtpEmaStore()