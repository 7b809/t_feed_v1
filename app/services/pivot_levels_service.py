"""Local opening-range pivot and previous-session average persistence."""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")


def _stamp(candle: dict[str, Any]) -> str:
    return str(candle.get("ts") or candle.get("timestamp") or "")


def _valid_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


class PivotLevelsService:
    """Stores daily opening-range pivots and prior-session average per instrument.

    The file is local-only: <DATA_DIR>/pivot_levels.json. It is not written to
    MongoDB or sent to the broker.
    """

    def __init__(self, data_dir: str | Path) -> None:
        self.path = Path(data_dir or "./data").expanduser().resolve() / "pivot_levels.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    @staticmethod
    def calculate(
        instrument_key: str,
        trade_date: str,
        today_candles: list[dict[str, Any]],
        previous_trade_date: str | None = None,
        previous_candles: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any] | None:
        """Calculate pivots from the earliest candle and yesterday range from all prior-day candles."""
        valid_today = [
            item for item in (today_candles or [])
            if isinstance(item, dict) and _stamp(item)
            and all(_valid_number(item.get(field)) is not None for field in ("open", "high", "low", "close"))
        ]
        if not valid_today:
            return None
        valid_today.sort(key=_stamp)
        first = valid_today[0]
        opening_high = _valid_number(first.get("high"))
        opening_low = _valid_number(first.get("low"))
        opening_avg = (opening_high + opening_low) / 2.0
        half_range = (opening_high - opening_low) / 2.0
        if half_range < 0:
            return None

        pivots = {
            "R1": round(opening_high, 4),
            "R2": round(opening_high + half_range, 4),
            "R3": round(opening_high + (half_range * 2), 4),
            "AVG": round(opening_avg, 4),
            "S1": round(opening_low, 4),
            "S2": round(opening_low - half_range, 4),
            "S3": round(opening_low - (half_range * 2), 4),
            "R1_Diff": round(half_range, 4),
            "S1_Diff": round(half_range, 4),
            "range": round(opening_high - opening_low, 4),
        }

        prior = [
            item for item in (previous_candles or [])
            if isinstance(item, dict)
            and _valid_number(item.get("high")) is not None
            and _valid_number(item.get("low")) is not None
            and _valid_number(item.get("high")) >= _valid_number(item.get("low"))
        ]
        yesterday = None
        if prior:
            previous_high = max(_valid_number(item["high"]) for item in prior)
            previous_low = min(_valid_number(item["low"]) for item in prior)
            yesterday = {
                "trade_date_ist": previous_trade_date,
                "high": round(previous_high, 4),
                "low": round(previous_low, 4),
                "avg": round((previous_high + previous_low) / 2.0, 4),
                "average_points": round((previous_high + previous_low) / 2.0, 4),
                "candles_count": len(prior),
                "formula": "(previous_session_high + previous_session_low) / 2",
            }

        return {
            "instrument_key": instrument_key,
            "trade_date_ist": trade_date,
            "opening_range_candle": {
                "timestamp": _stamp(first),
                "open": _valid_number(first.get("open")),
                "high": opening_high,
                "low": opening_low,
                "close": _valid_number(first.get("close")),
            },
            "pivot_levels": pivots,
            "previous_session": yesterday,
            "yesterday_avg": yesterday["avg"] if yesterday else None,
            "yesterday_avg_points": yesterday["average_points"] if yesterday else None,
            "source": "local_live_feed_one_minute_candles",
            "pivot_formula": "R1=opening_high; S1=opening_low; AVG=(R1+S1)/2; R2/R3 and S2/S3 extend by half_range",
        }

    def update(self, record: dict[str, Any]) -> dict[str, Any]:
        """Atomically merge one instrument record into pivot_levels.json."""
        trade_date = str(record.get("trade_date_ist") or "")
        instrument_key = str(record.get("instrument_key") or "")
        if not trade_date or not instrument_key:
            raise ValueError("trade_date_ist and instrument_key are required")
        with self._lock:
            payload: dict[str, Any] = {"schema_version": 1, "trade_dates": {}}
            try:
                if self.path.exists():
                    loaded = json.loads(self.path.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        payload.update(loaded)
            except (OSError, json.JSONDecodeError):
                logger.exception("Could not read existing pivot-level file %s", self.path)
            trade_dates = payload.get("trade_dates")
            if not isinstance(trade_dates, dict):
                trade_dates = {}
                payload["trade_dates"] = trade_dates
            day_data = trade_dates.get(trade_date)
            if not isinstance(day_data, dict):
                day_data = {"instruments": {}}
                trade_dates[trade_date] = day_data
            instruments = day_data.get("instruments")
            if not isinstance(instruments, dict):
                instruments = {}
                day_data["instruments"] = instruments
            instruments[instrument_key] = record
            payload["updated_at_ist"] = datetime.now(IST).isoformat()
            fd, temp_name = tempfile.mkstemp(
                prefix="pivot_levels.", suffix=".tmp", dir=str(self.path.parent)
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump(payload, stream, indent=2, ensure_ascii=False, default=str)
                Path(temp_name).replace(self.path)
            except Exception:
                Path(temp_name).unlink(missing_ok=True)
                raise
        return record

    def get(self, instrument_key: str, trade_date: str) -> dict[str, Any] | None:
        with self._lock:
            try:
                payload = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return None
            day = (payload.get("trade_dates") or {}).get(trade_date) or {}
            return ((day.get("instruments") or {}).get(instrument_key))
