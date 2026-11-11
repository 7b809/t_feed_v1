# app/services/sr_feed_service.py
"""
Job 4 orchestrator: live SR levels per (instrument, config).

Lifecycle mirrors EmaFeedService:
  * bootstrap : seed (index × default configs), warmup, start_live
  * live loop : cycle-aligned to HH:MM:10 every SR_POLL_SECONDS
  * per cycle : one intraday fetch per instrument; SR computed for every
                subscribed config; only state-changing levels emit
                `sr_update`; every config emits a `heartbeat` per
                SR_HEARTBEAT_CYCLES.

Config format: "<left_bars>/<right_bars>" (see sr_calculator.py).

Broadcast channel per (instrument, config):
    sr:<instrument_key>:<left_bars>/<right_bars>

Today-only policy
-----------------
Per the product requirement, SR data exposed to WebSocket clients is
filtered to levels whose pivot bar was formed **today (IST)**. The
underlying calculation still uses the merged (historical + intraday)
candle set so pivots are detected accurately, but `_today_only_result`
drops any pivot whose `pivot_time` is not today before the payload is
emitted. Every snapshot / sr_update / heartbeat payload therefore
carries:

    "data_scope":  "today_only"
    "today_ist_date": "YYYY-MM-DD"

The initial WebSocket snapshot additionally carries:

    "feed_status":  "connected"
    "feed_message": "Feed connected successfully"
    "scope_note":   "For further reference, only today's SR levels are used"

Full-pivot view (``levels``)
----------------------------
Alongside the side-filtered + capped ``resistances`` / ``supports``
views, every snapshot and sr_update also carries a ``levels`` array
containing **every** today pivot, chronological by ``pivot_bar_index``
— i.e. exactly what a manual export of today's SR pivots would contain.
This lets consumers (dashboards, the ``today_sr.json`` writer, external
tools) see the full today-only SR picture without having to infer the
side-filtered subset back to the original pivot list.

``levels`` is the today-only projection of ``compute_sr()``'s
``all_pivots`` field. ``sr_level_count`` equals ``len(levels)``.

See app/api/sr.py for the `connection_ack` frame that precedes the
first snapshot on every new WebSocket connection.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, TYPE_CHECKING

from app.config import INDEX_METADATA, Settings
from app.services.ema_calculator import parse_ts
from app.services.feed_broadcaster import FeedBroadcaster
from app.services.instrument_store import InstrumentStore, InstrumentStoreError
from app.services.sr_calculator import (
    DEFAULT_CONFIGS,
    SrCalculationError,
    compute_sr,
    config_slug,
    parse_config,
)
from app.services.sr_state_store import SrStateStore
from app.services.upstox_client_service import UpstoxError, UpstoxService

if TYPE_CHECKING:  # pragma: no cover
    from app.services.telegram_service import ProjectNotifier

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))
CYCLE_OFFSET_SECONDS = 10

class SrFeedError(RuntimeError):
    pass

def _now_ist() -> datetime:
    return datetime.now(tz=IST)

def _level_signature(p: dict) -> tuple:
    """
    Stable identity of one SR level record for change detection.

    Two levels are the same if their level-kind, price and pivot bar index
    match. ``confirmed_time``/``confirmed_time_price`` are derived from the
    pivot and do not need separate comparison.

    Handles legacy on-disk records that only carry ``type`` / ``price``.
    """
    try:
        price = round(float(p.get("price", 0.0)), 4)
    except (TypeError, ValueError):
        price = None
    return (
        p.get("level") or p.get("type"),
        price,
        p.get("pivot_bar_index"),
    )

def _levels_equal(a: dict | None, b: dict | None) -> bool:
    """True if both snapshots carry the same resistance/support identities."""
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    if not isinstance(a, dict) or not isinstance(b, dict):
        return a == b
    for side in ("resistances", "supports"):
        la = a.get(side) or []
        lb = b.get(side) or []
        if len(la) != len(lb):
            return False
        for x, y in zip(la, lb):
            if _level_signature(x) != _level_signature(y):
                return False
    return True

class SrFeedService:
    def __init__(
        self,
        config: Settings,
        broadcaster: FeedBroadcaster,
        instruments: InstrumentStore,
        upstox: UpstoxService,
        state_store: SrStateStore,
        notifier: "ProjectNotifier | None" = None,
    ) -> None:
        self.config = config
        self.broadcaster = broadcaster
        self.instruments = instruments
        self.upstox = upstox
        self.state_store = state_store
        self.notifier = notifier

        self._lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(max(1, config.sr_max_workers))

        # instrument_key -> {"meta": {...},
        #                    "configs": {cfg: {"last": ..., "last_filtered": ...,
        #                                      "candle_count": N}},
        #                    "_history": [...]}
        self._runtime: dict[str, dict[str, Any]] = {}
        self._subscribed: set[tuple[str, str]] = set()

        self._live_task: asyncio.Task | None = None
        self._last_cycle_start: datetime | None = None
        self._cycle_number: int = 0

    # ------------------------------------------------------------------
    # Identity helpers
    # ------------------------------------------------------------------
    def canonical_key(self, raw: str) -> str:
        """Map 'nifty' / 'NIFTY' / 'NSE_INDEX|Nifty 50' → the raw instrument_key."""
        if raw in INDEX_METADATA:
            return raw
        lowered = (raw or "").strip().lower()
        if not lowered:
            return raw
        for key, meta in INDEX_METADATA.items():
            if lowered in (
                (meta.get("folder") or "").lower(),
                (meta.get("canonical") or "").lower(),
                key.lower(),
            ):
                return key
        return raw

    def default_configs(self) -> list[str]:
        return list(self.config.sr_default_configs_list or DEFAULT_CONFIGS)

    # ------------------------------------------------------------------
    # Today-only filtering
    # ------------------------------------------------------------------
    def _filter_pivots_today(
        self, pivots: list[dict], today_ist
    ) -> list[dict]:
        """
        Keep only pivots whose ``pivot_time`` falls on ``today_ist`` (IST).

        Pivots with an unparseable / missing timestamp are dropped — we
        never want to leak yesterday's levels into a today-only payload.
        Order is preserved (find_pivots returns chronological by
        ``pivot_bar_index``).
        """
        filtered: list[dict] = []
        today_iso = today_ist.isoformat()
        for p in pivots:
            if not isinstance(p, dict):
                continue
            ts = parse_ts(p.get("pivot_time"))
            if ts is None:
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=IST)
            try:
                if ts.astimezone(IST).date().isoformat() == today_iso:
                    filtered.append(p)
            except (ValueError, OSError):
                continue
        return filtered

    def _today_only_result(
        self, result: dict[str, Any] | None, today_ist
    ) -> dict[str, Any]:
        """
        Return a copy of ``result`` with:

          * ``resistances`` / ``supports`` filtered to today's pivots
            (side-filtered + capped subsets, unchanged semantics);
          * ``levels`` — the FULL today-pivot list, chronological by
            ``pivot_bar_index`` (mirrors a manual export of today's SR
            pivots). Same shape as every other level entry;
          * ``sr_level_count`` — ``len(levels)`` for convenience;
          * ``data_scope`` / ``today_ist_date`` markers.
        """
        if not isinstance(result, dict):
            return {
                "resistances": [],
                "supports": [],
                "levels": [],
                "sr_level_count": 0,
                "current_price": None,
                "last_ts": None,
                "config": None,
                "left_bars": None,
                "right_bars": None,
                "data_scope": "today_only",
                "today_ist_date": today_ist.isoformat(),
            }
        out = dict(result)
        out.pop("all_pivots", None)  # avoid leaking the raw key; use ``levels``
        out["resistances"] = self._filter_pivots_today(
            result.get("resistances") or [], today_ist
        )
        out["supports"] = self._filter_pivots_today(
            result.get("supports") or [], today_ist
        )
        levels = self._filter_pivots_today(
            result.get("all_pivots") or [], today_ist
        )
        out["levels"] = levels
        out["sr_level_count"] = len(levels)
        out["data_scope"] = "today_only"
        out["today_ist_date"] = today_ist.isoformat()
        return out

    # ------------------------------------------------------------------
    # Bootstrap
    # ------------------------------------------------------------------
    async def bootstrap(self) -> None:
        if not self.config.sr_enabled:
            logger.info("SR service disabled (SR_ENABLED=false)")
            return

        for key in self.config.index_instrument_keys:
            for cfg in self.default_configs():
                try:
                    await self.subscribe(key, cfg, seed_only=True)
                except Exception:  # noqa: BLE001
                    logger.exception("SR bootstrap failed for %s / %s", key, cfg)

        try:
            await self.warmup_all()
        except Exception:  # noqa: BLE001
            logger.exception("SR warmup failed; will retry on next cycle")

        await self.start_live()

    async def warmup_all(self) -> None:
        pairs = list(self._subscribed)
        await asyncio.gather(
            *(self._warm_up(k, c) for k, c in pairs),
            return_exceptions=True,
        )

    async def _warm_up(self, instrument_key: str, config: str) -> None:
        runtime = self._runtime.get(instrument_key)
        if runtime is None:
            return
        meta = runtime["meta"]
        try:
            async with self._semaphore:
                historical = await asyncio.wait_for(
                    asyncio.to_thread(
                        self.upstox.historical_window,
                        instrument_key,
                        self.config.sr_history_days,
                    ),
                    timeout=self.config.sr_fetch_timeout_seconds * 4,
                )
                intraday = await asyncio.wait_for(
                    asyncio.to_thread(self.upstox.intraday_1m, instrument_key),
                    timeout=self.config.sr_fetch_timeout_seconds,
                )
        except (asyncio.TimeoutError, UpstoxError) as exc:
            logger.warning("SR warmup fetch failed for %s: %s", instrument_key, exc)
            return

        merged = self._merge_candles(historical, intraday)
        if not merged:
            logger.info("SR warmup: no candles for %s", instrument_key)
            return
        self._set_history(instrument_key, merged)

        try:
            result = compute_sr(
                merged, config, max_per_side=self.config.sr_max_levels_per_side
            )
        except SrCalculationError as exc:
            logger.warning(
                "SR warmup calc failed for %s / %s: %s", instrument_key, config, exc
            )
            return

        today_ist = _now_ist().date()
        runtime["configs"].setdefault(config, {})
        runtime["configs"][config]["last"] = result
        runtime["configs"][config]["last_filtered"] = self._today_only_result(
            result, today_ist
        )
        runtime["configs"][config]["candle_count"] = len(merged)
        try:
            self.state_store.save(instrument_key, meta, config, result)
        except Exception:  # noqa: BLE001
            logger.exception("SR state persist failed during warmup")

    # ------------------------------------------------------------------
    # Subscription management
    # ------------------------------------------------------------------
    async def subscribe(
        self, instrument_key: str, config: str, *, seed_only: bool = False
    ) -> dict[str, Any]:
        try:
            left_bars, right_bars = parse_config(config)
        except SrCalculationError as exc:
            raise SrFeedError(str(exc)) from exc
        canonical_cfg = f"{left_bars}/{right_bars}"
        canonical_key = self.canonical_key(instrument_key)
        meta = self._resolve_meta(canonical_key)

        async with self._lock:
            runtime = self._runtime.get(canonical_key)
            if runtime is None:
                runtime = {"meta": meta, "configs": {}}
                self._runtime[canonical_key] = runtime
            else:
                runtime["meta"] = meta

            already = (canonical_key, canonical_cfg) in self._subscribed
            self._subscribed.add((canonical_key, canonical_cfg))

            if canonical_cfg not in runtime["configs"]:
                persisted = self.state_store.load(canonical_key, meta, canonical_cfg)
                last_result = (persisted or {}).get("last") or persisted
                runtime["configs"][canonical_cfg] = {
                    "last": last_result,
                    "last_filtered": (
                        self._today_only_result(last_result, _now_ist().date())
                        if last_result else None
                    ),
                    "candle_count": int((persisted or {}).get("candle_count") or 0),
                }

        if not seed_only and not already:
            await self._warm_up(canonical_key, canonical_cfg)

        return {
            "instrument_key": canonical_key,
            "config": canonical_cfg,
            "status": "already_subscribed" if already else "subscribed",
        }

    async def unsubscribe(self, instrument_key: str, config: str) -> bool:
        try:
            canonical_cfg = config_slug(config)
        except SrCalculationError:
            return False
        canonical_key = self.canonical_key(instrument_key)
        async with self._lock:
            self._subscribed.discard((canonical_key, canonical_cfg))
            runtime = self._runtime.get(canonical_key)
            if runtime:
                runtime["configs"].pop(canonical_cfg, None)
                if not runtime["configs"]:
                    self._runtime.pop(canonical_key, None)
        return True

    # ------------------------------------------------------------------
    # Live loop
    # ------------------------------------------------------------------
    async def start_live(self) -> None:
        if self._live_task and not self._live_task.done():
            return
        self._live_task = asyncio.create_task(self._live_loop(), name="sr-live-loop")
        logger.info(
            "SR live loop started interval=%ss offset=:%02ds heartbeat_every=%s "
            "fetch_timeout=%ss",
            self.config.sr_poll_seconds,
            CYCLE_OFFSET_SECONDS,
            self.config.sr_heartbeat_cycles,
            self.config.sr_fetch_timeout_seconds,
        )

    async def shutdown(self) -> None:
        if self._live_task and not self._live_task.done():
            self._live_task.cancel()
            try:
                await self._live_task
            except asyncio.CancelledError:
                pass
        self._live_task = None

    async def _live_loop(self) -> None:
        try:
            while True:
                now = _now_ist()
                target = self._next_cycle_target(now)
                wait = (target - now).total_seconds()
                if wait > 0.05:
                    await asyncio.sleep(wait)
                try:
                    await self._run_cycle()
                except Exception:  # noqa: BLE001
                    logger.exception("SR live cycle failed")
        except asyncio.CancelledError:
            logger.info("SR live loop cancelled")
            raise

    def _next_cycle_target(self, now: datetime) -> datetime:
        """Cycle targets land on HH:MM:10 boundaries every SR_POLL_SECONDS."""
        if self._last_cycle_start is None:
            return now
        interval = max(5, self.config.sr_poll_seconds)
        min_gap = max(5, interval // 2)
        last = self._last_cycle_start.timestamp()
        target_epoch = last + min_gap
        target_epoch = (
            (target_epoch // interval) * interval + CYCLE_OFFSET_SECONDS
        )
        while target_epoch <= now.timestamp():
            target_epoch += interval
        return datetime.fromtimestamp(target_epoch, tz=now.tzinfo)

    async def _run_cycle(self) -> None:
        cycle_start = _now_ist()
        self._last_cycle_start = cycle_start
        self._cycle_number += 1
        cycle_num = self._cycle_number

        pairs = list(self._subscribed)
        if not pairs:
            logger.debug("SR cycle %d: no subscriptions", cycle_num)
            return

        per_instrument: dict[str, list[str]] = {}
        for key, cfg in pairs:
            per_instrument.setdefault(key, []).append(cfg)

        cycle_timeout = max(5.0, self.config.sr_poll_seconds - 5.0)
        coros = [
            self._poll_instrument(key, cfgs)
            for key, cfgs in per_instrument.items()
        ]
        try:
            results = await asyncio.wait_for(
                asyncio.gather(*coros, return_exceptions=True),
                timeout=cycle_timeout,
            )
        except asyncio.TimeoutError:
            logger.warning("SR cycle %d exceeded %.1fs", cycle_num, cycle_timeout)
            results = [None] * len(per_instrument)

        emit_heartbeat = (
            self.config.sr_heartbeat_cycles > 0
            and cycle_num % self.config.sr_heartbeat_cycles == 0
        )
        duration = (_now_ist() - cycle_start).total_seconds()

        for (key, _cfgs), result in zip(per_instrument.items(), results):
            if result is None or isinstance(result, BaseException):
                continue
            for cfg, payload in result.get("emissions", []):
                await self.broadcaster.broadcast(
                    payload, [self._channel(key, cfg)]
                )
            if emit_heartbeat:
                for cfg, payload in result.get("heartbeats", []):
                    await self.broadcaster.broadcast(
                        payload, [self._channel(key, cfg)]
                    )

        logger.info(
            "SR cycle %d complete at %s duration=%.2fs instruments=%d heartbeat=%s",
            cycle_num,
            cycle_start.strftime("%H:%M:%S"),
            duration,
            len(per_instrument),
            "yes" if emit_heartbeat else "no",
            extra={"cycle_complete": True, "cycle_number": cycle_num},
        )

    async def _poll_instrument(
        self, instrument_key: str, configs: list[str]
    ) -> dict[str, Any]:
        runtime = self._runtime.get(instrument_key)
        if runtime is None:
            return {"emissions": [], "heartbeats": []}

        try:
            async with self._semaphore:
                intraday = await asyncio.wait_for(
                    asyncio.to_thread(self.upstox.intraday_1m, instrument_key),
                    timeout=self.config.sr_fetch_timeout_seconds,
                )
        except (asyncio.TimeoutError, UpstoxError) as exc:
            logger.warning("SR intraday fetch failed for %s: %s", instrument_key, exc)
            return {"emissions": [], "heartbeats": []}

        if not intraday:
            return {"emissions": [], "heartbeats": []}

        merged = self._merge_candles(self._history(instrument_key), intraday)
        if not merged:
            return {"emissions": [], "heartbeats": []}
        self._set_history(instrument_key, merged)

        emissions: list[tuple[str, dict]] = []
        heartbeats: list[tuple[str, dict]] = []
        meta = runtime["meta"]
        today_ist = _now_ist().date()

        for cfg in configs:
            try:
                result = compute_sr(
                    merged, cfg, max_per_side=self.config.sr_max_levels_per_side
                )
            except SrCalculationError as exc:
                logger.debug(
                    "SR calc failed for %s / %s: %s", instrument_key, cfg, exc
                )
                continue

            # Today-only projection — this is what clients actually see.
            filtered = self._today_only_result(result, today_ist)

            slot = runtime["configs"].setdefault(
                cfg, {"last": None, "last_filtered": None, "candle_count": 0}
            )
            # Change detection runs on the FILTERED payload so historical
            # pivots appearing/disappearing do not force redundant updates.
            changed = not _levels_equal(slot.get("last_filtered"), filtered)

            slot["last"] = result            # full result kept in memory
            slot["last_filtered"] = filtered # today-only view for change detection
            slot["candle_count"] = len(merged)

            try:
                # Persist the full result — snapshot() re-applies the
                # today-only filter at read time so restarts stay consistent.
                self.state_store.save(instrument_key, meta, cfg, result)
            except Exception:  # noqa: BLE001
                logger.exception("SR persist failed for %s / %s", instrument_key, cfg)

            if changed:
                emissions.append(
                    (cfg, self._build_update_event(instrument_key, meta, filtered))
                )
            heartbeats.append(
                (cfg, self._build_heartbeat(instrument_key, meta, filtered))
            )

        return {"emissions": emissions, "heartbeats": heartbeats}

    # ------------------------------------------------------------------
    # Snapshot / status
    # ------------------------------------------------------------------
    def snapshot(self, instrument_key: str, config: str) -> dict[str, Any] | None:
        try:
            canonical_cfg = config_slug(config)
        except SrCalculationError:
            return None
        canonical_key = self.canonical_key(instrument_key)
        runtime = self._runtime.get(canonical_key)
        if not runtime:
            return None
        cfg = runtime["configs"].get(canonical_cfg)
        if not cfg or not cfg.get("last"):
            return None

        today_ist = _now_ist().date()
        filtered = self._today_only_result(cfg["last"], today_ist)

        return {
            "instrument_key": canonical_key,
            "meta": runtime["meta"],
            "config": canonical_cfg,
            "cycle_number": self._cycle_number,
            "cycle_time": (
                self._last_cycle_start.isoformat()
                if self._last_cycle_start else None
            ),
            # ---- Initial-feed markers ----
            "feed_status": "connected",
            "feed_message": "Feed connected successfully",
            "scope_note": "For further reference, only today's SR levels are used",
            # ---- Today-only data (includes ``levels`` and ``sr_level_count``)
            **filtered,
        }

    def status(self) -> dict[str, Any]:
        by_instrument: dict[str, list[str]] = {}
        for key, cfg in sorted(self._subscribed):
            by_instrument.setdefault(key, []).append(cfg)
        return {
            "cycle_number": self._cycle_number,
            "cycle_time": (
                self._last_cycle_start.isoformat()
                if self._last_cycle_start else None
            ),
            "instrument_count": len(by_instrument),
            "subscription_count": len(self._subscribed),
            "subscriptions": by_instrument,
            "channels": self.broadcaster.channel_counts(),
            "data_scope": "today_only",
            "scope_note": "For further reference, only today's SR levels are used",
        }

    def subscribed_pairs(self) -> list[tuple[str, str]]:
        return sorted(self._subscribed)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _channel(self, instrument_key: str, config: str) -> str:
        return f"sr:{instrument_key}:{config_slug(config)}"

    def _resolve_meta(self, instrument_key: str) -> dict[str, Any]:
        meta = self.instruments.get(instrument_key)
        if meta is not None:
            return meta
        synthetic = INDEX_METADATA.get(instrument_key)
        if synthetic is None:
            raise SrFeedError(
                f"Instrument {instrument_key} is not present in all_instruments.json"
            )
        try:
            return self.instruments.upsert(instrument_key, synthetic)
        except InstrumentStoreError:
            return dict(synthetic)

    def _history(self, instrument_key: str) -> list[dict]:
        return (self._runtime.get(instrument_key) or {}).get("_history") or []

    def _set_history(self, instrument_key: str, candles: list[dict]) -> None:
        runtime = self._runtime.get(instrument_key)
        if runtime is None:
            return
        limit = max(self.config.sr_history_bars, 500)
        runtime["_history"] = candles[-limit:]

    def _build_update_event(
        self, instrument_key: str, meta: dict, filtered_result: dict
    ) -> dict[str, Any]:
        return {
            "event": "sr_update",
            "instrument_key": instrument_key,
            "trading_symbol": meta.get("trading_symbol"),
            "config": filtered_result.get("config"),
            "current_price": filtered_result.get("current_price"),
            "last_ts": filtered_result.get("last_ts"),
            "resistances": filtered_result.get("resistances"),
            "supports": filtered_result.get("supports"),
            # Full today-pivot view (same shape as a manual export).
            "levels": filtered_result.get("levels"),
            "sr_level_count": filtered_result.get("sr_level_count"),
            "data_scope": "today_only",
            "today_ist_date": filtered_result.get("today_ist_date"),
        }

    def _build_heartbeat(
        self, instrument_key: str, meta: dict, filtered_result: dict
    ) -> dict[str, Any]:
        res = filtered_result.get("resistances") or []
        sup = filtered_result.get("supports") or []
        return {
            "event": "heartbeat",
            "instrument_key": instrument_key,
            "trading_symbol": meta.get("trading_symbol"),
            "config": filtered_result.get("config"),
            "cycle": self._cycle_number,
            "cycle_time": (
                self._last_cycle_start.isoformat()
                if self._last_cycle_start else None
            ),
            "current_price": filtered_result.get("current_price"),
            "last_ts": filtered_result.get("last_ts"),
            "nearest_resistance": res[0]["price"] if res else None,
            "nearest_support": sup[0]["price"] if sup else None,
            "sr_level_count": filtered_result.get("sr_level_count"),
            "status": "ok",
            "data_scope": "today_only",
        }

    @staticmethod
    def _merge_candles(*collections: list[dict]) -> list[dict]:
        merged: dict[str, dict] = {}
        for collection in collections:
            for candle in collection or []:
                merged[str(candle.get("timestamp"))] = candle
        return sorted(merged.values(), key=lambda c: str(c.get("timestamp")))