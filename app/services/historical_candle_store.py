"""Second and third jobs: keep a rolling local cache of candles.

Job 2 — ``ensure_recent_candles`` (historical, last N days)
----------------------------------------------------------
1. For every instrument in the in-memory ordered list, build its on-disk
   path: ``data/<underlying>/<strike>_<strike_type>/historical.json``.
2. If the local file already covers the most recent market day inside the
   lookback window, skip it.
3. Otherwise, fetch the missing range from Upstox ``HistoryV3Api`` in
   7-day chunks.
4. Merge new candles with whatever was already stored, drop anything older
   than ``HISTORY_LOOKBACK_DAYS``, and rewrite the file atomically.

Job 3 — ``ensure_intraday_candles`` (today only)
------------------------------------------------
Runs only when the current time (in ``settings.market_timezone``) is a
weekday between ``settings.market_open_time`` and ``settings.market_close_time``.
Uses ``HistoryV3Api.get_intra_day_candle_data`` which returns only today's
candles. Merged into the same ``historical.json`` so the file remains the
single source of truth.

All instruments are processed concurrently, bounded by
``settings.history_max_concurrency``.

Logging
-------
Dict payloads (job summaries, skip summaries) are emitted as JSON via
``json_log``. When ``settings.instrument_errors_only`` is True, per-instrument
progress lines are suppressed and only errors raised while processing an
instrument are logged. Job-level start/finish summaries are always logged.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from app.core.config import settings
from app.core.logger import get_logger, json_log
from app.services.instrument_paths import build_candle_path
from app.services.upstox_fetcher import fetch_historical, fetch_intraday

logger = get_logger(__name__)

def _parse_hhmm(value: str) -> dt_time:
    try:
        hh, mm = value.strip().split(":")
        return dt_time(int(hh), int(mm))
    except Exception:
        logger.warning("Invalid time value '%s'; falling back to 00:00", value)
        return dt_time(0, 0)

class HistoricalCandleStore:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._semaphore: asyncio.Semaphore | None = None

        # Job 2 state
        self.last_run_at: str | None = None
        self.last_error: str | None = None
        self.fetched_count = 0
        self.skipped_count = 0
        self.failed_count = 0
        self._processed_count = 0
        self._total_count = 0

        # Job 3 state
        self.intraday_last_run_at: str | None = None
        self.intraday_last_error: str | None = None
        self.intraday_updated_count = 0
        self.intraday_empty_count = 0
        self.intraday_failed_count = 0
        self._intraday_processed_count = 0
        self._intraday_total_count = 0

    # ------------------------------------------------------------------ #
    # Market hours helper                                                #
    # ------------------------------------------------------------------ #
    def _now_market_tz(self) -> datetime:
        try:
            tz = ZoneInfo(settings.market_timezone)
        except Exception:
            logger.exception(
                "Invalid MARKET_TIMEZONE=%s; falling back to UTC",
                settings.market_timezone,
            )
            tz = timezone.utc
        return datetime.now(tz)

    def is_market_hours(self) -> bool:
        now = self._now_market_tz()
        if now.weekday() >= 5:
            return False
        open_t = _parse_hhmm(settings.market_open_time)
        close_t = _parse_hhmm(settings.market_close_time)
        return open_t <= now.time() <= close_t

    # ------------------------------------------------------------------ #
    # Job 2                                                              #
    # ------------------------------------------------------------------ #
    async def ensure_recent_candles(
        self,
        instruments: list[dict[str, Any]],
        reason: str = "startup",
    ) -> dict[str, Any]:
        async with self._lock:
            logger.info(
                "Historical candle job started; reason=%s instruments=%d concurrency=%d",
                reason,
                len(instruments),
                settings.history_max_concurrency,
            )
            self.fetched_count = 0
            self.skipped_count = 0
            self.failed_count = 0
            self._processed_count = 0

            lookback = max(1, settings.history_lookback_days)
            to_date = date.today()
            cutoff = to_date - timedelta(days=lookback)

            concurrency = max(1, settings.history_max_concurrency)
            self._semaphore = asyncio.Semaphore(concurrency)

            valid_instruments = [i for i in instruments if isinstance(i, dict)]
            self._total_count = len(valid_instruments)

            logger.info(
                "Historical candle job queue ready; total=%d lookback_days=%d cutoff=%s",
                self._total_count,
                lookback,
                cutoff.isoformat(),
            )

            tasks = [
                asyncio.create_task(
                    self._process_instrument(item, cutoff, to_date)
                )
                for item in valid_instruments
            ]
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=False)

            self.last_run_at = datetime.now(timezone.utc).isoformat()
            self.last_error = (
                None
                if self.failed_count == 0
                else f"{self.failed_count} instrument(s) failed"
            )

            summary = {
                "reason": reason,
                "lookback_days": lookback,
                "concurrency": concurrency,
                "total": self._total_count,
                "processed": self._processed_count,
                "fetched": self.fetched_count,
                "skipped": self.skipped_count,
                "failed": self.failed_count,
                "last_run_at": self.last_run_at,
                "last_error": self.last_error,
            }
            logger.info("Historical candle job completed; %s", json_log(summary))
            return summary

    def _report_progress(self, instrument_key: str, outcome: str) -> None:
        self._processed_count += 1
        if settings.instrument_errors_only:
            return
        logger.info(
            "Historical candle job progress: %d/%d processed "
            "(fetched=%d skipped=%d failed=%d) last=%s [%s]",
            self._processed_count,
            self._total_count,
            self.fetched_count,
            self.skipped_count,
            self.failed_count,
            instrument_key,
            outcome,
        )

    async def _process_instrument(
        self,
        item: dict[str, Any],
        cutoff: date,
        to_date: date,
    ) -> None:
        instrument_key = item.get("instrument_key")
        if not isinstance(instrument_key, str) or not instrument_key.strip():
            self.skipped_count += 1
            self._report_progress(str(instrument_key), "invalid-key")
            return

        target_path = build_candle_path(item)
        existing = self._load_existing(target_path)

        latest = self._latest_candle_date(existing)
        expected_last = self._last_market_day(to_date - timedelta(days=1))

        if latest is not None and latest >= expected_last:
            self.skipped_count += 1
            self._report_progress(
                instrument_key,
                f"up-to-date latest={latest.isoformat()} "
                f"expected_last={expected_last.isoformat()} "
                f"candles={len(existing)}",
            )
            return

        assert self._semaphore is not None
        async with self._semaphore:
            try:
                candles = await asyncio.to_thread(
                    fetch_historical, instrument_key, cutoff, to_date
                )
            except Exception:
                self.failed_count += 1
                logger.exception(
                    "Historical candle fetch failed; instrument=%s "
                    "range=%s..%s local_latest=%s expected_last=%s",
                    instrument_key,
                    cutoff.isoformat(),
                    to_date.isoformat(),
                    latest.isoformat() if latest else "none",
                    expected_last.isoformat(),
                )
                self._report_progress(
                    instrument_key,
                    f"fetch-failed latest={latest.isoformat() if latest else 'none'} "
                    f"expected_last={expected_last.isoformat()}",
                )
                return

        merged = self._merge_and_trim(existing, candles, cutoff)
        self._save(target_path, merged)
        self.fetched_count += 1

        merged_latest = self._latest_candle_date(merged)
        self._report_progress(
            instrument_key,
            f"fetched={len(candles)} stored={len(merged)} "
            f"range={cutoff.isoformat()}..{to_date.isoformat()} "
            f"latest={merged_latest.isoformat() if merged_latest else 'none'}",
        )

    # ------------------------------------------------------------------ #
    # Job 3                                                              #
    # ------------------------------------------------------------------ #
    async def ensure_intraday_candles(
        self,
        instruments: list[dict[str, Any]],
        reason: str = "startup",
    ) -> dict[str, Any]:
        async with self._lock:
            now_local = self._now_market_tz()
            if not self.is_market_hours():
                summary = {
                    "reason": reason,
                    "skipped": True,
                    "skip_reason": (
                        "outside market hours "
                        f"({settings.market_open_time}-{settings.market_close_time} "
                        f"{settings.market_timezone})"
                    ),
                    "now_market_tz": now_local.isoformat(),
                    "today": now_local.date().isoformat(),
                }
                logger.info("Intraday candle job skipped; %s", json_log(summary))
                return summary

            logger.info(
                "Intraday candle job started; reason=%s instruments=%d concurrency=%d",
                reason,
                len(instruments),
                settings.history_max_concurrency,
            )
            self.intraday_updated_count = 0
            self.intraday_empty_count = 0
            self.intraday_failed_count = 0
            self._intraday_processed_count = 0

            lookback = max(1, settings.history_lookback_days)
            cutoff = date.today() - timedelta(days=lookback)
            today = now_local.date()

            concurrency = max(1, settings.history_max_concurrency)
            self._semaphore = asyncio.Semaphore(concurrency)

            valid_instruments = [i for i in instruments if isinstance(i, dict)]
            self._intraday_total_count = len(valid_instruments)

            logger.info(
                "Intraday candle job queue ready; total=%d today=%s window=%s-%s tz=%s cutoff=%s",
                self._intraday_total_count,
                today.isoformat(),
                settings.market_open_time,
                settings.market_close_time,
                settings.market_timezone,
                cutoff.isoformat(),
            )

            tasks = [
                asyncio.create_task(
                    self._process_intraday_instrument(item, cutoff)
                )
                for item in valid_instruments
            ]
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=False)

            self.intraday_last_run_at = datetime.now(timezone.utc).isoformat()
            self.intraday_last_error = (
                None
                if self.intraday_failed_count == 0
                else f"{self.intraday_failed_count} instrument(s) failed"
            )

            summary = {
                "reason": reason,
                "skipped": False,
                "concurrency": concurrency,
                "total": self._intraday_total_count,
                "processed": self._intraday_processed_count,
                "updated": self.intraday_updated_count,
                "empty": self.intraday_empty_count,
                "failed": self.intraday_failed_count,
                "today": today.isoformat(),
                "last_run_at": self.intraday_last_run_at,
                "last_error": self.intraday_last_error,
            }
            logger.info("Intraday candle job completed; %s", json_log(summary))
            return summary

    def _report_intraday_progress(
        self,
        instrument_key: str,
        outcome: str,
    ) -> None:
        self._intraday_processed_count += 1
        if settings.instrument_errors_only:
            return
        logger.info(
            "Intraday candle job progress: %d/%d processed "
            "(updated=%d empty=%d failed=%d) last=%s [%s]",
            self._intraday_processed_count,
            self._intraday_total_count,
            self.intraday_updated_count,
            self.intraday_empty_count,
            self.intraday_failed_count,
            instrument_key,
            outcome,
        )

    async def _process_intraday_instrument(
        self,
        item: dict[str, Any],
        cutoff: date,
    ) -> None:
        instrument_key = item.get("instrument_key")
        if not isinstance(instrument_key, str) or not instrument_key.strip():
            self.intraday_empty_count += 1
            self._report_intraday_progress(str(instrument_key), "invalid-key")
            return

        target_path = build_candle_path(item)
        existing = self._load_existing(target_path)
        local_latest = self._latest_candle_date(existing)
        today = self._now_market_tz().date()

        assert self._semaphore is not None
        async with self._semaphore:
            try:
                candles = await asyncio.to_thread(
                    fetch_intraday, instrument_key
                )
            except Exception:
                self.intraday_failed_count += 1
                logger.exception(
                    "Intraday candle fetch failed; instrument=%s "
                    "today=%s local_latest=%s",
                    instrument_key,
                    today.isoformat(),
                    local_latest.isoformat() if local_latest else "none",
                )
                self._report_intraday_progress(
                    instrument_key,
                    f"fetch-failed today={today.isoformat()} "
                    f"local_latest={local_latest.isoformat() if local_latest else 'none'}",
                )
                return

        if not candles:
            self.intraday_empty_count += 1
            self._report_intraday_progress(
                instrument_key,
                f"empty today={today.isoformat()} "
                f"local_latest={local_latest.isoformat() if local_latest else 'none'}",
            )
            return

        merged = self._merge_and_trim(existing, candles, cutoff)
        self._save(target_path, merged)
        self.intraday_updated_count += 1

        new_latest = self._latest_candle_date(candles)
        merged_latest = self._latest_candle_date(merged)
        self._report_intraday_progress(
            instrument_key,
            f"today_candles={len(candles)} day={today.isoformat()} "
            f"newest_today={new_latest.isoformat() if new_latest else 'none'} "
            f"stored={len(merged)} "
            f"latest={merged_latest.isoformat() if merged_latest else 'none'}",
        )

    # ------------------------------------------------------------------ #
    # Disk IO / freshness                                                #
    # ------------------------------------------------------------------ #
    def _load_existing(self, path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("Unable to read existing candle file: %s", path)
            return []

        candles = payload.get("candles") if isinstance(payload, dict) else payload
        if not isinstance(candles, list):
            return []
        return [c for c in candles if isinstance(c, dict)]

    def _save(self, path: Path, candles: list[dict[str, Any]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "count": len(candles),
            "candles": candles,
        }
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(path)

    def _needs_fetch(
        self,
        existing: list[dict[str, Any]],
        cutoff: date,
        to_date: date,
    ) -> bool:
        if not existing:
            return True

        latest = self._latest_candle_date(existing)
        if latest is None:
            return True

        expected_last = self._last_market_day(to_date - timedelta(days=1))
        return latest < expected_last

    def _latest_candle_date(
        self, candles: list[dict[str, Any]]
    ) -> date | None:
        """Return the most recent date present in a list of candles."""
        latest: date | None = None
        for candle in candles:
            day = self._candle_date(candle.get("timestamp"))
            if day is None:
                continue
            if latest is None or day > latest:
                latest = day
        return latest

    @staticmethod
    def _last_market_day(day: date) -> date:
        while day.weekday() >= 5:
            day -= timedelta(days=1)
        return day

    @staticmethod
    def _candle_date(ts: Any) -> date | None:
        if isinstance(ts, datetime):
            return ts.date()
        if isinstance(ts, date):
            return ts
        if isinstance(ts, str):
            try:
                return datetime.fromisoformat(ts.replace("Z", "+00:00")).date()
            except ValueError:
                try:
                    return datetime.strptime(ts[:10], "%Y-%m-%d").date()
                except ValueError:
                    return None
        return None

    def _merge_and_trim(
        self,
        existing: list[dict[str, Any]],
        fresh: list[dict[str, Any]],
        cutoff: date,
    ) -> list[dict[str, Any]]:
        merged: dict[str, dict[str, Any]] = {}
        for candle in list(existing) + list(fresh):
            ts = candle.get("timestamp")
            if ts is None:
                continue
            day = self._candle_date(ts)
            if day is None or day < cutoff:
                continue
            merged[str(ts)] = candle

        return sorted(merged.values(), key=lambda c: str(c.get("timestamp", "")))

historical_candle_store = HistoricalCandleStore()