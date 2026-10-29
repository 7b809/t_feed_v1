# services/dummy_order_service.py
"""Live-tracked dummy orders persisted to disk.

Purpose
-------
Mirror the post-close simulation, but LIVE. Every DUMMY_ORDER_TRACK_INTERVAL_
SECONDS the service:

  1. Loads today's dummy orders from ``data/dummy_orders.json``.
  2. Computes the strategy entry signal.
  3. For each ARMED candidate, upserts ONE order per scenario
     (``signal_v2`` and — if enabled — ``regime``). Idempotent: an order
     is only created once per (instrument_key, side, entry_time, scenario).
  4. Updates every OPEN order from live LTP (cheap) and — every
     DUMMY_ORDER_RECONCILE_MINUTES — from fresh intraday 1-min bars.
  5. Closes orders that hit target / stop. At
     DUMMY_ORDER_EOD_HOUR:MINUTE, auto-closes any remaining OPEN order
     (last pre-close bar, or stop / target if that fired during the day).
  6. Atomically re-writes ``data/dummy_orders.json``.

This service **never places a real order**. Every record carries
``"dummy": true`` so audits can't confuse it with a real Upstox order.

Telegram (NEW)
--------------
When ``DUMMY_ORDER_TELEGRAM_ENABLED`` is True (default), each order
fires exactly two Telegram messages over its lifetime:

    * one on creation   (``_notify_created``)
    * one on close      (``_notify_closed``)

Updates (LTP ticks, MFE / MAE movements, current R) stay silent — only
state transitions notify. Both flags (``_notified_created``,
``_notified_closed``) are persisted on the record, so a mid-day restart
never re-sends an already-sent message.

Given the strategy's shape (one armed candidate → at most two scenario
orders per day), worst-case Telegram volume is 4 messages/day.

Modes
-----
* ``live``       (now < EOD): LTP updates + periodic bar reconcile.
* ``retroactive`` (now ≥ EOD, e.g. app started post-close): full bar
                   reconcile to reconstruct the day's hits, then close
                   any surviving OPEN order at the last pre-close bar.

Execution paths (G1 v4.1)
-------------------------
The service now tracks a session-level ``execution_path``, populated
once per day from the isolation record's ``trigger_is_self_nifty``
field (with a defensive fallback for legacy records that predate that
field):

    * ``A_self_trigger``     — the trigger index was NIFTY. Behaviour
      is unchanged from v4.0: the strategy-entry signal drives order
      creation, at most one armed candidate → up to two scenario
      orders per day.

    * ``B_external_trigger`` — the trigger index was MIDCPNIFTY /
      BANKNIFTY / SENSEX. The strategy-entry 10-minute scan and
      auto-reverse are bypassed entirely. Instead, every confirmed
      post-isolation SR level on the bias-side instrument produces
      its **own** dummy BUY order, using a Rupee-based trailing
      profit engine (activation threshold, trailing gap, trailing
      floor) with an optional bearish-regime exit. On the first
      Path B trade to hit its target, ``strategy_locked_today`` is
      set True and no further Path B orders are created for the
      rest of the session (existing open orders continue to trail
      and close normally).

G1 v4.2 fix bundle (2026-10-29 replay analysis)
-----------------------------------------------
The 2026-10-29 retroactive replay exposed several Path B defects.
This revision closes them all:

  1. **Lockdown in retroactive mode** — ``_reconcile_from_bars``
     used to funnel Path B closes through ``_close_order`` (the Path A
     close path), which does not set ``strategy_locked_today``. Every
     Path B target-hit in the replay therefore left ``locked=False``.
     ``_reconcile_from_bars`` now dispatches to a dedicated
     ``_reconcile_path_b_from_bars`` that closes via
     ``_close_path_b_order`` — which sets the lockdown flag.

  2. **Rupee-trailing engine in retroactive mode** — the old replay
     used ``simulation_service._replay_scenario`` (a fixed
     stop/target walk). Path B needs the Rupee-trailing state machine
     (activation, trailing gap, trailing floor, R3 breakout). A new
     ``_replay_path_b_scenario`` implements the Path B state machine
     bar-by-bar; both live reconcile and retroactive close use it.

  3. **Sequential create → simulate → check-lock** — the old
     ``_run_path_b_once`` created all eligible SR orders first and
     only then ran the close pass. In retroactive mode this fabricated
     orders that would never exist after an early target-hit +
     lockdown. The new loop sorts levels by ``confirmed_time`` and,
     for each: creates the order, immediately reconciles it, and
     breaks if ``strategy_locked_today`` is now True.

  4. **Master switch honouring** — ``_path_b_mode`` now returns
     ``A_self_trigger`` when ``STRATEGY_EXT_PATH_B_ENABLED=false``,
     matching the entry-signal service. Previously the flag was
     ignored here, so Path B order creation continued even with
     Path B disabled.

  5. **Legacy record derivation** — the Path B detection now also
     falls back to the persisted ``execution_path`` field (when
     present) and to ``trigger_index != "NIFTY"``. Records missing
     ``trigger_is_self_nifty`` are no longer silently treated as
     Path A.

  6. **Simulated exit timestamps** — ``_close_order`` /
     ``_close_path_b_order`` accept an ``exit_dt`` argument.
     Retroactive closes now use the bar's own time instead of the
     wall-clock moment of processing.

  7. **CE/PE on Path B records** — the ``type`` field now carries
     ``"CE"`` / ``"PE"`` (was ``"BUY"``), and an explicit
     ``instrument_type`` field is added. ``side`` stays ``"BUY"``
     per spec.

  8. **Closed Rupee PnL on Path B** — ``_close_path_b_order``
     persists ``closed_profit_rs`` and ``closed_r_multiple`` so the
     dashboard Rupee PnL strip no longer needs to re-derive from
     ``closed_pnl_pct * entry * lot``.

  9. **Live MFE/MAE tracking** — ``_update_path_b_profit`` now updates
     ``max_high_since_entry`` / ``min_low_since_entry`` / ``mfe`` /
     ``mae`` on every LTP tick, not only during bar reconcile.

 10. **Per-order ``execution_path_at_creation``** — every order (Path
     A and Path B) carries this field so a stale Path A order created
     before a Path B session can be distinguished from a genuine
     Path A order in the dashboard.

Failure modes (all returned in ``status``)
------------------------------------------
    ok            — scan completed
    skipped       — disabled or outside window
    no_signal     — no isolated instrument / no armed candidate
    error         — unexpected exception

Never raises. All expected failure modes land in the returned dict.

Persistence
-----------
Shape of ``data/dummy_orders.json``::

    {
      "day": "2026-10-27",
      "created_at": "...",
      "updated_at": "...",
      "orders": [ { ...order record... }, ... ],
      "execution_path": "A_self_trigger" | "B_external_trigger" | null,
      "strategy_locked_today": false,
      "path_b_placed_keys": [ ... ]
    }

Each order record is **self-contained** — the instrument metadata,
entry details, stop, both targets, current tracking, close state, and
the two notification flags are all in the record.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, time as dtime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from core import config
from core.logger import get_logger
from services.strategy_entry_signal_service import (
    strategy_entry_signal_service,
)

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

def _fmt_time(iso: Any) -> str:
    dt = _parse_iso(iso)
    if dt is None:
        return "—"
    return dt.strftime("%H:%M:%S IST")

# --------------------------------------------------------------------------- #
# Numeric helpers                                                             #
# --------------------------------------------------------------------------- #
def _safe_float(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None

# --------------------------------------------------------------------------- #
# Service                                                                     #
# --------------------------------------------------------------------------- #
class DummyOrderService:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._state: dict[str, Any] = self._empty_state()
        self._by_id: dict[str, dict[str, Any]] = {}
        self._last_reconcile_at: float = 0.0
        self._load_from_disk()

    # ------------------------------------------------------------------ #
    # Public API                                                          #
    # ------------------------------------------------------------------ #
    def run_once(self, reason: str = "manual") -> dict[str, Any]:
        """One full scan. Never raises."""
        t0 = time.monotonic()
        with self._lock:
            try:
                if not bool(getattr(config, "DUMMY_ORDER_ENABLED", True)):
                    return self._skipped(reason, "disabled", t0)

                now = _now_market()
                window_state = self._classify_window(now)
                if window_state == "before_start":
                    return self._skipped(reason, "before_start", t0)

                self._reset_if_new_day(now)

                # -------------------------------------------------- #
                # G1 v4.1 — Path B branch.
                #
                # When the session was isolated by an EXTERNAL index,
                # the 10-minute scan / auto-reverse / promotion cycle
                # from Path A must NOT run. Instead, every confirmed
                # post-isolation SR level on the bias instrument
                # produces its own dummy BUY order.
                # -------------------------------------------------- #
                path_b_ctx = self._path_b_mode()
                if path_b_ctx.get("mode") == "B_external_trigger":
                    if self._state.get("execution_path") != "B_external_trigger":
                        self._state["execution_path"] = "B_external_trigger"
                        logger.info(
                            "[dummy-order] switching to PATH B "
                            "(external-index trigger)"
                        )
                    pb_created, _, pb_closed = self._run_path_b_once(
                        reason, now, window_state, path_b_ctx["iso"],
                    )
                    pb_updated, extra_closed = self._update_all_open(
                        now, window_state,
                    )
                    pb_closed += extra_closed
                    if window_state == "retroactive":
                        pb_closed += self._auto_close_eod(now)
                    self._save(now)
                    logger.info(
                        "[dummy-order] %s — PATH B created=%d updated=%d "
                        "closed=%d open=%d total=%d mode=%s locked=%s",
                        reason, pb_created, pb_updated, pb_closed,
                        self._open_count(),
                        len(self._state.get("orders") or []),
                        window_state,
                        bool(self._state.get("strategy_locked_today")),
                    )
                    return self._summary(
                        reason, "ok", now, window_state, t0,
                        created=pb_created, updated=pb_updated,
                        closed=pb_closed,
                    )
                else:
                    if self._state.get("execution_path") != "A_self_trigger":
                        self._state["execution_path"] = "A_self_trigger"

                # ---- Signal --------------------------------------- #
                try:
                    signal = strategy_entry_signal_service.compute()
                except Exception:
                    logger.exception("[dummy-order] entry-signal compute raised")
                    return self._error(reason, "entry_signal_failed", t0)

                if not isinstance(signal, dict) or signal.get("status") != "ok":
                    return self._no_signal(
                        reason,
                        (signal or {}).get("status") or "no_signal",
                        t0,
                    )

                armed = [
                    c for c in (signal.get("candidates") or [])
                    if c.get("state") == "armed"
                ]
                if not armed:
                    updated, closed = self._update_all_open(now, window_state)
                    if window_state == "retroactive":
                        closed += self._auto_close_eod(now)
                    if updated or closed:
                        self._save(now)
                    return self._no_signal(
                        reason, "no_armed_candidate", t0,
                        updated=updated, closed=closed,
                    )

                # ---- Create + update ------------------------------ #
                created = 0
                updated = 0
                closed = 0

                for cand in armed:
                    for scenario in self._scenarios_for(cand):
                        order, was_created = self._ensure_order(
                            cand, signal, scenario, now, window_state,
                        )
                        if was_created:
                            created += 1

                updated, upd_closed = self._update_all_open(now, window_state)
                closed += upd_closed

                if window_state == "retroactive":
                    closed += self._auto_close_eod(now)

                self._save(now)

                logger.info(
                    "[dummy-order] %s — created=%d updated=%d closed=%d "
                    "open=%d total=%d mode=%s",
                    reason, created, updated, closed,
                    self._open_count(), len(self._state.get("orders") or []),
                    window_state,
                )
                return self._summary(
                    reason, "ok", now, window_state, t0,
                    created=created, updated=updated, closed=closed,
                )

            except Exception:
                logger.exception("[dummy-order] run_once failed")
                return self._error(reason, "unexpected", t0)

    def get_state(self) -> dict[str, Any]:
        """Return the full persisted state (safe to expose)."""
        with self._lock:
            return dict(self._state)

    def get_summary(self) -> dict[str, Any]:
        """Return aggregates only (for a lightweight dashboard poll)."""
        with self._lock:
            return self._summary(
                "read", "ok", _now_market(),
                self._classify_window(_now_market()),
                time.monotonic(),
            )

    def reset(self) -> dict[str, Any]:
        """Clear today's orders. Admin-only — used for testing."""
        with self._lock:
            self._state = self._empty_state()
            self._by_id = {}
            self._save(_now_market())
            logger.info("[dummy-order] state reset")
            return {"status": "reset", "day": self._state.get("day")}

    # ------------------------------------------------------------------ #
    # Path B debug helper                                                 #
    # ------------------------------------------------------------------ #
    def get_path_b_state(self) -> dict[str, Any]:
        """Return the Path B debug state (for admin endpoints)."""
        with self._lock:
            return {
                "execution_path": self._state.get("execution_path"),
                "strategy_locked_today": bool(
                    self._state.get("strategy_locked_today")
                ),
                "placed_keys": list(self._state.get("path_b_placed_keys") or []),
                "open_orders": sum(
                    1 for o in (self._state.get("orders") or [])
                    if isinstance(o, dict)
                    and str(o.get("path") or "") == "B_external_trigger"
                    and o.get("status") == "OPEN"
                ),
                "closed_orders": sum(
                    1 for o in (self._state.get("orders") or [])
                    if isinstance(o, dict)
                    and str(o.get("path") or "") == "B_external_trigger"
                    and o.get("status") != "OPEN"
                ),
            }

    # ------------------------------------------------------------------ #
    # Path B detection helper                                             #
    # ------------------------------------------------------------------ #
    def _path_b_mode(self) -> dict[str, Any]:
        """Return the current isolation's Path B context (or empty).

        G1 v4.2 fixes:

          * The ``STRATEGY_EXT_PATH_B_ENABLED`` master switch is
            honoured. When false, returns ``A_self_trigger`` regardless
            of trigger source, matching the entry-signal service.
          * Legacy records without ``trigger_is_self_nifty`` now
            derive from ``execution_path`` first, then from
            ``trigger_index != "NIFTY"``.
        """
        if not bool(getattr(config, "STRATEGY_EXT_PATH_B_ENABLED", True)):
            return {"mode": "A_self_trigger", "iso": None}

        try:
            from services.level_isolated_instrument_service import (
                level_isolated_instrument_service,
            )
            iso_status = level_isolated_instrument_service.last_status()
        except Exception:
            logger.exception("[dummy-order] path-B iso lookup failed")
            return {"mode": "A_self_trigger", "iso": None}

        iso = (iso_status or {}).get("isolated") or {}
        if not iso:
            return {"mode": None, "iso": None}

        is_self_nifty = iso.get("trigger_is_self_nifty")
        if is_self_nifty is None:
            # Legacy record — derive from execution_path first, then
            # from trigger_index, then default to Path A.
            path_field = str(iso.get("execution_path") or "").strip()
            if path_field == "B_external_trigger":
                is_self_nifty = False
            elif path_field == "A_self_trigger":
                is_self_nifty = True
            else:
                trig = str(iso.get("trigger_index") or "").upper()
                is_self_nifty = (trig == "NIFTY")

        mode = "A_self_trigger" if is_self_nifty else "B_external_trigger"
        return {"mode": mode, "iso": iso}

    # ------------------------------------------------------------------ #
    # Window / mode                                                       #
    # ------------------------------------------------------------------ #
    def _classify_window(self, now: datetime) -> str:
        """Return 'before_start' / 'live' / 'retroactive'."""
        start = dtime(
            int(getattr(config, "DUMMY_ORDER_START_HOUR", 9)),
            int(getattr(config, "DUMMY_ORDER_START_MINUTE", 15)),
        )
        eod = dtime(
            int(getattr(config, "DUMMY_ORDER_EOD_HOUR", 15)),
            int(getattr(config, "DUMMY_ORDER_EOD_MINUTE", 35)),
        )
        t = now.time()
        if t < start:
            return "before_start"
        if t < eod:
            return "live"
        return "retroactive"

    # ------------------------------------------------------------------ #
    # State plumbing                                                      #
    # ------------------------------------------------------------------ #
    def _state_path(self) -> Path:
        return Path(config.LIVE_DATA_DIR) / str(
            getattr(config, "DUMMY_ORDER_FILENAME", "dummy_orders.json")
        )

    def _empty_state(self) -> dict[str, Any]:
        return {
            "day": _today_iso(),
            "created_at": _now_utc_iso(),
            "updated_at": None,
            "orders": [],
            # NEW (G1 v4.1): session-level Path B state.
            "execution_path": None,
            "strategy_locked_today": False,
            "path_b_placed_keys": [],
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
        if not isinstance(payload.get("orders"), list):
            payload["orders"] = []
        self._state = payload
        self._by_id = {
            str(o.get("order_id") or ""): o
            for o in payload["orders"]
            if isinstance(o, dict) and o.get("order_id")
        }

    def _reset_if_new_day(self, now: datetime) -> None:
        today = now.date().isoformat()
        if str(self._state.get("day") or "") != today:
            self._state = self._empty_state()
            self._state["day"] = today
            self._state["created_at"] = _now_utc_iso()
            self._by_id = {}
            logger.info(
                "[dummy-order] new day — state reset for %s", today,
            )

    def _save(self, now: datetime) -> None:
        path = self._state_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._state["updated_at"] = _now_utc_iso()
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(self._state, indent=2, default=str),
                encoding="utf-8",
            )
            tmp.replace(path)
        except Exception:
            logger.exception("[dummy-order] save failed")

    # ------------------------------------------------------------------ #
    # Order creation (Path A — unchanged)                                 #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _scenarios_for(candidate: dict[str, Any]) -> list[str]:
        """Which scenarios to create for this candidate."""
        modes = ["signal_v2"]
        if bool(getattr(config, "DUMMY_ORDER_CREATE_REGIME_VARIANT", True)):
            modes.append("regime")
        return modes

    def _ensure_order(
        self,
        candidate: dict[str, Any],
        signal: dict[str, Any],
        scenario: str,
        now: datetime,
        window_state: str,
    ) -> tuple[dict[str, Any], bool]:
        """Create the order if it doesn't exist. Return (order, was_created)."""

        # G1 v4.1 — Path B owns order creation for the day. Do not
        # create Path A orders on top of it.
        if str(self._state.get("execution_path") or "") == "B_external_trigger":
            return {}, False

        entry = candidate.get("entry") or {}
        entry_price = _safe_float(entry.get("price"))
        entry_time_iso = entry.get("time")

        if entry_price is None or entry_price <= 0 or not entry_time_iso:
            return {}, False

        instrument_key = str(candidate.get("instrument_key") or "")
        side = str(candidate.get("side") or "")
        if not instrument_key or not side:
            return {}, False

        order_id = f"{instrument_key}|{side}|{entry_time_iso}|{scenario}"
        existing = self._by_id.get(order_id)
        if existing is not None:
            return existing, False

        cap = int(getattr(config, "DUMMY_ORDER_MAX_ORDERS_PER_DAY", 100))
        if len(self._state["orders"]) >= cap:
            logger.warning(
                "[dummy-order] cap reached (%d), not creating %s",
                cap, order_id,
            )
            return {}, False

        stop_price = _safe_float(entry.get("stop"))
        if stop_price is None:
            return {}, False

        if scenario == "signal_v2":
            target_price = _safe_float(entry.get("target"))
        else:  # regime
            target_price = self._regime_target_for(candidate, entry_price)
            if target_price is None:
                return {}, False

        if target_price is None or target_price <= entry_price:
            return {}, False

        entry_support = candidate.get("entry_support") or {}
        support_price = _safe_float(entry_support.get("price"))

        stop_pct = (entry_price - stop_price) / entry_price
        target_pct = (target_price - entry_price) / entry_price
        rr = (target_pct / stop_pct) if stop_pct > 0 else None

        created_at = now
        entry_dt = _parse_iso(entry_time_iso)
        latency = (
            int((created_at - entry_dt).total_seconds())
            if entry_dt is not None else 0
        )
        retroactively_created = (
            window_state == "retroactive"
            or latency > 300
        )

        warnings: list[str] = []
        if retroactively_created:
            warnings.append(
                f"created_retroactively: latency {latency}s "
                f"(window_state={window_state})"
            )

        order = {
            "order_id": order_id,
            "dummy": True,
            "scenario": scenario,
            "instrument_key": instrument_key,
            "trading_symbol": candidate.get("trading_symbol"),
            "underlying": (
                signal.get("isolated", {}).get("underlying")
                or None
            ),
            "strike": signal.get("isolated", {}).get("strike"),
            "type": side,
            "instrument_type": side,
            "side": side,
            "is_bias_side": bool(candidate.get("is_bias_side")),
            # G1 v4.2 — every order records which execution path was
            # active when it was created. Lets the dashboard distinguish
            # a legacy Path A order from a genuine Path B session.
            "execution_path_at_creation": "A_self_trigger",

            "entry_time": entry_time_iso,
            "entry_price": round(entry_price, 4),
            "entry_price_source": entry.get("price_source"),
            "support_price": round(support_price, 4) if support_price is not None else None,
            "support_pivot_time": entry_support.get("pivot_time"),
            "support_confirmed_fast": entry_support.get("confirmed_time_fast"),
            "support_confirmed_display": entry_support.get("confirmed_time"),

            "stop_price": round(stop_price, 4),
            "stop_pct": round(stop_pct, 6),
            "sl_pct": entry.get("sl_pct"),
            "stop_source": entry.get("stop_source"),

            "target_price": round(target_price, 4),
            "target_pct": round(target_pct, 6),
            "rr_ratio": round(rr, 4) if rr is not None else None,

            "warnings": warnings,
            "created_at": created_at.isoformat(),
            "created_retroactively": retroactively_created,
            "latency_seconds": latency,

            "status": "OPEN",
            "current_ltp": None,
            "current_ltp_at": None,
            "max_high_since_entry": round(entry_price, 4),
            "min_low_since_entry": round(entry_price, 4),
            "mfe": 0.0,
            "mae": 0.0,
            "current_pnl_pct": 0.0,
            "current_r_multiple": 0.0,

            "last_reconciled_bar_time": entry_time_iso,

            "closed_at": None,
            "closed_price": None,
            "closed_pnl_pct": None,
            "closed_r_multiple": None,
            "closed_mfe": None,
            "closed_mae": None,

            "_notified_created": False,
            "_notified_closed": False,
        }

        self._state["orders"].append(order)
        self._by_id[order_id] = order
        logger.info(
            "[dummy-order] created %s | entry=%.2f stop=%.2f target=%.2f "
            "(scenario=%s, retroactive=%s)",
            order_id, entry_price, stop_price, target_price,
            scenario, retroactively_created,
        )

        self._notify_created(order)

        return order, True

    def _regime_target_for(
        self,
        candidate: dict[str, Any],
        entry_price: float,
    ) -> float | None:
        """Compute the regime-based target for this candidate."""
        try:
            from services.simulation_service import (
                _classify_regime, _read_option_or_levels,
            )
            from services.strategy_entry_signal_service import _run_pivot_scan
        except Exception:
            return None

        key = str(candidate.get("instrument_key") or "")
        side = str(candidate.get("side") or "")

        or_levels = _read_option_or_levels(key, side)
        if or_levels is None:
            return None

        try:
            left = int(getattr(
                config, "STRATEGY_ENTRY_PIVOT_LEFT_BARS",
                config.LIVE_SR_LEFT_BARS,
            ))
            right = int(getattr(
                config, "STRATEGY_ENTRY_PIVOT_RIGHT_BARS", 3,
            ))
            pivots = _run_pivot_scan(key, left, right)
            nearest_r: float | None = None
            for p in pivots:
                if str(p.get("level") or "").lower() != "resistance":
                    continue
                pp = _safe_float(p.get("price"))
                if pp is None or pp <= entry_price:
                    continue
                if nearest_r is None or pp < nearest_r:
                    nearest_r = pp
        except Exception:
            nearest_r = None

        regime = _classify_regime(entry_price, or_levels, nearest_r)
        target = _safe_float(regime.get("target_price"))
        if target is None or target <= entry_price:
            return None

        try:
            from services.simulation_service import _round_to_tick
            tick = _get_tick(key)
            mode = str(getattr(
                config, "SIMULATION_TICK_ROUNDING", "nearest",
            )).lower()
            target = _round_to_tick(target, tick, mode)
        except Exception:
            pass

        return float(target) if target > entry_price else None

    # ------------------------------------------------------------------ #
    # Path B order builder                                                #
    # ------------------------------------------------------------------ #
    def _run_path_b_once(
        self,
        reason: str,
        now: datetime,
        window_state: str,
        iso: dict[str, Any],
    ) -> tuple[int, int, int]:
        """Path B — watch post-isolation SR levels on the bias instrument.

        G1 v4.2 — sequential create → simulate → check-lock. Levels are
        processed in ``confirmed_time`` order so that retroactive replay
        mirrors live behaviour: a target-hit early in the day freezes
        the strategy before later levels can create orders.

        Returns (created, updated, closed).
        """
        created = 0
        updated = 0
        closed = 0

        if bool(self._state.get("strategy_locked_today")):
            return created, updated, closed

        bias_key = str(iso.get("instrument_key") or "").strip()
        if not bias_key:
            return created, updated, closed

        touched_at_iso = iso.get("touched_at") or iso.get("isolated_at") or ""
        touched_dt = _parse_iso(touched_at_iso)

        # Resolve bias entry from registry.
        bias_entry = None
        try:
            from services.instrument_registry import instrument_registry
            bias_entry = instrument_registry.get(bias_key)
        except Exception:
            bias_entry = None
        if bias_entry is None:
            return created, updated, closed

        underlying = str(bias_entry.get("underlying") or "").strip().upper()
        strike = bias_entry.get("strike_price")
        bias_type = str(
            bias_entry.get("strike_type") or iso.get("type") or ""
        ).upper()

        # Whitelist check (NIFTY/SENSEX only per spec).
        tracked = set(getattr(config, "STRATEGY_EXT_TRACKED_UNDERLYINGS", []))
        tracked = {str(u).strip().upper() for u in tracked}
        if underlying not in tracked:
            return created, updated, closed

        lot_size = int(
            getattr(config, "STRATEGY_LOT_SIZE_MAP", {}).get(underlying, 0)
        )
        if lot_size <= 0:
            return created, updated, closed

        counter_key: str | None = None
        if bias_type in {"CE", "PE"} and strike is not None:
            try:
                from services.instrument_registry import instrument_registry
                counter_type = "PE" if bias_type == "CE" else "CE"
                counter_entry = instrument_registry.resolve(
                    underlying, strike, counter_type,
                )
                if counter_entry:
                    counter_key = str(counter_entry.get("instrument_key") or "")
            except Exception:
                counter_key = None
        if not counter_key:
            return created, updated, closed

        # Pull the live SR levels for the BIAS instrument.
        try:
            from services.live_sr_levels_service import (
                live_sr_levels_service,
            )
            sr_payload = live_sr_levels_service.get_levels(bias_key)
        except Exception:
            sr_payload = None
        if not sr_payload:
            return created, updated, closed

        placed_keys = set(self._state.get("path_b_placed_keys") or [])

        # ---- Collect eligible levels, then sort chronologically ---- #
        eligible: list[dict[str, Any]] = []
        for level in (sr_payload.get("levels") or []):
            if not isinstance(level, dict):
                continue
            try:
                level_type = str(level.get("level") or "").lower()
                if level_type not in {"support", "resistance"}:
                    continue
                confirmed_iso = str(level.get("confirmed_time") or "")
                confirmed_dt = _parse_iso(confirmed_iso)
                if confirmed_dt is None:
                    continue
                if touched_dt is not None and confirmed_dt <= touched_dt:
                    continue  # strictly post-isolation only

                try:
                    level_price = float(level.get("price"))
                except (TypeError, ValueError):
                    continue

                dedup = (
                    bias_key,
                    level_type,
                    round(level_price, 4),
                    confirmed_iso,
                )
                dedup_key = "|".join(str(x) for x in dedup)
                if dedup_key in placed_keys:
                    continue

                eligible.append({
                    "level": level,
                    "level_type": level_type,
                    "level_price": level_price,
                    "confirmed_iso": confirmed_iso,
                    "confirmed_dt": confirmed_dt,
                    "dedup_key": dedup_key,
                })
            except Exception:
                logger.exception(
                    "[dummy-order] path B level pre-validation failed for %s",
                    level,
                )

        eligible.sort(key=lambda e: e["confirmed_dt"])

        # ---- Sequential create → simulate → check-lock ----------- #
        for info in eligible:
            if self._state.get("strategy_locked_today"):
                break

            if info["dedup_key"] in placed_keys:
                continue

            try:
                level_type = info["level_type"]
                level_price = info["level_price"]
                confirmed_iso = info["confirmed_iso"]

                target_key = (
                    bias_key if level_type == "support" else counter_key
                )

                # Determine CE / PE of the TARGET instrument.
                target_entry = None
                try:
                    from services.instrument_registry import instrument_registry
                    target_entry = instrument_registry.get(target_key)
                except Exception:
                    target_entry = None

                target_instrument_type = str(
                    (target_entry or {}).get("strike_type")
                    or iso.get("type")
                    or ""
                ).upper()
                if target_instrument_type not in {"CE", "PE"}:
                    # Fallback: derive from bias side.
                    target_instrument_type = (
                        bias_type if target_key == bias_key
                        else ("PE" if bias_type == "CE" else "CE")
                    )

                entry_price, price_source = self._resolve_path_b_entry_price(
                    target_key, confirmed_iso, now,
                )
                if entry_price is None or entry_price <= 0:
                    continue

                stop_price, sl_pct, stop_pct, stop_source = (
                    self._compute_path_b_stop(entry_price, target_key)
                )
                target_price, target_time = self._pick_path_b_target(
                    target_key, entry_price, confirmed_iso,
                )

                target_pct_val: float | None = None
                if target_price is not None and entry_price > 0:
                    target_pct_val = (target_price - entry_price) / entry_price

                order: dict[str, Any] = {
                    "order_id": (
                        f"{target_key}|{level_type.upper()}|{confirmed_iso}"
                    ),
                    "dummy": True,
                    "path": "B_external_trigger",
                    "scenario": "path_b_sr_level",
                    "instrument_key": target_key,
                    "trading_symbol": (
                        (target_entry or {}).get("trading_symbol")
                        or target_key
                    ),
                    "underlying": underlying,
                    "strike": strike,
                    "type": target_instrument_type,
                    "instrument_type": target_instrument_type,
                    "side": "BUY",
                    "is_bias_side": (target_key == bias_key),
                    "execution_path_at_creation": "B_external_trigger",

                    "entry_time": confirmed_iso,
                    "entry_price": round(entry_price, 4),
                    "entry_price_source": price_source,
                    "stop_price": round(stop_price, 4),
                    "stop_pct": round(stop_pct, 6),
                    "sl_pct": sl_pct,
                    "stop_source": stop_source,
                    "target_price": (
                        round(target_price, 4)
                        if target_price is not None else None
                    ),
                    "target_pct": (
                        round(target_pct_val, 6)
                        if target_pct_val is not None else None
                    ),
                    "target_pivot_time": target_time,

                    "lot_size": lot_size,
                    "origin_level": {
                        "instrument": bias_key,
                        "type": level_type,
                        "price": round(level_price, 4),
                        "pivot_time": info["level"].get("pivot_time"),
                        "confirmed_time": confirmed_iso,
                    },

                    "profit_state": "PRE_TRAIL",
                    "peak_profit_rs": 0.0,
                    "trailing_stop_price": None,
                    "current_profit_rs": 0.0,

                    "warnings": [],
                    "created_at": now.isoformat(),
                    "created_retroactively": (
                        window_state == "retroactive"
                    ),
                    "latency_seconds": 0,
                    "status": "OPEN",
                    "current_ltp": None,
                    "current_ltp_at": None,
                    "max_high_since_entry": round(entry_price, 4),
                    "min_low_since_entry": round(entry_price, 4),
                    "mfe": 0.0,
                    "mae": 0.0,
                    "current_pnl_pct": 0.0,
                    "current_r_multiple": 0.0,
                    "last_reconciled_bar_time": confirmed_iso,
                    "closed_at": None,
                    "closed_price": None,
                    "closed_pnl_pct": None,
                    "closed_r_multiple": None,
                    "closed_mfe": None,
                    "closed_mae": None,
                    "_notified_created": False,
                    "_notified_closed": False,
                }

                self._state["orders"].append(order)
                self._by_id[order["order_id"]] = order
                placed_keys.add(info["dedup_key"])
                self._state["path_b_placed_keys"] = sorted(placed_keys)
                created += 1
                logger.info(
                    "[dummy-order] PATH B created %s | level=%s px=%s "
                    "entry=%.2f stop=%.2f target=%s lot=%d inst=%s",
                    order["order_id"], level_type, level_price,
                    entry_price, stop_price, target_price, lot_size,
                    target_instrument_type,
                )
                self._notify_created(order)

                # G1 v4.2 — immediately reconcile this order so that a
                # retroactive replay sees the target-hit → lockdown
                # before later orders are created.
                before_status = order.get("status")
                self._reconcile_path_b_from_bars(
                    order, now,
                    complete_day=(window_state == "retroactive"),
                )
                if (
                    order.get("status") != before_status
                    and order.get("status") != "OPEN"
                ):
                    closed += 1

            except Exception:
                logger.exception(
                    "[dummy-order] path B order build failed for level %s",
                    info.get("level"),
                )

        return created, updated, closed

    # ------------------------------------------------------------------ #
    # Path B helper methods                                               #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _resolve_path_b_entry_price(
        instrument_key: str,
        confirmed_time_iso: str,
        now: datetime,
    ) -> tuple[float | None, str | None]:
        """Resolve entry price for a Path B order.

        Resolution order per spec:
          1. Candle close at confirmed_time_fast (SR engine cache)
          2. Same, from stateless intraday API fallback
          3. Live LTP (degraded — allowed on Path B)
        """
        try:
            from services.strategy_entry_signal_service import (
                _entry_price_for,
            )
            return _entry_price_for(instrument_key, confirmed_time_iso)
        except Exception:
            logger.exception(
                "[dummy-order] path B entry price lookup failed for %s",
                instrument_key,
            )
            return None, None

    @staticmethod
    def _compute_path_b_stop(
        entry_price: float,
        instrument_key: str,
    ) -> tuple[float, float, float, str]:
        try:
            from services.strategy_entry_signal_service import (
                _compute_ltp_tier_stop,
            )
            return _compute_ltp_tier_stop(entry_price, instrument_key)
        except Exception:
            logger.exception(
                "[dummy-order] path B stop calculation failed for %s",
                instrument_key,
            )
            pct = 0.15
            stop = round(entry_price * (1 - pct), 4)
            return stop, pct, pct, "fallback_15pct"

    @staticmethod
    def _pick_path_b_target(
        instrument_key: str,
        entry_price: float,
        entry_confirmed_iso: str,
    ) -> tuple[float | None, str | None]:
        """Return (target_price, target_confirmed_time) for a Path B order."""
        try:
            from services.strategy_entry_signal_service import (
                _run_pivot_scan, _pick_target_resistance,
            )
            left = int(getattr(
                config, "STRATEGY_ENTRY_PIVOT_LEFT_BARS",
                config.LIVE_SR_LEFT_BARS,
            ))
            right = int(getattr(
                config, "STRATEGY_ENTRY_PIVOT_RIGHT_BARS", 3,
            ))
            pivots = _run_pivot_scan(instrument_key, left, right)
            best = _pick_target_resistance(
                pivots, entry_price, entry_confirmed_iso,
            )
            if not best:
                return None, None
            try:
                return float(best.get("price")), best.get("confirmed_time")
            except (TypeError, ValueError):
                return None, None
        except Exception:
            logger.exception(
                "[dummy-order] path B target selection failed for %s",
                instrument_key,
            )
            return None, None

    # ------------------------------------------------------------------ #
    # Order updates                                                       #
    # ------------------------------------------------------------------ #
    def _update_all_open(
        self,
        now: datetime,
        window_state: str,
    ) -> tuple[int, int]:
        """Update all OPEN orders. Return (updated_count, closed_count)."""
        orders = [
            o for o in self._state["orders"]
            if isinstance(o, dict) and o.get("status") == "OPEN"
        ]
        if not orders:
            return 0, 0

        do_reconcile = self._should_reconcile(now) or (
            window_state == "retroactive"
        )
        if do_reconcile:
            self._last_reconcile_at = now.timestamp()

        updated = 0
        closed = 0

        for order in orders:
            if order.get("status") != "OPEN":
                continue
            before = order.get("status")

            if window_state == "live":
                ltp = self._get_ltp(str(order.get("instrument_key") or ""))
                if ltp is not None:
                    self._update_from_ltp(order, ltp, now)

            if do_reconcile or window_state == "retroactive":
                self._reconcile_from_bars(
                    order, now,
                    complete_day=(window_state == "retroactive"),
                )

            if order.get("status") != before:
                closed += 1
            updated += 1

        return updated, closed

    def _should_reconcile(self, now: datetime) -> bool:
        minutes = int(getattr(config, "DUMMY_ORDER_RECONCILE_MINUTES", 2))
        if minutes <= 0:
            return False
        elapsed = now.timestamp() - self._last_reconcile_at
        return elapsed >= minutes * 60

    @staticmethod
    def _get_ltp(instrument_key: str) -> float | None:
        try:
            from services.live_feed_service import live_feed_service
            return live_feed_service.get_latest_ltp(instrument_key)
        except Exception:
            return None

    def _update_from_ltp(
        self,
        order: dict[str, Any],
        ltp: float,
        now: datetime,
    ) -> None:
        """Fast LTP-only update: high/low watermarks + stop/target check."""
        entry = _safe_float(order.get("entry_price"))
        stop = _safe_float(order.get("stop_price"))
        target = _safe_float(order.get("target_price"))

        # G1 v4.1 — Path B Rupee-based profit engine
        if str(order.get("path") or "") == "B_external_trigger":
            self._update_path_b_profit(order, ltp, now)
            return

        if entry is None or stop is None or target is None:
            return

        order["current_ltp"] = round(ltp, 4)
        order["current_ltp_at"] = now.isoformat()

        order["max_high_since_entry"] = round(
            max(_safe_float(order.get("max_high_since_entry")) or entry, ltp),
            4,
        )
        order["min_low_since_entry"] = round(
            min(_safe_float(order.get("min_low_since_entry")) or entry, ltp),
            4,
        )
        order["mfe"] = round(
            (_safe_float(order.get("max_high_since_entry")) or entry) - entry,
            4,
        )
        order["mae"] = round(
            (_safe_float(order.get("min_low_since_entry")) or entry) - entry,
            4,
        )
        order["current_pnl_pct"] = round((ltp - entry) / entry, 6) if entry else 0.0
        stop_pct = (entry - stop) / entry if entry else 0.0
        order["current_r_multiple"] = (
            round(order["current_pnl_pct"] / stop_pct, 4)
            if stop_pct > 0 else 0.0
        )

        if ltp <= stop:
            self._close_order(order, "STOP_HIT", stop, now)
            return
        if ltp >= target:
            self._close_order(order, "TARGET_HIT", target, now)
            return

    # ------------------------------------------------------------------ #
    # Path B Rupee-based profit engine                                    #
    # ------------------------------------------------------------------ #
    def _update_path_b_profit(
        self,
        order: dict[str, Any],
        ltp: float,
        now: datetime,
    ) -> None:
        """Path B Rupee-based trailing profit engine.

        G1 v4.2 — also updates the price-based watermarks
        (``max_high_since_entry`` / ``min_low_since_entry`` / ``mfe`` /
        ``mae``) so the dashboard reflects them on every LTP tick
        instead of only during bar reconcile.
        """
        entry = _safe_float(order.get("entry_price"))
        lot = int(order.get("lot_size") or 0)
        target = _safe_float(order.get("target_price"))
        if entry is None or lot <= 0:
            return

        order["current_ltp"] = round(ltp, 4)
        order["current_ltp_at"] = now.isoformat()

        # Price-based watermarks (dashboard MFE/MAE columns).
        order["max_high_since_entry"] = round(
            max(_safe_float(order.get("max_high_since_entry")) or entry, ltp),
            4,
        )
        order["min_low_since_entry"] = round(
            min(_safe_float(order.get("min_low_since_entry")) or entry, ltp),
            4,
        )
        order["mfe"] = round(
            order["max_high_since_entry"] - entry, 4,
        )
        order["mae"] = round(
            order["min_low_since_entry"] - entry, 4,
        )

        # Rupee-based profit tracking.
        profit_rs = (ltp - entry) * lot
        order["current_profit_rs"] = round(profit_rs, 2)
        peak = max(
            float(order.get("peak_profit_rs") or 0.0), profit_rs,
        )
        order["peak_profit_rs"] = round(peak, 2)

        # ---- Activation (PRE_TRAIL → TRAILING) ------------------- #
        if order.get("profit_state") == "PRE_TRAIL":
            activation_rs = float(getattr(
                config, "STRATEGY_EXT_PROFIT_ACTIVATION_RS", 1000.0,
            ))
            r3_breakout_hit = self._path_b_r3_breakout(order, ltp)
            if peak >= activation_rs or r3_breakout_hit:
                order["profit_state"] = "TRAILING"
                logger.info(
                    "[dummy-order] PATH B order %s switched to TRAILING "
                    "(peak_rs=%.2f, r3=%s)",
                    order.get("order_id"), peak, r3_breakout_hit,
                )

        # ---- Target hit (highest priority) ----------------------- #
        if target is not None and ltp >= target:
            self._close_path_b_order(
                order, "TARGET_HIT", target, now,
            )
            return

        # ---- Trailing-stop hit ----------------------------------- #
        if order.get("profit_state") == "TRAILING":
            trail = self._compute_path_b_trailing_stop(order)
            if trail is not None:
                order["trailing_stop_price"] = round(trail, 4)
                if ltp <= trail:
                    self._close_path_b_order(
                        order, "TRAILING_STOP_HIT", trail, now,
                    )
                    return
            # ---- Bearish-regime exit ----------------------------- #
            if bool(getattr(config, "STRATEGY_EXT_BEARISH_EXIT_ENABLED", True)):
                if self._is_bearish_regime(order):
                    self._close_path_b_order(
                        order, "BEARISH_EXIT", ltp, now,
                    )
                    return
        else:
            # PRE_TRAIL: original fixed stop.
            stop = _safe_float(order.get("stop_price"))
            if stop is not None and ltp <= stop:
                self._close_path_b_order(order, "STOP_HIT", stop, now)
                return

    def _compute_path_b_trailing_stop(
        self,
        order: dict[str, Any],
    ) -> float | None:
        """Return the current trailing stop price.

        lock_rs = max(TRAIL_FLOOR_RS, peak_profit_rs - TRAIL_GAP_RS)
        trail   = entry + lock_rs / lot_size
        """
        entry = _safe_float(order.get("entry_price"))
        lot = int(order.get("lot_size") or 0)
        peak = float(order.get("peak_profit_rs") or 0.0)
        if entry is None or lot <= 0:
            return None

        gap = float(getattr(config, "STRATEGY_EXT_TRAIL_GAP_RS", 500.0))
        floor = float(getattr(config, "STRATEGY_EXT_TRAIL_FLOOR_RS", 1000.0))
        lock_rs = max(floor, peak - gap)
        return entry + (lock_rs / lot)

    def _path_b_r3_breakout(
        self,
        order: dict[str, Any],
        ltp: float,
    ) -> bool:
        if not bool(getattr(
            config, "STRATEGY_EXT_R3_BREAKOUT_ACTIVATION_ENABLED", True,
        )):
            return False
        try:
            from services.simulation_service import _read_option_or_levels
            side = (
                str(order.get("instrument_type") or "").upper()
                or str(order.get("side") or "").upper()
            )
            or_levels = _read_option_or_levels(
                order.get("instrument_key"), side,
            )
            if not or_levels:
                return False
            r2 = _safe_float(or_levels.get("R2"))
            r3 = _safe_float(or_levels.get("R3"))
            entry = _safe_float(order.get("entry_price"))
            if r2 is None or r3 is None or entry is None:
                return False
            return entry < r2 and ltp >= r3
        except Exception:
            return False

    def _is_bearish_regime(self, order: dict[str, Any]) -> bool:
        """Return True when 2/3 bearish criteria are met on the order."""
        try:
            n = int(getattr(config, "STRATEGY_EXT_BEARISH_CANDLES", 2))
            instrument_key = str(order.get("instrument_key") or "")
            if not instrument_key:
                return False

            from services.live_sr_calc_service import live_sr_calc_service
            candles = live_sr_calc_service.fetch_intraday_candles_for_entry(
                instrument_key,
            )
            if not candles or len(candles) < n:
                return False

            tail = candles[-n:]
            bearish_candles = all(
                _safe_float(c.get("close")) is not None
                and _safe_float(c.get("open")) is not None
                and float(c["close"]) < float(c["open"])
                for c in tail
            )

            nifty_ltp = None
            try:
                from services.live_feed_service import live_feed_service
                nifty_ltp = live_feed_service.get_latest_ltp(
                    "NSE_INDEX|Nifty 50"
                )
            except Exception:
                nifty_ltp = None

            support_price = None
            origin = order.get("origin_level") or {}
            if str(origin.get("type") or "").lower() == "support":
                support_price = _safe_float(origin.get("price"))

            nifty_below_support = (
                nifty_ltp is not None
                and support_price is not None
                and float(nifty_ltp) < support_price
            )

            nifty_lower_closes = False
            if nifty_ltp is not None:
                try:
                    from services.live_sr_calc_service import (
                        live_sr_calc_service as _calc,
                    )
                    nifty_candles = (
                        _calc.fetch_intraday_candles_for_entry(
                            "NSE_INDEX|Nifty 50"
                        )
                    )
                    if nifty_candles and len(nifty_candles) >= n:
                        tail_n = nifty_candles[-n:]
                        nifty_lower_closes = all(
                            _safe_float(tail_n[i].get("close")) is not None
                            and _safe_float(tail_n[i - 1].get("close")) is not None
                            and float(tail_n[i]["close"]) < float(tail_n[i - 1]["close"])
                            for i in range(1, len(tail_n))
                        )
                except Exception:
                    pass

            score = sum(
                1 for x in (bearish_candles, nifty_below_support, nifty_lower_closes)
                if x
            )
            return score >= 2
        except Exception:
            logger.exception("[dummy-order] bearish regime check failed")
            return False

    # ------------------------------------------------------------------ #
    # Path B bar replay                                                   #
    # ------------------------------------------------------------------ #
    def _replay_path_b_scenario(
        self,
        *,
        candles: list[dict[str, Any]],
        entry_time_iso: str,
        entry_price: float,
        stop_price: float,
        target_price: float | None,
        lot_size: int,
        or_levels: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Replay the Rupee-trailing Path B state machine bar-by-bar.

        Returns ``None`` when the replay cannot run (missing candles,
        invalid entry time, or no post-entry bars). Otherwise returns:

            exit_price        — the price at which the order would exit
            exit_time         — bar time of the exit
            exit_reason       — TARGET_HIT / TRAILING_STOP_HIT /
                                STOP_HIT / END_OF_DAY
            max_high          — max bar high since entry
            min_low           — min bar low since entry
            peak_profit_rs    — max Rupee profit since entry
            profit_state_final — "TRAILING" or "PRE_TRAIL"
        """
        if lot_size <= 0:
            return None

        entry_dt = _parse_iso(entry_time_iso)
        if entry_dt is None:
            return None

        same_bar_stop_first = bool(getattr(
            config, "SIMULATION_SAME_BAR_STOP_FIRST", True,
        ))
        activation_rs = float(getattr(
            config, "STRATEGY_EXT_PROFIT_ACTIVATION_RS", 1000.0,
        ))
        gap = float(getattr(config, "STRATEGY_EXT_TRAIL_GAP_RS", 500.0))
        floor = float(getattr(
            config, "STRATEGY_EXT_TRAIL_FLOOR_RS", 1000.0,
        ))

        r3_breakout_enabled = bool(getattr(
            config, "STRATEGY_EXT_R3_BREAKOUT_ACTIVATION_ENABLED", True,
        ))
        r2 = _safe_float((or_levels or {}).get("R2"))
        r3 = _safe_float((or_levels or {}).get("R3"))
        r3_activation_possible = (
            r3_breakout_enabled
            and r2 is not None
            and r3 is not None
            and entry_price < r2
        )

        profit_state = "PRE_TRAIL"
        peak_rs = 0.0
        max_high = entry_price
        min_low = entry_price

        exit_price: float | None = None
        exit_time: str | None = None
        exit_reason: str | None = None

        for candle in candles:
            c_dt = _parse_iso(candle.get("time"))
            if c_dt is None or c_dt <= entry_dt:
                continue

            hi = _safe_float(candle.get("high"))
            lo = _safe_float(candle.get("low"))
            if hi is None or lo is None:
                continue

            max_high = max(max_high, hi)
            min_low = min(min_low, lo)

            bar_peak = (hi - entry_price) * lot_size
            peak_rs = max(peak_rs, bar_peak)

            was_trailing = (profit_state == "TRAILING")
            activated_here = False
            if not was_trailing:
                if peak_rs >= activation_rs:
                    activated_here = True
                elif r3_activation_possible and hi >= r3:
                    activated_here = True

            in_trailing = was_trailing or activated_here

            if in_trailing:
                lock_rs = max(floor, peak_rs - gap)
                adverse = entry_price + (lock_rs / lot_size)
                adverse_reason = "TRAILING_STOP_HIT"
            else:
                adverse = stop_price
                adverse_reason = "STOP_HIT"

            # Conservative: if PRE_TRAIL at bar start but activates
            # mid-bar, and the bar's low hits the ORIGINAL stop, assume
            # the stop was hit first (before activation could lift the
            # stop).
            if (
                not was_trailing
                and activated_here
                and lo <= stop_price
                and same_bar_stop_first
            ):
                adverse = stop_price
                adverse_reason = "STOP_HIT"

            hit_target = target_price is not None and hi >= target_price
            hit_adverse = lo <= adverse

            if hit_target and hit_adverse:
                if same_bar_stop_first:
                    exit_price = adverse
                    exit_reason = adverse_reason
                else:
                    exit_price = target_price
                    exit_reason = "TARGET_HIT"
                exit_time = str(candle.get("time"))
                break

            if hit_target:
                exit_price = target_price
                exit_reason = "TARGET_HIT"
                exit_time = str(candle.get("time"))
                break

            if hit_adverse:
                exit_price = adverse
                exit_reason = adverse_reason
                exit_time = str(candle.get("time"))
                break

            # No exit — roll state forward.
            if in_trailing:
                profit_state = "TRAILING"

        if exit_price is None:
            # Unresolved — walk backwards for the last close.
            last_close = None
            last_time = None
            for candle in reversed(candles):
                c_dt = _parse_iso(candle.get("time"))
                if c_dt is None or c_dt <= entry_dt:
                    continue
                cl = _safe_float(candle.get("close"))
                if cl is not None:
                    last_close = cl
                    last_time = str(candle.get("time"))
                    break
            if last_close is None:
                return None
            exit_price = last_close
            exit_time = last_time
            exit_reason = "END_OF_DAY"

        return {
            "exit_price": exit_price,
            "exit_time": exit_time,
            "exit_reason": exit_reason,
            "max_high": max_high,
            "min_low": min_low,
            "peak_profit_rs": peak_rs,
            "profit_state_final": profit_state,
        }

    # ------------------------------------------------------------------ #
    # Close paths                                                         #
    # ------------------------------------------------------------------ #
    def _close_order(
        self,
        order: dict[str, Any],
        reason: str,
        price: float,
        exit_dt: datetime | None = None,
    ) -> None:
        """Close ANY order. Dispatches to the Path A or Path B close.

        ``exit_dt`` is the actual exit timestamp — for retroactive
        reconciles this is the bar time, for live LTP closes it is
        the wall time. When ``None``, falls back to ``_now_market()``.
        """
        if exit_dt is None:
            exit_dt = _now_market()

        if str(order.get("path") or "") == "B_external_trigger":
            self._close_path_b_order(order, reason, price, exit_dt)
            return
        self._close_path_a_order(order, reason, price, exit_dt)

    def _close_path_a_order(
        self,
        order: dict[str, Any],
        reason: str,
        price: float,
        exit_dt: datetime,
    ) -> None:
        """Path A close: fixed stop/target logic."""
        if order.get("status") != "OPEN":
            return

        entry = _safe_float(order.get("entry_price"))
        stop = _safe_float(order.get("stop_price"))
        if entry is None or entry <= 0:
            return
        pnl = (price - entry) / entry
        stop_pct = ((entry - stop) / entry) if stop is not None else 0.0
        r = (pnl / stop_pct) if stop_pct > 0 else 0.0

        order["status"] = reason
        order["closed_at"] = exit_dt.isoformat()
        order["closed_price"] = round(price, 4)
        order["closed_pnl_pct"] = round(pnl, 6)
        order["closed_r_multiple"] = round(r, 4)
        order["closed_mfe"] = round(_safe_float(order.get("mfe")) or 0.0, 4)
        order["closed_mae"] = round(_safe_float(order.get("mae")) or 0.0, 4)
        order["current_ltp"] = round(price, 4)
        order["current_ltp_at"] = exit_dt.isoformat()

        # Defensive: persist Rupee PnL when a lot size is available.
        lot = int(order.get("lot_size") or 0)
        if lot > 0:
            order["closed_profit_rs"] = round((price - entry) * lot, 2)

        logger.info(
            "[dummy-order] closed %s | reason=%s price=%.2f pnl=%+.2f%% R=%+.2f",
            order.get("order_id"), reason, price, pnl * 100, r,
        )

        self._notify_closed(order)

    def _close_path_b_order(
        self,
        order: dict[str, Any],
        reason: str,
        price: float,
        exit_dt: datetime,
    ) -> None:
        """Close a Path B order and trigger strategy lockdown if needed."""
        if order.get("status") != "OPEN":
            return

        entry = _safe_float(order.get("entry_price"))
        lot = int(order.get("lot_size") or 0)
        stop = _safe_float(order.get("stop_price"))
        if entry is None or entry <= 0 or lot <= 0:
            return

        profit_rs = (price - entry) * lot
        pnl_pct = (price - entry) / entry
        stop_pct = ((entry - stop) / entry) if stop is not None else 0.0
        r_multiple = (pnl_pct / stop_pct) if stop_pct > 0 else 0.0

        order["status"] = reason
        order["closed_at"] = exit_dt.isoformat()
        order["closed_price"] = round(price, 4)
        order["closed_pnl_pct"] = round(pnl_pct, 6)
        order["closed_profit_rs"] = round(profit_rs, 2)
        order["closed_r_multiple"] = round(r_multiple, 4)
        order["closed_mfe"] = round(
            _safe_float(order.get("mfe")) or 0.0, 4,
        )
        order["closed_mae"] = round(
            _safe_float(order.get("mae")) or 0.0, 4,
        )
        order["current_ltp"] = round(price, 4)
        order["current_ltp_at"] = exit_dt.isoformat()

        logger.info(
            "[dummy-order] PATH B closed %s | reason=%s price=%.2f "
            "profit_rs=%.2f pnl=%+.2f%% R=%+.2f",
            order.get("order_id"), reason, price, profit_rs,
            pnl_pct * 100, r_multiple,
        )

        # Target hit → strategy lockdown.
        if (
            reason == "TARGET_HIT"
            and bool(getattr(config, "STRATEGY_EXT_LOCKDOWN_ON_TARGET", True))
            and not bool(self._state.get("strategy_locked_today"))
        ):
            self._state["strategy_locked_today"] = True
            logger.warning(
                "[dummy-order] STRATEGY LOCKDOWN — Path B target hit on %s",
                order.get("order_id"),
            )

        self._notify_closed(order)

    # ------------------------------------------------------------------ #
    # Bar-level reconcile                                                 #
    # ------------------------------------------------------------------ #
    def _reconcile_from_bars(
        self,
        order: dict[str, Any],
        now: datetime,
        complete_day: bool,
    ) -> None:
        """Dispatch to the Path A or Path B bar reconcile."""
        if order.get("status") != "OPEN":
            return

        if str(order.get("path") or "") == "B_external_trigger":
            self._reconcile_path_b_from_bars(order, now, complete_day)
            return
        self._reconcile_path_a_from_bars(order, now, complete_day)

    def _reconcile_path_a_from_bars(
        self,
        order: dict[str, Any],
        now: datetime,
        complete_day: bool,
    ) -> None:
        """Path A reconcile: fixed stop/target walk."""
        if order.get("status") != "OPEN":
            return
        try:
            from services.live_sr_calc_service import live_sr_calc_service
            from services.simulation_service import _replay_scenario
        except Exception:
            return

        key = str(order.get("instrument_key") or "")
        if not key:
            return

        candles = live_sr_calc_service.fetch_intraday_candles_for_entry(key)
        if not candles:
            return

        if not complete_day:
            now_ts = now.timestamp()
            trimmed = []
            for c in candles:
                c_dt = _parse_iso(c.get("time"))
                if c_dt is None:
                    continue
                if c_dt.timestamp() <= now_ts:
                    trimmed.append(c)
            candles = trimmed or candles

        entry_iso = order.get("entry_time")
        entry_price = _safe_float(order.get("entry_price"))
        stop_price = _safe_float(order.get("stop_price"))
        target_price = _safe_float(order.get("target_price"))
        if entry_iso is None or entry_price is None or stop_price is None \
                or target_price is None:
            return

        replay = _replay_scenario(
            candles=candles,
            entry_time_iso=entry_iso,
            entry_price=entry_price,
            stop_price=stop_price,
            target_price=target_price,
            instrument_key=key,
        )
        if not isinstance(replay, dict) or replay.get("status") != "replayed":
            return

        max_high = _safe_float(replay.get("max_high"))
        min_low = _safe_float(replay.get("min_low"))
        if max_high is not None:
            order["max_high_since_entry"] = round(max_high, 4)
        if min_low is not None:
            order["min_low_since_entry"] = round(min_low, 4)
        order["mfe"] = round(_safe_float(replay.get("mfe")) or 0.0, 4)
        order["mae"] = round(_safe_float(replay.get("mae")) or 0.0, 4)
        if candles:
            order["last_reconciled_bar_time"] = candles[-1].get("time")

        reason = str(replay.get("exit_reason") or "")
        if reason in {"STOP_HIT", "STOP_HIT_SAME_BAR_CONSERVATIVE"}:
            self._close_order(order, "STOP_HIT", stop_price, now)
            return
        if reason in {"TARGET_HIT", "TARGET_HIT_SAME_BAR_OPTIMISTIC"}:
            self._close_order(order, "TARGET_HIT", target_price, now)
            return

    def _reconcile_path_b_from_bars(
        self,
        order: dict[str, Any],
        now: datetime,
        complete_day: bool,
    ) -> None:
        """Path B reconcile: Rupee-trailing state machine."""
        if order.get("status") != "OPEN":
            return
        try:
            from services.live_sr_calc_service import live_sr_calc_service
        except Exception:
            return

        key = str(order.get("instrument_key") or "")
        if not key:
            return

        candles = live_sr_calc_service.fetch_intraday_candles_for_entry(key)
        if not candles:
            return

        if not complete_day:
            now_ts = now.timestamp()
            trimmed = []
            for c in candles:
                c_dt = _parse_iso(c.get("time"))
                if c_dt is None:
                    continue
                if c_dt.timestamp() <= now_ts:
                    trimmed.append(c)
            candles = trimmed or candles

        entry_iso = order.get("entry_time")
        entry_price = _safe_float(order.get("entry_price"))
        stop_price = _safe_float(order.get("stop_price"))
        target_price = _safe_float(order.get("target_price"))
        lot_size = int(order.get("lot_size") or 0)

        if (
            entry_iso is None
            or entry_price is None
            or stop_price is None
            or lot_size <= 0
        ):
            return

        # OR levels for the R3-breakout activation trigger.
        or_levels = None
        try:
            from services.simulation_service import _read_option_or_levels
            side = (
                str(order.get("instrument_type") or "").upper()
                or str(order.get("side") or "").upper()
            )
            or_levels = _read_option_or_levels(key, side)
        except Exception:
            or_levels = None

        replay = self._replay_path_b_scenario(
            candles=candles,
            entry_time_iso=entry_iso,
            entry_price=entry_price,
            stop_price=stop_price,
            target_price=target_price,
            lot_size=lot_size,
            or_levels=or_levels,
        )
        if replay is None:
            return

        order["max_high_since_entry"] = round(replay["max_high"], 4)
        order["min_low_since_entry"] = round(replay["min_low"], 4)
        order["mfe"] = round(replay["max_high"] - entry_price, 4)
        order["mae"] = round(replay["min_low"] - entry_price, 4)
        order["peak_profit_rs"] = round(replay["peak_profit_rs"], 2)
        if replay.get("profit_state_final"):
            order["profit_state"] = replay["profit_state_final"]

        if candles:
            order["last_reconciled_bar_time"] = candles[-1].get("time")

        exit_reason = replay.get("exit_reason")
        exit_price = replay.get("exit_price")
        exit_time_iso = replay.get("exit_time")
        exit_dt = _parse_iso(exit_time_iso) if exit_time_iso else None

        # Live mode: END_OF_DAY just means "no exit yet in available
        # candles". Keep the order OPEN for the next reconcile.
        if exit_reason == "END_OF_DAY" and not complete_day:
            return

        if exit_reason and exit_price is not None:
            if exit_dt is None:
                exit_dt = now
            self._close_path_b_order(order, exit_reason, exit_price, exit_dt)

    def _auto_close_eod(self, now: datetime) -> int:
        """Close any OPEN orders at EOD. Returns count closed."""
        closed = 0
        for order in list(self._state["orders"]):
            if not isinstance(order, dict) or order.get("status") != "OPEN":
                continue

            self._reconcile_from_bars(order, now, complete_day=True)
            if order.get("status") != "OPEN":
                closed += 1
                continue

            last_price, last_time = self._last_bar_close_and_time(order)
            if last_price is not None:
                exit_dt = _parse_iso(last_time) if last_time else now
                self._close_order(
                    order, "END_OF_DAY", last_price, exit_dt or now,
                )
                closed += 1
        return closed

    def _last_bar_close_and_time(
        self,
        order: dict[str, Any],
    ) -> tuple[float | None, str | None]:
        try:
            from services.live_sr_calc_service import live_sr_calc_service
        except Exception:
            return None, None
        key = str(order.get("instrument_key") or "")
        if not key:
            return None, None
        candles = live_sr_calc_service.fetch_intraday_candles_for_entry(key)
        if not candles:
            return None, None
        last = candles[-1]
        return _safe_float(last.get("close")), last.get("time")

    # ------------------------------------------------------------------ #
    # Telegram notifications                                              #
    # ------------------------------------------------------------------ #
    def _notify_created(self, order: dict[str, Any]) -> None:
        """Fire the CREATED Telegram once per order. Never raises."""
        if not bool(getattr(config, "DUMMY_ORDER_TELEGRAM_ENABLED", True)):
            return
        if order.get("_notified_created"):
            return
        try:
            from services.telegram_format import build
            from services.telegram_service import telegram_service

            entry = _safe_float(order.get("entry_price")) or 0.0
            stop = _safe_float(order.get("stop_price")) or 0.0
            target = _safe_float(order.get("target_price")) or 0.0
            stop_pct = _safe_float(order.get("stop_pct")) or 0.0
            target_pct = _safe_float(order.get("target_pct")) or 0.0
            rr = _safe_float(order.get("rr_ratio"))
            scenario = str(order.get("scenario") or "")
            side = str(order.get("side") or "")
            inst_type = str(
                order.get("instrument_type")
                or order.get("type")
                or ""
            ).upper()
            lot = int(order.get("lot_size") or 0)

            # Path B orders use CE/PE on the type field; Path A uses the
            # option side. Display whichever is meaningful.
            type_label = inst_type if inst_type in {"CE", "PE"} else side
            if side == "BUY" and inst_type in {"CE", "PE"}:
                type_label = inst_type
                side_display = f"{inst_type} · BUY"
            else:
                side_display = f"{side} · {scenario}"

            warnings = order.get("warnings") or []
            notes_lines = warnings if warnings else ["—"]

            extra_fields: list[tuple[str, Any]] = []
            origin = order.get("origin_level") or {}
            if origin:
                extra_fields.append((
                    "Origin level",
                    f"{origin.get('type', '?').upper()} @ "
                    f"₹{_safe_float(origin.get('price')) or 0.0:.2f}",
                ))
                if origin.get("confirmed_time"):
                    extra_fields.append((
                        "Level confirmed",
                        _fmt_time(origin["confirmed_time"]),
                    ))
            if lot > 0:
                extra_fields.append(("Lot size", lot))

            title = f"Dummy Order · Created · {scenario}"
            fields = [
                (
                    "Instrument",
                    order.get("trading_symbol") or order.get("instrument_key"),
                ),
                ("Key", order.get("instrument_key")),
                ("Type", type_label or side),
                ("Scenario", scenario),
                (
                    "Entry",
                    f"₹{entry:.2f} @ {_fmt_time(order.get('entry_time'))}",
                ),
                (
                    "Stop",
                    f"₹{stop:.2f} (−{stop_pct * 100:.1f}%)",
                ),
                (
                    "Target",
                    f"₹{target:.2f} (+{target_pct * 100:.1f}%)"
                    if target > 0 else "—",
                ),
                (
                    "R:R (nominal)",
                    f"{rr:.2f} : 1" if rr is not None else "—",
                ),
            ] + extra_fields

            msg = build(
                emoji="📝",
                title=title,
                fields=fields,
                sections=[("Notes", "📝", notes_lines)],
                status="info",
            )
            telegram_service.send_message(msg, level="INFO")
            order["_notified_created"] = True
        except Exception:
            logger.exception("[dummy-order] telegram notify (created) failed")

    def _notify_closed(self, order: dict[str, Any]) -> None:
        """Fire the CLOSED Telegram once per order. Never raises."""
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
            r = _safe_float(order.get("closed_r_multiple")) or 0.0
            mfe = _safe_float(order.get("closed_mfe")) or 0.0
            mae = _safe_float(order.get("closed_mae")) or 0.0
            profit_rs = _safe_float(order.get("closed_profit_rs"))
            status = str(order.get("status") or "")
            scenario = str(order.get("scenario") or "")
            side = str(order.get("side") or "")
            inst_type = str(
                order.get("instrument_type")
                or order.get("type")
                or ""
            ).upper()

            type_label = inst_type if inst_type in {"CE", "PE"} else side
            side_display = (
                f"{inst_type} · BUY"
                if side == "BUY" and inst_type in {"CE", "PE"}
                else f"{side} · {scenario}"
            )

            status_label = status.replace("_", " ")
            log_level = {
                "TARGET_HIT": "SUCCESS",
                "STOP_HIT": "WARNING",
                "TRAILING_STOP_HIT": "WARNING",
                "BEARISH_EXIT": "INFO",
                "END_OF_DAY": "INFO",
            }.get(status, "INFO")
            build_status = "success" if pnl >= 0 else "warning"

            pnl_str = f"{pnl * 100:+.2f}%"
            if profit_rs is not None:
                pnl_str = f"{pnl_str} ({'+' if profit_rs >= 0 else ''}₹{profit_rs:.2f})"

            title = f"Dummy Order · Closed · {status_label}"
            msg = build(
                emoji="🏁",
                title=title,
                fields=[
                    (
                        "Instrument",
                        order.get("trading_symbol") or order.get("instrument_key"),
                    ),
                    ("Key", order.get("instrument_key")),
                    ("Type", type_label or side),
                    ("Scenario", scenario),
                    (
                        "Exit",
                        f"{status_label} @ ₹{exit_price:.2f}",
                    ),
                    ("Closed at", _fmt_time(order.get("closed_at"))),
                    ("PnL", pnl_str),
                    ("R-multiple", f"{r:+.2f}R"),
                    ("MFE", f"{mfe:+.2f}"),
                    ("MAE", f"{mae:+.2f}"),
                ],
                status=build_status,
                status_detail=f"{pnl * 100:+.2f}% · {r:+.2f}R",
            )
            telegram_service.send_message(msg, level=log_level)
            order["_notified_closed"] = True
        except Exception:
            logger.exception("[dummy-order] telegram notify (closed) failed")

    # ------------------------------------------------------------------ #
    # Summary builders                                                    #
    # ------------------------------------------------------------------ #
    def _open_count(self) -> int:
        return sum(
            1 for o in self._state.get("orders") or []
            if isinstance(o, dict) and o.get("status") == "OPEN"
        )

    def _summary(
        self,
        reason: str,
        status: str,
        now: datetime,
        window_state: str,
        t0: float,
        *,
        created: int = 0,
        updated: int = 0,
        closed: int = 0,
    ) -> dict[str, Any]:
        orders = [
            o for o in (self._state.get("orders") or [])
            if isinstance(o, dict)
        ]
        open_orders = [o for o in orders if o.get("status") == "OPEN"]
        closed_orders = [o for o in orders if o.get("status") != "OPEN"]

        closed_pnls = [
            _safe_float(o.get("closed_pnl_pct"))
            for o in closed_orders
        ]
        closed_pnls = [p for p in closed_pnls if p is not None]
        closed_rs = [
            _safe_float(o.get("closed_r_multiple"))
            for o in closed_orders
        ]
        closed_rs = [r for r in closed_rs if r is not None]

        # Sum of per-trade Rupee PnL (accurate for both paths).
        closed_rs_rupees: list[float] = []
        for o in closed_orders:
            val = _safe_float(o.get("closed_profit_rs"))
            if val is not None:
                closed_rs_rupees.append(val)

        by_scenario: dict[str, dict[str, Any]] = {}
        for scenario in ("signal_v2", "regime", "path_b_sr_level"):
            subset = [o for o in orders if o.get("scenario") == scenario]
            subset_closed = [o for o in subset if o.get("status") != "OPEN"]
            pnls = [_safe_float(o.get("closed_pnl_pct")) for o in subset_closed]
            pnls = [p for p in pnls if p is not None]
            rs = [_safe_float(o.get("closed_r_multiple")) for o in subset_closed]
            rs = [r for r in rs if r is not None]
            rs_rupees = [
                _safe_float(o.get("closed_profit_rs"))
                for o in subset_closed
            ]
            rs_rupees = [v for v in rs_rupees if v is not None]
            by_scenario[scenario] = {
                "count": len(subset),
                "open": sum(1 for o in subset if o.get("status") == "OPEN"),
                "closed": len(subset_closed),
                "sum_pnl_pct": round(sum(pnls), 6) if pnls else 0.0,
                "sum_r_multiple": round(sum(rs), 4) if rs else 0.0,
                "sum_pnl_rs": round(sum(rs_rupees), 2) if rs_rupees else 0.0,
            }

        # Path A vs Path B counts — uses execution_path_at_creation
        # when present, falling back to ``path`` for legacy records.
        path_a = 0
        path_b = 0
        for o in orders:
            ep = o.get("execution_path_at_creation")
            if ep is None:
                ep = (
                    "B_external_trigger"
                    if str(o.get("path") or "") == "B_external_trigger"
                    else "A_self_trigger"
                )
            if ep == "B_external_trigger":
                path_b += 1
            else:
                path_a += 1

        return {
            "status": status,
            "reason": reason,
            "day": self._state.get("day"),
            "mode": window_state,
            "created": created,
            "updated": updated,
            "closed": closed,
            "open_count": len(open_orders),
            "closed_count": len(closed_orders),
            "total_today": len(orders),
            # NOTE: ``day_pnl_pct`` is a simple sum of per-trade pnl
            # percentages, NOT a portfolio return. Kept for backward
            # compatibility; the dashboard should label it accordingly.
            "day_pnl_pct": round(sum(closed_pnls), 6) if closed_pnls else 0.0,
            "day_r_multiple": round(sum(closed_rs), 4) if closed_rs else 0.0,
            "day_pnl_rs": (
                round(sum(closed_rs_rupees), 2)
                if closed_rs_rupees else 0.0
            ),
            "avg_pnl_pct": (
                round(sum(closed_pnls) / len(closed_pnls), 6)
                if closed_pnls else 0.0
            ),
            "avg_r_multiple": (
                round(sum(closed_rs) / len(closed_rs), 4)
                if closed_rs else 0.0
            ),
            "by_scenario": by_scenario,
            "path_a_count": path_a,
            "path_b_count": path_b,
            "execution_path": self._state.get("execution_path"),
            "strategy_locked_today": bool(
                self._state.get("strategy_locked_today")
            ),
            "path_b_placed_count": len(
                self._state.get("path_b_placed_keys") or []
            ),
            "elapsed_seconds": round(time.monotonic() - t0, 3),
            "generated_at": _now_utc_iso(),
        }

    def _skipped(self, reason: str, why: str, t0: float) -> dict[str, Any]:
        return {
            "status": "skipped",
            "reason": reason,
            "skip_reason": why,
            "day": self._state.get("day"),
            "open_count": self._open_count(),
            "total_today": len(self._state.get("orders") or []),
            "elapsed_seconds": round(time.monotonic() - t0, 3),
            "generated_at": _now_utc_iso(),
        }

    def _no_signal(
        self,
        reason: str,
        why: str,
        t0: float,
        *,
        updated: int = 0,
        closed: int = 0,
    ) -> dict[str, Any]:
        return {
            "status": "no_signal",
            "reason": reason,
            "detail": why,
            "day": self._state.get("day"),
            "updated": updated,
            "closed": closed,
            "open_count": self._open_count(),
            "total_today": len(self._state.get("orders") or []),
            "elapsed_seconds": round(time.monotonic() - t0, 3),
            "generated_at": _now_utc_iso(),
        }

    def _error(self, reason: str, why: str, t0: float) -> dict[str, Any]:
        return {
            "status": "error",
            "reason": reason,
            "error": why,
            "elapsed_seconds": round(time.monotonic() - t0, 3),
            "generated_at": _now_utc_iso(),
        }


# --------------------------------------------------------------------------- #
# Tick-size helper                                                            #
# --------------------------------------------------------------------------- #
def _get_tick(instrument_key: str) -> float:
    try:
        from services.instrument_registry import instrument_registry
        entry = instrument_registry.get(instrument_key)
        if entry:
            ts = _safe_float(entry.get("tick_size"))
            if ts is not None and ts > 0:
                return ts
    except Exception:
        pass
    return 0.05


# --------------------------------------------------------------------------- #
# Singleton                                                                   #
# --------------------------------------------------------------------------- #
dummy_order_service = DummyOrderService()