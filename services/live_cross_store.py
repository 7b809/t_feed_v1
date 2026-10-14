"""Persist live EMA crosses for the current day per instrument.

Path per instrument:

    data/<underlying>/<strike>_<TYPE>/live_crosses.json
    data/index/<underlying>/live_crosses.json

The file holds only today's data. When a cross arrives for a new day,
the file is replaced (it never grows across days).

Within a single minute we may see multiple crosses for the same
instrument (EMA wiggle). We keep only the *last* cross observed for
that minute — the file is keyed by minute timestamp.

Cross record shape (matches the candle-backup ema_crosses.json):

    {
      "timestamp": "2026-10-14T15:37:00+05:30",
      "type": "bullish",
      "fast_ema": 155.3814,
      "slow_ema": 155.1767,
      "volume": 163800.0,
      "volume_diff": 30251.0,
      "ema_diff": 0.2047,
      "score": 4,
      "max_score": 6,
      "valid": true,
      "filters": {...}
    }
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core import config

logger = logging.getLogger(__name__)


class LiveCrossStore:
    def __init__(self) -> None:
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()
        # instrument_key -> (day_str, path, {minute_ts: record})
        self._state: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------ #
    # Internals                                                          #
    # ------------------------------------------------------------------ #
    def _lock_for(self, instrument_key: str) -> threading.Lock:
        with self._guard:
            lock = self._locks.get(instrument_key)
            if lock is None:
                lock = threading.Lock()
                self._locks[instrument_key] = lock
            return lock

    @staticmethod
    def _path_for(entry: dict[str, Any]) -> Path:
        ema_path = Path(entry["ema_path"])
        return ema_path.with_name(config.LIVE_CROSSES_FILENAME)

    @staticmethod
    def _minute_key(ts: str) -> str:
        """Normalise an ISO timestamp to its minute bucket."""
        try:
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        except Exception:
            return str(ts)
        return dt.replace(second=0, microsecond=0).isoformat()

    @staticmethod
    def _day_key(ts: str) -> str:
        try:
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        except Exception:
            return "unknown"
        return dt.strftime("%Y-%m-%d")

    # ------------------------------------------------------------------ #
    # Public API                                                         #
    # ------------------------------------------------------------------ #
    def save(self, entry: dict[str, Any], cross: dict[str, Any]) -> dict[str, Any]:
        """Store a cross, overwriting any earlier cross for the same minute.

        Returns the current day's file payload after the write.
        """
        key = entry["instrument_key"]
        path = self._path_for(entry)
        minute = self._minute_key(cross.get("timestamp", ""))
        day = self._day_key(cross.get("timestamp", ""))

        with self._lock_for(key):
            state = self._state.get(key)
            if state is None or state["day"] != day:
                # New day (or first write) — reset state and file.
                state = {
                    "day": day,
                    "path": path,
                    "records": {},
                }
                self._state[key] = state
                # Wipe the file for the new day so stale data is gone.
                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.unlink(missing_ok=True)
                except Exception:
                    logger.exception("Failed clearing live_crosses for %s", key)

            state["records"][minute] = cross
            payload = {
                "instrument_key": key,
                "underlying": entry.get("underlying"),
                "strike_price": entry.get("strike_price"),
                "strike_type": entry.get("strike_type"),
                "expiry": entry.get("expiry"),
                "trading_symbol": entry.get("trading_symbol"),
                "day": day,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "cross_count": len(state["records"]),
                "crosses": sorted(
                    state["records"].values(),
                    key=lambda c: str(c.get("timestamp", "")),
                ),
            }
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(path.suffix + ".tmp")
                tmp.write_text(
                    json.dumps(payload, indent=2, default=str),
                    encoding="utf-8",
                )
                tmp.replace(path)
            except Exception:
                logger.exception("Failed writing live_crosses.json for %s", key)

            return payload

    def read_today(self, entry: dict[str, Any]) -> dict[str, Any]:
        """Return the on-disk payload for the given instrument (may be empty)."""
        key = entry["instrument_key"]
        path = self._path_for(entry)
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                logger.exception("Unreadable live_crosses.json: %s", path)
        return {
            "instrument_key": key,
            "cross_count": 0,
            "crosses": [],
        }

    def reset_all(self) -> None:
        """Called by daily refresh — drop all in-memory state."""
        with self._guard:
            self._state.clear()


live_cross_store = LiveCrossStore()