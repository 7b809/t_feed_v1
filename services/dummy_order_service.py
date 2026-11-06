# services/dummy_order_service.py
"""Post-isolation dummy order execution engine — unified standard Path A.

All pre-11:30 isolation sources use the same standard execution strategy.
The selected isolated bias-side instrument is monitored for post-isolation
15/3 S/R confirmation and existing same/opposite-leg routing. OR-grid price
eligibility is not part of entry execution. Configured stop-loss and the
dynamic target / post-target Resistance exit are preserved.

Standard Path A dynamic target milestone:
    * 0 < entry <= 100: entry + entry / 3
    * 100 < entry <= 200: entry + entry / 4
    * entry > 200: entry + entry / 5

The target is not an immediate exit. Stop-loss remains active while the
engine waits for a NEW 15/3 Resistance confirmation strictly after the
milestone, then exits at that confirmation candle's 1-minute close.
EOD handling is unchanged.

This service creates dummy orders only. Optional broker execution consumes
persisted order transitions through sandbox_execution_service.

POST-11:30 remains a separate time-based strategy with its existing entry,
target, stop-loss and 15/15 Resistance exit behavior.
"""


from __future__ import annotations

import json
import math
import threading
import time
from datetime import datetime, time as dtime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from core import config
from core.logger import get_logger

logger = get_logger(__file__)

# --------------------------------------------------------------------------- #
# Time helpers                                                                #
# --------------------------------------------------------------------------- #
def _market_tz() -> ZoneInfo:
    try:
        return ZoneInfo(config.MARKET_TIMEZONE)
    except Exception:
        return ZoneInfo("Asia/Kolkata")


def _now_market() -> datetime:
    return datetime.now(_market_tz())


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today_iso() -> str:
    return _now_market().date().isoformat()


def _parse_iso(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_market_tz())
    return dt.astimezone(_market_tz())


def _iso(dt: datetime) -> str:
    return dt.astimezone(_market_tz()).isoformat()


def _fmt_time(iso_val: Any) -> str:
    dt = _parse_iso(iso_val)
    return dt.strftime("%H:%M:%S IST") if dt else "—"


# --------------------------------------------------------------------------- #
# Numeric helpers                                                             #
# --------------------------------------------------------------------------- #
def _safe_float(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _dynamic_target_for_entry(entry_price: float) -> tuple[float, int]:
    """Return the fixed Path A target and divisor for a positive entry price.

    0 < entry <= 100: entry + entry / 3
    100 < entry <= 200: entry + entry / 4
    entry > 200: entry + entry / 5
    """
    price = float(entry_price)
    if not math.isfinite(price) or price <= 0:
        raise ValueError(f"entry_price must be finite and positive: {entry_price!r}")
    divisor = 3 if price <= 100.0 else 4 if price <= 200.0 else 5
    return round(price + price / divisor, 4), divisor

def _fmt_price(v: Any, fmt: str = ".4f") -> str:
    f = _safe_float(v)
    return format(f, fmt) if f is not None else "none"


# --------------------------------------------------------------------------- #
# [CALC] logging helpers (log-only)                                           #
# --------------------------------------------------------------------------- #
def _log_calc_routing(
    trigger: dict[str, Any],
    routing_mode: str,
    traded_key: str,
    traded_symbol: str | None,
    prior_support: dict[str, Any] | None,
    reversal_cutoff: datetime | None,
) -> None:
    post_conf = _fmt_time(trigger.get("confirmed_time"))
    pivot = _fmt_time(trigger.get("pivot_time"))
    cutoff_txt = reversal_cutoff.strftime("%H:%M:%S IST") if reversal_cutoff else "—"
    prior_txt = "none"
    if prior_support:
        prior_txt = (
            f"price={_fmt_price(prior_support.get('price'))} "
            f"confirmed={_fmt_time(prior_support.get('confirmed_time'))}"
        )
    logger.info(
        "[CALC][ROUTING] mode=%s\n"
        "    TRIGGER   type=%s price=%s pivot=%s confirmed=%s\n"
        "    PRIOR-S   %s\n"
        "    CUTOFF    %s\n"
        "    TRADED    %s (%s)",
        routing_mode,
        str(trigger.get("level") or "").upper(),
        _fmt_price(trigger.get("price")),
        pivot, post_conf,
        prior_txt,
        cutoff_txt,
        traded_key, traded_symbol or "—",
    )


def _log_calc_post_1130(
    order: dict[str, Any],
    reference_type: str | None,
    reference_price: float | None,
) -> None:
    """Informational log line for a POST-1130 order creation."""
    trigger = order.get("trigger") or {}
    logger.info(
        "[CALC][POST_1130] regime=POST_1130_ISOLATED\n"
        "    REF-TYPE  %s @ ₹%s\n"
        "    PIVOT     %s\n"
        "    CONFIRM   %s\n"
        "    ENTRY     ₹%s (source=%s)\n"
        "    STOP      ₹%s (−%.1f%%)\n"
        "    TARGET    ₹%s (+%s pts)\n"
        "    EXIT-SR   L=%s R=%s",
        str(reference_type or "—").upper(),
        _fmt_price(reference_price),
        _fmt_time(trigger.get("pivot_time")),
        _fmt_time(trigger.get("confirmed_time")),
        _fmt_price(order.get("entry_price")),
        order.get("entry_price_source") or "—",
        _fmt_price(order.get("stop_price")),
        float(order.get("stop_pct") or 0.0) * 100.0,
        _fmt_price(order.get("target_price")),
        order.get("target_points") if order.get("target_points") is not None else "—",
        (order.get("exit_sr_config") or [15, 15])[0],
        (order.get("exit_sr_config") or [15, 15])[1],
    )


# --------------------------------------------------------------------------- #
# Close-reason → transition-type mapping (module-level)                       #
# --------------------------------------------------------------------------- #
# Only closes whose `closed_reason` appears in this map are surfaced to
# the external transition callback.  SKIPPED is deliberately absent — a
# skipped order is not an actionable event for downstream consumers.
#
# POST-1130 close reasons (2026-11-05) are mapped onto the existing
# vocabulary so the sandbox executor's translation table treats them
# uniformly — no sandbox change is required.
_CLOSE_REASON_TO_TRANSITION: dict[str, str] = {
    "STOP_HIT":                 "STOP_HIT",
    "TARGET_TRAIL_STOP_HIT":    "STOP_HIT",
    "STAGE_2_RESISTANCE_EXIT":  "STAGE_2_RESISTANCE_EXIT",
    "TARGET_MILESTONE_RESISTANCE_EXIT": "STAGE_2_RESISTANCE_EXIT",
    "NEXT_15_3_RESISTANCE_EXIT": "NEXT_15_3_RESISTANCE_EXIT",
    "END_OF_DAY":               "EOD_CLOSE",
    # ---- POST-1130 exit reasons (2026-11-05) ----------------------- #
    # Mapped to the closest existing transition label so the sandbox
    # translation table (CREATED → place, STOP_HIT / *_EXIT / EOD →
    # exit_all) handles them without further changes.
    "STOPLOSS_30_PCT_HIT":          "STOP_HIT",
    "TARGET_50_POINTS_HIT":         "STAGE_2_RESISTANCE_EXIT",
    "RESISTANCE_15_15_CONFIRMED":   "STAGE_2_RESISTANCE_EXIT",
}


# --------------------------------------------------------------------------- #
# Service                                                                     #
# --------------------------------------------------------------------------- #
class DummyOrderService:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._state: dict[str, Any] = self._empty_state()
        # One-shot warning latch for a missing readiness check on the
        # upstream sr_backfill_service (kept so the fail-open fallback
        # does not spam the log on every 30 s scheduler tick).
        self._warned_no_readiness_check: bool = False
        # Post-persistence transition callback (optional).
        # Signature: fn(transition_type, order_record, reason)
        # Set via set_transition_callback(); see module docstring.
        self._transition_callback: Any = None
        self._load_from_disk()

    # ------------------------------------------------------------------ #
    # Public API                                                         #
    # ------------------------------------------------------------------ #
    def set_transition_callback(self, fn: Any) -> None:
        """Register a post-persistence transition callback.

        ``fn`` must be callable with signature
            fn(transition_type: str, order_record: dict, reason: str)
        or ``None`` to clear.  The callback fires AFTER the state file
        is written, synchronously inside ``run_once``'s lock.  It must
        not block for more than a few hundred milliseconds — slow work
        (broker I/O, fill polling) must be handed off to a worker.
        """
        if fn is not None and not callable(fn):
            logger.warning(
                "[dummy-order] set_transition_callback: argument is "
                "not callable — ignored (type=%s)",
                type(fn).__name__,
            )
            return
        with self._lock:
            self._transition_callback = fn
            logger.info(
                "[dummy-order] transition callback %s",
                "registered" if fn else "cleared",
            )

    def run_once(self, reason: str = "manual") -> dict[str, Any]:
        """One full scan. Never raises.

        Gated on ``_upstream_ready()`` — if the SR backfill has not yet
        populated today's series for every tracked instrument, the run
        is skipped and the next scheduler tick retries.
        """
        t0 = time.monotonic()
        with self._lock:
            try:
                if not bool(getattr(config, "DUMMY_ORDER_ENABLED", True)):
                    return self._skipped(reason, "disabled", t0)

                now = _now_market()
                window = self._classify_window(now)
                if window == "before_start":
                    return self._skipped(reason, "before_start", t0)

                # ---- G1 v4.1: readiness gate --------------------- #
                ready, ready_reason = self._upstream_ready()
                if not ready:
                    logger.info(
                        "[dummy-order] %s — skipping run (upstream "
                        "not ready: %s)",
                        reason, ready_reason,
                    )
                    return self._skipped(
                        reason, f"not_ready:{ready_reason}", t0,
                    )

                self._reset_if_new_day(now)

                # Old releases persisted SKIPPED orders solely because
                # the entry price was outside the OR grid or OR levels
                # were unavailable. Those filters are retired. Re-open
                # only these legacy OR-only skips so today's confirmed
                # signal can be evaluated by the current shared engine.
                existing_order = self._state.get("order")
                if (
                    isinstance(existing_order, dict)
                    and str(existing_order.get("status") or "").upper() == "SKIPPED"
                ):
                    legacy_reason = " ".join(str(value or "") for value in (
                        existing_order.get("skip_reason"),
                        existing_order.get("zone"),
                    )).lower()
                    if any(marker in legacy_reason for marker in (
                        "above_r2", "below_s2", " > r2", "below s2",
                        "missing_or_levels", "incomplete_or_levels",
                        "or grid",
                    )):
                        logger.warning(
                            "[dummy-order] reopening legacy OR-grid-only "
                            "SKIPPED order %s (reason=%s); evaluating with "
                            "unrestricted entry eligibility",
                            existing_order.get("order_id"),
                            existing_order.get("skip_reason"),
                        )
                        self._state["order"] = None

                # ---- 1. Fetch isolation record -------------------- #
                try:
                    from services.level_isolated_instrument_service import (
                        level_isolated_instrument_service,
                    )
                    iso_status = level_isolated_instrument_service.last_status()
                except Exception:
                    logger.exception("[dummy-order] iso lookup failed")
                    return self._error(reason, "iso_lookup_failed", t0)

                iso = (iso_status or {}).get("isolated") or {}
                if not iso:
                    return self._no_signal(reason, "no_isolation", t0)

                # ---- Unified standard Path A ---------------------- #
                # The trigger index is diagnostic metadata only; every
                # pre-11:30 isolation uses PATH_A_UNIFIED.
                path = self._resolve_execution_path(iso)
                if path:
                    self._state["execution_path"] = path
                self._state.setdefault(
                    "isolated_key", iso.get("instrument_key"),
                )
                self._state.setdefault("bias_type", iso.get("type"))
                self._state.setdefault(
                    "t_iso",
                    iso.get("touched_at") or iso.get("isolated_at"),
                )

                # ---- 2. Ensure order (created at most once) ------- #
                # pending_transitions is populated during the scan and
                # flushed AFTER self._save(now) so downstream consumers
                # only ever observe transitions that are already durable
                # in dummy_orders.json.
                created = 0
                updated = 0
                closed = 0
                pending_transitions: list[tuple[str, dict[str, Any]]] = []

                order = self._state.get("order")
                if order is None:
                    order = self._try_create_order(iso, now, window)
                    if order is not None:
                        self._state["order"] = order
                        created = 1
                        # Only surface CREATED for actionable OPEN orders.
                        # Non-entry SKIPPED records remain silent to downstream
                        # execution consumers.
                        if str(order.get("status")) == "OPEN":
                            pending_transitions.append(
                                ("CREATED", dict(order)),
                            )
                else:
                    # ---- G1 v4.1: idempotency log ----------------- #
                    # If an order already exists for today, do NOT
                    # recompute it.  The entry/stop/target are frozen.
                    if isinstance(order, dict):
                        logger.debug(
                            "[dummy-order] order already exists for "
                            "today (%s) — catch-up is a no-op",
                            order.get("order_id"),
                        )

                # ---- 3. Update OPEN order ------------------------- #
                if isinstance(order, dict) and order.get("status") == "OPEN":
                    before_status = order.get("status")
                    before_stage2 = order.get("stage_2_triggered_at")
                    self._update_order(order, now, window)
                    updated = 1
                    # Stage 2 arming happened this tick →
                    # snapshot it before checking for a same-tick close.
                    if (
                        before_stage2 is None
                        and order.get("stage_2_triggered_at") is not None
                    ):
                        pending_transitions.append(
                            ("STAGE_2_MILESTONE", dict(order)),
                        )
                    # Close happened this tick → map the closed_reason
                    # onto one of the transition_type labels.
                    if order.get("status") != before_status:
                        closed = 1
                        trans = _CLOSE_REASON_TO_TRANSITION.get(
                            str(order.get("closed_reason") or "").upper(),
                        )
                        if trans:
                            pending_transitions.append(
                                (trans, dict(order)),
                            )

                # ================================================== #
                # 2026-11-05 — POST-1130 branch                      #
                #                                                    #
                # Runs alongside unified standard Path A so the      #
                # standard and POST-11:30 branches can coexist.       #
                # performed by strategy_entry_signal_service.compute() #
                # — this branch only ACTS when compute() returned      #
                # execution_path="POST_1130" AND status="ok".          #
                # ================================================== #
                post_1130_created = 0
                post_1130_updated = 0
                post_1130_closed = 0
                p1130_order = self._state.get("post_1130_order")

                if p1130_order is None:
                    p1130_order = self._try_create_post_1130_order(
                        iso, now, window,
                    )
                    if p1130_order is not None:
                        self._state["post_1130_order"] = p1130_order
                        post_1130_created = 1
                        if str(p1130_order.get("status")) == "OPEN":
                            pending_transitions.append(
                                ("CREATED", dict(p1130_order)),
                            )
                else:
                    if isinstance(p1130_order, dict):
                        logger.debug(
                            "[dummy-order] post-1130 order already "
                            "exists for today (%s) — catch-up is a no-op",
                            p1130_order.get("order_id"),
                        )

                if (
                    isinstance(p1130_order, dict)
                    and p1130_order.get("status") == "OPEN"
                ):
                    before_p1130 = p1130_order.get("status")
                    self._update_post_1130_order(p1130_order, now, window)
                    post_1130_updated = 1
                    if p1130_order.get("status") != before_p1130:
                        post_1130_closed = 1
                        trans = _CLOSE_REASON_TO_TRANSITION.get(
                            str(p1130_order.get("closed_reason") or "").upper(),
                        )
                        if trans:
                            pending_transitions.append(
                                (trans, dict(p1130_order)),
                            )
                    # Re-fetch in case the update created no state change.
                    p1130_order = self._state.get("post_1130_order") or p1130_order

                # ---- 4. Persist state BEFORE firing transitions -- #
                self._save(now)

                # ---- 5. Flush transition callback --------------- #
                # Fire-and-observe: wrapped by _emit_transition which
                # can never raise into run_once.
                for trans_type, order_snapshot in pending_transitions:
                    self._emit_transition(
                        trans_type, order_snapshot, reason,
                    )

                logger.info(
                    "[dummy-order] %s — created=%d updated=%d closed=%d "
                    "order_status=%s | post_1130 created=%d updated=%d "
                    "closed=%d post_1130_status=%s mode=%s",
                    reason, created, updated, closed,
                    (order or {}).get("status") if order else "none",
                    post_1130_created, post_1130_updated, post_1130_closed,
                    (p1130_order or {}).get("status") if p1130_order else "none",
                    window,
                )
                return self._summary(
                    reason, "ok", now, window, t0,
                    created=created, updated=updated, closed=closed,
                    post_1130_created=post_1130_created,
                    post_1130_updated=post_1130_updated,
                    post_1130_closed=post_1130_closed,
                    readiness=ready_reason,
                )

            except Exception:
                logger.exception("[dummy-order] run_once failed")
                return self._error(reason, "unexpected", t0)

    def get_state(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._state)

    def get_summary(self) -> dict[str, Any]:
        with self._lock:
            ready, ready_reason = self._upstream_ready()
            return self._summary(
                "read", "ok", _now_market(),
                self._classify_window(_now_market()),
                time.monotonic(),
                readiness=ready_reason if ready else f"not_ready:{ready_reason}",
            )

    def reset(self) -> dict[str, Any]:
        with self._lock:
            self._state = self._empty_state()
            self._save(_now_market())
            logger.info("[dummy-order] state reset")
            return {"status": "reset", "day": self._state.get("day")}

    # ------------------------------------------------------------------ #
    # Transition callback plumbing                                       #
    # ------------------------------------------------------------------ #
    def _emit_transition(
        self,
        transition_type: str,
        order_record: dict[str, Any],
        reason: str,
    ) -> None:
        """Fire the registered callback. Never raises into run_once.

        The callback is invoked synchronously inside ``_lock``.  Slow
        work must be dispatched by the consumer to its own worker.
        """
        cb = self._transition_callback
        if cb is None:
            return
        try:
            cb(transition_type, order_record, reason)
        except Exception:
            logger.exception(
                "[dummy-order] transition callback failed for %s "
                "(order_id=%s reason=%s)",
                transition_type,
                (order_record or {}).get("order_id"),
                reason,
            )

    # ------------------------------------------------------------------ #
    # G1 v4.1 — readiness gate                                           #
    # ------------------------------------------------------------------ #
    def _upstream_ready(self) -> tuple[bool, str]:
        """Return ``(ready, reason)`` for the SR backfill readiness latch.

        * If ``DUMMY_ORDER_WAIT_FOR_SR_BACKFILL`` is false → ready.
        * If ``sr_backfill_service.is_ready_for_today`` is missing →
          ready (fail-open, one-shot WARNING).
        * If the helper raises → NOT ready (fail-closed, retry next tick).
        * If the helper returns False → NOT ready.
        * If the helper returns True → ready.
        """
        if not bool(
            getattr(config, "DUMMY_ORDER_WAIT_FOR_SR_BACKFILL", True)
        ):
            return True, "ok_disabled"

        try:
            from services.sr_backfill_service import sr_backfill_service
        except Exception:
            return True, "ok_backfill_module_missing"

        check = getattr(
            sr_backfill_service, "is_ready_for_today", None,
        )
        if not callable(check):
            if not self._warned_no_readiness_check:
                logger.warning(
                    "[dummy-order] sr_backfill_service.is_ready_for_today() "
                    "is not implemented — proceeding without the readiness "
                    "gate (this re-opens the pre-v4.1 race; implement the "
                    "helper to close it)."
                )
                self._warned_no_readiness_check = True
            return True, "ok_readiness_check_missing"

        try:
            ready = bool(check())
        except Exception:
            logger.exception(
                "[dummy-order] sr_backfill_service.is_ready_for_today() "
                "raised — skipping run (fail-closed)"
            )
            return False, "backfill_check_raised"

        if not ready:
            return False, "sr_backfill_not_ready"
        return True, "ok"

    # ------------------------------------------------------------------ #
    # G1 v4.1 — data-derived execution path                              #
    # ------------------------------------------------------------------ #
    def _resolve_execution_path(
        self, iso: dict[str, Any],
    ) -> str | None:
        """Return the only standard execution path: unified Path A.

        The isolation trigger index remains in the record for diagnostics
        but never selects a different standard-session execution strategy.
        POST-11:30 is dispatched separately by strategy_entry_signal_service.
        """
        if not isinstance(iso, dict) or not iso:
            return None
        return "PATH_A_UNIFIED"

    # ------------------------------------------------------------------ #
    # Window / state plumbing                                            #
    # ------------------------------------------------------------------ #
    def _classify_window(self, now: datetime) -> str:
        start = dtime(
            int(getattr(config, "DUMMY_ORDER_START_HOUR", 9)),
            int(getattr(config, "DUMMY_ORDER_START_MINUTE", 15)),
        )
        retroactive_start = dtime(
            int(getattr(config, "DUMMY_ORDER_RETROACTIVE_START_HOUR", 15)),
            int(getattr(config, "DUMMY_ORDER_RETROACTIVE_START_MINUTE", 30)),
        )
        t = now.time()
        if t < start:
            return "before_start"
        if t < retroactive_start:
            return "live"
        return "retroactive"

    def _state_path(self) -> Path:
        return Path(config.LIVE_DATA_DIR) / str(
            getattr(config, "DUMMY_ORDER_FILENAME", "dummy_orders.json")
        )

    def _empty_state(self) -> dict[str, Any]:
        return {
            "day": _today_iso(),
            "execution_path": None,
            "isolated_key": None,
            "bias_type": None,
            "t_iso": None,
            "order": None,
            # POST-11:30 order (separate from unified standard Path A).
            "post_1130_order": None,
            "orders": [],
            "created_at": _now_utc_iso(),
            "updated_at": None,
        }

    def _load_from_disk(self) -> None:
        path = self._state_path()
        if not path.exists():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("[dummy-order] unreadable file: %s", path)
            return
        if not isinstance(payload, dict):
            return
        if not isinstance(payload.get("order"), (dict, type(None))):
            payload["order"] = None
        if not isinstance(payload.get("post_1130_order"), (dict, type(None))):
            payload["post_1130_order"] = None
        if not isinstance(payload.get("orders"), list):
            payload["orders"] = []
        self._state = payload

    def _reset_if_new_day(self, now: datetime) -> None:
        today = now.date().isoformat()
        if str(self._state.get("day") or "") != today:
            self._state = self._empty_state()
            self._state["day"] = today
            logger.info("[dummy-order] new day — state reset for %s", today)

    def _save(self, now: datetime) -> None:
        path = self._state_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._state["updated_at"] = _now_utc_iso()
            # 2026-11-05 — include BOTH branches in the compat list.
            orders_list: list[dict[str, Any]] = []
            if self._state.get("order"):
                orders_list.append(self._state["order"])
            if self._state.get("post_1130_order"):
                orders_list.append(self._state["post_1130_order"])
            self._state["orders"] = orders_list
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(self._state, indent=2, default=str),
                encoding="utf-8",
            )
            tmp.replace(path)
        except Exception:
            logger.exception("[dummy-order] save failed")

    # ------------------------------------------------------------------ #
    # G1 v4.1 — single deterministic candle series                       #
    # ------------------------------------------------------------------ #
    def _build_fixed_series(
        self, instrument_key: str,
    ) -> list[dict[str, Any]]:
        """Build ONE deterministic candle series for the trading day.

        This series is the sole input to entry resolution, candle
        replay, and resistance-exit lookup.  Building it once per run
        guarantees the three computations agree on the underlying data
        — the pre-v4.1 code mixed in-memory / intraday-API / LTP sources,
        which is what made entries drift across restarts.

        Source priority:

            1. ``live_sr_calc_service.build_series_for_day`` — dedicated
               helper that merges historical + today's intraday into one
               ordered series.  Preferred when the helper exists.
            2. ``live_sr_levels_service.get_candles`` — the merged
               per-instrument series the SR engine already maintains.
               Already deterministic given the same feed inputs.
            3. ``live_sr_calc_service.fetch_intraday_candles_for_entry``
               — fresh intraday API fetch.  Deterministic per call, but
               may lag the day.  Used only as a last resort.
        """
        # 1. Preferred: dedicated builder
        try:
            from services.live_sr_calc_service import live_sr_calc_service
            builder = getattr(
                live_sr_calc_service, "build_series_for_day", None,
            )
            if callable(builder):
                series = builder(instrument_key)
                if series:
                    return list(series)
        except Exception:
            logger.exception(
                "[dummy-order] build_series_for_day failed for %s",
                instrument_key,
            )

        # 2. Merged per-instrument series
        try:
            from services.live_sr_levels_service import live_sr_levels_service
            candles = live_sr_levels_service.get_candles(instrument_key)
            if candles:
                return list(candles)
        except Exception:
            pass

        # 3. Fresh intraday API fetch
        try:
            from services.live_sr_calc_service import live_sr_calc_service
            return list(
                live_sr_calc_service.fetch_intraday_candles_for_entry(
                    instrument_key,
                ) or []
            )
        except Exception:
            return []

    def _resolve_entry_price_from_series(
        self,
        instrument_key: str,
        confirmed_time_iso: str,
        series: list[dict[str, Any]],
    ) -> tuple[float | None, str]:
        """Resolve entry price from an already-built fixed series.

        Returns ``(price, source)``.  ``source`` is one of:

            * ``"candle_close_from_fixed_series"`` — the deterministic
              happy path.  Order will be created.
            * ``"live_ltp"`` — flag-gated fallback.  Order will be
              created but flagged non-deterministic.
            * ``"no_candle_at_confirmed_time"`` — retry next poll.
        """
        target = _parse_iso(confirmed_time_iso)
        if target is None:
            return None, "invalid_confirmed_time"
        target_min = target.replace(second=0, microsecond=0)

        for c in series:
            c_dt = _parse_iso(c.get("time"))
            if c_dt is None:
                continue
            if c_dt.replace(second=0, microsecond=0) == target_min:
                close = _safe_float(c.get("close"))
                if close is not None:
                    return close, "candle_close_from_fixed_series"

        # Flag-gated LTP fallback (default false — G1 v4.1)
        if bool(
            getattr(config, "DUMMY_ORDER_ALLOW_LTP_ENTRY_FALLBACK", False)
        ):
            try:
                from services.live_feed_service import live_feed_service
                ltp = live_feed_service.get_latest_ltp(instrument_key)
                if ltp is not None:
                    logger.warning(
                        "[dummy-order] entry price for %s fell back to "
                        "live_ltp (no candle close at %s; flag "
                        "DUMMY_ORDER_ALLOW_LTP_ENTRY_FALLBACK=true) — "
                        "this value is NON-DETERMINISTIC across restarts",
                        instrument_key, confirmed_time_iso,
                    )
                    return float(ltp), "live_ltp"
            except Exception:
                pass

        return None, "no_candle_at_confirmed_time"

    # Kept for backward compat with any external caller — now a thin
    # wrapper that builds the fixed series and delegates.
    def _resolve_entry_price(
        self,
        instrument_key: str,
        confirmed_time_iso: str,
    ) -> tuple[float | None, str | None]:
        series = self._build_fixed_series(instrument_key)
        return self._resolve_entry_price_from_series(
            instrument_key, confirmed_time_iso, series,
        )

    # Kept for backward compat with any external caller — now a thin
    # wrapper over the fixed-series builder.
    def _get_candles_for(
        self, instrument_key: str,
    ) -> list[dict[str, Any]]:
        return self._build_fixed_series(instrument_key)

    def _build_resistance_exit_map(
        self,
        traded_key: str,
        series: list[dict[str, Any]],
    ) -> dict[str, float]:
        """Return ``{confirmed_time_iso: close_price}`` for every
        resistance on ``traded_key`` whose confirming bar's close is
        available **in the fixed series**.

        The level LIST comes from ``live_sr_levels_service`` (the
        authoritative source of confirmed pivots).  The CLOSE price for
        each level is resolved from the fixed series — not from the
        live in-memory buffer — so the exit map is reproducible across
        restarts.
        """
        out: dict[str, float] = {}

        # Build a minute → close lookup from the fixed series
        closes_by_minute: dict[datetime, float] = {}
        for c in series or []:
            c_dt = _parse_iso(c.get("time"))
            if c_dt is None:
                continue
            key = c_dt.replace(second=0, microsecond=0)
            close = _safe_float(c.get("close"))
            if close is not None:
                closes_by_minute[key] = close

        try:
            from services.live_sr_levels_service import live_sr_levels_service
            payload = live_sr_levels_service.get_levels(traded_key)
        except Exception:
            return out
        if not payload:
            return out

        for lvl in payload.get("levels") or []:
            if str(lvl.get("level") or "").lower() != "resistance":
                continue
            conf_iso = lvl.get("confirmed_time")
            if not conf_iso:
                continue
            conf_dt = _parse_iso(conf_iso)
            if conf_dt is None:
                continue
            key = conf_dt.replace(second=0, microsecond=0)
            close_f = closes_by_minute.get(key)
            if close_f is not None:
                out[str(conf_iso)] = close_f
        return dict(sorted(
            out.items(),
            key=lambda item: (
                _parse_iso(item[0]).timestamp()
                if _parse_iso(item[0]) is not None else float("inf")
            ),
        ))

    # ------------------------------------------------------------------ #
    # Trigger + routing                                                  #
    # ------------------------------------------------------------------ #
    def _resolve_trigger_and_route(
        self, iso: dict[str, Any], now: datetime,
    ) -> dict[str, Any] | None:
        """Return routing info or None if the trigger hasn't confirmed yet."""
        bias_key = str(iso.get("instrument_key") or "").strip()
        t_iso = _parse_iso(iso.get("touched_at") or iso.get("isolated_at"))
        if not bias_key or t_iso is None:
            return None

        try:
            from services.live_sr_levels_service import live_sr_levels_service
            payload = live_sr_levels_service.get_levels(bias_key)
        except Exception:
            logger.exception("[dummy-order] SR levels lookup failed")
            return None
        if not payload:
            return None
        levels = payload.get("levels") or []
        if not levels:
            return None

        post_trigger: dict[str, Any] | None = None
        prior_support: dict[str, Any] | None = None
        for lvl in sorted(
            (l for l in levels if isinstance(l, dict)),
            key=lambda x: str(x.get("confirmed_time") or ""),
        ):
            pivot_dt = _parse_iso(lvl.get("pivot_time"))
            conf_dt = _parse_iso(lvl.get("confirmed_time"))
            if pivot_dt is None or conf_dt is None:
                continue
            if pivot_dt > t_iso and conf_dt > t_iso:
                if post_trigger is None:
                    post_trigger = {
                        "level": str(lvl.get("level") or "").lower(),
                        "price": _safe_float(lvl.get("price")),
                        "pivot_time": _iso(pivot_dt),
                        "confirmed_time": _iso(conf_dt),
                    }
            elif conf_dt <= t_iso and str(
                lvl.get("level") or "",
            ).lower() == "support":
                prior_support = {
                    "price": _safe_float(lvl.get("price")),
                    "confirmed_time": _iso(conf_dt),
                }

        if post_trigger is None:
            return None

        try:
            from services.instrument_registry import instrument_registry
            bias_entry = instrument_registry.get(bias_key) or {}
        except Exception:
            bias_entry = {}
        underlying = str(bias_entry.get("underlying") or "").strip()
        strike = bias_entry.get("strike_price")
        bias_type = str(
            bias_entry.get("strike_type") or iso.get("type") or "",
        ).upper()

        cutoff_hour = int(getattr(config, "STRATEGY_ORDER_REVERSAL_CUTOFF_HOUR", 9))
        cutoff_min = int(getattr(config, "STRATEGY_ORDER_REVERSAL_CUTOFF_MINUTE", 30))
        post_conf_dt = _parse_iso(post_trigger["confirmed_time"])
        cutoff_dt = post_conf_dt.replace(
            hour=cutoff_hour, minute=cutoff_min, second=0, microsecond=0,
        ) if post_conf_dt is not None else None

        post_is_resistance = post_trigger["level"] == "resistance"
        # The isolation service pins this mode once using the official
        # isolation timestamp and isolated option price. Never recompute it
        # from a later pivot, confirmation, or current market price.
        early_extreme_reversal = bool(
            iso.get("early_extreme_reversal")
            or iso.get("standard_execution_mode") == "EARLY_EXTREME_REVERSAL"
        )
        reversal_active = (
            cutoff_dt is not None
            and post_conf_dt is not None
            and post_conf_dt <= cutoff_dt
            and prior_support is not None
            and post_is_resistance
        )

        if early_extreme_reversal and post_is_resistance:
            # Reversal mode: Resistance buys the isolated (same) leg.
            routing_mode = "early_extreme_resistance_same_leg"
            traded_key = bias_key
            traded_symbol = bias_entry.get("trading_symbol")
        elif reversal_active:
            routing_mode = "reversal_same_leg"
            traded_key = bias_key
            traded_symbol = bias_entry.get("trading_symbol")
        elif post_trigger["level"] == "support" and not early_extreme_reversal:
            routing_mode = "support_same_leg"
            traded_key = bias_key
            traded_symbol = bias_entry.get("trading_symbol")
        else:
            # Normal Resistance OR reversal-mode Support buys the opposite leg.
            routing_mode = (
                "early_extreme_support_opposite_leg"
                if early_extreme_reversal else "resistance_opposite_leg"
            )
            opposite_type = "PE" if bias_type == "CE" else "CE"
            opposite_entry = None
            if underlying and strike is not None:
                try:
                    from services.instrument_registry import instrument_registry
                    opposite_entry = instrument_registry.resolve(
                        underlying, strike, opposite_type,
                    )
                except Exception:
                    opposite_entry = None
            if not opposite_entry:
                logger.warning(
                    "[dummy-order] no opposite-leg instrument for %s "
                    "(%s %s %s) — cannot route",
                    bias_key, underlying, strike, opposite_type,
                )
                return None
            traded_key = str(opposite_entry.get("instrument_key") or "")
            traded_symbol = opposite_entry.get("trading_symbol")

        if not traded_key:
            return None

        _log_calc_routing(
            post_trigger, routing_mode, traded_key, traded_symbol,
            prior_support, cutoff_dt,
        )

        return {
            "trigger": post_trigger,
            "routing_mode": routing_mode,
            "traded_key": traded_key,
            "traded_symbol": traded_symbol,
            "prior_support": prior_support,
            "early_extreme_reversal": early_extreme_reversal,
            "standard_execution_mode": (
                "EARLY_EXTREME_REVERSAL" if early_extreme_reversal else "STANDARD"
            ),
            "execution_mode_price": _safe_float(iso.get("execution_mode_price") or iso.get("instrument_ltp")),
            "execution_mode_decided_at": iso.get("execution_mode_decided_at") or iso.get("touched_at") or iso.get("isolated_at"),
            "entry_time_iso": post_trigger["confirmed_time"],
        }

    # ================================================================== #
    # POST-1130 branch (2026-11-05)                                      #
    # ================================================================== #
    def _try_create_post_1130_order(
        self,
        iso: dict[str, Any],
        now: datetime,
        window: str,
    ) -> dict[str, Any] | None:
        """Create a POST-1130 dummy order if the signal is armed.

        Calls strategy_entry_signal_service.compute() and acts only when
        execution_path is POST_1130 and status is ok. Unified standard Path A,
        POST-11:30 waiting, and disabled states return None; the next tick retries.

        The order is written on the **isolated instrument itself** —
        CE for a CE isolation, PE for a PE isolation.  Entry / stop /
        target come verbatim from the enriched BUY payload; no
        re-derivation happens here.

        The order is registered with ``order_runtime_service`` so the
        (30 % stop / +50 pt target / 15/15 resistance) exit conditions
        are tracked on subsequent ticks.
        """
        try:
            from services.strategy_entry_signal_service import (
                strategy_entry_signal_service,
            )
            signal = strategy_entry_signal_service.compute()
        except Exception:
            logger.exception(
                "[dummy-order] post-1130 signal lookup failed"
            )
            return None

        if not isinstance(signal, dict):
            return None
        if signal.get("status") != "ok":
            return None
        if signal.get("execution_path") != "POST_1130":
            return None

        candidates = signal.get("candidates") or []
        if not isinstance(candidates, list) or not candidates:
            logger.warning(
                "[dummy-order] post-1130 signal is ok but carries no "
                "candidates — will retry next tick"
            )
            return None
        candidate = candidates[0] or {}
        entry = candidate.get("entry") or {}

        entry_price = _safe_float(entry.get("price"))
        entry_time_iso = entry.get("time")
        if entry_price is None or entry_price <= 0 or not entry_time_iso:
            logger.warning(
                "[dummy-order] post-1130 signal present but entry "
                "price/time missing (price=%s time=%s) — will retry "
                "next tick",
                entry_price, entry_time_iso,
            )
            return None

        traded_key = str(
            signal.get("chosen_instrument_key")
            or candidate.get("instrument_key")
            or ""
        ).strip()
        if not traded_key:
            return None
        traded_symbol = candidate.get("trading_symbol")
        side = str(candidate.get("side") or "").upper() or None

        stop_price = _safe_float(entry.get("stop"))
        target_price = _safe_float(entry.get("target"))
        stoploss_pct = _safe_float(entry.get("sl_pct")) or float(
            getattr(config, "POST_1130_STOPLOSS_PERCENT", 0.30)
        )
        target_points = _safe_float(entry.get("target_points"))
        reference_type = entry.get("reference_type")
        reference_price = _safe_float(entry.get("reference_price"))
        pivot_time = entry.get("pivot_time")
        confirmation_time = (
            entry.get("confirmation_time") or entry_time_iso
        )

        post_1130 = signal.get("post_1130") or {}
        exit_cfg_raw = (
            post_1130.get("exit_sr_config")
            or getattr(config, "POST_1130_EXIT_SR_CONFIG", (15, 15))
        )
        try:
            exit_cfg = [int(exit_cfg_raw[0]), int(exit_cfg_raw[1])]
        except (TypeError, ValueError, IndexError):
            exit_cfg = [15, 15]

        # Make sure the traded instrument is tracked by the SR engine
        # so future ticks can resolve 15/15 resistance confirmations.
        self._ensure_sr_tracking(traded_key)

        order_id = f"{traded_key}|POST_1130|{entry_time_iso}"
        order: dict[str, Any] = {
            "order_id": order_id,
            "dummy": True,
            "regime": "POST_1130_ISOLATED",
            "status": "OPEN",
            "instrument_key": traded_key,
            "trading_symbol": traded_symbol,
            "side": "BUY",
            "routing_mode": "post_1130_sr_trigger",
            "isolated_key": iso.get("instrument_key"),
            "bias_type": iso.get("type"),
            "t_iso": iso.get("touched_at") or iso.get("isolated_at"),
            "trigger": {
                "level": reference_type,
                "price": reference_price,
                "pivot_time": pivot_time,
                "confirmed_time": confirmation_time,
            },
            "prior_support": None,
            "entry_time": entry_time_iso,
            "entry_price": round(entry_price, 4),
            "entry_price_source": entry.get("price_source"),
            "stop_price": (
                round(stop_price, 4) if stop_price is not None else None
            ),
            "stop_pct": round(stoploss_pct, 4),
            "target_price": (
                round(target_price, 4) if target_price is not None else None
            ),
            "target_points": (
                round(target_points, 4) if target_points is not None else None
            ),
            # The POST-1130 spec does not use OR-levels, zones or
            # milestones — kept as empty containers so downstream
            # consumers that iterate these fields don't need to
            # special-case POST-1130 orders.
            "or_levels": {},
            "zone": "POST_1130",
            "milestones": [],
            "exit_sr_config": exit_cfg,
            "profit_state": "STAGE_1",
            "stage_2_triggered_at": None,
            "stage_2_trigger_reason": None,
            "current_ltp": round(entry_price, 4),
            "current_ltp_at": now.isoformat(),
            "max_high_since_entry": round(entry_price, 4),
            "min_low_since_entry": round(entry_price, 4),
            "mfe": 0.0,
            "mae": 0.0,
            "created_at": now.isoformat(),
            "created_retroactively": (window == "retroactive"),
            "closed_at": None,
            "closed_time": None,
            "closed_price": None,
            "closed_pnl_pct": None,
            "closed_profit_pts": None,
            "closed_reason": None,
            "_notified_created": False,
            "_notified_closed": False,
        }

        # ---- Register the trade for exit tracking ----------------- #
        try:
            from services.order_runtime_service import order_runtime_service
            order_runtime_service.register_post_1130_trade(
                order_id=order_id,
                fill_price=entry_price,
                entry_time=entry_time_iso,
                trading_symbol=traded_symbol,
                instrument_key=traded_key,
                side=side,
            )
        except Exception:
            logger.exception(
                "[dummy-order] register_post_1130_trade failed for %s — "
                "order will be placed but exits will retry registration "
                "on the next tick",
                order_id,
            )

        _log_calc_post_1130(order, reference_type, reference_price)

        logger.info(
            "[dummy-order] POST-1130 CREATED %s | entry=%.4f stop=%.4f "
            "target=%.4f ref_type=%s ref_price=%s exit_sr=%s "
            "retroactive=%s",
            order_id, entry_price,
            stop_price if stop_price is not None else 0.0,
            target_price if target_price is not None else 0.0,
            reference_type, reference_price, exit_cfg,
            order["created_retroactively"],
        )
        self._notify_created(order)
        return order

    def _update_post_1130_order(
        self,
        order: dict[str, Any],
        now: datetime,
        window: str,
    ) -> None:
        """Tick-loop update for an OPEN POST-1130 order.

        Responsibilities:

            1. Ensure the trade is registered with
               ``order_runtime_service`` (idempotent — handles the
               restart case where the order was loaded from disk but
               the runtime service lost its in-memory state).
            2. Refresh the current LTP / watermarks.
            3. Call ``check_and_close_post_1130`` with the (15, 15) SR
               levels + current LTP — this is the authoritative exit
               evaluator (30 % stop / +50 pt target / 15/15 resistance).
            4. EOD-close any surviving OPEN order at
               ``DUMMY_ORDER_EOD_HOUR:MINUTE`` (15:35).
        """
        if order.get("status") != "OPEN":
            return

        traded_key = str(order.get("instrument_key") or "").strip()
        order_id = str(order.get("order_id") or "").strip()
        if not traded_key or not order_id:
            return

        # ---- 1. Idempotent re-registration after restart ---------- #
        self._ensure_post_1130_trade_registered(order)

        # ---- 2. Fetch latest (15, 15) SR levels ------------------- #
        sr_15_15: list[dict[str, Any]] = []
        exit_cfg = order.get("exit_sr_config") or [15, 15]
        try:
            from services.strategy_entry_signal_service import (
                _run_pivot_scan_enriched,
            )
            left = int(exit_cfg[0])
            right = int(exit_cfg[1])
            sr_15_15 = _run_pivot_scan_enriched(traded_key, left, right)
        except Exception:
            logger.exception(
                "[dummy-order] post-1130 SR (15,15) scan failed for %s",
                traded_key,
            )

        # ---- 3. Current LTP / current candle ---------------------- #
        current_ltp: float | None = None
        current_candle: dict[str, Any] | None = None

        try:
            from services.live_feed_service import live_feed_service
            ltp_val = live_feed_service.get_latest_ltp(traded_key)
            if ltp_val is not None:
                current_ltp = float(ltp_val)
        except Exception:
            pass

        # Fallback: last close of the fixed series.
        if current_ltp is None:
            candles = self._build_fixed_series(traded_key)
            if candles:
                current_candle = candles[-1]
                close_f = _safe_float(current_candle.get("close"))
                if close_f is not None:
                    current_ltp = close_f

        # ---- 4. Update watermarks (informational) ----------------- #
        if current_ltp is not None:
            order["current_ltp"] = round(current_ltp, 4)
            order["current_ltp_at"] = now.isoformat()
            entry = _safe_float(order.get("entry_price")) or 0.0
            if entry > 0:
                prev_hi = _safe_float(order.get("max_high_since_entry")) or entry
                prev_lo = _safe_float(order.get("min_low_since_entry")) or entry
                order["max_high_since_entry"] = round(
                    max(prev_hi, current_ltp), 4,
                )
                order["min_low_since_entry"] = round(
                    min(prev_lo, current_ltp), 4,
                )
                order["mfe"] = round(
                    order["max_high_since_entry"] - entry, 4,
                )
                order["mae"] = round(
                    order["min_low_since_entry"] - entry, 4,
                )

        # ---- 5. Exit evaluation ----------------------------------- #
        exit_result: dict[str, Any] | None = None
        try:
            from services.order_runtime_service import order_runtime_service
            exit_result = order_runtime_service.check_and_close_post_1130(
                order_id=order_id,
                current_candle=current_candle,
                current_ltp=current_ltp,
                latest_15_15_sr=sr_15_15,
            )
        except Exception:
            logger.exception(
                "[dummy-order] check_and_close_post_1130 failed for %s",
                order_id,
            )

        if exit_result is not None:
            reason = str(exit_result.get("reason") or "UNKNOWN")
            exit_price = _safe_float(exit_result.get("exit_price"))
            if exit_price is None:
                exit_price = current_ltp
            if exit_price is None:
                exit_price = _safe_float(order.get("entry_price")) or 0.0

            self._close_post_1130_order(
                order, reason, exit_price, now,
            )
            return

        # ---- 6. EOD close ----------------------------------------- #
        eod = dtime(
            int(getattr(config, "DUMMY_ORDER_EOD_HOUR", 15)),
            int(getattr(config, "DUMMY_ORDER_EOD_MINUTE", 35)),
        )
        if now.time() >= eod:
            candles = self._build_fixed_series(traded_key)
            last_close = (
                _safe_float(candles[-1].get("close")) if candles else None
            )
            if last_close is None:
                last_close = current_ltp
            if last_close is None:
                last_close = _safe_float(order.get("entry_price")) or 0.0
            last_time_iso = (
                str(candles[-1].get("time")) if candles else now.isoformat()
            )
            self._close_post_1130_order(
                order, "END_OF_DAY", last_close, now,
                close_time_iso=last_time_iso,
            )

    def _ensure_post_1130_trade_registered(
        self, order: dict[str, Any],
    ) -> None:
        """Idempotent re-registration with ``order_runtime_service``.

        The runtime service is in-memory only.  After a process
        restart, an OPEN POST-1130 order loaded from ``dummy_orders
        .json`` will be missing from the runtime service's trade map —
        so exits would silently never fire.  This helper detects that
        case and re-registers the trade.

        For orders already registered, ``register_post_1130_trade`` is
        a no-op (idempotent per ``order_id``), so this is safe to call
        on every tick.
        """
        order_id = str(order.get("order_id") or "").strip()
        if not order_id:
            return

        try:
            from services.order_runtime_service import order_runtime_service
        except Exception:
            return

        existing = None
        try:
            existing = order_runtime_service.get_post_1130_trade(order_id)
        except Exception:
            existing = None
        if existing is not None:
            return  # already registered

        entry = _safe_float(order.get("entry_price")) or 0.0
        if entry <= 0:
            return

        try:
            order_runtime_service.register_post_1130_trade(
                order_id=order_id,
                fill_price=entry,
                entry_time=order.get("entry_time"),
                trading_symbol=order.get("trading_symbol"),
                instrument_key=order.get("instrument_key"),
                side=None,
            )
            logger.info(
                "[dummy-order] re-registered POST-1130 trade after "
                "restart — %s",
                order_id,
            )
        except Exception:
            logger.exception(
                "[dummy-order] re-register_post_1130_trade failed for %s",
                order_id,
            )

    def _close_post_1130_order(
        self,
        order: dict[str, Any],
        reason: str,
        price: float,
        now: datetime,
        close_time_iso: str | None = None,
    ) -> None:
        """Mark a POST-1130 order CLOSED with the given reason."""
        entry = _safe_float(order.get("entry_price")) or 0.0
        if entry <= 0:
            return
        pnl_pct = (price - entry) / entry
        profit_pts = price - entry

        order["status"] = reason
        order["closed_at"] = now.isoformat()
        order["closed_time"] = close_time_iso or now.isoformat()
        order["closed_price"] = round(price, 4)
        order["closed_pnl_pct"] = round(pnl_pct, 6)
        order["closed_profit_pts"] = round(profit_pts, 4)
        order["closed_reason"] = reason
        order["current_ltp"] = round(price, 4)
        order["current_ltp_at"] = now.isoformat()

        logger.info(
            "[dummy-order] POST-1130 CLOSED %s | reason=%s price=%.4f "
            "pnl=%+.2f%% pts=%+.2f",
            order.get("order_id"), reason, price,
            pnl_pct * 100.0, profit_pts,
        )
        self._notify_closed(order)

    # ------------------------------------------------------------------ #
    # Order creation (unified standard Path A)                                          #
    # ------------------------------------------------------------------ #
    def _try_create_order(
        self,
        iso: dict[str, Any],
        now: datetime,
        window: str,
    ) -> dict[str, Any] | None:
        routing = self._resolve_trigger_and_route(iso, now)
        if routing is None:
            return None

        traded_key = routing["traded_key"]
        entry_time_iso = routing["entry_time_iso"]

        # ---- G1 v4.1: build the fixed series ONCE ---------------- #
        series = self._build_fixed_series(traded_key)
        if not series:
            logger.warning(
                "[dummy-order] trigger confirmed but no fixed series for "
                "%s — will retry next poll",
                traded_key,
            )
            return None

        # ---- G1 v4.1: resolve entry from the fixed series -------- #
        entry_price, entry_source = self._resolve_entry_price_from_series(
            traded_key, entry_time_iso, series,
        )
        if entry_price is None or entry_price <= 0:
            logger.warning(
                "[dummy-order] trigger confirmed but no entry price for "
                "%s @ %s (source=%s) — will retry next poll",
                traded_key, entry_time_iso, entry_source,
            )
            return None

        if entry_source == "live_ltp":
            logger.warning(
                "[dummy-order] DETERMINISM VIOLATED: entry price for %s "
                "is live_ltp (flag DUMMY_ORDER_ALLOW_LTP_ENTRY_FALLBACK="
                "true) — orders may differ across restarts",
                traded_key,
            )

        stop_pct = float(
            getattr(config, "STRATEGY_ORDER_STOP_LOSS_PCT", 0.30),
        )
        stop_price = round(entry_price * (1.0 - stop_pct), 4)
        target_price, target_divisor = _dynamic_target_for_entry(entry_price)

        # Ensure the SR engine is tracking the traded instrument so
        # live_sr_levels_service.get_levels() returns pivots for it.
        self._ensure_sr_tracking(traded_key)

        order = {
            "order_id": f"{traded_key}|{entry_time_iso}",
            "dummy": True,
            "status": "OPEN",
            "instrument_key": traded_key,
            "trading_symbol": routing["traded_symbol"],
            "side": "BUY",
            "routing_mode": routing["routing_mode"],
            "standard_execution_mode": routing["standard_execution_mode"],
            "early_extreme_reversal": routing["early_extreme_reversal"],
            "execution_mode_price": routing["execution_mode_price"],
            "execution_mode_decided_at": routing["execution_mode_decided_at"],
            "isolated_key": iso.get("instrument_key"),
            "bias_type": iso.get("type"),
            "t_iso": iso.get("touched_at") or iso.get("isolated_at"),
            "trigger": routing["trigger"],
            "prior_support": routing.get("prior_support"),
            "entry_time": entry_time_iso,
            "entry_price": round(entry_price, 4),
            "entry_price_source": entry_source,
            "stop_price": stop_price,
            "initial_stop_price": stop_price,
            "active_stop_price": stop_price,
            "stop_pct": stop_pct,
            "target_price": target_price,
            "initial_target_price": target_price,
            "active_target_price": target_price,
            "target_divisor": target_divisor,
            "target_ladder_enabled": bool(routing["early_extreme_reversal"]),
            "target_ladder_active": False,
            "target_ladder_stage": 0,
            "target_ladder_history": [],
            "target_milestone_met": False,
            "target_milestone_at": None,
            "target_milestone_price": None,
            "resistance_exit_confirmation_at": None,
            "execution_path": "PATH_A_UNIFIED",
            "isolation_trigger_index": iso.get("trigger_index"),
            # Compatibility fields only; OR-grid levels/zones are not
            # consulted by the entry, risk, target, or exit calculations.
            "or_levels": {},
            "zone": "UNRESTRICTED",
            "milestones": [{
                "name": "DYNAMIC_TARGET",
                "price": target_price,
                "divisor": target_divisor,
            }],
            "profit_state": "STAGE_1",
            "stage_2_triggered_at": None,
            "stage_2_trigger_reason": None,
            "current_ltp": round(entry_price, 4),
            "current_ltp_at": now.isoformat(),
            "max_high_since_entry": round(entry_price, 4),
            "min_low_since_entry": round(entry_price, 4),
            "mfe": 0.0,
            "mae": 0.0,
            "created_at": now.isoformat(),
            "created_retroactively": (window == "retroactive"),
            # G1 v4.1 audit trail
            "series_length": len(series),
            "series_snapshot_at": now.isoformat(),
            "closed_at": None,
            "closed_time": None,
            "closed_price": None,
            "closed_pnl_pct": None,
            "closed_profit_pts": None,
            "closed_reason": None,
            "_notified_created": False,
            "_notified_closed": False,
        }

        logger.info(
            "[dummy-order] CREATED %s | execution=PATH_A_UNIFIED "
            "routing=%s entry=%.4f stop=%.4f target=%.4f divisor=%d "
            "entry_eligibility=UNRESTRICTED series_len=%d",
            order["order_id"], routing["routing_mode"],
            entry_price, stop_price, target_price, target_divisor,
            len(series),
        )
        self._notify_created(order)
        return order

    def _skipped_order(
        self,
        iso: dict[str, Any],
        routing: dict[str, Any],
        entry_time_iso: str,
        entry_price: float,
        entry_source: str | None,
        now: datetime,
        *,
        zone: dict[str, Any] | None = None,
        or_levels: dict[str, Any] | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        zone = zone or {"zone": "SKIPPED", "reason": reason or "unknown"}
        order = {
            "order_id": f"{routing['traded_key']}|SKIPPED|{entry_time_iso}",
            "dummy": True,
            "status": "SKIPPED",
            "instrument_key": routing["traded_key"],
            "trading_symbol": routing["traded_symbol"],
            "side": "BUY",
            "routing_mode": routing["routing_mode"],
            "standard_execution_mode": routing.get("standard_execution_mode", "STANDARD"),
            "early_extreme_reversal": routing.get("early_extreme_reversal", False),
            "execution_mode_price": routing.get("execution_mode_price"),
            "execution_mode_decided_at": routing.get("execution_mode_decided_at"),
            "isolated_key": iso.get("instrument_key"),
            "bias_type": iso.get("type"),
            "t_iso": iso.get("touched_at") or iso.get("isolated_at"),
            "trigger": routing["trigger"],
            "prior_support": routing.get("prior_support"),
            "entry_time": entry_time_iso,
            "entry_price": round(entry_price, 4),
            "entry_price_source": entry_source,
            "stop_price": None,
            "stop_pct": None,
            "or_levels": or_levels or {},
            "zone": zone.get("zone"),
            "milestones": zone.get("milestones") or [],
            "skip_reason": zone.get("reason") or reason or "unknown",
            "profit_state": "NONE",
            "stage_2_triggered_at": None,
            "stage_2_trigger_reason": None,
            "created_at": now.isoformat(),
            "closed_at": now.isoformat(),
            "closed_reason": "SKIPPED",
            "closed_price": None,
            "closed_pnl_pct": None,
            "closed_profit_pts": None,
            "_notified_created": False,
            "_notified_closed": False,
        }
        logger.info(
            "[dummy-order] SKIPPED %s | reason=%s zone=%s",
            order["order_id"], order["skip_reason"], order["zone"],
        )
        self._notify_skipped(order)
        return order

    def _ensure_sr_tracking(self, instrument_key: str) -> None:
        """Make sure ``instrument_key`` has a candle series in the SR
        engine so the resistance-exit map and stop replay work."""
        try:
            from services.live_sr_levels_service import live_sr_levels_service
            if live_sr_levels_service.is_tracking(instrument_key):
                return
            from services.instrument_registry import instrument_registry
            entry = instrument_registry.get(instrument_key)
            if entry is None:
                entry = instrument_registry.register_adhoc(instrument_key)
            if entry is None:
                return
            live_sr_levels_service.register_instrument(entry)
            try:
                from services.live_sr_calc_service import live_sr_calc_service
                candles = live_sr_calc_service.fetch_intraday_candles_for_entry(
                    instrument_key,
                )
                if candles:
                    live_sr_levels_service.seed_candles(
                        instrument_key, entry, candles,
                    )
            except Exception:
                logger.exception(
                    "[dummy-order] seed failed for %s", instrument_key,
                )
        except Exception:
            logger.exception(
                "[dummy-order] ensure_sr_tracking failed for %s",
                instrument_key,
            )

    # ------------------------------------------------------------------ #
    # Order update (bar replay + dynamic target / Resistance exit)                           #
    # ------------------------------------------------------------------ #
    def _update_early_extreme_target_ladder(
        self,
        order: dict[str, Any],
        candles: list[dict[str, Any]],
        entry_dt: datetime,
        entry_price: float,
        now: datetime,
    ) -> None:
        """Replay Early Extreme Reversal candles using the 15% target lock.

        Before Target 1, the original configured stop remains active.
        After each target is touched, lock the achieved target's 85% as
        the protective stop and raise the next target by 30%. The newly
        calculated stop becomes active from the next candle; OHLC data
        cannot establish the intrabar order of a target and its new stop.
        """
        initial_stop = _safe_float(
            order.get("initial_stop_price", order.get("stop_price")),
        )
        initial_target = _safe_float(
            order.get("initial_target_price", order.get("target_price")),
        )
        if initial_stop is None or initial_target is None:
            return
        if initial_stop <= 0 or initial_target <= 0:
            return

        active_stop = initial_stop
        active_target = initial_target
        target_stage = 0
        ladder_history: list[dict[str, Any]] = []
        exit_reason: str | None = None
        exit_price: float | None = None
        exit_time_iso: str | None = None
        max_high = entry_price
        min_low = entry_price

        for candle in candles:
            candle_dt = _parse_iso(candle.get("time"))
            if candle_dt is None or candle_dt <= entry_dt:
                continue

            high = _safe_float(candle.get("high"))
            low = _safe_float(candle.get("low"))
            if high is None or low is None:
                continue

            max_high = max(max_high, high)
            min_low = min(min_low, low)

            # Existing active stop is evaluated before this candle's
            # target touches. This is the conservative OHLC assumption.
            if low <= active_stop:
                exit_reason = (
                    "TARGET_TRAIL_STOP_HIT"
                    if target_stage > 0 else "STOP_HIT"
                )
                exit_price = active_stop
                exit_time_iso = str(candle.get("time"))
                break

            # A candle high can pass more than one target. Each crossed
            # target is recorded in order; the final achieved target
            # determines the protective stop and next active target.
            while high >= active_target:
                achieved_target = active_target
                target_stage += 1
                active_stop = round(achieved_target * 0.85, 4)
                next_target = round(achieved_target * 1.30, 4)
                ladder_history.append({
                    "stage": target_stage,
                    "target_price": round(achieved_target, 4),
                    "hit_at": _iso(candle_dt),
                    "protective_stop_price": active_stop,
                    "next_target_price": next_target,
                })
                active_target = next_target

        order["initial_stop_price"] = round(initial_stop, 4)
        order["initial_target_price"] = round(initial_target, 4)
        order["target_ladder_enabled"] = True
        order["target_ladder_stage"] = target_stage
        order["target_ladder_active"] = target_stage > 0
        order["target_ladder_history"] = ladder_history
        order["active_stop_price"] = round(active_stop, 4)
        order["active_target_price"] = round(active_target, 4)
        # Keep the established fields aligned with the currently active
        # stop and next target so existing API/UI consumers stay useful.
        order["stop_price"] = round(active_stop, 4)
        order["target_price"] = round(active_target, 4)
        order["target_milestone_met"] = target_stage > 0
        if ladder_history:
            first_hit = ladder_history[0]
            latest_hit = ladder_history[-1]
            order["target_milestone_at"] = first_hit["hit_at"]
            order["target_milestone_price"] = first_hit["target_price"]
            order["latest_target_hit_at"] = latest_hit["hit_at"]
            order["latest_target_hit_price"] = latest_hit["target_price"]
            order["stage_2_triggered_at"] = first_hit["hit_at"]
            order["stage_2_trigger_reason"] = "DYNAMIC_TARGET_MILESTONE"
            order["profit_state"] = "STAGE_2"
        else:
            order["target_milestone_at"] = None
            order["target_milestone_price"] = None
            order["latest_target_hit_at"] = None
            order["latest_target_hit_price"] = None
            order["stage_2_triggered_at"] = None
            order["stage_2_trigger_reason"] = None
            order["profit_state"] = "STAGE_1"

        order["max_high_since_entry"] = round(max_high, 4)
        order["min_low_since_entry"] = round(min_low, 4)
        order["mfe"] = round(max_high - entry_price, 4)
        order["mae"] = round(min_low - entry_price, 4)
        last_close = _safe_float(candles[-1].get("close"))
        order["current_ltp"] = round(
            last_close if last_close is not None else entry_price, 4,
        )
        order["current_ltp_at"] = now.isoformat()

        if exit_reason:
            self._close_order(
                order, exit_reason, exit_price, exit_time_iso, now,
            )
            return

        # No S/R Resistance exit is used by Early Extreme Reversal after
        # entry; the target-protection ladder or EOD is its exit mechanism.
        self._maybe_eod_close(order, now, candles)

    def _update_order(
        self,
        order: dict[str, Any],
        now: datetime,
        window: str,
    ) -> None:
        if order.get("status") != "OPEN":
            return

        # ---- G1 v4.1: build the fixed series ONCE ---------------- #
        # The same series feeds (a) candle replay for stops/milestones
        # and (b) the resistance-exit map.  Pre-v4.1 the two used
        # different sources, which could produce inconsistent exits.
        traded_key = order.get("instrument_key")
        candles = self._build_fixed_series(traded_key)
        if not candles:
            return

        entry_dt = _parse_iso(order.get("entry_time"))
        if entry_dt is None:
            return

        entry_price = _safe_float(order.get("entry_price")) or 0.0
        stop_price = _safe_float(order.get("stop_price")) or 0.0
        if entry_price <= 0 or stop_price <= 0:
            return

        if bool(
            order.get("early_extreme_reversal")
            or order.get("standard_execution_mode") == "EARLY_EXTREME_REVERSAL"
        ):
            # Early Extreme Reversal uses the repeating target/15% lock
            # ladder. Its post-target exit is not driven by S/R resistance.
            self._update_early_extreme_target_ladder(
                order, candles, entry_dt, entry_price, now,
            )
            return

        # The entry-price target is the only Stage 1 profit milestone.
        # OR-grid levels are not used for entry eligibility or exits.
        target_price = _safe_float(order.get("target_price"))
        if target_price is None:
            target_price, target_divisor = _dynamic_target_for_entry(entry_price)
            order["target_price"] = target_price
            order["target_divisor"] = target_divisor

        # Resistance-exit map for Stage 2 — closes resolved from the
        # SAME fixed series used for replay.
        resistance_exits = self._build_resistance_exit_map(
            traded_key, candles,
        )

        stage_2_dt = _parse_iso(order.get("stage_2_triggered_at"))
        if (
            stage_2_dt is not None
            and order.get("stage_2_trigger_reason") != "DYNAMIC_TARGET_MILESTONE"
        ):
            logger.info(
                "[dummy-order] resetting legacy Stage-2 marker for %s; "
                "replaying the dynamic target milestone",
                order.get("order_id"),
            )
            stage_2_dt = None
            order["stage_2_triggered_at"] = None
            order["stage_2_trigger_reason"] = None
            order["profit_state"] = "STAGE_1"
            order["target_milestone_met"] = False
            order["target_milestone_at"] = None
            order["target_milestone_price"] = None
        exit_reason: str | None = None
        exit_price: float | None = None
        exit_time_iso: str | None = None

        max_high = entry_price
        min_low = entry_price

        for c in candles:
            c_dt = _parse_iso(c.get("time"))
            if c_dt is None or c_dt <= entry_dt:
                continue

            hi = _safe_float(c.get("high"))
            lo = _safe_float(c.get("low"))
            if hi is None or lo is None:
                continue

            max_high = max(max_high, hi)
            min_low = min(min_low, lo)

            # ---- Stop-loss ---------------------------------------- #
            if lo <= stop_price:
                exit_reason = "STOP_HIT"
                exit_price = stop_price
                exit_time_iso = str(c.get("time"))
                break

            # ---- Stage 1 → Stage 2 -------------------------------- #
            if stage_2_dt is None and order.get("profit_state") == "STAGE_1":
                if hi >= target_price:
                    stage_2_dt = c_dt
                    order["stage_2_triggered_at"] = _iso(c_dt)
                    order["stage_2_trigger_reason"] = "DYNAMIC_TARGET_MILESTONE"
                    order["target_milestone_met"] = True
                    order["target_milestone_at"] = _iso(c_dt)
                    order["target_milestone_price"] = round(target_price, 4)
                    logger.info(
                        "[dummy-order] TARGET MILESTONE MET %s | entry=%.4f "
                        "target=%.4f divisor=%d at=%s; waiting for NEW resistance",
                        order.get("order_id"), entry_price, target_price,
                        order.get("target_divisor"), _fmt_time(_iso(c_dt)),
                    )

            # ---- Stage 2 exit ------------------------------------- #
            if stage_2_dt is not None:
                for conf_iso, conf_close in resistance_exits.items():
                    conf_dt = _parse_iso(conf_iso)
                    if conf_dt is None:
                        continue
                    if conf_dt <= stage_2_dt:
                        continue
                    if conf_dt <= c_dt:
                        exit_reason = "TARGET_MILESTONE_RESISTANCE_EXIT"
                        exit_price = conf_close
                        exit_time_iso = conf_iso
                        order["resistance_exit_confirmation_at"] = conf_iso
                        break
                if exit_reason:
                    break

        # ---- Persist tracking -------------------------------------- #
        order["max_high_since_entry"] = round(max_high, 4)
        order["min_low_since_entry"] = round(min_low, 4)
        order["mfe"] = round(max_high - entry_price, 4)
        order["mae"] = round(min_low - entry_price, 4)
        last_close = _safe_float(candles[-1].get("close"))
        order["current_ltp"] = round(
            last_close if last_close is not None else entry_price, 4,
        )
        order["current_ltp_at"] = now.isoformat()

        if exit_reason:
            self._close_order(
                order, exit_reason, exit_price, exit_time_iso, now,
            )
            return

        if stage_2_dt is not None and order.get("profit_state") != "STAGE_2":
            order["profit_state"] = "STAGE_2"

        # ---- EOD close --------------------------------------------- #
        self._maybe_eod_close(order, now, candles)

    def _maybe_eod_close(
        self,
        order: dict[str, Any],
        now: datetime,
        candles: list[dict[str, Any]],
    ) -> None:
        eod = dtime(
            int(getattr(config, "DUMMY_ORDER_EOD_HOUR", 15)),
            int(getattr(config, "DUMMY_ORDER_EOD_MINUTE", 35)),
        )
        if now.time() < eod:
            return
        last_close = _safe_float(candles[-1].get("close")) if candles else None
        if last_close is None:
            last_close = _safe_float(order.get("current_ltp"))
        if last_close is None:
            return
        last_time_iso = (
            str(candles[-1].get("time")) if candles else now.isoformat()
        )
        self._close_order(
            order, "END_OF_DAY", last_close, last_time_iso, now,
        )

    def _close_order(
        self,
        order: dict[str, Any],
        reason: str,
        price: float,
        close_time_iso: str | None,
        now: datetime,
    ) -> None:
        entry = _safe_float(order.get("entry_price")) or 0.0
        if entry <= 0:
            return
        pnl_pct = (price - entry) / entry
        profit_pts = price - entry

        order["status"] = reason
        order["closed_at"] = now.isoformat()
        order["closed_time"] = close_time_iso
        order["closed_price"] = round(price, 4)
        order["closed_pnl_pct"] = round(pnl_pct, 6)
        order["closed_profit_pts"] = round(profit_pts, 4)
        order["closed_reason"] = reason
        order["current_ltp"] = round(price, 4)
        order["current_ltp_at"] = now.isoformat()

        logger.info(
            "[dummy-order] CLOSED %s | reason=%s price=%.4f "
            "pnl=%+.2f%% pts=%+.2f",
            order.get("order_id"), reason, price,
            pnl_pct * 100.0, profit_pts,
        )
        self._notify_closed(order)

    # ------------------------------------------------------------------ #
    # Telegram notifications                                             #
    # ------------------------------------------------------------------ #
    def _notify_created(self, order: dict[str, Any]) -> None:
        if not bool(getattr(config, "DUMMY_ORDER_TELEGRAM_ENABLED", True)):
            return
        if order.get("_notified_created"):
            return
        try:
            from services.telegram_format import build
            from services.telegram_service import telegram_service

            trigger = order.get("trigger") or {}
            entry = _safe_float(order.get("entry_price")) or 0.0
            stop = _safe_float(order.get("stop_price")) or 0.0
            stop_pct = _safe_float(order.get("stop_pct")) or 0.0
            milestones = [
                f"{m.get('name')} @ ₹{_fmt_price(m.get('price'))}"
                for m in (order.get("milestones") or [])
            ]
            prior = order.get("prior_support") or {}
            prior_line = (
                f"₹{_fmt_price(prior.get('price'))} @ "
                f"{_fmt_time(prior.get('confirmed_time'))}"
                if prior else "—"
            )
            entry_source = order.get("entry_price_source") or "—"

            msg = build(
                emoji="📝",
                title=f"Dummy Order · CREATED · {order.get('routing_mode')}",
                fields=[
                    ("Instrument", order.get("trading_symbol")
                     or order.get("instrument_key")),
                    ("Side", "BUY"),
                    ("Routing", order.get("routing_mode")),
                    ("Trigger", (
                        f"{str(trigger.get('level') or '').upper()} "
                        f"@ ₹{_fmt_price(trigger.get('price'))} "
                        f"({_fmt_time(trigger.get('confirmed_time'))})"
                    )),
                    ("Prior support", prior_line),
                    ("Entry", f"₹{entry:.2f}"),
                    ("Entry source", entry_source),
                    ("Stop-loss", f"₹{stop:.2f} (−{stop_pct * 100:.1f}%)"),
                    ("Zone", order.get("zone")),
                ],
                sections=[
                    ("Milestones (Stage 1)", "🎯",
                     milestones or ["dynamic target milestone"]),
                ],
                status="info",
            )
            telegram_service.send_message(msg, level="INFO")
            order["_notified_created"] = True
        except Exception:
            logger.exception("[dummy-order] notify(created) failed")

    def _notify_skipped(self, order: dict[str, Any]) -> None:
        if not bool(getattr(config, "DUMMY_ORDER_TELEGRAM_ENABLED", True)):
            return
        if order.get("_notified_created"):
            return
        try:
            from services.telegram_format import build
            from services.telegram_service import telegram_service

            trigger = order.get("trigger") or {}
            entry = _safe_float(order.get("entry_price")) or 0.0
            msg = build(
                emoji="🚫",
                title="Dummy Order · SKIPPED",
                fields=[
                    ("Instrument", order.get("trading_symbol")
                     or order.get("instrument_key")),
                    ("Routing", order.get("routing_mode")),
                    ("Trigger", (
                        f"{str(trigger.get('level') or '').upper()} "
                        f"@ ₹{_fmt_price(trigger.get('price'))}"
                    )),
                    ("Entry", f"₹{entry:.2f}"),
                    ("Zone", order.get("zone")),
                    ("Reason", order.get("skip_reason")),
                ],
                status="warning",
            )
            telegram_service.send_message(msg, level="WARNING")
            order["_notified_created"] = True
        except Exception:
            logger.exception("[dummy-order] notify(skipped) failed")

    def _notify_closed(self, order: dict[str, Any]) -> None:
        if not bool(getattr(config, "DUMMY_ORDER_TELEGRAM_ENABLED", True)):
            return
        if order.get("_notified_closed"):
            return
        try:
            from services.telegram_format import build
            from services.telegram_service import telegram_service

            entry = _safe_float(order.get("entry_price")) or 0.0
            exit_price = _safe_float(order.get("closed_price")) or entry
            pnl = _safe_float(order.get("closed_pnl_pct")) or 0.0
            pts = _safe_float(order.get("closed_profit_pts")) or 0.0
            status = str(order.get("status") or "")

            status_label = status.replace("_", " ")
            build_status = "success" if pnl >= 0 else "warning"
            log_level = {
                "STAGE_2_RESISTANCE_EXIT": "SUCCESS",
                "STOP_HIT": "WARNING",
                "END_OF_DAY": "INFO",
                # ---- POST-1130 exit reasons ----------------------- #
                "STOPLOSS_30_PCT_HIT": "WARNING",
                "TARGET_50_POINTS_HIT": "SUCCESS",
                "RESISTANCE_15_15_CONFIRMED": "SUCCESS",
            }.get(status, "INFO")

            msg = build(
                emoji="🏁",
                title=f"Dummy Order · CLOSED · {status_label}",
                fields=[
                    ("Instrument", order.get("trading_symbol")
                     or order.get("instrument_key")),
                    ("Routing", order.get("routing_mode")),
                    ("Entry", f"₹{entry:.2f} "
                     f"({_fmt_time(order.get('entry_time'))})"),
                    ("Entry source", order.get("entry_price_source") or "—"),
                    ("Exit", f"{status_label} @ ₹{exit_price:.2f}"),
                    ("Closed at", _fmt_time(
                        order.get("closed_time") or order.get("closed_at"),
                    )),
                    ("PnL", f"{pnl * 100:+.2f}%"),
                    ("Profit", f"{pts:+.2f} pts"),
                    ("Stage 2 trigger",
                     order.get("stage_2_trigger_reason") or "—"),
                ],
                status=build_status,
                status_detail=f"{pnl * 100:+.2f}% · {pts:+.2f} pts",
            )
            telegram_service.send_message(msg, level=log_level)
            order["_notified_closed"] = True
        except Exception:
            logger.exception("[dummy-order] notify(closed) failed")

    # ------------------------------------------------------------------ #
    # Summary builders                                                   #
    # ------------------------------------------------------------------ #
    def _summary(
        self,
        reason: str,
        status: str,
        now: datetime,
        window: str,
        t0: float,
        *,
        created: int = 0,
        updated: int = 0,
        closed: int = 0,
        post_1130_created: int = 0,
        post_1130_updated: int = 0,
        post_1130_closed: int = 0,
        readiness: str = "ok",
    ) -> dict[str, Any]:
        order = self._state.get("order")
        post_1130_order = self._state.get("post_1130_order")
        out: dict[str, Any] = {
            "status": status,
            "reason": reason,
            "day": self._state.get("day"),
            "mode": window,
            # ---- Unified standard Path A counters ----------------- #
            "created": created,
            "updated": updated,
            "closed": closed,
            "has_order": bool(order),
            "order_status": order.get("status") if order else None,
            # ---- POST-1130 counters (2026-11-05) ------------------ #
            "post_1130_created": post_1130_created,
            "post_1130_updated": post_1130_updated,
            "post_1130_closed": post_1130_closed,
            "has_post_1130_order": bool(post_1130_order),
            "post_1130_order_status": (
                post_1130_order.get("status") if post_1130_order else None
            ),
            # ---- shared metadata ---------------------------------- #
            "execution_path": self._state.get("execution_path"),
            "readiness": readiness,
            "elapsed_seconds": round(time.monotonic() - t0, 3),
            "generated_at": _now_utc_iso(),
        }
        if isinstance(order, dict) and order.get("status") == "OPEN":
            out["open_order"] = {
                "instrument_key": order.get("instrument_key"),
                "routing_mode": order.get("routing_mode"),
                "entry_price": order.get("entry_price"),
                "entry_price_source": order.get("entry_price_source"),
                "stop_price": order.get("stop_price"),
                "current_ltp": order.get("current_ltp"),
                "profit_state": order.get("profit_state"),
                "stage_2_triggered_at": order.get("stage_2_triggered_at"),
            }
        if (
            isinstance(post_1130_order, dict)
            and post_1130_order.get("status") == "OPEN"
        ):
            trig = post_1130_order.get("trigger") or {}
            out["post_1130_open_order"] = {
                "instrument_key": post_1130_order.get("instrument_key"),
                "routing_mode": post_1130_order.get("routing_mode"),
                "entry_price": post_1130_order.get("entry_price"),
                "entry_price_source": post_1130_order.get("entry_price_source"),
                "stop_price": post_1130_order.get("stop_price"),
                "target_price": post_1130_order.get("target_price"),
                "current_ltp": post_1130_order.get("current_ltp"),
                "reference_type": trig.get("level"),
                "reference_price": trig.get("price"),
                "exit_sr_config": post_1130_order.get("exit_sr_config"),
            }
        return out

    def _skipped(
        self, reason: str, why: str, t0: float,
    ) -> dict[str, Any]:
        return {
            "status": "skipped",
            "reason": reason,
            "skip_reason": why,
            "day": self._state.get("day"),
            "elapsed_seconds": round(time.monotonic() - t0, 3),
            "generated_at": _now_utc_iso(),
        }

    def _no_signal(
        self, reason: str, why: str, t0: float,
    ) -> dict[str, Any]:
        return {
            "status": "no_signal",
            "reason": reason,
            "detail": why,
            "day": self._state.get("day"),
            "elapsed_seconds": round(time.monotonic() - t0, 3),
            "generated_at": _now_utc_iso(),
        }

    def _error(
        self, reason: str, why: str, t0: float,
    ) -> dict[str, Any]:
        return {
            "status": "error",
            "reason": reason,
            "error": why,
            "elapsed_seconds": round(time.monotonic() - t0, 3),
            "generated_at": _now_utc_iso(),
        }


# --------------------------------------------------------------------------- #
# Singleton                                                                   #
# --------------------------------------------------------------------------- #
dummy_order_service = DummyOrderService()