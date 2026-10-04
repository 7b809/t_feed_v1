"""
candle_scheduler.py

Daily candle refresh scheduler.

Fires once per day at OPTIONS_CANDLES_DAILY_REFRESH_TIME IST (default
09:00). On each fire it:

  1. Reloads option contracts.
  2. Rewrites any candle file that is stale (missing/empty/old) or whose
     stored expiry no longer matches the live contract.
  3. Recomputes 9/21 EMA crossovers for every contract.
  4. Bulk-subscribes every enabled index and every option contract that
     is not already on the market streamer.
"""
import asyncio
from datetime import datetime, time, timedelta, timezone
from typing import Optional

from core.logger import get_logger
from upstox_app.candle.candle_service import candle_service
from upstox_app.candle.crossover_service import crossover_service
from upstox_app.common.config import upstox_config

logger = get_logger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))


def _cfg(name, default):
    return getattr(upstox_config, name, default)


class CandleScheduler:
    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        self._stop_event: Optional[asyncio.Event] = None
        self._running = False
        self._last_run_date: Optional[str] = None

    # ---- lifecycle -----------------------------------------------------
    def start(self) -> None:
        if self._task and not self._task.done():
            logger.info("Candle daily refresh scheduler already running")
            return
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._run_loop())
        logger.info(
            "Candle daily refresh scheduler started | daily_time=%s IST | run_if_missed=%s",
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
        if bool(_cfg("CANDLES_RUN_IF_MISSED", True)):
            today_str = datetime.now(IST).date().isoformat()
            if self._last_run_date != today_str and self._is_past_todays_slot():
                logger.info("Candle scheduler | start after scheduled time | running once now")
                await self._run_once()

        while self._stop_event and not self._stop_event.is_set():
            next_run = self._next_run_at()
            now = datetime.now(IST)
            wait_sec = max(1.0, (next_run - now).total_seconds())
            logger.info(
                "Candle scheduler | now=%s next=%s wait=%.0fs (%.1fh)",
                now.isoformat(timespec="seconds"),
                next_run.isoformat(timespec="seconds"),
                wait_sec, wait_sec / 3600.0,
            )
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=wait_sec)
                return
            except asyncio.TimeoutError:
                pass
            await self._run_once()

    async def _run_once(self) -> None:
        if not _cfg("CANDLES_DAILY_REFRESH_ENABLED", True):
            logger.info("Candle daily refresh disabled; skipping run")
            return

        if self._running:
            logger.warning("Candle daily refresh already in progress; skipping")
            return
        self._running = True

        logger.info("=" * 60)
        logger.info("Candle daily refresh starting")

        # 1) Refresh candle files.
        try:
            summary = await asyncio.to_thread(candle_service.refresh_if_outdated, True)
            t = summary.get("totals", {})
            logger.info(
                "Candle daily refresh done | indexes=%d | checked=%d "
                "| expiry_changed=%d | stale=%d | outdated=%d | refreshed=%d "
                "| failed=%d | stale_indexes=%s",
                t.get("indexes_checked", 0),
                t.get("contracts_checked", 0),
                t.get("expiry_changed", 0),
                t.get("stale", 0),
                t.get("outdated", 0),
                t.get("refreshed", 0),
                t.get("failed", 0),
                t.get("stale_indexes", []),
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Candle daily refresh failed: %s", exc)
        finally:
            self._running = False

        # 2) Recompute crossovers on the freshly written candles.
        if bool(_cfg("CROSSOVER_CALC_ON_DAILY_REFRESH", True)):
            try:
                logger.info("Daily crossovers starting")
                cross = await asyncio.to_thread(crossover_service.calculate_all_enabled)
                ct = cross.get("totals", {})
                logger.info(
                    "Daily crossovers done | indexes=%d | total=%d "
                    "| historic_ok=%d historic_empty=%d "
                    "| intraday_ok=%d intraday_empty=%d | errors=%d",
                    ct.get("indexes", 0), ct.get("total_contracts", 0),
                    ct.get("historic_success", 0), ct.get("historic_empty", 0),
                    ct.get("intraday_success", 0), ct.get("intraday_empty", 0),
                    ct.get("errors", 0),
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("Daily crossover computation failed: %s", exc)

        # 3) Bulk-subscribe every enabled index + option contract.
        try:
            from upstox_app.streamer.streamer_manager import subscribe_all_on_refresh
            logger.info("Daily subscribe-all starting")
            sub = await asyncio.to_thread(subscribe_all_on_refresh)
            logger.info("Daily subscribe-all done | %s", sub)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Daily subscribe-all failed | step=streamer_manager | reason=%s",
                f"{type(exc).__name__}: {str(exc)[:150]}",
            )

        self._last_run_date = datetime.now(IST).date().isoformat()
        logger.info("=" * 60)

    # ---- helpers -------------------------------------------------------
    def _scheduled_time(self) -> time:
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
            if self._task and not self._task.done() else None,
            "run_if_missed": bool(_cfg("CANDLES_RUN_IF_MISSED", True)),
            "missed_today": self._last_run_date
            != today_slot.date().isoformat()
            and now >= today_slot,
        }


candle_scheduler = CandleScheduler()