"""Fifth job: live intraday EMA cross detection during market hours.

Every minute — at ``minute boundary + LIVE_EMA_TICK_OFFSET_SECONDS`` — while
inside the configured market window, this job:

1. Fetches today's intraday candles for every subscribed instrument.
2. Merges them with the existing ``historical.json`` (older days + today's
   candles already captured by job 3).
3. Recomputes the fast/slow EMA series.
4. Checks the most recently completed candle for a bullish or bearish cross.
5. Appends the cross to ``ema_crosses.json`` only if it is new.

The job runs as a long-lived ``asyncio.Task`` started from the FastAPI
lifespan. Every instrument is processed on a worker thread; the number of
in-flight Upstox calls is bounded by ``settings.history_max_concurrency``.

Index instruments (``NSE_INDEX|Nifty 50`` etc.) are processed the same way;
their files live under ``data/index/<name>/`` thanks to
``instrument_paths``.

Time-of-day logic uses ``settings.market_timezone`` (default Asia/Kolkata).

Logging
-------
Per tick, exactly three kinds of lines are emitted (subject to the gating
rules below):

* **Start** — ``Live EMA tick #N starting; total=T test_mode=...``
* **Progress** — ``Live EMA tick #N progress; processed=X/T`` emitted
  roughly 10 times per tick (every ``max(1, T // 10)`` instruments), plus
  once at completion.
* **Complete** — ``Live EMA tick #N complete; processed=X/T new_crosses=...
  ... elapsed=E.EEs next_tick_in=R.RRs`` where ``next_tick_in`` is the
  number of seconds until the next scheduled tick.

A new cross is still reported individually:
``Live EMA cross: <instrument> <type> ts=... close=... fast=... slow=...``
and errors from individual instruments are still logged via
``logger.exception``.

Gating
------
* ``INSTRUMENT_ERRORS_ONLY=true`` suppresses the start/progress/complete
  lines. Cross-detection lines and errors are always emitted.
* ``EMA_TEST_MODE=true`` forces the verbose (start/progress/complete)
  lines on, regardless of ``INSTRUMENT_ERRORS_ONLY``, and ignores the
  market-hours window so the job ticks 24x7.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from app.core.config import settings
from app.core.logger import get_logger
from app.services.ema_utils import classify_cross, compute_ema, to_float
from app.services.instrument_paths import (
    build_candle_path,
    build_ema_cross_path,
    get_file_write_lock,
)
from app.services.upstox_fetcher import fetch_intraday

logger = get_logger(__name__)

def _parse_hhmm(value: str) -> dt_time:
    try:
        hh, mm = value.strip().split(":")
        return dt_time(int(hh), int(mm))
    except Exception:
        logger.warning("Invalid time value '%s'; falling back to 00:00", value)
        return dt_time(0, 0)

class LiveEmaCrossStore:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop_event: asyncio.Event | None = None
        self._tick_lock = asyncio.Lock()

        self.running = False
        self.started_at: str | None = None
        self.stopped_at: str | None = None
        self.last_tick_started_at: str | None = None
        self.last_tick_completed_at: str | None = None
        self.last_tick_error: str | None = None
        self.last_tick_instruments = 0
        self.last_tick_new_crosses = 0
        self.total_ticks = 0
        self.total_crosses_detected = 0

    # ------------------------------------------------------------------ #
    # Lifecycle                                                          #
    # ------------------------------------------------------------------ #
    async def start(
        self,
        instruments_provider: Callable[[], list[dict[str, Any]]],
    ) -> dict[str, Any]:
        if self._task is not None and not self._task.done():
            return {"started": False, "reason": "already-running"}
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(
            self._run_loop(instruments_provider),
            name="live-ema-cross",
        )
        self.running = True
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.stopped_at = None
        logger.info(
            "Live EMA cross job starting; tick_offset=%ds tz=%s window=%s-%s test_mode=%s",
            settings.live_ema_tick_offset_seconds,
            settings.market_timezone,
            settings.market_open_time,
            settings.market_close_time,
            settings.ema_test_mode,
        )
        return {"started": True}

    async def stop(self) -> dict[str, Any]:
        if self._task is None or self._task.done():
            self.running = False
            return {"stopped": False, "reason": "not-running"}
        assert self._stop_event is not None
        self._stop_event.set()
        try:
            await asyncio.wait_for(self._task, timeout=10)
        except asyncio.TimeoutError:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self.running = False
        self.stopped_at = datetime.now(timezone.utc).isoformat()
        logger.info("Live EMA cross job stopped")
        return {"stopped": True}

    # ------------------------------------------------------------------ #
    # Main loop                                                          #
    # ------------------------------------------------------------------ #
    async def _run_loop(
        self,
        instruments_provider: Callable[[], list[dict[str, Any]]],
    ) -> None:
        try:
            while not self._stop_event.is_set():
                now = self._now_market_tz()
                if not self._is_market_hours(now):
                    await self._wait(settings.live_ema_idle_sleep_seconds)
                    continue

                target = self._next_tick(now)
                wait_seconds = (target - now).total_seconds()
                if wait_seconds > 0:
                    await self._wait(wait_seconds)
                    if self._stop_event.is_set():
                        break

                try:
                    await self._tick(instruments_provider())
                except Exception:
                    self.last_tick_error = "tick failed"
                    logger.exception("Live EMA cross tick failed")
                    await self._wait(5)
        except asyncio.CancelledError:
            logger.info("Live EMA cross loop cancelled")
            raise
        finally:
            self.running = False

    def _now_market_tz(self) -> datetime:
        try:
            tz = ZoneInfo(settings.market_timezone)
        except Exception:
            tz = timezone.utc
        return datetime.now(tz)

    def _is_market_hours(self, now: datetime) -> bool:
        # In test mode the market window is ignored entirely so the tick
        # fires every minute, 24x7.
        if settings.ema_test_mode:
            return True

        if now.weekday() >= 5:
            return False
        open_t = _parse_hhmm(settings.market_open_time)
        close_t = _parse_hhmm(settings.market_close_time)
        return open_t <= now.time() <= close_t

    def _next_tick(self, now: datetime) -> datetime:
        offset = max(0, settings.live_ema_tick_offset_seconds)
        minute_start = now.replace(second=0, microsecond=0)
        candidate = minute_start + timedelta(seconds=offset)
        if candidate <= now:
            candidate = minute_start + timedelta(minutes=1, seconds=offset)
        return candidate

    async def _wait(self, seconds: float) -> None:
        if seconds <= 0 or self._stop_event is None:
            return
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    # ------------------------------------------------------------------ #
    # One tick                                                           #
    # ------------------------------------------------------------------ #
    async def _tick(self, instruments: list[dict[str, Any]]) -> None:
        async with self._tick_lock:
            valid = [i for i in instruments if isinstance(i, dict)]
            total = len(valid)

            self.total_ticks += 1
            tick_start = datetime.now(timezone.utc)
            self.last_tick_started_at = tick_start.isoformat()
            self.last_tick_error = None

            # Verbose = start/progress/complete lines are emitted.
            verbose = settings.ema_test_mode or not settings.instrument_errors_only

            if verbose:
                logger.info(
                    "Live EMA tick #%d starting; total=%d test_mode=%s",
                    self.total_ticks,
                    total,
                    settings.ema_test_mode,
                )

            concurrency = max(1, settings.history_max_concurrency)
            semaphore = asyncio.Semaphore(concurrency)

            instruments_checked = 0
            new_crosses = 0
            no_data = 0
            insufficient = 0
            warmup = 0
            no_cross = 0
            duplicate = 0
            failed = 0

            # Emit roughly 10 progress lines per tick.
            progress_interval = max(1, total // 10) if total else 1

            async def process(item: dict[str, Any]) -> None:
                nonlocal instruments_checked, new_crosses
                nonlocal no_data, insufficient, warmup, no_cross, duplicate, failed

                instrument_key = item.get("instrument_key")

                async with semaphore:
                    try:
                        status, event = await asyncio.to_thread(
                            self._process_tick, item
                        )
                    except Exception:
                        failed += 1
                        logger.exception(
                            "Live EMA tick failed; instrument=%s",
                            instrument_key,
                        )
                        instruments_checked += 1
                        if (
                            verbose
                            and instruments_checked % progress_interval == 0
                        ):
                            logger.info(
                                "Live EMA tick #%d progress; processed=%d/%d",
                                self.total_ticks,
                                instruments_checked,
                                total,
                            )
                        return

                    instruments_checked += 1

                    if status == "no-intraday":
                        no_data += 1
                    elif status == "insufficient-candles":
                        insufficient += 1
                    elif status == "ema-warmup":
                        warmup += 1
                    elif status == "no-cross":
                        no_cross += 1
                    elif status == "duplicate-cross":
                        duplicate += 1
                    elif status == "cross":
                        new_crosses += 1
                        assert event is not None
                        logger.info(
                            "Live EMA cross: %s %s ts=%s close=%s fast=%.4f slow=%.4f",
                            instrument_key,
                            event["type"],
                            event["timestamp"],
                            event.get("close"),
                            event["ema_fast"],
                            event["ema_slow"],
                        )

                    if verbose and instruments_checked % progress_interval == 0:
                        logger.info(
                            "Live EMA tick #%d progress; processed=%d/%d",
                            self.total_ticks,
                            instruments_checked,
                            total,
                        )

            await asyncio.gather(*(asyncio.create_task(process(i)) for i in valid))

            tick_end_utc = datetime.now(timezone.utc)
            self.last_tick_completed_at = tick_end_utc.isoformat()
            self.last_tick_instruments = instruments_checked
            self.last_tick_new_crosses = new_crosses
            self.total_crosses_detected += new_crosses

            if verbose:
                elapsed = (tick_end_utc - tick_start).total_seconds()

                # How long until the next scheduled tick (same clock the
                # run loop uses). Computed in market tz, then converted
                # to a delta in seconds.
                now_market = self._now_market_tz()
                next_tick = self._next_tick(now_market)
                next_tick_in = (next_tick - now_market).total_seconds()

                logger.info(
                    "Live EMA tick #%d complete; processed=%d/%d new_crosses=%d "
                    "no_data=%d insufficient=%d warmup=%d no_cross=%d "
                    "duplicate=%d failed=%d elapsed=%.2fs next_tick_in=%.2fs",
                    self.total_ticks,
                    instruments_checked,
                    total,
                    new_crosses,
                    no_data,
                    insufficient,
                    warmup,
                    no_cross,
                    duplicate,
                    failed,
                    elapsed,
                    next_tick_in,
                )

    # ------------------------------------------------------------------ #
    # Per-instrument tick (worker thread)                                #
    # ------------------------------------------------------------------ #
    def _process_tick(
        self, item: dict[str, Any]
    ) -> tuple[str, dict[str, Any] | None]:
        """Process one instrument.

        Returns ``(status, event)`` where ``status`` is one of:

        * ``"invalid-key"``          — missing / blank ``instrument_key``
        * ``"no-intraday"``          — upstream returned no candles
        * ``"insufficient-candles"`` — merged history too short for EMA
        * ``"ema-warmup"``           — EMA series not ready at last index
        * ``"no-cross"``             — EMAs ready, no cross this minute
        * ``"duplicate-cross"``      — cross already recorded for this ts
        * ``"cross"``                — new cross (event is the payload)
        """
        instrument_key = item.get("instrument_key")
        if not isinstance(instrument_key, str) or not instrument_key.strip():
            return "invalid-key", None

        intraday = fetch_intraday(instrument_key)
        if not intraday:
            return "no-intraday", None

        candle_path = build_candle_path(item)
        historical = self._load_candles(candle_path)

        merged_map: dict[str, dict[str, Any]] = {}
        for candle in historical:
            ts = candle.get("timestamp")
            if ts is not None:
                merged_map[str(ts)] = candle
        # Fresh intraday wins on collision (same minute timestamp).
        for candle in intraday:
            ts = candle.get("timestamp")
            if ts is not None:
                merged_map[str(ts)] = candle

        merged = sorted(
            merged_map.values(), key=lambda c: str(c.get("timestamp", ""))
        )
        fast = settings.ema_fast_period
        slow = settings.ema_slow_period
        if len(merged) < max(fast, slow) + 1:
            return "insufficient-candles", None

        closes = [to_float(c.get("close")) or 0.0 for c in merged]
        fast_emas = compute_ema(closes, fast)
        slow_emas = compute_ema(closes, slow)

        i = len(merged) - 1
        fp, sp = fast_emas[i - 1], slow_emas[i - 1]
        fc, sc = fast_emas[i], slow_emas[i]
        if None in (fp, sp, fc, sc):
            return "ema-warmup", None

        cross_type = classify_cross(float(fp) - float(sp), float(fc) - float(sc))
        if cross_type is None:
            return "no-cross", None

        last_ts = merged[i].get("timestamp")
        cross_path = build_ema_cross_path(item)
        lock = get_file_write_lock(cross_path)

        with lock:
            existing = self._load_crosses(cross_path)
            if any(c.get("timestamp") == last_ts for c in existing["crosses"]):
                return "duplicate-cross", None

            event = {
                "index": i,
                "timestamp": last_ts,
                "type": cross_type,
                "close": merged[i].get("close"),
                "ema_fast": fc,
                "ema_slow": sc,
                "ema_fast_prev": fp,
                "ema_slow_prev": sp,
                "candle": merged[i],
                "detected_by": "live",
                "detected_at": datetime.now(timezone.utc).isoformat(),
            }
            existing["crosses"].append(event)
            existing["instrument_key"] = instrument_key
            existing["fast_period"] = fast
            existing["slow_period"] = slow
            existing["cross_count"] = len(existing["crosses"])
            existing["updated_at"] = datetime.now(timezone.utc).isoformat()
            self._save_json(cross_path, existing)

        return "cross", event

    # ------------------------------------------------------------------ #
    # IO helpers                                                         #
    # ------------------------------------------------------------------ #
    def _load_candles(self, path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("Unable to read candles: %s", path)
            return []
        candles = payload.get("candles") if isinstance(payload, dict) else payload
        if not isinstance(candles, list):
            return []
        return [c for c in candles if isinstance(c, dict)]

    def _load_crosses(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {"crosses": []}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("Unable to read crosses: %s", path)
            return {"crosses": []}
        if not isinstance(payload, dict):
            return {"crosses": []}
        if not isinstance(payload.get("crosses"), list):
            payload["crosses"] = []
        return payload

    def _save_json(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(path)

live_ema_cross_store = LiveEmaCrossStore()