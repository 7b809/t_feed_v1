# File: services/strategy_state_service.py
"""Day-level strategy gate and state.

Determines which strategy is active for the day and tracks whether
Strategy 1 has fired (order placed). Persists state per day under
``meta_data/strategy_state_YYYY-MM-DD.json`` so restart-safe.

Gate logic
----------
After ``first_candle_service`` completes for the day:

* If ``computed == 0`` (all instruments failed)
    -> ``first_candle_status = "failed"``
    -> no strategy is active
    -> operator must retry via ``POST /admin/ops/first-candle``.

* If ``computed < total`` (some instruments failed)
    -> ``first_candle_status = "partial"``
    -> strategy gate proceeds normally based on ``crosses_detected``.

* If ``computed == total`` (all instruments succeeded)
    -> ``first_candle_status = "ok"``
    -> if **no instrument** has ``ema_cross_at_first_candle.detected``:
        -> Strategy 2 activates immediately.
    -> if **any instrument** has ``detected = True``:
        -> Strategy 1 activates with an 11:00 IST cutoff window.
        -> If 11:00 passes with no Strategy 1 order placed, Strategy 2
           activates and can scan touches from the whole day.

Strategy 1 "fired" means an order was successfully placed. Once fired,
Strategy 2 is suppressed for the day.

Late-start rule (NEW)
---------------------
If this Python process first imported inside
``STRATEGY1_STARTUP_SKIP_FROM_*`` .. ``STRATEGY1_STARTUP_SKIP_TO_*``
(default 09:30–15:00 IST) on a trading day, Strategy 1 is disabled
for that day and Strategy 2 is activated immediately. Rationale: by
09:30 the S1 setup window has largely passed and a late "catch-up"
order risks more than falling back to the S2 R3/S3 pair detector.

The decision is made by ``apply_startup_gate()``, called from
``first_candle_service.run()`` **before** the per-instrument loop.
This ordering matters because the replay in ``_one()`` fires strategy
hooks — those hooks need to see the skip flag to avoid placing an S1
order on a replayed touch.

Re-fire pass
------------
After the gate is set at the end of ``run()``, ``on_first_candle_complete``
re-fires strategy hooks for all touches already recorded. This is what
makes a mid-session backfill correct: historic R2/S2/R3/S3 touches
that were recorded *before* the gate was known are re-evaluated against
the now-active strategy. Both strategy services dedupe internally
(``strategy1_service._fired_today``, ``strategy2_service._emitted``),
so the pass is idempotent — safe to run on the scheduled 09:16:10
pass, a manual retry, and a startup backfill alike.

Daily reset: state is keyed by date; a new date auto-resets in memory
and on disk.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from core import config
from core.logger import get_logger

logger = get_logger(__file__)

_TZ = ZoneInfo("Asia/Kolkata")
_STATE_DIR = Path(config.PROJECT_ROOT) / "meta_data"

# Captured once at module load time. This is effectively the process
# start time because main.py imports this module during startup (via
# routes.admin_routes → first_candle_service → strategy_state_service).
_PROCESS_STARTED_AT: datetime = datetime.now(_TZ)


def _today_iso() -> str:
    return datetime.now(_TZ).date().isoformat()


def _state_path(day: str) -> Path:
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    return _STATE_DIR / f"strategy_state_{day}.json"


def _skip_window_bounds() -> tuple[dtime, dtime]:
    """Return (from, to) for the late-start skip window from config."""
    from_t = dtime(
        getattr(config, "STRATEGY1_STARTUP_SKIP_FROM_HOUR", 9),
        getattr(config, "STRATEGY1_STARTUP_SKIP_FROM_MINUTE", 30),
    )
    to_t = dtime(
        getattr(config, "STRATEGY1_STARTUP_SKIP_TO_HOUR", 15),
        getattr(config, "STRATEGY1_STARTUP_SKIP_TO_MINUTE", 0),
    )
    return from_t, to_t


def _should_skip_strategy1_today() -> bool:
    """True when this process started inside the skip window today.

    * ``_PROCESS_STARTED_AT.date() != today``  -> False (started on a
      prior day; today is a normal day for this process).
    * ``_PROCESS_STARTED_AT.time()`` inside ``[from, to)`` -> True.
    * otherwise                                -> False.

    Set ``STRATEGY1_STARTUP_SKIP_FROM_HOUR == STRATEGY1_STARTUP_SKIP_TO_HOUR``
    (and the minutes) to disable the rule entirely — ``[from, to)``
    becomes empty and no start time is ever inside it.
    """
    started = _PROCESS_STARTED_AT
    today = datetime.now(_TZ).date()
    if started.date() != today:
        return False
    from_t, to_t = _skip_window_bounds()
    return from_t <= started.time() < to_t


def _empty_state(day: str) -> dict[str, Any]:
    return {
        "day": day,
        "first_candle_status": None,       # ok | partial | failed | skipped
        "strategy1_skip_reason": None,     # late_start | None
        "strategy1_eligible": False,
        "strategy1_active": False,
        "strategy1_fired": False,
        "strategy1_fired_at": None,
        "strategy1_target_key": None,
        "strategy1_source": None,
        "strategy1_error": None,
        "strategy2_active": False,
        "strategy2_activated_at": None,
        "nifty_avg": None,
        "sensex_avg": None,
        "cross_true_count": 0,
        "computed_count": 0,
        "total_count": 0,
        "computed_at": None,
        "process_started_at": None,
        "process_started_date": None,
    }


class StrategyStateService:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: dict[str, Any] | None = None

    # ------------------------------------------------------------------ #
    # Persistence                                                        #
    # ------------------------------------------------------------------ #
    def _load(self, day: str) -> dict[str, Any]:
        path = _state_path(day)
        if not path.exists():
            return _empty_state(day)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or payload.get("day") != day:
                return _empty_state(day)
            return payload
        except Exception:
            logger.exception("[strategy-state] unreadable %s", path)
            return _empty_state(day)

    def _write(self, state: dict[str, Any]) -> None:
        path = _state_path(state["day"])
        try:
            path.write_text(
                json.dumps(state, indent=2, default=str),
                encoding="utf-8",
            )
        except Exception:
            logger.exception("[strategy-state] write failed for %s", path)

    def _ensure_today(self) -> dict[str, Any]:
        today = _today_iso()
        if self._state is None or self._state.get("day") != today:
            self._state = self._load(today)
        return self._state

    # ------------------------------------------------------------------ #
    # Read helpers                                                       #
    # ------------------------------------------------------------------ #
    def get_state(self) -> dict[str, Any]:
        """Return the day's strategy state.

        Enriched with process-start diagnostics that are computed on
        read (not persisted): ``process_started_at``,
        ``process_started_date``, ``skip_window_from``,
        ``skip_window_to``, ``should_skip_today``. This makes
        ``/admin/ops/strategy-state`` immediately informative about
        why Strategy 1 is or is not active.
        """
        with self._lock:
            state = dict(self._ensure_today())

        from_t, to_t = _skip_window_bounds()
        state.setdefault(
            "process_started_at", _PROCESS_STARTED_AT.isoformat(),
        )
        state.setdefault(
            "process_started_date", _PROCESS_STARTED_AT.date().isoformat(),
        )
        state["skip_window_from"] = f"{from_t.hour:02d}:{from_t.minute:02d}"
        state["skip_window_to"] = f"{to_t.hour:02d}:{to_t.minute:02d}"
        state["should_skip_today"] = _should_skip_strategy1_today()
        return state

    def nifty_avg(self) -> float | None:
        with self._lock:
            val = self._ensure_today().get("nifty_avg")
            try:
                return float(val) if val is not None else None
            except (TypeError, ValueError):
                return None

    def sensex_avg(self) -> float | None:
        with self._lock:
            val = self._ensure_today().get("sensex_avg")
            try:
                return float(val) if val is not None else None
            except (TypeError, ValueError):
                return None

    def is_strategy1_fired(self) -> bool:
        with self._lock:
            return bool(self._ensure_today().get("strategy1_fired"))

    def is_strategy1_skipped_today(self) -> bool:
        with self._lock:
            state = self._ensure_today()
            return state.get("strategy1_skip_reason") == "late_start"

    def is_strategy1_window_open(self) -> bool:
        """True when Strategy 1 can act on a touch right now.

        Requires all of:
          * ``STRATEGY1_ENABLED``
          * today is a trading day
          * current time is inside [09:16, cutoff)
          * ``strategy1_active`` is set in state (i.e. first_candle has
            determined cross detection and activated S1)
          * the late-start skip flag is NOT set
        """
        if not getattr(config, "STRATEGY1_ENABLED", True):
            return False
        if not self._is_day_active():
            return False
        now = datetime.now(_TZ).time()
        open_t = dtime(9, 16)
        cutoff = dtime(
            getattr(config, "STRATEGY1_CUTOFF_HOUR", 11),
            getattr(config, "STRATEGY1_CUTOFF_MINUTE", 0),
        )
        if not (open_t <= now < cutoff):
            return False
        with self._lock:
            state = self._ensure_today()
            if state.get("strategy1_skip_reason") == "late_start":
                return False
            if not state.get("strategy1_active"):
                return False
        return True

    def is_strategy2_window_open(self) -> bool:
        if not getattr(config, "STRATEGY2_ENABLED", True):
            return False
        if not self._is_day_active():
            return False
        with self._lock:
            state = self._ensure_today()
            if not state.get("strategy2_active"):
                return False
        now = datetime.now(_TZ).time()
        open_t = dtime(9, 16)
        end_t = dtime(
            getattr(config, "STRATEGY2_END_HOUR", 15),
            getattr(config, "STRATEGY2_END_MINUTE", 25),
        )
        return open_t <= now <= end_t

    def _is_day_active(self) -> bool:
        now = datetime.now(_TZ)
        if now.weekday() > 4:
            return False
        try:
            from services.market_holiday_service import market_holiday_service
            payload = market_holiday_service.get_today()
            if str(payload.get("status") or "").lower() == "holiday":
                return False
        except Exception:
            pass
        return True

    # ------------------------------------------------------------------ #
    # Late-start gate (called before first_candle loop)                  #
    # ------------------------------------------------------------------ #
    def apply_startup_gate(self) -> dict[str, Any]:
        """Persist the late-start skip decision for today, if applicable.

        Idempotent — safe to call from ``first_candle_service.run()``
        on every reason (``scheduled``, ``startup_backfill``, ``manual``).

        The decision is based on ``_PROCESS_STARTED_AT``, not on the
        current time. So running this method at, say, 11:00 on a
        process that started at 08:00 is a no-op (skip=False). Running
        it at 10:00 on a process that started at 10:00 sets skip=True.

        The skip decision is persisted to today's state file so
        subsequent calls and restarts within the same day see the same
        outcome.

        When the skip flag is set, ``strategy2_active`` is flipped to
        True immediately. This is what lets the replay inside
        ``first_candle_service._one()`` correctly fire S2 hooks for
        historic R3/S3 touches — without the early activation, the
        replay would record touches but no S2 hook would fire on them.
        """
        with self._lock:
            state = self._ensure_today()

            # Always record process-start metadata (informational).
            state["process_started_at"] = _PROCESS_STARTED_AT.isoformat()
            state["process_started_date"] = (
                _PROCESS_STARTED_AT.date().isoformat()
            )

            if not _should_skip_strategy1_today():
                self._write(state)
                return dict(state)

            # Idempotent: do not re-log if already set.
            if state.get("strategy1_skip_reason") == "late_start":
                self._write(state)
                return dict(state)

            state["strategy1_skip_reason"] = "late_start"
            state["strategy1_eligible"] = False
            state["strategy1_active"] = False
            # Do not undo a legitimate S1 fire from an earlier day/run.
            if not state.get("strategy1_fired"):
                state["strategy2_active"] = True
                if not state.get("strategy2_activated_at"):
                    state["strategy2_activated_at"] = (
                        datetime.now(_TZ).isoformat()
                    )

            self._write(state)

            from_t, to_t = _skip_window_bounds()
            logger.warning(
                "[strategy-state] LATE START — process started at %s IST, "
                "inside skip window %02d:%02d–%02d:%02d. "
                "Strategy 1 DISABLED for today; Strategy 2 activated.",
                _PROCESS_STARTED_AT.strftime("%H:%M:%S"),
                from_t.hour, from_t.minute,
                to_t.hour, to_t.minute,
            )
            return dict(state)

    # ------------------------------------------------------------------ #
    # First-candle completion hook                                       #
    # ------------------------------------------------------------------ #
    def on_first_candle_complete(
        self,
        summary: dict[str, Any],
    ) -> dict[str, Any]:
        """Set the day's strategy gate after first-candle computation.

        Called from ``first_candle_service.run()``. Idempotent.

        Classification:
          * ``skipped == True``   -> first_candle_status="skipped"
          * ``computed == 0``     -> first_candle_status="failed"
          * ``computed < total``  -> first_candle_status="partial"
          * otherwise             -> first_candle_status="ok"

        Strategies activate only on "ok" / "partial". The "failed"
        state leaves both strategies inactive so the operator sees a
        clear signal instead of a phantom "S2 active" with no data.

        Late-start skip (``strategy1_skip_reason == "late_start"``)
        forces S2 activation regardless of the cross count, provided
        the first-candle data is usable (ok/partial).

        After the gate is written, a re-fire pass is executed outside
        the lock so historic touches recorded before the gate was set
        are re-evaluated against the now-active strategy. This is what
        makes mid-session backfills correct.
        """
        result_state: dict[str, Any] | None = None
        refire_needed = False

        with self._lock:
            state = self._ensure_today()
            now = datetime.now(_TZ)
            state["computed_at"] = now.isoformat()

            skipped = bool(summary.get("skipped"))
            total = int(summary.get("total") or 0)
            computed = int(summary.get("computed") or 0)
            cross_true_count = int(summary.get("crosses_detected") or 0)

            state["computed_count"] = computed
            state["total_count"] = total
            state["cross_true_count"] = cross_true_count

            # Classify the outcome.
            if skipped:
                fc_status = "skipped"
            elif computed == 0:
                fc_status = "failed"
            elif computed < total:
                fc_status = "partial"
            else:
                fc_status = "ok"
            state["first_candle_status"] = fc_status

            # Record skip reason if applicable (independent of fc_status;
            # the operator should still see that S1 was intentionally
            # skipped even if the run failed).
            if _should_skip_strategy1_today():
                state["strategy1_skip_reason"] = "late_start"

            if fc_status in ("failed", "skipped"):
                # No usable OR data — no strategy can act.
                state["strategy1_eligible"] = False
                state["strategy1_active"] = False
                state["strategy2_active"] = False
                self._write(state)
                result_state = dict(state)
                logger.warning(
                    "[strategy-state] gate: first_candle_status=%s "
                    "(computed=%d total=%d) — no strategy activated",
                    fc_status, computed, total,
                )
            else:
                # Read NIFTY / SENSEX first-candle closes for range filters.
                nifty_avg, sensex_avg = (
                    self._read_index_first_candle_closes()
                )
                if nifty_avg is not None:
                    state["nifty_avg"] = nifty_avg
                if sensex_avg is not None:
                    state["sensex_avg"] = sensex_avg

                if state.get("strategy1_skip_reason") == "late_start":
                    # Late-start today: S1 is skipped regardless of
                    # cross state. S2 is already active (set by
                    # apply_startup_gate). Reaffirm for safety.
                    state["strategy1_eligible"] = False
                    state["strategy1_active"] = False
                    if not state.get("strategy1_fired"):
                        state["strategy2_active"] = True
                        if not state.get("strategy2_activated_at"):
                            state["strategy2_activated_at"] = (
                                now.isoformat()
                            )
                    self._write(state)
                    result_state = dict(state)
                    logger.info(
                        "[strategy-state] gate: S1 SKIPPED (late start) "
                        "→ S2 active (fc=%s, cross=%d)",
                        fc_status, cross_true_count,
                    )
                elif cross_true_count == 0:
                    # No cross detected anywhere -> S2 from now.
                    state["strategy1_eligible"] = False
                    state["strategy1_active"] = False
                    if not state.get("strategy1_fired"):
                        state["strategy2_active"] = True
                        if not state.get("strategy2_activated_at"):
                            state["strategy2_activated_at"] = (
                                now.isoformat()
                            )
                    self._write(state)
                    result_state = dict(state)
                    refire_needed = True
                    logger.info(
                        "[strategy-state] gate: no cross → S2 active "
                        "(fc=%s)", fc_status,
                    )
                else:
                    # At least one instrument has a detected cross.
                    state["strategy1_eligible"] = True
                    cutoff_t = dtime(
                        getattr(
                            config, "STRATEGY1_CUTOFF_HOUR", 11,
                        ),
                        getattr(
                            config, "STRATEGY1_CUTOFF_MINUTE", 0,
                        ),
                    )
                    already_fired = bool(state.get("strategy1_fired"))

                    if (
                        now.time() < cutoff_t
                        and not already_fired
                    ):
                        state["strategy1_active"] = True
                        logger.info(
                            "[strategy-state] gate: cross=%d → S1 active "
                            "(cutoff %02d:%02d)",
                            cross_true_count,
                            cutoff_t.hour, cutoff_t.minute,
                        )
                    else:
                        state["strategy1_active"] = False
                        if not already_fired:
                            state["strategy2_active"] = True
                            if not state.get("strategy2_activated_at"):
                                state["strategy2_activated_at"] = (
                                    now.isoformat()
                                )
                            logger.info(
                                "[strategy-state] gate: cross=%d but "
                                "past cutoff / already_fired → S2 active",
                                cross_true_count,
                            )
                        else:
                            logger.info(
                                "[strategy-state] gate: S1 already fired "
                                "at %s — S2 remains inactive",
                                state.get("strategy1_fired_at"),
                            )

                    self._write(state)
                    result_state = dict(state)
                    refire_needed = True

        # Re-fire pass OUTSIDE the lock so strategy hooks can call
        # back into strategy_state_service.is_strategy*_window_open()
        # without deadlocking on the same lock.
        if refire_needed and result_state is not None:
            try:
                self._refire_touches_through_hooks()
            except Exception:
                logger.exception(
                    "[strategy-state] refire pass failed"
                )

        return result_state or {}

    def _read_index_first_candle_closes(
        self,
    ) -> tuple[float | None, float | None]:
        """Read 09:15 close for NIFTY 50 and SENSEX from first_candle.json."""
        from services.first_candle_service import _folder_for_entry
        from services.instrument_registry import instrument_registry

        def _close_of(instrument_key: str) -> float | None:
            entry = instrument_registry.get(instrument_key)
            if not entry:
                return None
            try:
                folder = _folder_for_entry(entry)
                p = folder / "first_candle.json"
                if not p.exists():
                    return None
                payload = json.loads(p.read_text(encoding="utf-8"))
                fc = payload.get("first_candle") or {}
                close = fc.get("close")
                return float(close) if close is not None else None
            except Exception:
                logger.exception(
                    "[strategy-state] index close read failed for %s",
                    instrument_key,
                )
                return None

        return (
            _close_of("NSE_INDEX|Nifty 50"),
            _close_of("BSE_INDEX|SENSEX"),
        )

    # ------------------------------------------------------------------ #
    # Re-fire existing touches (post-gate)                               #
    # ------------------------------------------------------------------ #
    def _refire_touches_through_hooks(self) -> int:
        """Re-fire strategy hooks for every recorded touch.

        Called after the gate is set so historic touches recorded
        before the gate was known are re-evaluated against the now
        active strategy. Both strategy services dedupe internally
        (S1 via ``_fired_today``, S2 via ``_emitted``), so this is
        idempotent — safe on the scheduled 09:16:10 pass, manual
        retry, and startup backfill alike.

        Returns the number of hook invocations made (each touch fires
        both S1 and S2 hooks).
        """
        try:
            from services.level_touch_tracker_service import (
                level_touch_tracker_service,
            )
            all_touches = level_touch_tracker_service.get_all_touches()
        except Exception:
            logger.exception(
                "[strategy-state] refire: could not read touches"
            )
            return 0

        fires = 0
        for key, touches in all_touches.items():
            for level_name, touch_data in touches.items():
                ltp = touch_data.get("ltp") if isinstance(
                    touch_data, dict,
                ) else None
                if ltp is None:
                    continue
                try:
                    ltp_f = float(ltp)
                except (TypeError, ValueError):
                    continue

                try:
                    from services.strategy1_service import (
                        strategy1_service,
                    )
                    strategy1_service.on_level_touch(
                        key, level_name, ltp_f,
                    )
                    fires += 1
                except Exception:
                    logger.exception(
                        "[strategy-state] refire S1 failed for %s %s",
                        key, level_name,
                    )
                try:
                    from services.strategy2_service import (
                        strategy2_service,
                    )
                    strategy2_service.on_level_touch(
                        key, level_name, ltp_f,
                    )
                    fires += 1
                except Exception:
                    logger.exception(
                        "[strategy-state] refire S2 failed for %s %s",
                        key, level_name,
                    )

        logger.info(
            "[strategy-state] refire pass complete — %d hook invocations",
            fires,
        )
        return fires

    # ------------------------------------------------------------------ #
    # Strategy 1 fired / error hooks                                     #
    # ------------------------------------------------------------------ #
    def on_strategy1_fired(
        self,
        *,
        target_key: str,
        source: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Record that Strategy 1 placed an order. Suppresses Strategy 2."""
        with self._lock:
            state = self._ensure_today()
            state["strategy1_fired"] = True
            state["strategy1_fired_at"] = datetime.now(_TZ).isoformat()
            state["strategy1_target_key"] = target_key
            state["strategy1_source"] = source
            # Strategy 2 must not run after S1 has fired.
            state["strategy2_active"] = False
            self._write(state)
            logger.info(
                "[strategy-state] Strategy 1 FIRED: target=%s source=%s",
                target_key, source,
            )

    def on_strategy1_error(self, error: str) -> None:
        with self._lock:
            state = self._ensure_today()
            state["strategy1_error"] = error
            self._write(state)

    # ------------------------------------------------------------------ #
    # Strategy 1 cutoff hook (called by 11:00 scheduler task)            #
    # ------------------------------------------------------------------ #
    def on_strategy1_cutoff_reached(self) -> dict[str, Any]:
        """At 11:00, if Strategy 1 didn't fire, activate Strategy 2.

        If the late-start skip rule applied, S2 is already active; this
        method is a no-op that preserves the skip flag so the cutoff
        task wrapper can suppress its misleading "S2 now active"
        Telegram message.
        """
        with self._lock:
            state = self._ensure_today()

            if state.get("strategy1_skip_reason") == "late_start":
                logger.info(
                    "[strategy-state] cutoff at 11:00 — S1 skipped "
                    "(late start), S2 already active"
                )
                return dict(state)

            if bool(state.get("strategy1_fired")):
                logger.info(
                    "[strategy-state] cutoff at 11:00 — "
                    "Strategy 1 already fired"
                )
                return dict(state)

            if state.get("strategy1_eligible"):
                state["strategy1_active"] = False
                state["strategy2_active"] = True
                if not state.get("strategy2_activated_at"):
                    state["strategy2_activated_at"] = (
                        datetime.now(_TZ).isoformat()
                    )
                self._write(state)
                logger.info(
                    "[strategy-state] cutoff at 11:00 — "
                    "Strategy 2 activated"
                )
            else:
                # No S1 eligibility -> S2 was already active
                logger.info(
                    "[strategy-state] cutoff at 11:00 — "
                    "S1 not eligible, S2 state unchanged"
                )
            return dict(state)


strategy_state_service = StrategyStateService()