# File: services/level_touch_tracker_service.py
"""Records the first time each instrument's live LTP touches an OR level.

Feeds from ``live_feed_service`` (called from ``_handle_feed``). Records
R1/R2/R3/S1/S2/S3/R3_Threshold/S3_Threshold per instrument per day.

Persists to ``meta_data/level_touches_YYYY-MM-DD.json``.

Strategy services (strategy1_service, strategy2_service) are notified
via their ``on_level_touch`` hooks. They decide whether to act based on
their own active-state and range filters.

One touch per level per instrument per day (first-touch wins).

Timestamp support (replay)
--------------------------
``on_tick`` accepts an optional ``timestamp`` parameter. When provided,
the touch record's ``ts`` field is set to that value instead of the
current wall-clock time. This lets ``first_candle_service`` replay
today's historical intraday candles through the tracker and preserve
the true candle-time for each touch — useful after a mid-session
restart where live ticks were missed between 09:16 and the restart.

Day rollover (Gap #5 fix)
-------------------------
Previously ``_levels`` (the cached OR levels) persisted across day
boundaries. On a live tick arriving after midnight, the tracker reset
``_touches`` but kept yesterday's levels, producing false touches
against stale level values.

This revision clears ``_levels`` on a genuine day rollover (i.e. when
``_day`` was already set to a *different* day). On the very first tick
of a fresh process (``_day is None``), the levels are NOT cleared
because ``first_candle_service`` may have already seeded them for
today before any tick arrives.

Both ``set_or_levels`` and ``on_tick`` share the same rollover helper
``_rollover_if_needed`` so the two entry points always agree on which
day the tracker is tracking.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from core import config
from core.logger import get_logger

logger = get_logger(__file__)

_TZ = ZoneInfo("Asia/Kolkata")
_TOUCH_DIR = Path(config.PROJECT_ROOT) / "meta_data"

_LEVELS_UP = ("R1", "R2", "R3", "R3_Threshold")
_LEVELS_DOWN = ("S1", "S2", "S3", "S3_Threshold")
_ALL_LEVELS = _LEVELS_UP + _LEVELS_DOWN


def _today_iso() -> str:
    return datetime.now(_TZ).date().isoformat()


def _touch_path(day: str) -> Path:
    _TOUCH_DIR.mkdir(parents=True, exist_ok=True)
    return _TOUCH_DIR / f"level_touches_{day}.json"


class LevelTouchTrackerService:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._day: str | None = None
        # instrument_key -> { level_name: float }
        self._levels: dict[str, dict[str, float]] = {}
        # instrument_key -> { level_name: {"ltp", "ts", "level_value"} }
        self._touches: dict[str, dict[str, dict[str, Any]]] = {}

    # ------------------------------------------------------------------ #
    # Day rollover helper (shared by set_or_levels and on_tick)          #
    # ------------------------------------------------------------------ #
    def _rollover_if_needed(self) -> None:
        """Ensure the tracker is tracking today.

        Must be called while holding ``self._lock``.

        * ``_day is None`` (fresh process):
            initialize ``_day`` and load today's touches from disk.
            Do **not** clear ``_levels`` — ``first_candle_service`` may
            have already seeded them for today before the first tick
            arrives (mid-session startup case).
        * ``_day != today`` (genuine day rollover):
            clear stale ``_levels`` from the previous day, set
            ``_day = today``, reload touches.
        * otherwise: no-op.
        """
        today = _today_iso()
        if self._day is None:
            self._day = today
            self._touches = self._load_touches(today)
            logger.info(
                "[level-tracker] initialized for %s (%d cached levels)",
                today, len(self._levels),
            )
        elif self._day != today:
            logger.info(
                "[level-tracker] day rollover %s -> %s (clearing %d cached levels)",
                self._day, today, len(self._levels),
            )
            self._day = today
            self._levels = {}
            self._touches = self._load_touches(today)

    # ------------------------------------------------------------------ #
    # Data feed from first_candle_service                                #
    # ------------------------------------------------------------------ #
    def set_or_levels(
        self,
        instrument_key: str,
        or_levels: dict[str, Any] | None,
    ) -> None:
        """Cache the OR levels for one instrument (called by first_candle)."""
        if not instrument_key or not or_levels:
            return
        clean: dict[str, float] = {}
        for name in _ALL_LEVELS:
            value = or_levels.get(name)
            if value is None:
                continue
            try:
                clean[name] = float(value)
            except (TypeError, ValueError):
                continue
        if not clean:
            return
        with self._lock:
            self._rollover_if_needed()
            self._levels[instrument_key] = clean

    # ------------------------------------------------------------------ #
    # Tick handler (from live_feed_service or replay)                    #
    # ------------------------------------------------------------------ #
    def on_tick(
        self,
        instrument_key: str,
        ltp: float,
        timestamp: str | None = None,
    ) -> None:
        """Record level touches for one tick. Never raises.

        ``timestamp`` is optional. When provided (e.g. during replay of
        historical candles), it becomes the touch record's ``ts`` field
        instead of ``datetime.now()``. Strategy services currently do
        not consume ``ts`` for their own logic — this is purely for
        audit accuracy in ``level_touches_<day>.json``.
        """
        if not instrument_key or ltp is None:
            return
        try:
            ltp = float(ltp)
        except (TypeError, ValueError):
            return
        if ltp <= 0:
            return

        ts_str = str(timestamp or "").strip() or datetime.now(_TZ).isoformat()

        new_touches: list[tuple[str, float]] = []
        with self._lock:
            self._rollover_if_needed()
            levels = self._levels.get(instrument_key)
            if not levels:
                return
            touched = self._touches.setdefault(instrument_key, {})

            for name in _LEVELS_UP:
                if name in touched:
                    continue
                value = levels.get(name)
                if value is None:
                    continue
                if ltp >= value:
                    touched[name] = {
                        "ltp": ltp,
                        "ts": ts_str,
                        "level_value": value,
                    }
                    new_touches.append((name, ltp))
            for name in _LEVELS_DOWN:
                if name in touched:
                    continue
                value = levels.get(name)
                if value is None:
                    continue
                if ltp <= value:
                    touched[name] = {
                        "ltp": ltp,
                        "ts": ts_str,
                        "level_value": value,
                    }
                    new_touches.append((name, ltp))

            if new_touches:
                try:
                    self._write_touches(self._day or _today_iso())
                except Exception:
                    logger.exception("[level-tracker] persist failed")

        # Fire hooks outside the lock to avoid deadlock with strategy
        # services (which may, in turn, call back into the tracker).
        for level_name, hit_ltp in new_touches:
            self._fire_hooks(instrument_key, level_name, hit_ltp)

    def _fire_hooks(self, key: str, level: str, ltp: float) -> None:
        try:
            from services.strategy1_service import strategy1_service
            strategy1_service.on_level_touch(key, level, ltp)
        except Exception:
            logger.exception(
                "[level-tracker] strategy1 hook failed for %s %s", key, level,
            )
        try:
            from services.strategy2_service import strategy2_service
            strategy2_service.on_level_touch(key, level, ltp)
        except Exception:
            logger.exception(
                "[level-tracker] strategy2 hook failed for %s %s", key, level,
            )

    # ------------------------------------------------------------------ #
    # Reseed from disk (mid-session restart helper)                      #
    # ------------------------------------------------------------------ #
    def reseed_from_disk(self) -> dict[str, int]:
        """Re-load today's OR levels from ``first_candle.json`` files.

        Useful after a mid-session process restart when
        ``first_candle_service`` is skipped (because today's
        ``first_candle.json`` already exists) but the in-memory tracker
        is empty in the fresh process.

        Scans ``data/**/first_candle.json``, reads each file whose
        ``date`` matches today, and re-seeds ``_levels`` via
        ``set_or_levels``.

        NOTE: this only re-seeds levels, not touches. To recover touches
        that occurred between 09:16 and the restart, use
        ``POST /admin/ops/first-candle`` (or the startup backfill) which
        re-runs ``first_candle_service`` and replays intraday candles
        through ``on_tick``.

        Returns a summary dict:
            {"scanned": int, "reseeded": int, "skipped": int, "errors": int}
        """
        from pathlib import Path as _Path

        root = _Path(config.LIVE_DATA_DIR)
        result = {"scanned": 0, "reseeded": 0, "skipped": 0, "errors": 0}
        if not root.exists():
            logger.warning("[level-tracker] reseed: %s missing", root)
            return result

        today = _today_iso()
        for path in root.rglob("first_candle.json"):
            result["scanned"] += 1
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                result["errors"] += 1
                continue

            if str(payload.get("date") or "") != today:
                result["skipped"] += 1
                continue

            key = str(payload.get("instrument_key") or "").strip()
            or_levels = payload.get("or_levels")
            if not key or not isinstance(or_levels, dict):
                result["errors"] += 1
                continue

            self.set_or_levels(key, or_levels)
            result["reseeded"] += 1

        logger.info(
            "[level-tracker] reseed_from_disk — scanned=%d reseeded=%d "
            "skipped=%d errors=%d",
            result["scanned"], result["reseeded"],
            result["skipped"], result["errors"],
        )
        return result

    # ------------------------------------------------------------------ #
    # Read accessors                                                     #
    # ------------------------------------------------------------------ #
    def get_touches(self, instrument_key: str) -> dict[str, dict[str, Any]]:
        with self._lock:
            return dict(self._touches.get(instrument_key) or {})

    def get_all_touches(self) -> dict[str, dict[str, dict[str, Any]]]:
        with self._lock:
            return {k: dict(v) for k, v in self._touches.items()}

    def get_levels(self, instrument_key: str) -> dict[str, float] | None:
        with self._lock:
            v = self._levels.get(instrument_key)
            return dict(v) if v else None

    def level_count(self) -> int:
        with self._lock:
            return len(self._levels)

    def get_day_snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "day": self._day,
                "instrument_count": len(self._levels),
                "touched_instrument_count": len(self._touches),
                "touches": {k: dict(v) for k, v in self._touches.items()},
            }

    # ------------------------------------------------------------------ #
    # Persistence                                                        #
    # ------------------------------------------------------------------ #
    def _load_touches(self, day: str) -> dict[str, dict[str, dict[str, Any]]]:
        path = _touch_path(day)
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                return {}
            if payload.get("day") != day:
                return {}
            touches = payload.get("touches")
            return touches if isinstance(touches, dict) else {}
        except Exception:
            logger.exception("[level-tracker] unreadable %s", path)
            return {}

    def _write_touches(self, day: str) -> None:
        path = _touch_path(day)
        payload = {
            "day": day,
            "updated_at": datetime.now(_TZ).isoformat(),
            "touches": self._touches,
        }
        path.write_text(
            json.dumps(payload, indent=2, default=str),
            encoding="utf-8",
        )


level_touch_tracker_service = LevelTouchTrackerService()