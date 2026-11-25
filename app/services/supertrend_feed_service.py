"""Multi-interval Supertrend backfill, persistence and confirmed live broadcaster."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import INDEX_METADATA, Settings
from app.services.ema_calculator import parse_ts
from app.services.feed_broadcaster import FeedBroadcaster
from app.services.instrument_store import InstrumentStore, InstrumentStoreError
from app.services.supertrend_calculator import calculate_supertrend, signal_identity
from app.services.supertrend_state_store import SupertrendStateStore
from app.services.upstox_client_service import UpstoxError, UpstoxService

logger = logging.getLogger(__name__)
IST = timezone(timedelta(hours=5, minutes=30))
CYCLE_OFFSET_SECONDS = 10


class SupertrendFeedError(RuntimeError):
    pass


def _now_ist() -> datetime:
    return datetime.now(IST)


def _normalize_interval(interval: str | int) -> str:
    raw = str(interval).strip().lower()
    raw = raw[:-1] if raw.endswith("m") else raw
    if not raw.isdigit() or int(raw) not in {1, 2, 3, 5, 10, 15, 30}:
        raise SupertrendFeedError(
            f"Unsupported interval {interval!r}; use 1m, 2m, 3m, 5m, 10m, 15m or 30m"
        )
    return f"{int(raw)}m"


class SupertrendFeedService:
    def __init__(
        self, config: Settings, broadcaster: FeedBroadcaster,
        instruments: InstrumentStore, upstox: UpstoxService,
        state_store: SupertrendStateStore,
    ) -> None:
        self.config, self.broadcaster, self.instruments = config, broadcaster, instruments
        self.upstox, self.state_store = upstox, state_store
        self._lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(max(1, config.supertrend_max_workers))
        # Runtime and subscriptions are isolated by (instrument_key, interval).
        self._runtime: dict[tuple[str, str], dict[str, Any]] = {}
        self._subscribed: set[tuple[str, str]] = set()
        self._live_task: asyncio.Task | None = None
        self._last_cycle_start: datetime | None = None
        self._cycle_number = 0

    @property
    def intervals(self) -> list[str]:
        return self.config.supertrend_intervals_list

    def canonical_key(self, raw: str) -> str:
        if raw in INDEX_METADATA:
            return raw
        lowered = (raw or "").strip().lower()
        for key, meta in INDEX_METADATA.items():
            if lowered in {
                (meta.get("folder") or "").lower(),
                (meta.get("canonical") or "").lower(), key.lower(),
            }:
                return key
        return raw

    def _resolve_meta(self, key: str) -> dict[str, Any]:
        meta = self.instruments.get(key)
        if meta is not None:
            return meta
        synthetic = INDEX_METADATA.get(key)
        if synthetic is None:
            raise SupertrendFeedError(
                f"Instrument {key} is not present in all_instruments.json"
            )
        try:
            return self.instruments.upsert(key, synthetic)
        except InstrumentStoreError:
            return dict(synthetic)

    @staticmethod
    def _merge_candles(*collections: list[dict]) -> list[dict]:
        merged: dict[str, dict] = {}
        for collection in collections:
            for candle in collection or []:
                if candle.get("timestamp") is not None:
                    merged[str(candle["timestamp"])] = candle
        return sorted(merged.values(), key=lambda c: str(c.get("timestamp")))

    @staticmethod
    def _confirmed_candles(
        candles: list[dict], now: datetime, interval_minutes: int
    ) -> list[dict]:
        result = []
        for candle in candles:
            ts = parse_ts(candle.get("timestamp"))
            if ts is None:
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=IST)
            if ts.astimezone(IST) + timedelta(minutes=interval_minutes) <= now:
                result.append(candle)
        return result

    @staticmethod
    def _today_signals(signals: list[dict], today) -> list[dict]:
        result = []
        for item in signals:
            ts = parse_ts(item.get("confirmed_time"))
            if ts is None:
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=IST)
            if ts.astimezone(IST).date() == today:
                result.append({k: item[k] for k in (
                    "signal", "price", "confirmed_time", "confirmed_time_price"
                )})
        return result

    async def bootstrap(self) -> None:
        if not self.config.supertrend_enabled:
            logger.info("Supertrend disabled (SUPERTREND_ENABLED=false)")
            return
        for key in self.config.index_instrument_keys:
            for interval in self.intervals:
                try:
                    await self.subscribe(key, interval, seed_only=True)
                except Exception:
                    logger.exception(
                        "Supertrend bootstrap subscription failed for %s interval=%s",
                        key, interval,
                    )
        await self.warmup_all()
        await self.start_live()

    async def subscribe(
        self, instrument_key: str, interval: str | int = "1m", *,
        seed_only: bool = False,
    ) -> dict[str, Any]:
        key = self.canonical_key(instrument_key)
        interval = _normalize_interval(interval)
        meta = self._resolve_meta(key)
        identity = (key, interval)
        async with self._lock:
            already = identity in self._subscribed
            runtime = self._runtime.get(identity)
            if runtime is None:
                saved = self.state_store.load(key, meta, interval) or {}
                saved_signals = saved.get("signals") or []
                if saved.get("today_ist_date") != _now_ist().date().isoformat():
                    saved_signals = []
                runtime = {
                    "meta": meta, "interval": interval, "history": [],
                    "signals": saved_signals, "state": saved.get("state") or {},
                }
                self._runtime[identity] = runtime
            else:
                runtime["meta"] = meta
            self._subscribed.add(identity)
        if not seed_only and not already:
            await self._warm_up(key, interval)
        return {
            "instrument_key": key, "interval": interval,
            "status": "already_subscribed" if already else "subscribed",
            "channel": self._channel(key, interval),
        }

    async def unsubscribe(
        self, instrument_key: str, interval: str | int | None = None
    ) -> bool:
        key = self.canonical_key(instrument_key)
        async with self._lock:
            identities = (
                [(key, _normalize_interval(interval))]
                if interval is not None
                else [item for item in self._subscribed if item[0] == key]
            )
            for identity in identities:
                self._subscribed.discard(identity)
                self._runtime.pop(identity, None)
        return True

    async def warmup_all(self) -> None:
        await asyncio.gather(
            *(self._warm_up(key, interval) for key, interval in list(self._subscribed)),
            return_exceptions=True,
        )

    async def _fetch_candles(self, key: str, interval: str) -> list[dict]:
        minutes = int(interval[:-1])
        async with self._semaphore:
            historical = await asyncio.wait_for(
                asyncio.to_thread(
                    self.upstox.historical_window_minutes,
                    key, self.config.supertrend_history_days, minutes,
                ),
                timeout=self.config.supertrend_fetch_timeout_seconds * 4,
            )
            intraday = await asyncio.wait_for(
                asyncio.to_thread(self.upstox.intraday_minutes, key, minutes),
                timeout=self.config.supertrend_fetch_timeout_seconds,
            )
        return self._confirmed_candles(
            self._merge_candles(historical, intraday), _now_ist(), minutes
        )

    async def _warm_up(self, key: str, interval: str) -> None:
        runtime = self._runtime.get((key, interval))
        if runtime is None:
            return
        try:
            candles = await self._fetch_candles(key, interval)
            if not candles:
                logger.info("Supertrend warmup: no confirmed candles for %s interval=%s",
                            key, interval)
                return
            signals, state = calculate_supertrend(
                candles, self.config.supertrend_period,
                self.config.supertrend_multiplier, self.config.supertrend_change_atr,
            )
        except (asyncio.TimeoutError, UpstoxError, ValueError) as exc:
            logger.warning("Supertrend warmup failed for %s interval=%s: %s",
                           key, interval, exc)
            return
        today = _now_ist().date()
        runtime["history"] = candles
        runtime["signals"] = self._today_signals(signals, today)
        runtime["state"] = state
        self._persist(key, interval, runtime, today)
        logger.info(
            "Supertrend warmup complete %s interval=%s candles=%d today_signals=%d trend=%s",
            key, interval, len(candles), len(runtime["signals"]), state.get("trend"),
        )

    def _persist(self, key: str, interval: str, runtime: dict, today) -> None:
        self.state_store.save(key, runtime["meta"], {
            "indicator": "supertrend", "interval": interval,
            "parameters": {
                "atr_period": self.config.supertrend_period,
                "atr_multiplier": self.config.supertrend_multiplier,
                "change_atr": self.config.supertrend_change_atr,
                "source": "hl2",
            },
            "today_ist_date": today.isoformat(), "data_scope": "today_only",
            "signals": runtime["signals"], "state": runtime["state"],
            "signal_count": len(runtime["signals"]),
        }, interval)

    async def start_live(self) -> None:
        if self._live_task and not self._live_task.done():
            return
        self._live_task = asyncio.create_task(
            self._live_loop(), name="supertrend-multi-interval-live-loop"
        )
        logger.info("Supertrend live loop started intervals=%s poll=%ss",
                    self.intervals, self.config.supertrend_poll_seconds)

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
                if self._last_cycle_start is None:
                    target = now
                else:
                    poll = max(5, self.config.supertrend_poll_seconds)
                    min_gap = max(5, poll // 2)
                    target_epoch = self._last_cycle_start.timestamp() + min_gap
                    target_epoch = (target_epoch // poll) * poll + CYCLE_OFFSET_SECONDS
                    while target_epoch <= now.timestamp():
                        target_epoch += poll
                    target = datetime.fromtimestamp(target_epoch, tz=IST)
                await asyncio.sleep(max(0.0, (target - now).total_seconds()))
                try:
                    await self._run_cycle()
                except Exception:
                    logger.exception("Supertrend live cycle failed")
        except asyncio.CancelledError:
            logger.info("Supertrend live loop cancelled")
            raise

    async def _run_cycle(self) -> None:
        started = _now_ist()
        self._last_cycle_start = started
        self._cycle_number += 1
        identities = list(self._subscribed)
        results = await asyncio.gather(
            *(self._poll_instrument(key, interval) for key, interval in identities),
            return_exceptions=True,
        )
        for (key, interval), result in zip(identities, results):
            if isinstance(result, BaseException):
                logger.warning("Supertrend cycle failed for %s interval=%s: %s",
                               key, interval, result)
                continue
            channel = self._channel(key, interval)
            for payload in result.get("signals", []):
                await self.broadcaster.broadcast(payload, [channel])
            await self.broadcaster.broadcast(result["heartbeat"], [channel])
        logger.info(
            "Supertrend cycle %d complete subscriptions=%d intervals=%s duration=%.2fs",
            self._cycle_number, len(identities), self.intervals,
            (_now_ist() - started).total_seconds(),
        )

    async def _poll_instrument(self, key: str, interval: str) -> dict[str, Any]:
        runtime = self._runtime.get((key, interval))
        if runtime is None:
            return {"signals": [], "heartbeat": {}}
        minutes = int(interval[:-1])
        async with self._semaphore:
            intraday = await asyncio.wait_for(
                asyncio.to_thread(self.upstox.intraday_minutes, key, minutes),
                timeout=self.config.supertrend_fetch_timeout_seconds,
            )
        candles = self._confirmed_candles(
            self._merge_candles(runtime.get("history") or [], intraday),
            _now_ist(), minutes,
        )
        if not candles:
            return {"signals": [], "heartbeat": self._heartbeat(key, interval, runtime)}
        calculated, state = calculate_supertrend(
            candles, self.config.supertrend_period,
            self.config.supertrend_multiplier, self.config.supertrend_change_atr,
        )
        today = _now_ist().date()
        current = self._today_signals(calculated, today)
        known = {signal_identity(signal) for signal in runtime.get("signals", [])}
        emissions = [
            self._signal_event(key, interval, runtime["meta"], signal)
            for signal in current if signal_identity(signal) not in known
        ]
        runtime["history"], runtime["signals"], runtime["state"] = candles, current, state
        self._persist(key, interval, runtime, today)
        return {"signals": emissions, "heartbeat": self._heartbeat(key, interval, runtime)}

    def _signal_event(self, key: str, interval: str, meta: dict, signal: dict) -> dict:
        return {
            "event": "supertrend_signal", "indicator": "supertrend",
            "instrument_key": key, "interval": interval,
            "trading_symbol": meta.get("trading_symbol"), **signal,
            "confirmed": True, "data_scope": "today_only",
            "today_ist_date": _now_ist().date().isoformat(),
        }

    def _heartbeat(self, key: str, interval: str, runtime: dict) -> dict:
        state = runtime.get("state") or {}
        return {
            "event": "heartbeat", "indicator": "supertrend",
            "instrument_key": key, "interval": interval,
            "trading_symbol": runtime["meta"].get("trading_symbol"),
            "trend": "up" if state.get("trend", 1) == 1 else "down",
            "supertrend": state.get("supertrend"), "atr": state.get("atr"),
            "last_ts": state.get("last_ts"), "last_close": state.get("last_close"),
            "signal_count_today": len(runtime.get("signals") or []),
            "cycle": self._cycle_number, "data_scope": "today_only",
            "today_ist_date": _now_ist().date().isoformat(),
        }

    def snapshot(
        self, instrument_key: str, interval: str | int = "1m"
    ) -> dict[str, Any] | None:
        key = self.canonical_key(instrument_key)
        interval = _normalize_interval(interval)
        runtime = self._runtime.get((key, interval))
        if runtime is None:
            return None
        today = _now_ist().date()
        signals = self._today_signals(runtime.get("signals") or [], today)
        state = runtime.get("state") or {}
        return {
            "instrument_key": key, "meta": runtime["meta"],
            "indicator": "supertrend", "interval": interval,
            "parameters": {
                "atr_period": self.config.supertrend_period,
                "atr_multiplier": self.config.supertrend_multiplier,
                "change_atr": self.config.supertrend_change_atr, "source": "hl2",
            },
            "trend": "up" if state.get("trend", 1) == 1 else "down",
            "supertrend": state.get("supertrend"), "atr": state.get("atr"),
            "last_ts": state.get("last_ts"), "last_close": state.get("last_close"),
            "signals": signals, "signal_count": len(signals),
            "data_scope": "today_only", "today_ist_date": today.isoformat(),
            "feed_status": "connected",
            "feed_message": "Supertrend feed connected successfully",
        }

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.config.supertrend_enabled,
            "intervals": self.intervals,
            "subscription_count": len(self._subscribed),
            "instrument_count": len({key for key, _ in self._subscribed}),
            "subscriptions": [
                {"instrument_key": key, "interval": interval}
                for key, interval in sorted(self._subscribed)
            ],
            "cycle_number": self._cycle_number,
            "cycle_time": self._last_cycle_start.isoformat() if self._last_cycle_start else None,
            "channels": self.broadcaster.channel_counts(),
            "parameters": {
                "atr_period": self.config.supertrend_period,
                "atr_multiplier": self.config.supertrend_multiplier,
                "change_atr": self.config.supertrend_change_atr,
            },
        }

    @staticmethod
    def _channel(key: str, interval: str = "1m") -> str:
        # Preserve the exact legacy channel name for the 1-minute feed.
        return f"supertrend:{key}" if interval == "1m" else f"supertrend:{key}:{interval}"
