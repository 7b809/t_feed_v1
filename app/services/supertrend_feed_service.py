"""Supertrend backfill, today-only snapshots and confirmed live signal broadcaster."""
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

class SupertrendFeedService:
    def __init__(self, config: Settings, broadcaster: FeedBroadcaster,
                 instruments: InstrumentStore, upstox: UpstoxService,
                 state_store: SupertrendStateStore) -> None:
        self.config, self.broadcaster, self.instruments = config, broadcaster, instruments
        self.upstox, self.state_store = upstox, state_store
        self._lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(max(1, config.supertrend_max_workers))
        self._runtime: dict[str, dict[str, Any]] = {}
        self._subscribed: set[str] = set()
        self._live_task: asyncio.Task | None = None
        self._last_cycle_start: datetime | None = None
        self._cycle_number = 0

    def canonical_key(self, raw: str) -> str:
        if raw in INDEX_METADATA:
            return raw
        lowered = (raw or "").strip().lower()
        for key, meta in INDEX_METADATA.items():
            if lowered in {(meta.get("folder") or "").lower(),
                           (meta.get("canonical") or "").lower(), key.lower()}:
                return key
        return raw

    def _resolve_meta(self, key: str) -> dict[str, Any]:
        meta = self.instruments.get(key)
        if meta is not None:
            return meta
        synthetic = INDEX_METADATA.get(key)
        if synthetic is None:
            raise SupertrendFeedError(f"Instrument {key} is not present in all_instruments.json")
        try:
            return self.instruments.upsert(key, synthetic)
        except InstrumentStoreError:
            return dict(synthetic)

    @staticmethod
    def _merge_candles(*collections: list[dict]) -> list[dict]:
        merged = {}
        for collection in collections:
            for candle in collection or []:
                if candle.get("timestamp") is not None:
                    merged[str(candle["timestamp"])] = candle
        return sorted(merged.values(), key=lambda c: str(c.get("timestamp")))

    @staticmethod
    def _confirmed_candles(candles: list[dict], now: datetime) -> list[dict]:
        # Upstox candle timestamps represent the candle open; only closed minutes qualify.
        result = []
        for candle in candles:
            ts = parse_ts(candle.get("timestamp"))
            if ts is None:
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=IST)
            if ts.astimezone(IST) + timedelta(minutes=1) <= now:
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
            try:
                await self.subscribe(key, seed_only=True)
            except Exception:
                logger.exception("Supertrend bootstrap subscription failed for %s", key)
        await self.warmup_all()
        await self.start_live()

    async def subscribe(self, instrument_key: str, *, seed_only: bool = False) -> dict[str, Any]:
        key = self.canonical_key(instrument_key)
        meta = self._resolve_meta(key)
        async with self._lock:
            already = key in self._subscribed
            runtime = self._runtime.get(key)
            if runtime is None:
                saved = self.state_store.load(key, meta) or {}
                saved_signals = saved.get("signals") or []
                if saved.get("today_ist_date") != _now_ist().date().isoformat():
                    saved_signals = []
                runtime = {"meta": meta, "history": [], "signals": saved_signals,
                           "state": saved.get("state") or {}}
                self._runtime[key] = runtime
            else:
                runtime["meta"] = meta
            self._subscribed.add(key)
        if not seed_only and not already:
            await self._warm_up(key)
        return {"instrument_key": key,
                "status": "already_subscribed" if already else "subscribed"}

    async def unsubscribe(self, instrument_key: str) -> bool:
        key = self.canonical_key(instrument_key)
        async with self._lock:
            self._subscribed.discard(key)
            self._runtime.pop(key, None)
        return True

    async def warmup_all(self) -> None:
        await asyncio.gather(*(self._warm_up(k) for k in list(self._subscribed)),
                             return_exceptions=True)

    async def _fetch_candles(self, key: str) -> list[dict]:
        async with self._semaphore:
            historical = await asyncio.wait_for(
                asyncio.to_thread(self.upstox.historical_window, key,
                                  self.config.supertrend_history_days),
                timeout=self.config.supertrend_fetch_timeout_seconds * 4)
            intraday = await asyncio.wait_for(
                asyncio.to_thread(self.upstox.intraday_1m, key),
                timeout=self.config.supertrend_fetch_timeout_seconds)
        return self._confirmed_candles(self._merge_candles(historical, intraday), _now_ist())

    async def _warm_up(self, key: str) -> None:
        runtime = self._runtime.get(key)
        if runtime is None:
            return
        try:
            candles = await self._fetch_candles(key)
            if not candles:
                logger.info("Supertrend warmup: no confirmed candles for %s", key)
                return
            signals, state = calculate_supertrend(
                candles, self.config.supertrend_period,
                self.config.supertrend_multiplier, self.config.supertrend_change_atr)
        except (asyncio.TimeoutError, UpstoxError, ValueError) as exc:
            logger.warning("Supertrend warmup failed for %s: %s", key, exc)
            return
        today = _now_ist().date()
        runtime["history"] = candles
        runtime["signals"] = self._today_signals(signals, today)
        runtime["state"] = state
        self._persist(key, runtime, today)
        logger.info("Supertrend warmup complete %s candles=%d today_signals=%d trend=%s",
                    key, len(candles), len(runtime["signals"]), state.get("trend"))

    def _persist(self, key: str, runtime: dict, today) -> None:
        self.state_store.save(key, runtime["meta"], {
            "indicator": "supertrend",
            "parameters": {"atr_period": self.config.supertrend_period,
                           "atr_multiplier": self.config.supertrend_multiplier,
                           "change_atr": self.config.supertrend_change_atr,
                           "source": "hl2"},
            "today_ist_date": today.isoformat(), "data_scope": "today_only",
            "signals": runtime["signals"], "state": runtime["state"],
            "signal_count": len(runtime["signals"]),
        })

    async def start_live(self) -> None:
        if self._live_task and not self._live_task.done():
            return
        self._live_task = asyncio.create_task(self._live_loop(), name="supertrend-live-loop")
        logger.info("Supertrend live loop started interval=%ss", self.config.supertrend_poll_seconds)

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
                interval = max(5, self.config.supertrend_poll_seconds)
                target_epoch = (int(now.timestamp() // interval) + 1) * interval + CYCLE_OFFSET_SECONDS
                target = datetime.fromtimestamp(target_epoch, tz=IST)
                await asyncio.sleep(max(0.05, (target-now).total_seconds()))
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
        keys = list(self._subscribed)
        results = await asyncio.gather(*(self._poll_instrument(k) for k in keys),
                                        return_exceptions=True)
        for key, result in zip(keys, results):
            if isinstance(result, BaseException):
                logger.warning("Supertrend cycle failed for %s: %s", key, result)
                continue
            for payload in result.get("signals", []):
                await self.broadcaster.broadcast(payload, [self._channel(key)])
            await self.broadcaster.broadcast(result["heartbeat"], [self._channel(key)])
        logger.info("Supertrend cycle %d complete instruments=%d duration=%.2fs",
                    self._cycle_number, len(keys), (_now_ist()-started).total_seconds())

    async def _poll_instrument(self, key: str) -> dict[str, Any]:
        runtime = self._runtime.get(key)
        if runtime is None:
            return {"signals": [], "heartbeat": {}}
        async with self._semaphore:
            intraday = await asyncio.wait_for(
                asyncio.to_thread(self.upstox.intraday_1m, key),
                timeout=self.config.supertrend_fetch_timeout_seconds)
        candles = self._confirmed_candles(
            self._merge_candles(runtime.get("history") or [], intraday), _now_ist())
        if not candles:
            return {"signals": [], "heartbeat": self._heartbeat(key, runtime)}
        calculated, state = calculate_supertrend(
            candles, self.config.supertrend_period,
            self.config.supertrend_multiplier, self.config.supertrend_change_atr)
        today = _now_ist().date()
        current = self._today_signals(calculated, today)
        known = {signal_identity(s) for s in runtime.get("signals", [])}
        emissions = [self._signal_event(key, runtime["meta"], s) for s in current
                     if signal_identity(s) not in known]
        runtime["history"], runtime["signals"], runtime["state"] = candles, current, state
        self._persist(key, runtime, today)
        return {"signals": emissions, "heartbeat": self._heartbeat(key, runtime)}

    def _signal_event(self, key: str, meta: dict, signal: dict) -> dict:
        return {"event": "supertrend_signal", "instrument_key": key,
                "trading_symbol": meta.get("trading_symbol"), **signal,
                "confirmed": True, "data_scope": "today_only",
                "today_ist_date": _now_ist().date().isoformat()}

    def _heartbeat(self, key: str, runtime: dict) -> dict:
        state = runtime.get("state") or {}
        return {"event": "heartbeat", "indicator": "supertrend",
                "instrument_key": key,
                "trading_symbol": runtime["meta"].get("trading_symbol"),
                "trend": "up" if state.get("trend", 1) == 1 else "down",
                "supertrend": state.get("supertrend"), "atr": state.get("atr"),
                "last_ts": state.get("last_ts"), "last_close": state.get("last_close"),
                "signal_count_today": len(runtime.get("signals") or []),
                "cycle": self._cycle_number, "data_scope": "today_only",
                "today_ist_date": _now_ist().date().isoformat()}

    def snapshot(self, instrument_key: str) -> dict[str, Any] | None:
        key = self.canonical_key(instrument_key)
        runtime = self._runtime.get(key)
        if runtime is None:
            return None
        today = _now_ist().date()
        signals = self._today_signals(runtime.get("signals") or [], today)
        state = runtime.get("state") or {}
        return {"instrument_key": key, "meta": runtime["meta"],
                "indicator": "supertrend",
                "parameters": {"atr_period": self.config.supertrend_period,
                               "atr_multiplier": self.config.supertrend_multiplier,
                               "change_atr": self.config.supertrend_change_atr,
                               "source": "hl2"},
                "trend": "up" if state.get("trend", 1) == 1 else "down",
                "supertrend": state.get("supertrend"), "atr": state.get("atr"),
                "last_ts": state.get("last_ts"), "last_close": state.get("last_close"),
                "signals": signals, "signal_count": len(signals),
                "data_scope": "today_only", "today_ist_date": today.isoformat(),
                "feed_status": "connected",
                "feed_message": "Supertrend feed connected successfully"}

    def status(self) -> dict[str, Any]:
        return {"enabled": self.config.supertrend_enabled,
                "instrument_count": len(self._subscribed),
                "subscriptions": sorted(self._subscribed),
                "cycle_number": self._cycle_number,
                "cycle_time": self._last_cycle_start.isoformat() if self._last_cycle_start else None,
                "channels": self.broadcaster.channel_counts(),
                "parameters": {"atr_period": self.config.supertrend_period,
                               "atr_multiplier": self.config.supertrend_multiplier,
                               "change_atr": self.config.supertrend_change_atr}}

    @staticmethod
    def _channel(key: str) -> str:
        return f"supertrend:{key}"
