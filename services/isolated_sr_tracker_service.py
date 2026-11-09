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

Timestamps (post-2026-11-05)
----------------------------
Every tracked isolated instrument records two timestamps:

    * ``isolated_time``       — the *effective event time* of the
                                isolation. In live mode this is the
                                promotion moment (``isolated_at``).
                                In backfill replay — i.e. the process
                                started hours after the real market
                                event — this is clamped down to
                                ``touched_at`` so the downstream
                                POST-11:30 buffer check anchors
                                against the REAL market moment
                                (e.g. 12:59) instead of the
                                process-start timestamp (e.g.
                                23:27).
    * ``isolated_touch_time`` — the moment price first TOUCHED the
                                isolating level (ISO string, IST, may
                                be None if the upstream isolation
                                record did not carry a touch time).

Both are:
  * persisted in the sidecar state file,
  * surfaced by ``get_detail()`` and the dedicated
    ``get_active_isolated_state()`` view, and
  * forwarded to the strategy-entry signal pipeline (see
    ``services/strategy_entry_signal_service.py``) so the POST-11:30
    regime gate compares against the actual event moment.

Backfill-replay correction (2026-11-05)
---------------------------------------
The pre-fix build stored ``isolated_time = isolated_at``, which in a
backfill replay equals the process-startup time (e.g. ``23:27``)
rather than the real event time (e.g. ``12:59``). Every downstream
``min_confirm_time = isolated_time − 10 min`` check then used
``23:17`` instead of ``12:49``, so real trigger levels at ``13:03``
were rejected with a misleading ``no_matching_sr_level`` reason.

Two complementary corrections are applied:

  1. ``_extract_isolated_time_with_source`` prefers ``touched_at``
     whenever it is **strictly earlier** than ``isolated_at``. This
     is the correct discriminator — in live mode the promotion and
     touch are seconds apart, whereas in a backfill replay the
     promotion is hours after the touch.

  2. ``_load_or_create_state`` re-applies the same heuristic to any
     on-disk state file written by the buggy build, so a mid-day
     restart picks up the fix without requiring a manual ``reset()``.
     The correction only fires when the gap exceeds
     ``_BACKFILL_GAP_THRESHOLD_SECONDS`` (30 minutes) — orders of
     magnitude larger than any legitimate live-mode latency, but far
     smaller than the hours-long backfill gap.

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

# Threshold (seconds) above which an ``isolated_time`` that is LATER
# than ``isolated_touch_time`` is treated as a backfill-replay
# artefact rather than a legitimate promotion-vs-touch gap.
#
#   * Live mode  : promotion is typically 0–300 s after the touch.
#   * Backfill   : promotion is 6–10 HOURS after the touch.
#
# 30 minutes cleanly separates the two cases without risking a false
# positive on a slow live-mode detection.
_BACKFILL_GAP_THRESHOLD_SECONDS = 1800

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

def _coerce_iso(value: Any) -> str | None:
    """Best-effort normalisation of a timestamp-ish value to ISO string."""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=_MARKET_TZ)
        return value.isoformat()
    text = str(value).strip()
    return text or None

def _parse_iso(value: Any) -> datetime | None:
    """Parse an ISO string / datetime / numeric epoch into a tz-aware
    datetime.

    Used by the backfill-replay detector to compare ``touched_at`` and
    ``isolated_at``.  Never raises — an unparseable value returns
    ``None`` so the caller can fall through to the next preference.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=_MARKET_TZ)
        return value.astimezone(_MARKET_TZ)
    try:
        n = float(value)
        if n > 1e12:
            n /= 1000.0
        if n > 1e9:
            return datetime.fromtimestamp(n, tz=_MARKET_TZ)
    except (TypeError, ValueError):
        pass
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_MARKET_TZ)
    return dt.astimezone(_MARKET_TZ)

def _extract_touch_time(record: dict[str, Any]) -> str | None:
    """Return the best-guess "price first touched the isolating level"
    timestamp from an isolation record.

    The isolation service has used several field names across versions,
    so we probe the common ones in priority order and normalise the
    result to an ISO string.
    """
    for candidate in (
        "isolated_touch_time",
        "touch_time",
        "touched_at",
        "level_touch_time",
        "trigger_time",
        "pivot_time",
    ):
        if candidate in record:
            normalised = _coerce_iso(record.get(candidate))
            if normalised:
                return normalised
    return None

def _extract_isolated_time_with_source(
    record: dict[str, Any],
) -> tuple[str, str]:
    """Return ``(iso_value, source_label)`` for the effective event time.

    Preference order (per the 2026-11-05 fix spec):

        (a) ``touched_at`` when it parses AND is STRICTLY earlier than
            ``isolated_at`` — the backfill-replay case where
            ``isolated_at`` was captured at process startup, hours
            after the real market event. Using the touch time here is
            the whole point of the fix: it re-anchors the downstream
            POST-11:30 buffer window to the correct minute (e.g.
            12:59) instead of the process start (e.g. 23:27).

        (b) An explicit ``isolated_time`` (or its legacy aliases
            ``promoted_at`` / ``promotion_time``).

        (c) ``isolated_at``.

        (d) ``now()`` as a last resort so downstream consumers always
            have a value.

    The returned ``source_label`` is logged verbatim by
    ``on_isolation`` so operators can audit WHICH field drove the
    value on any given day — critical when debugging a
    backfill-vs-live mismatch (e.g. "isolated_time=12:59
    source='touched_at (earlier than isolated_at)'" is a clear signal
    that the backfill correction fired).
    """
    touched_at = _coerce_iso(record.get("touched_at"))
    isolated_at = _coerce_iso(record.get("isolated_at"))

    # (a) Backfill-replay detection.
    if touched_at and isolated_at:
        touched_dt = _parse_iso(touched_at)
        isolated_dt = _parse_iso(isolated_at)
        if (
            touched_dt is not None
            and isolated_dt is not None
            and touched_dt < isolated_dt
        ):
            return touched_at, "touched_at (earlier than isolated_at)"

    # (b) Explicit isolated_time + legacy aliases.
    for candidate in ("isolated_time", "promoted_at", "promotion_time"):
        if candidate in record:
            normalised = _coerce_iso(record.get(candidate))
            if normalised:
                return normalised, candidate

    # (c) isolated_at.
    if isolated_at:
        return isolated_at, "isolated_at"

    # (d) Fallback — now().
    return _now_market_iso(), "now_fallback"

def _extract_isolated_time(record: dict[str, Any]) -> str | None:
    """Legacy wrapper — returns only the resolved timestamp.

    Kept for backward compatibility with any external caller that
    imported the private helper directly.  New code should call
    ``_extract_isolated_time_with_source`` so the source label is
    available for logging.
    """
    value, _source = _extract_isolated_time_with_source(record)
    return value

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
            # --- 2026-11-05 timestamp additions ---------------------- #
            # ``isolated_time``       — effective event time (see
            #                           module docstring for the
            #                           backfill-replay semantics).
            # ``isolated_touch_time`` — when price first touched the
            #                           isolating level (may be None).
            "isolated_time": None,
            "isolated_touch_time": None,
            # --------------------------------------------------------- #
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

        # --- 2026-11-05 backfill ------------------------------------- #
        # Older state files will not carry the new timestamp keys.
        # Backfill them so downstream consumers always see the fields,
        # even if their values are None.
        loaded.setdefault("isolated_time", loaded.get("isolated_at"))
        loaded.setdefault("isolated_touch_time", None)
        # ------------------------------------------------------------- #

        # --- 2026-11-05 in-place correction -------------------------- #
        # A state file written by the pre-fix build will have
        # ``isolated_time == isolated_at`` — i.e. the process-startup
        # timestamp (e.g. 23:27) — while ``isolated_touch_time`` holds
        # the REAL market event time (e.g. 12:59). If the gap is
        # larger than the backfill threshold, clamp ``isolated_time``
        # down to ``isolated_touch_time`` so the POST-11:30 buffer
        # check uses the correct anchor.
        iso_time_raw = loaded.get("isolated_time")
        iso_touch_raw = loaded.get("isolated_touch_time")
        if iso_time_raw and iso_touch_raw:
            iso_time_dt = _parse_iso(iso_time_raw)
            iso_touch_dt = _parse_iso(iso_touch_raw)
            if (
                iso_time_dt is not None
                and iso_touch_dt is not None
                and (iso_time_dt - iso_touch_dt).total_seconds()
                > _BACKFILL_GAP_THRESHOLD_SECONDS
            ):
                logger.warning(
                    "[isolated-sr] correcting stale isolated_time on load "
                    "— key=%s stored=%s touch=%s gap=%.0f min "
                    "(likely pre-fix backfill pollution) — "
                    "isolated_time reset to isolated_touch_time",
                    loaded.get("instrument_key"),
                    iso_time_raw,
                    iso_touch_raw,
                    (iso_time_dt - iso_touch_dt).total_seconds() / 60.0,
                )
                loaded["isolated_time"] = iso_touch_raw
        # ------------------------------------------------------------- #

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

        Also records the two lifecycle timestamps requested by the
        POST-11:30 strategy:

            * ``isolated_time``       — effective event time. Prefers
                                        ``touched_at`` when it is
                                        earlier than ``isolated_at``
                                        (backfill-replay case); falls
                                        back through the preference
                                        chain described in
                                        ``_extract_isolated_time_with_source``.
            * ``isolated_touch_time`` — when price first touched the
                                        isolating level (may be None
                                        if the upstream record did not
                                        carry it).

        The startup log line prints BOTH values plus the source label
        the effective ``isolated_time`` came from, so future backfill
        runs are auditable.
        """
        with self._lock:
            self._ensure_today()

            key = str(isolated_record.get("instrument_key") or "").strip()
            if not key:
                return

            if str(self._state.get("instrument_key") or "") == key:
                # Re-init guard: keep the timestamps we already captured
                # (they are the authoritative "first isolation" of the
                # day) but refresh the isolated_reason if the upstream
                # record now provides one.
                if (
                    not self._state.get("isolated_reason")
                    and isolated_record.get("reason")
                ):
                    self._state["isolated_reason"] = isolated_record.get("reason")
                    self._save_state()
                logger.info(
                    "[isolated-sr] already tracking %s for today; skipping re-init",
                    key,
                )
                return

            # --- 2026-11-05 timestamps ------------------------------- #
            # Use the source-aware extractor so the log line can print
            # WHICH field drove the effective event time.
            isolated_time, isolated_time_source = (
                _extract_isolated_time_with_source(isolated_record)
            )
            isolated_touch_time = _extract_touch_time(isolated_record)
            # --------------------------------------------------------- #

            # Fresh state for the newly isolated instrument.
            self._state = self._empty_state()
            self._state["day"] = _today_iso()
            self._state["instrument_key"] = key
            self._state["trading_symbol"] = isolated_record.get("trading_symbol")
            self._state["isolated_at"] = (
                isolated_record.get("isolated_at") or _now_market_iso()
            )
            # 2026-11-05 — persist the corrected effective event time.
            self._state["isolated_time"] = isolated_time
            self._state["isolated_touch_time"] = isolated_touch_time
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

            # Enhanced startup log — prints BOTH timestamps + the
            # source label for the effective ``isolated_time`` so a
            # backfill-vs-live mismatch is instantly visible in the
            # log stream.
            logger.info(
                "[isolated-sr] tracking started — key=%s symbol=%s "
                "pre_levels=%d isolated_time=%s (source=%s) "
                "isolated_touch_time=%s isolated_at=%s",
                key,
                isolated_record.get("trading_symbol"),
                len(self._state["pre_isolation_levels"]),
                isolated_time,
                isolated_time_source,
                isolated_touch_time,
                self._state.get("isolated_at"),
            )

    def _snapshot_current_levels(self, instrument_key: str) -> list[dict[str, Any]]:
        """Return the live SR engine's current levels for ``instrument_key``."""
        try:
            from services.external_sr_websocket_service import external_sr_websocket_service
            payload = external_sr_websocket_service.get_levels(instrument_key, "15/3")
            if isinstance(payload, dict) and payload.get("is_ready"):
                levels = payload.get("levels") or []
                if isinstance(levels, list):
                    return [dict(x) for x in levels if isinstance(x, dict)]
        except Exception:
            logger.exception(
                "[isolated-sr] external SR snapshot failed for %s",
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
        """Called by the external EMA V2 WebSocket on confirmed SR updates."""
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
                "key=%s type=%s price=%s confirmed=%s confirmed_price=%s first=%s",
                instrument_key,
                level.get("level"),
                level.get("price"),
                level.get("confirmed_time"),
                level.get("confirmed_time_price"),
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
            confirmed_price_line = ""
            if level.get("confirmed_time_price") is not None:
                confirmed_price_line = f"💵 Confirmed Price: {level.get('confirmed_time_price')}\n"

            # 2026-11-05 — surface the two lifecycle timestamps.
            iso_time_line = (
                f"⏱ Isolated At:      {self._state.get('isolated_time')}\n"
                if self._state.get("isolated_time")
                else ""
            )
            touch_time_line = (
                f"👆 Touch Time:      {self._state.get('isolated_touch_time')}\n"
                if self._state.get("isolated_touch_time")
                else ""
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
                f"{confirmed_price_line}"
                f"📏 Bars:           L={level.get('left_bars')} R={level.get('right_bars')}\n"
                f"{iso_time_line}"
                f"{touch_time_line}"
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

        Also exposes the two lifecycle timestamps
        (``isolated_time`` / ``isolated_touch_time``) introduced on
        2026-11-05 so the dashboard and the strategy-entry signal
        service can consume them directly.
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
                # --- 2026-11-05 timestamp additions ------------------ #
                "isolated_time": self._state.get("isolated_time"),
                "isolated_touch_time": self._state.get("isolated_touch_time"),
                # ----------------------------------------------------- #
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

    def get_active_isolated_state(self) -> dict[str, Any]:
        """Focused snapshot for the strategy-entry signal pipeline.

        This is the dict handed to
        ``services.strategy_entry_signal_service.evaluate_isolated_entry()``
        (via ``_build_instrument_data``) and any other consumer that
        needs the current isolated instrument.  It includes the two
        lifecycle timestamps so the caller can compare ``now`` against
        the actual event moment instead of guessing from a wall-clock
        cutoff.

        Returns a JSON-safe dict.  Fields:

            day                       — calendar day (IST) the state
                                        belongs to.
            instrument_key            — the isolated underlying's key.
            trading_symbol            — human-readable symbol.
            isolated_at               — **legacy debug field**: raw
                                        promotion timestamp as
                                        recorded by the isolation
                                        service. In a backfill
                                        replay this is the
                                        process-start time (e.g.
                                        23:27), so it must NOT be
                                        used as a regime anchor.
            isolated_time             — **effective event time**.
                                        Live mode: equals the
                                        promotion moment. Backfill
                                        replay: clamped to
                                        ``isolated_touch_time`` so
                                        the POST-11:30 buffer check
                                        anchors against the real
                                        market minute. Consumers
                                        that compute regime
                                        boundaries MUST read this
                                        field, not ``isolated_at``.
            isolated_touch_time       — when price first touched the
                                        isolating level (may be None).
            isolated_reason           — why it was isolated.
            first_post_isolation_seen — whether any post-isolation
                                        SR level has been confirmed
                                        yet.
            pre_isolation_count       — number of pre-isolation levels.
            post_isolation_count      — number of post-isolation levels.
            last_update               — last persistence timestamp.

        Field semantics (2026-11-05 clarification)
        ------------------------------------------
        ``isolated_time`` is the effective event time of the isolation:

            * Live mode → equals the promotion moment
                          (``isolated_at``).
            * Backfill  → clamped down to ``touched_at`` so the
                          downstream POST-11:30 buffer check anchors
                          against the real market minute (e.g.
                          12:59) rather than the process-start
                          timestamp (e.g. 23:27).

        ``isolated_at`` is kept as a legacy debug field carrying the
        raw process-recorded promotion time (which may be the startup
        timestamp in a backfill replay).  New code should read
        ``isolated_time``, not ``isolated_at``, when computing regime
        boundaries or buffer windows.
        """
        with self._lock:
            self._ensure_today()
            return {
                "day": self._state.get("day"),
                "instrument_key": self._state.get("instrument_key"),
                "trading_symbol": self._state.get("trading_symbol"),
                # Legacy debug field — do NOT use as a regime anchor.
                "isolated_at": self._state.get("isolated_at"),
                # --- 2026-11-05 timestamp additions ------------------ #
                # Effective event time (backfill-replay corrected).
                "isolated_time": self._state.get("isolated_time"),
                "isolated_touch_time": self._state.get("isolated_touch_time"),
                # ----------------------------------------------------- #
                "isolated_reason": self._state.get("isolated_reason"),
                "first_post_isolation_seen": bool(
                    self._state.get("first_post_isolation_seen")
                ),
                "pre_isolation_count": len(
                    self._state.get("pre_isolation_levels") or []
                ),
                "post_isolation_count": len(
                    self._state.get("post_isolation_levels") or []
                ),
                "last_update": self._state.get("last_update"),
            }

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