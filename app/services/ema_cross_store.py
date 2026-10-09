"""Fourth job: batch 9/21 EMA crosses for every subscribed instrument.

Reads ``historical.json`` (merged history+intraday from jobs 2 and 3),
computes fast/slow EMA series, detects crosses, and writes
``ema_crosses.json`` next to it. The file is fully rewritten each run.

Cross detection:
- Bullish: fast - slow transitions from ``<= 0`` to ``> 0``.
- Bearish: fast - slow transitions from ``>= 0`` to ``< 0``.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.core.logger import get_logger
from app.services.ema_utils import classify_cross, compute_ema, to_float
from app.services.instrument_paths import (
    build_candle_path,
    build_ema_cross_path,
    get_file_write_lock,
)

logger = get_logger(__name__)


def _detect_crosses(
    candles: list[dict[str, Any]],
    fast_emas: list[float | None],
    slow_emas: list[float | None],
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for i in range(1, len(candles)):
        fp, sp = fast_emas[i - 1], slow_emas[i - 1]
        fc, sc = fast_emas[i], slow_emas[i]
        if None in (fp, sp, fc, sc):
            continue
        cross_type = classify_cross(float(fp) - float(sp), float(fc) - float(sc))
        if cross_type is None:
            continue
        candle = candles[i]
        events.append(
            {
                "index": i,
                "timestamp": candle.get("timestamp"),
                "type": cross_type,
                "close": candle.get("close"),
                "ema_fast": fc,
                "ema_slow": sc,
                "ema_fast_prev": fp,
                "ema_slow_prev": sp,
                "candle": candle,
            }
        )
    return events


class EmaCrossStore:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._semaphore: asyncio.Semaphore | None = None

        self.last_run_at: str | None = None
        self.last_error: str | None = None
        self.computed_count = 0
        self.empty_count = 0
        self.failed_count = 0
        self.total_crosses = 0
        self._processed_count = 0
        self._total_count = 0

    async def compute_all(
        self,
        instruments: list[dict[str, Any]],
        reason: str = "startup",
    ) -> dict[str, Any]:
        async with self._lock:
            fast = max(1, settings.ema_fast_period)
            slow = max(fast + 1, settings.ema_slow_period)

            logger.info(
                "EMA cross job started; reason=%s instruments=%d fast=%d slow=%d concurrency=%d",
                reason,
                len(instruments),
                fast,
                slow,
                settings.history_max_concurrency,
            )

            self.computed_count = 0
            self.empty_count = 0
            self.failed_count = 0
            self.total_crosses = 0
            self._processed_count = 0

            concurrency = max(1, settings.history_max_concurrency)
            self._semaphore = asyncio.Semaphore(concurrency)

            valid_instruments = [i for i in instruments if isinstance(i, dict)]
            self._total_count = len(valid_instruments)

            logger.info(
                "EMA cross job queue ready; total=%d fast=%d slow=%d",
                self._total_count,
                fast,
                slow,
            )

            tasks = [
                asyncio.create_task(self._process_instrument(item, fast, slow))
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
                "fast_period": fast,
                "slow_period": slow,
                "concurrency": concurrency,
                "total": self._total_count,
                "processed": self._processed_count,
                "computed": self.computed_count,
                "empty": self.empty_count,
                "failed": self.failed_count,
                "total_crosses": self.total_crosses,
                "last_run_at": self.last_run_at,
                "last_error": self.last_error,
            }
            logger.info("EMA cross job completed; %s", summary)
            return summary

    def _report_progress(self, instrument_key: str, outcome: str) -> None:
        self._processed_count += 1
        logger.info(
            "EMA cross job progress: %d/%d processed "
            "(computed=%d empty=%d failed=%d total_crosses=%d) last=%s [%s]",
            self._processed_count,
            self._total_count,
            self.computed_count,
            self.empty_count,
            self.failed_count,
            self.total_crosses,
            instrument_key,
            outcome,
        )

    async def _process_instrument(
        self,
        item: dict[str, Any],
        fast: int,
        slow: int,
    ) -> None:
        instrument_key = item.get("instrument_key")
        if not isinstance(instrument_key, str) or not instrument_key.strip():
            self.empty_count += 1
            self._report_progress(str(instrument_key), "invalid-key")
            return

        assert self._semaphore is not None
        async with self._semaphore:
            try:
                result = await asyncio.to_thread(
                    self._compute_for_instrument, item, fast, slow
                )
            except Exception:
                self.failed_count += 1
                logger.exception(
                    "EMA cross computation failed; instrument=%s",
                    instrument_key,
                )
                self._report_progress(instrument_key, "compute-failed")
                return

        if result is None:
            self.empty_count += 1
            self._report_progress(instrument_key, "no-candles")
            return

        self.computed_count += 1
        self.total_crosses += result["cross_count"]
        self._report_progress(
            instrument_key, f"crosses={result['cross_count']} candles={result['candles']}"
        )

    def _compute_for_instrument(
        self,
        item: dict[str, Any],
        fast: int,
        slow: int,
    ) -> dict[str, Any] | None:
        instrument_key = str(item.get("instrument_key") or "")
        candle_path = build_candle_path(item)
        cross_path = build_ema_cross_path(item)

        candles = self._load_candles(candle_path)
        if not candles:
            return None

        candles = sorted(candles, key=lambda c: str(c.get("timestamp", "")))
        closes: list[float] = [to_float(c.get("close")) or 0.0 for c in candles]

        fast_emas = compute_ema(closes, fast)
        slow_emas = compute_ema(closes, slow)
        events = _detect_crosses(candles, fast_emas, slow_emas)

        payload = {
            "instrument_key": instrument_key,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "fast_period": fast,
            "slow_period": slow,
            "candles_analyzed": len(candles),
            "cross_count": len(events),
            "crosses": events,
        }

        lock = get_file_write_lock(cross_path)
        with lock:
            cross_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = cross_path.with_suffix(cross_path.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tmp.replace(cross_path)

        return {"cross_count": len(events), "candles": len(candles)}

    def _load_candles(self, path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("Unable to read candle file: %s", path)
            return []
        candles = payload.get("candles") if isinstance(payload, dict) else payload
        if not isinstance(candles, list):
            return []
        return [c for c in candles if isinstance(c, dict)]


ema_cross_store = EmaCrossStore()