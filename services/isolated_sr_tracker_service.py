# services/isolated_sr_tracker_service.py
"""Watch SR-level confirmations for the currently isolated instrument.

Lifecycle
---------
1. ``level_isolated_instrument_service._isolate()`` calls
   ``on_isolation(isolated_record)``.
2. The tracker snapshots every level currently returned by
   ``live_sr_levels_service.get_levels(key)`` as ``pre_isolation``.
3. Every time ``live_sr_levels_service._append_level()`` confirms a
   **new** pivot for the tracked instrument, it calls
   ``on_sr_level_confirmed(key, level)``.
4. The tracker records the level as ``post_isolation``, persists the
   sidecar file, and (optionally) sends a Telegram message.
5. ``reset()`` clears state when the isolation record is reset.

Persistence
-----------
Sidecar file, separate from ``level_isolated_instrument.json`` — the
isolation state file is written atomically by the isolation service,
so this tracker must not touch it.

    data/level_isolated_instrument_sr.json

Dedup
-----
Levels are deduped on ``(level_type, price, confirmed_time)``.  This
means:
  * re-seeding ``live_sr_levels_service`` does not double-count pivots;
  * a level already snapshotted as pre-isolation is never re-added as
    post-isolation;
  * a genuinely new pivot (new candle) that happens to share a price
    with an older one is still recorded because ``confirmed_time``
    differs.

Dashboard output
----------------
``get_detail()`` returns both ``pre_isolation_levels`` and
``post_isolation_levels`` **sorted by price ascending** (supports and
resistances intermixed). There is no broken / intact distinction —
every confirmed pivot is retained for the day.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from core import config
from core.logger import get_logger

logger = get_logger(__file__)

_MARKET_TZ = ZoneInfo("Asia/Kolkata")

_DIV = "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
_THIN = "────────────────────────────────"

# --------------------------------------------------------------------------- #
# Small helpers                                                               #
# --------------------------------------------------------------------------- #
def _today_iso() -> str:
    return datetime.now(_MARKET_TZ).date().isoformat()

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def _now_market_iso() -> str:
    return datetime.now(_MARKET_TZ).isoformat()

def _strip_legacy_fields(level: dict[str, Any]) -> dict[str, Any]:
    """Return a shallow copy of ``level`` without any broken-* fields."""
    if not isinstance(level, dict):
        return {}
    out = dict(level)
    out.pop("broken", None)
    out.pop("broken_at", None)
    out.pop("broken_price", None)
    out.pop("broken_bar_index", None)
    return out

# --------------------------------------------------------------------------- #
# Service                                                                     #
# --------------------------------------------------------------------------- #
class IsolatedSRTrackerService:
    """Track SR-level confirmations for the currently isolated instrument."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._state: dict[str, Any] = self._empty_state()
        self._seen_keys: set[tuple] = set()
        self._load_or_create_state()

    # ------------------------------------------------------------------ #
    # State file I/O                                                     #
    # ------------------------------------------------------------------ #
    def _state_path(self) -> Path:
        return Path(config.LIVE_DATA_DIR) / config.LEVEL_ISOLATION_SR_FILENAME

    def _empty_state(self) -> dict[str, Any]:
        return {
            "day": _today_iso(),
            "instrument_key": None,
            "trading_symbol": None,
            "isolated_at": None,
            "isolated_reason": None,
            "pre_isolation_levels": [],
            "post_isolation_levels": [],
            "pre_isolation_count": 0,
            "post_isolation_count": 0,
            "first_post_isolation_seen": False,
            "last_update": None,
        }

    def _load_or_create_state(self) -> None:
        path = self._state_path()
        if not path.exists():
            return
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("[isolated-sr] unreadable state file")
            return
        if not isinstance(loaded, dict):
            return
        if str(loaded.get("day") or "") != _today_iso():
            return

        # Strip any residual broken fields persisted by an older build.
        for phase in ("pre_isolation_levels", "post_isolation_levels"):
            cleaned = [
                _strip_legacy_fields(lvl)
                for lvl in (loaded.get(phase) or [])
                if isinstance(lvl, dict)
            ]
            loaded[phase] = cleaned

        self._state = loaded
        self._seen_keys = set()
        for phase in ("pre_isolation_levels", "post_isolation_levels"):
            for lvl in self._state.get(phase) or []:
                self._seen_keys.add(self._dedup_key(lvl))

    def _save_state(self) -> None:
        path = self._state_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._state["last_update"] = _now_iso()
            self._state["pre_isolation_count"] = len(
                self._state.get("pre_isolation_levels") or []
            )
            self._state["post_isolation_count"] = len(
                self._state.get("post_isolation_levels") or []
            )
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(self._state, indent=2, default=str),
                encoding="utf-8",
            )
            tmp.replace(path)
        except Exception:
            logger.exception("[isolated-sr] failed saving state")

    def _ensure_today(self) -> None:
        """Reset state if the calendar day has rolled over."""
        today = _today_iso()
        if str(self._state.get("day") or "") != today:
            self._state = self._empty_state()
            self._state["day"] = today
            self._seen_keys = set()

    # ------------------------------------------------------------------ #
    # Dedup                                                              #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _dedup_key(level: dict[str, Any]) -> tuple:
        try:
            price = round(float(level.get("price") or 0.0), 4)
        except (TypeError, ValueError):
            price = 0.0
        return (
            str(level.get("level") or ""),
            price,
            str(level.get("confirmed_time") or ""),
        )

    # ------------------------------------------------------------------ #
    # Public fast-path check                                             #
    # ------------------------------------------------------------------ #
    def is_tracking(self, instrument_key: str) -> bool:
        """Return True iff we are watching ``instrument_key`` for *today*."""
        key = str(instrument_key or "")
        if not key:
            return False
        if str(self._state.get("day") or "") != _today_iso():
            return False
        return str(self._state.get("instrument_key") or "") == key

    # ------------------------------------------------------------------ #
    # Isolation hook                                                     #
    # ------------------------------------------------------------------ #
    def on_isolation(self, isolated_record: dict[str, Any]) -> None:
        """Called by ``level_isolated_instrument_service._isolate``.

        Snapshots every SR level currently present for the isolated
        instrument as **pre-isolation**.  Idempotent when the same
        instrument is already being tracked for today.
        """
        with self._lock:
            self._ensure_today()

            key = str(isolated_record.get("instrument_key") or "").strip()
            if not key:
                return

            if str(self._state.get("instrument_key") or "") == key:
                logger.info(
                    "[isolated-sr] already tracking %s for today; skipping re-init",
                    key,
                )
                return

            # Fresh state for the newly isolated instrument.
            self._state = self._empty_state()
            self._state["day"] = _today_iso()
            self._state["instrument_key"] = key
            self._state["trading_symbol"] = isolated_record.get("trading_symbol")
            self._state["isolated_at"] = (
                isolated_record.get("isolated_at") or _now_market_iso()
            )
            self._state["isolated_reason"] = isolated_record.get("reason")
            self._seen_keys = set()

            # Snapshot existing SR levels as pre-isolation.
            for lvl in self._snapshot_current_levels(key):
                record = _strip_legacy_fields(lvl)
                record["_phase"] = "pre_isolation"
                self._state["pre_isolation_levels"].append(record)
                self._seen_keys.add(self._dedup_key(record))

            self._state["first_post_isolation_seen"] = False
            self._save_state()

            logger.info(
                "[isolated-sr] tracking started — key=%s symbol=%s pre_levels=%d",
                key,
                isolated_record.get("trading_symbol"),
                len(self._state["pre_isolation_levels"]),
            )

    def _snapshot_current_levels(self, instrument_key: str) -> list[dict[str, Any]]:
        """Return the live SR engine's current levels for ``instrument_key``."""
        try:
            from services.live_sr_levels_service import live_sr_levels_service
            payload = live_sr_levels_service.get_levels(instrument_key)
            if isinstance(payload, dict):
                levels = payload.get("levels") or []
                if isinstance(levels, list):
                    return [dict(x) for x in levels if isinstance(x, dict)]
        except Exception:
            logger.exception(
                "[isolated-sr] snapshot current levels failed for %s",
                instrument_key,
            )
        return []

    # ------------------------------------------------------------------ #
    # Confirmation hook                                                  #
    # ------------------------------------------------------------------ #
    def on_sr_level_confirmed(
        self,
        instrument_key: str,
        level: dict[str, Any],
    ) -> None:
        """Called by ``live_sr_levels_service._append_level``."""
        with self._lock:
            self._ensure_today()

            if str(self._state.get("instrument_key") or "") != str(instrument_key or ""):
                return

            if not isinstance(level, dict):
                return

            dedup = self._dedup_key(level)
            if dedup in self._seen_keys:
                return
            self._seen_keys.add(dedup)

            record = _strip_legacy_fields(level)
            record["_phase"] = "post_isolation"
            self._state["post_isolation_levels"].append(record)

            is_first = not self._state.get("first_post_isolation_seen")
            self._state["first_post_isolation_seen"] = True
            self._save_state()

            logger.info(
                "[isolated-sr] POST-ISOLATION level confirmed — "
                "key=%s type=%s price=%s confirmed=%s first=%s",
                instrument_key,
                level.get("level"),
                level.get("price"),
                level.get("confirmed_time"),
                is_first,
            )

            if config.LEVEL_ISOLATION_SR_TELEGRAM_ENABLED:
                threading.Thread(
                    target=self._send_telegram,
                    args=(record, is_first),
                    daemon=True,
                    name="isolated-sr-telegram",
                ).start()

    # ------------------------------------------------------------------ #
    # Telegram                                                           #
    # ------------------------------------------------------------------ #
    def _send_telegram(self, level: dict[str, Any], is_first: bool) -> None:
        try:
            from services.telegram_service import telegram_service

            title = (
                "Isolated Instrument · First SR Level Confirmed"
                if is_first
                else "Isolated Instrument · New SR Level Confirmed"
            )
            phase_line = (
                "🆕 Phase:          POST-ISOLATION (first)\n"
                if is_first
                else "🆕 Phase:          POST-ISOLATION\n"
            )
            msg = (
                f"{_DIV}\n"
                f"🎯 {('FIRST ' if is_first else '')}SR LEVEL CONFIRMED\n"
                f"{_DIV}\n"
                f"📌 Instrument:     "
                f"{self._state.get('trading_symbol') or self._state.get('instrument_key')}\n"
                f"🔑 Key:            {self._state.get('instrument_key')}\n"
                f"📊 Level Type:     {str(level.get('level') or '').upper()}\n"
                f"💰 Price:          {level.get('price')}\n"
                f"🕒 Pivot Time:     {level.get('pivot_time')}\n"
                f"✅ Confirmed At:   {level.get('confirmed_time')}\n"
                f"📏 Bars:           L={level.get('left_bars')} R={level.get('right_bars')}\n"
                f"{phase_line}"
                f"{_THIN}\n"
                f"📈 Post-Isolation Levels: "
                f"{len(self._state.get('post_isolation_levels') or [])}\n"
                f"📉 Pre-Isolation Levels:  "
                f"{len(self._state.get('pre_isolation_levels') or [])}\n"
                f"🎯 Isolation Reason:      {self._state.get('isolated_reason')}\n"
                f"🕒 Isolated At:           {self._state.get('isolated_at')}\n"
                f"💾 Saved to data/{config.LEVEL_ISOLATION_SR_FILENAME}"
            )
            telegram_service.send_message(title, msg, level="SUCCESS")
        except Exception:
            logger.exception("[isolated-sr] telegram send failed")

    # ------------------------------------------------------------------ #
    # Dashboard / API                                                    #
    # ------------------------------------------------------------------ #
    def get_detail(self) -> dict[str, Any]:
        """Return a JSON-safe snapshot for the dashboard / admin API.

        ``pre_isolation_levels`` and ``post_isolation_levels`` are both
        returned **sorted by price ascending**. There is no broken /
        intact distinction; every confirmed pivot is retained.
        """
        with self._lock:
            self._ensure_today()
            pre = [
                _strip_legacy_fields(x)
                for x in (self._state.get("pre_isolation_levels") or [])
                if isinstance(x, dict)
            ]
            post = [
                _strip_legacy_fields(x)
                for x in (self._state.get("post_isolation_levels") or [])
                if isinstance(x, dict)
            ]
            detail: dict[str, Any] = {
                "enabled": bool(config.LEVEL_ISOLATION_ENABLED),
                "day": self._state.get("day"),
                "instrument_key": self._state.get("instrument_key"),
                "trading_symbol": self._state.get("trading_symbol"),
                "isolated_at": self._state.get("isolated_at"),
                "isolated_reason": self._state.get("isolated_reason"),
                "pre_isolation_levels": pre,
                "post_isolation_levels": post,
                "pre_isolation_count": len(pre),
                "post_isolation_count": len(post),
                "first_post_isolation_seen": bool(
                    self._state.get("first_post_isolation_seen")
                ),
                "waiting_for_first_confirmation": (
                    bool(self._state.get("instrument_key"))
                    and not self._state.get("first_post_isolation_seen")
                ),
                "last_update": self._state.get("last_update"),
                "generated_at": _now_iso(),
            }

        # Sort both lists by price ascending — TradingView behaviour,
        # supports and resistances intermixed.
        detail["pre_isolation_levels"].sort(
            key=lambda l: float(l.get("price") or 0.0)
        )
        detail["post_isolation_levels"].sort(
            key=lambda l: float(l.get("price") or 0.0)
        )

        return detail

    # ------------------------------------------------------------------ #
    # Reset                                                              #
    # ------------------------------------------------------------------ #
    def reset(self) -> dict[str, Any]:
        with self._lock:
            self._state = self._empty_state()
            self._state["day"] = _today_iso()
            self._seen_keys = set()
            self._save_state()
        logger.info("[isolated-sr] state reset")
        return {"status": "reset", "day": self._state.get("day")}

isolated_sr_tracker_service = IsolatedSRTrackerService()