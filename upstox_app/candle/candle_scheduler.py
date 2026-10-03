"""
candle_scheduler.py

Daily candle refresh scheduler.

Fires once per day at OPTIONS_CANDLES_DAILY_REFRESH_TIME IST (default
09:00). On each fire, it calls candle_service.refresh_if_outdated(),
which reloads option contracts and rewrites any readonly candle file
whose stored `expiry` no longer matches the live contract.

Startup behaviour:
- If the app starts AFTER the scheduled time and the refresh has not
  yet run today, it runs immediately once (opt-in via
  OPTIONS_CANDLES_RUN_IF_MISSED, default true).
- If the app starts BEFORE the scheduled time, it waits for the next
  fire as normal.

Runs as a single asyncio task. Independent from the token scheduler.
"""

import asyncio
from datetime import datetime, time, timedelta, timezone
from typing import Optional

from core.logger import get_logger
from upstox_app.candle.candle_service import candle_service
from upstox_app.common.config import upstox_config

logger = get_logger(__name__)

# India Standard Time — no DST, so a fixed offset is safe.
IST = timezone(timedelta(hours=5, minutes=30))


def _cfg(name, default):
    return getattr(upstox_config, name, default)


class CandleScheduler:
    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        self._stop_event: Optional[asyncio.Event] = None
        # Tracks the last calendar date (IST) on which the refresh ran.
        # Used to decide whether a "missed" run should fire on startup.
        self._last_run_date: Optional[str] = None

    # ---- lifecycle -----------------------------------------------------
    def start(self) -> None:
        if self._task and not self._task.done():
            logger.info("Candle daily refresh scheduler already running")
            return
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._run_loop())
        logger.info(
            "Candle daily refresh scheduler started | daily_time=%s IST "
            "| run_if_missed=%s",
            _cfg("CANDLES_DAILY_REFRESH_TIME", "09:00"),
            bool(_cfg("CANDLES_RUN_IF_MISSED", True)),
        )

    async def stop(self) -> None:
        if not self._task:
            return
        if self._stop_event:
            self._stop_event.set()
        try:
            await asyncio.wait_for(self._task, timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning("Candle scheduler did not stop in time; cancelling")
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._task = None
        self._stop_event = None
        logger.info("Candle daily refresh scheduler stopped")

    # ---- loop ----------------------------------------------------------
    async def _run_loop(self) -> None:
        # Optional: run immediately if we started after today's scheduled
        # time and haven't run yet today.
        if bool(_cfg("CANDLES_RUN_IF_MISSED", True)):
            today_str = datetime.now(IST).date().isoformat()
            if self._last_run_date != today_str and self._is_past_todays_slot():
                logger.info(
                    "Candle scheduler | start after scheduled time | running once now"
                )
                await self._run_once()

        while self._stop_event and not self._stop_event.is_set():
            next_run = self._next_run_at()
            now = datetime.now(IST)
            wait_sec = max(1.0, (next_run - now).total_seconds())

            logger.info(
                "Candle scheduler | now=%s next=%s wait=%.0fs (%.1fh)",
                now.isoformat(timespec="seconds"),
                next_run.isoformat(timespec="seconds"),
                wait_sec,
                wait_sec / 3600.0,
            )

            try:
                # Wakes immediately if stop() is called.
                await asyncio.wait_for(self._stop_event.wait(), timeout=wait_sec)
                return  # stop requested
            except asyncio.TimeoutError:
                pass

            await self._run_once()

    async def _run_once(self) -> None:
        if not _cfg("CANDLES_DAILY_REFRESH_ENABLED", True):
            logger.info("Candle daily refresh disabled; skipping run")
            return

        # Best-effort guard against overlapping runs.
        already_running = self._task is not None and getattr(
            self, "_running", False
        )
        if already_running:
            logger.warning("Candle daily refresh already in progress; skipping")
            return
        self._running = True

        logger.info("=" * 60)
        logger.info("Candle daily refresh starting")
        try:
            summary = await asyncio.to_thread(
                candle_service.refresh_if_outdated, True
            )
            t = summary.get("totals", {})
            logger.info(
                "Candle daily refresh done | indexes=%d | checked=%d "
                "| outdated=%d | refreshed=%d | failed=%d | stale_indexes=%s",
                t.get("indexes_checked", 0),
                t.get("contracts_checked", 0),
                t.get("outdated", 0),
                t.get("refreshed", 0),
                t.get("failed", 0),
                t.get("stale_indexes", []),
            )
            # Mark this calendar day as done, regardless of how many
            # files were actually rewritten.
            self._last_run_date = datetime.now(IST).date().isoformat()
        except Exception as exc:  # noqa: BLE001
            logger.exception("Candle daily refresh failed: %s", exc)
        finally:
            self._running = False
        logger.info("=" * 60)

    # ---- helpers -------------------------------------------------------
    def _scheduled_time(self) -> time:
        """Parse CANDLES_DAILY_REFRESH_TIME (HH:MM) into a time object."""
        time_str = str(_cfg("CANDLES_DAILY_REFRESH_TIME", "09:00"))
        try:
            hh, mm = time_str.split(":")
            return time(int(hh), int(mm), 0)
        except Exception:
            logger.warning(
                "Invalid CANDLES_DAILY_REFRESH_TIME=%r; defaulting to 09:00",
                time_str,
            )
            return time(9, 0, 0)

    def _is_past_todays_slot(self) -> bool:
        """True if the current IST clock is at or past today's slot."""
        now = datetime.now(IST)
        slot = self._scheduled_time()
        today_slot = now.replace(
            hour=slot.hour, minute=slot.minute, second=0, microsecond=0
        )
        return now >= today_slot

    def _next_run_at(self) -> datetime:
        slot = self._scheduled_time()
        now = datetime.now(IST)
        target = now.replace(
            hour=slot.hour, minute=slot.minute, second=0, microsecond=0
        )
        if target <= now:
            target += timedelta(days=1)
        return target

    def status(self) -> dict:
        """Small introspection helper for /health or debug endpoints."""
        now = datetime.now(IST)
        slot = self._scheduled_time()
        today_slot = now.replace(
            hour=slot.hour, minute=slot.minute, second=0, microsecond=0
        )
        return {
            "running": self._task is not None and not self._task.done(),
            "enabled": bool(_cfg("CANDLES_DAILY_REFRESH_ENABLED", True)),
            "scheduled_time_ist": slot.strftime("%H:%M"),
            "now_ist": now.isoformat(timespec="seconds"),
            "last_run_date": self._last_run_date,
            "next_run_at": self._next_run_at().isoformat(timespec="seconds")
            if self._task and not self._task.done()
            else None,
            "run_if_missed": bool(_cfg("CANDLES_RUN_IF_MISSED", True)),
            "missed_today": self._last_run_date
            != today_slot.date().isoformat()
            and now >= today_slot,
        }


candle_scheduler = CandleScheduler()