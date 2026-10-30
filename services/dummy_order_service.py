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

Feed subscription (2026 capacity policy)
----------------------------------------
Every order created by this service — Path A signal_v2, Path A
regime, Path B support, and Path B resistance — calls
``_pin_instrument()`` after the order record is appended to
``_state["orders"]``. That helper delegates to
``live_feed_service.subscribe_instrument()``, which pins the key in
the ``feed_subscription_manager`` on-demand LRU tier and ensures the
streamer is actually subscribed. The net effect: the order's LTP
updates keep arriving for the rest of the session, even if the
per-index ATM band slides away from the strike after entry.

Path B's counter-side instrument especially depends on this — it is
not part of the mandatory set that ``live_feed_service.start()``
rebuilds from ``level_isolated_instrument_service`` +
``isolated_instrument_promotion_service`` on restart, so without the
pin its LTP would freeze at the entry price after a mid-session
reload. The same is true for Path A's regime-scenario orders on
strikes that drift outside the ATM band during a trend day.

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

G1 v4.2 — pivot-right-bars fallback hardening (2026-10-30)
-----------------------------------------------------------
Two call-sites that delegate to ``_run_pivot_scan`` for the fast
pivot window previously used a hard-coded ``3`` as the ``getattr``
fallback for ``STRATEGY_ENTRY_PIVOT_RIGHT_BARS``. The attribute
always exists today, so the fallback is dead code — but it would
silently reintroduce the old right=3 behaviour if the config line
were ever removed. Both call-sites now fall back to
``config.LIVE_SR_RIGHT_BARS`` (which itself defaults to ``1``), so
the pivot scan tracks the display window by construction:

    * ``_regime_target_for()``   — nearest-resistance target search
    * ``_pick_path_b_target()``  — Path B target selection
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
                    pb_created, _, _ = self._run_path_b_once(
                        reason, now, window_state, path_b_ctx["iso"],
                    )
                    pb_updated, pb_closed = self._update_all_open(
                        now, window_state,
                    )
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
    # NEW (G1 v4.1): Path B debug helper                                  #
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
                "consecutive_losses": int(self._state.get("consecutive_losses") or 0),
                "daily_realized_pnl_rs": float(self._state.get("daily_realized_pnl_rs") or 0.0),
                "lock_reason": self._state.get("lock_reason"),
                "trailing_stop_mode": getattr(config, "STRATEGY_TRAILING_STOP_MODE", "hybrid"),
            }

    # ------------------------------------------------------------------ #
    # NEW (G1 v4.1): Path B detection helper                              #
    # ------------------------------------------------------------------ #
    def _path_b_mode(self) -> dict[str, Any]:
        """Return the current isolation's Path B context (or empty)."""
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
            # Legacy record — derive defensively from trigger index.
            is_self_nifty = (
                str(iso.get("trigger_index") or "").upper() == "NIFTY"
            )
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
            "execution_path": None,          # "A_self_trigger" | "B_external_trigger" | None
            "strategy_locked_today": False,  # set True when any Path B trade hits target
            "path_b_placed_keys": [],        # dedup key list for Path B orders
            # NEW (G1 v4.2): risk management & circuit breaker tracking
            "consecutive_losses": 0,
            "daily_realized_pnl_rs": 0.0,
            "lock_reason": None,
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
    # NEW (2026 capacity policy): feed-subscription pinning               #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _pin_instrument(instrument_key: str) -> None:
        """Ensure the dummy order's instrument is live-subscribed.

        Called whenever this service creates a Path A or Path B order.
        Delegates to ``live_feed_service.subscribe_instrument()``, which
        (as of the 2026 capacity policy) does two things atomically:

          1. Pins the key in ``feed_subscription_manager``'s on-demand
             LRU tier so it is protected from ATM-band eviction.
          2. Ensures the streamer is actually subscribed (idempotent if
             the key is already in ``_subscribed``).

        This matters for the counter side of a Path B pair: on a
        mid-session restart, ``live_feed_service._build_initial_
        subscription_set()`` only re-adds the four mandatory index rows
        + health feed + the currently isolated / promoted key. The
        counter-side option is not in that set, so without this pin its
        LTP would not update and the trailing / target checks would
        freeze at the entry price for the rest of the session. Pinning
        restores the live feed for that instrument for the entire day.

        The same protection applies to Path A regime-scenario orders on
        strikes that drift outside the ATM band during a trending
        session — without the pin they would stop ticking as soon as
        the band slides away from the strike.

        Never raises. Failures are logged and do not block order
        creation — a dummy order that is not live-subscribed still
        gets reconciled from the intraday 1-min bars via
        ``_reconcile_from_bars()``.
        """
        key = str(instrument_key or "").strip()
        if not key:
            return
        try:
            from services.live_feed_service import live_feed_service
            live_feed_service.subscribe_instrument(key)
        except Exception:
            logger.exception(
                "[dummy-order] feed subscribe failed for %s", key
            )

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
            "side": side,
            "is_bias_side": bool(candidate.get("is_bias_side")),

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

            # G1 v4.2 ATR trailing stop fields for Path A
            "atr_14": (
                _safe_float(entry.get("atr"))
                or (
                    __import__('services.regime_detector_service', fromlist=['regime_detector_service']).regime_detector_service.calculate_instrument_atr(instrument_key)
                    if bool(getattr(config, "STRATEGY_TRAILING_STOP_ENABLED", True)) else None
                )
            ),
            "trailing_stop_mode": getattr(config, "STRATEGY_TRAILING_STOP_MODE", "hybrid"),
            "profit_state": "PRE_TRAIL",
            "trailing_stop_price": None,

            "closed_at": None,
            "closed_price": None,
            "closed_pnl_pct": None,
            "closed_r_multiple": None,
            "closed_mfe": None,
            "closed_mae": None,

            # Notification dedup flags — survive restart because
            # they live on the persisted record.
            "_notified_created": False,
            "_notified_closed": False,
        }

        self._state["orders"].append(order)
        self._by_id[order_id] = order

        # Pin the instrument in the feed-subscription manager so the
        # order's LTP keeps ticking for the rest of the day even if the
        # ATM band slides away from this strike. See
        # ``_pin_instrument`` for the full rationale.
        self._pin_instrument(instrument_key)

        logger.info(
            "[dummy-order] created %s | entry=%.2f stop=%.2f target=%.2f "
            "(scenario=%s, retroactive=%s)",
            order_id, entry_price, stop_price, target_price,
            scenario, retroactively_created,
        )

        # Notify (once per order)
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
                config, "STRATEGY_ENTRY_PIVOT_RIGHT_BARS",
                config.LIVE_SR_RIGHT_BARS,
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
    # NEW (G1 v4.1): Path B order builder                                 #
    # ------------------------------------------------------------------ #
    def _run_path_b_once(
        self,
        reason: str,
        now: datetime,
        window_state: str,
        iso: dict[str, Any],
    ) -> tuple[int, int, int]:
        """Path B — watch post-isolation SR levels on the bias instrument.

        Returns (created, updated, closed).
        """
        created = 0
        updated = 0
        closed = 0

        if bool(self._state.get("strategy_locked_today")):
            # Day is frozen. Existing open orders continue their own
            # trailing loops via _update_all_open (called by caller).
            return created, updated, closed

        bias_key = str(iso.get("instrument_key") or "").strip()
        if not bias_key:
            return created, updated, closed

        touched_at_iso = iso.get("touched_at") or iso.get("isolated_at") or ""
        touched_dt = _parse_iso(touched_at_iso)

        # Resolve counter side from the same strike.
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

        # G1 v4.2 Circuit Breaker & Consecutive Loss Check
        consecutive_losses = int(self._state.get("consecutive_losses") or 0)
        halt_limit = int(getattr(config, "STRATEGY_RISK_MAX_CONSECUTIVE_LOSSES_HALT", 5))
        if consecutive_losses >= halt_limit:
            self._state["strategy_locked_today"] = True
            self._state["lock_reason"] = f"consecutive_losses_circuit_breaker: {consecutive_losses}"
            logger.warning("[dummy-order] Halting Path B: %d consecutive losses circuit breaker hit", consecutive_losses)
            return created, updated, closed

        # Position Sizing Throttle (50% size after 3 consecutive losses)
        throttle_limit = int(getattr(config, "STRATEGY_RISK_MAX_CONSECUTIVE_LOSSES_THROTTLE", 3))
        effective_lot_size = lot_size
        is_risk_throttled = False
        if consecutive_losses >= throttle_limit:
            effective_lot_size = max(1, lot_size // 2)
            is_risk_throttled = True
            logger.info("[dummy-order] Throttling Path B lot size from %d to %d due to %d consecutive losses", lot_size, effective_lot_size, consecutive_losses)

        # G1 v4.2 Market Regime & ADX Filter Check for Path B
        regime_info: dict[str, Any] | None = None
        if bool(getattr(config, "STRATEGY_REGIME_FILTER_ENABLED", True)):
            try:
                from services.regime_detector_service import regime_detector_service
                underlying_key = "NSE_INDEX|Nifty 50" if underlying == "NIFTY" else ("BSE_INDEX|SENSEX" if underlying == "SENSEX" else underlying)
                regime_info = regime_detector_service.evaluate_market_regime(underlying_key)
            except Exception:
                regime_info = None

        if (
            regime_info is not None
            and bool(getattr(config, "STRATEGY_REGIME_GATE_ENTRIES", True))
            and regime_info.get("regime") == "RANGING"
        ):
            logger.info(
                "[dummy-order] PATH B skipping SR orders: market is RANGING (ADX=%.2f < %.2f)",
                regime_info.get("adx", 0.0), regime_info.get("adx_threshold", 22.0),
            )
            return created, updated, closed

        placed_keys = set(self._state.get("path_b_placed_keys") or [])

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

                price_raw = level.get("price")
                try:
                    level_price = float(price_raw)
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

                target_key = bias_key if level_type == "support" else counter_key
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

                target_entry = None
                try:
                    from services.instrument_registry import instrument_registry
                    target_entry = instrument_registry.get(target_key)
                except Exception:
                    target_entry = None

                order = {
                    "order_id": (
                        f"{target_key}|{level_type.upper()}|{confirmed_iso}"
                    ),
                    "dummy": True,
                    "path": "B_external_trigger",
                    "scenario": "path_b_sr_level",
                    "instrument_key": target_key,
                    "trading_symbol": (
                        target_entry.get("trading_symbol")
                        if target_entry else target_key
                    ),
                    "underlying": underlying,
                    "strike": strike,
                    "type": "BUY",
                    "side": "BUY",
                    "is_bias_side": (target_key == bias_key),

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
                    "target_pivot_time": target_time,

                    "lot_size": effective_lot_size,
                    "is_risk_throttled": is_risk_throttled,
                    "consecutive_losses_at_entry": consecutive_losses,
                    "market_regime": regime_info,
                    "atr_14": (
                        __import__('services.regime_detector_service', fromlist=['regime_detector_service']).regime_detector_service.calculate_instrument_atr(target_key)
                        or round(entry_price * 0.05, 4)
                    ),
                    "trailing_stop_mode": getattr(config, "STRATEGY_TRAILING_STOP_MODE", "hybrid"),
                    "origin_level": {
                        "instrument": bias_key,
                        "type": level_type,
                        "price": round(level_price, 4),
                        "pivot_time": level.get("pivot_time"),
                        "confirmed_time": confirmed_iso,
                    },

                    # ---- Rupee-based profit engine state ---------------- #
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
                placed_keys.add(dedup_key)
                self._state["path_b_placed_keys"] = sorted(placed_keys)
                created += 1

                # Pin the target instrument (bias OR counter side) so
                # Path B's Rupee-profit trailing keeps getting live
                # ticks even after the ATM band slides away from the
                # strike. Critical for the counter side, which is not
                # in the initial mandatory set on restart — without
                # this pin its LTP would freeze at the entry price
                # for the rest of the session. See
                # ``_pin_instrument`` for the full rationale.
                self._pin_instrument(target_key)

                logger.info(
                    "[dummy-order] PATH B created %s | level=%s px=%s "
                    "entry=%.2f stop=%.2f target=%s lot=%d",
                    order["order_id"], level_type, level_price,
                    entry_price, stop_price, target_price, lot_size,
                )
                self._notify_created(order)
            except Exception:
                logger.exception(
                    "[dummy-order] path B order build failed for level %s",
                    level,
                )

        return created, updated, closed

    # ------------------------------------------------------------------ #
    # NEW (G1 v4.1): Path B helper methods                                #
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
            # Conservative fallback — 15% floor.
            pct = 0.15
            stop = round(entry_price * (1 - pct), 4)
            return stop, pct, pct, "fallback_15pct"

    @staticmethod
    def _pick_path_b_target(
        instrument_key: str,
        entry_price: float,
        entry_confirmed_iso: str,
    ) -> tuple[float | None, str | None]:
        """Return (target_price, target_confirmed_time) for a Path B order.

        Uses the same `_pick_target_resistance` helper that Path A uses.
        """
        try:
            from services.strategy_entry_signal_service import (
                _run_pivot_scan, _pick_target_resistance,
            )
            left = int(getattr(
                config, "STRATEGY_ENTRY_PIVOT_LEFT_BARS",
                config.LIVE_SR_LEFT_BARS,
            ))
            right = int(getattr(
                config, "STRATEGY_ENTRY_PIVOT_RIGHT_BARS",
                config.LIVE_SR_RIGHT_BARS,
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

        # ---- G1 v4.1 — Path B Rupee-based profit engine ----------- #
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

        # ---- G1 v4.2 ATR Trailing Stop for Path A ---------------- #
        if bool(getattr(config, "STRATEGY_TRAILING_STOP_ENABLED", True)):
            atr = _safe_float(order.get("atr_14"))
            if atr is not None and atr > 0:
                act_mult = float(getattr(config, "STRATEGY_TRAILING_ACTIVATION_PROFIT_ATR", 1.2))
                step_mult = float(getattr(config, "STRATEGY_TRAILING_TRAIL_STEP_ATR", 0.5))
                activation_dist = act_mult * atr
                step_dist = step_mult * atr

                max_high = _safe_float(order.get("max_high_since_entry")) or ltp
                advance = max_high - entry

                # Check activation: price reached entry + 1.2 * ATR
                if advance >= activation_dist:
                    order["profit_state"] = "TRAILING"
                    trail = entry
                    excess = advance - activation_dist
                    if excess >= step_dist and step_dist > 0:
                        steps = int(excess // step_dist)
                        trail = entry + (steps * step_dist)

                    curr_trail = _safe_float(order.get("trailing_stop_price"))
                    if curr_trail is not None:
                        trail = max(curr_trail, trail)
                    order["trailing_stop_price"] = round(trail, 4)

                    if ltp <= trail:
                        self._close_order(order, "TRAILING_STOP_HIT", trail, now)
                        return

        if ltp <= stop:
            self._close_order(order, "STOP_HIT", stop, now)
            return
        if ltp >= target:
            self._close_order(order, "TARGET_HIT", target, now)
            return

    # ------------------------------------------------------------------ #
    # NEW (G1 v4.1): Path B Rupee-based profit engine                     #
    # ------------------------------------------------------------------ #
    def _update_path_b_profit(
        self,
        order: dict[str, Any],
        ltp: float,
        now: datetime,
    ) -> None:
        """Path B Rupee-based trailing profit engine."""
        entry = _safe_float(order.get("entry_price"))
        lot = int(order.get("lot_size") or 0)
        target = _safe_float(order.get("target_price"))
        if entry is None or lot <= 0:
            return

        order["current_ltp"] = round(ltp, 4)
        order["current_ltp_at"] = now.isoformat()

        profit_rs = (ltp - entry) * lot
        order["current_profit_rs"] = round(profit_rs, 2)
        peak = max(
            float(order.get("peak_profit_rs") or 0.0), profit_rs,
        )
        order["peak_profit_rs"] = round(peak, 2)

        # ---- Activation check (PRE_TRAIL -> TRAILING) ------------ #
        if order.get("profit_state") == "PRE_TRAIL":
            activation_rs = float(getattr(
                config, "STRATEGY_EXT_PROFIT_ACTIVATION_RS", 1000.0,
            ))
            r3_breakout_hit = self._path_b_r3_breakout(order, ltp)

            atr = _safe_float(order.get("atr_14"))
            act_mult = float(getattr(config, "STRATEGY_TRAILING_ACTIVATION_PROFIT_ATR", 1.2))
            atr_act_hit = (atr is not None and atr > 0 and (ltp - entry) >= (act_mult * atr))

            if peak >= activation_rs or r3_breakout_hit or atr_act_hit:
                order["profit_state"] = "TRAILING"
                logger.info(
                    "[dummy-order] PATH B order %s switched to TRAILING "
                    "(peak_rs=%.2f, r3=%s, atr_act=%s)",
                    order.get("order_id"), peak, r3_breakout_hit, atr_act_hit,
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

        Supports Rupee mode, ATR mode, and Hybrid mode (G1 v4.2).
        """
        entry = _safe_float(order.get("entry_price"))
        lot = int(order.get("lot_size") or 0)
        peak = float(order.get("peak_profit_rs") or 0.0)
        if entry is None or lot <= 0:
            return None

        mode = str(getattr(config, "STRATEGY_TRAILING_STOP_MODE", "hybrid")).lower()

        # 1. Rupee-based calculation
        gap = float(getattr(config, "STRATEGY_EXT_TRAIL_GAP_RS", 500.0))
        floor = float(getattr(config, "STRATEGY_EXT_TRAIL_FLOOR_RS", 1000.0))
        lock_rs = max(floor, peak - gap)
        rupee_trail = entry + (lock_rs / lot)
        if mode == "rupee":
            return rupee_trail

        # 2. ATR-based calculation
        atr = _safe_float(order.get("atr_14"))
        atr_trail = None
        if atr is not None and atr > 0:
            act_mult = float(getattr(config, "STRATEGY_TRAILING_ACTIVATION_PROFIT_ATR", 1.2))
            step_mult = float(getattr(config, "STRATEGY_TRAILING_TRAIL_STEP_ATR", 0.5))
            max_high = _safe_float(order.get("max_high_since_entry")) or entry
            advance = max_high - entry
            activation_dist = act_mult * atr
            step_dist = step_mult * atr

            if advance >= activation_dist:
                trail_base = entry
                excess = advance - activation_dist
                if excess >= step_dist and step_dist > 0:
                    steps = int(excess // step_dist)
                    trail_base = entry + (steps * step_dist)
                atr_trail = trail_base

        if mode == "atr":
            return atr_trail or rupee_trail

        # 3. Hybrid mode: choose tighter (higher) trailing stop
        if atr_trail is not None:
            return max(rupee_trail, atr_trail)
        return rupee_trail

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
            or_levels = _read_option_or_levels(
                order.get("instrument_key"),
                order.get("side"),
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

    def _close_path_b_order(
        self,
        order: dict[str, Any],
        reason: str,
        price: float,
        now: datetime,
    ) -> None:
        """Close a Path B order and trigger strategy lockdown if needed."""
        entry = _safe_float(order.get("entry_price"))
        lot = int(order.get("lot_size") or 0)
        if entry is None or entry <= 0 or lot <= 0:
            return

        profit_rs = (price - entry) * lot
        pnl_pct = (price - entry) / entry

        order["status"] = reason
        order["closed_at"] = now.isoformat()
        order["closed_price"] = round(price, 4)
        order["closed_pnl_pct"] = round(pnl_pct, 6)
        order["closed_profit_rs"] = round(profit_rs, 2)
        order["current_ltp"] = round(price, 4)
        order["current_ltp_at"] = now.isoformat()

        # Update consecutive loss counter & cumulative daily realized PnL
        is_loss = (profit_rs < 0)
        if is_loss:
            self._state["consecutive_losses"] = int(self._state.get("consecutive_losses") or 0) + 1
        else:
            self._state["consecutive_losses"] = 0

        # Check circuit breaker halt
        halt_limit = int(getattr(config, "STRATEGY_RISK_MAX_CONSECUTIVE_LOSSES_HALT", 5))
        if self._state["consecutive_losses"] >= halt_limit and not bool(self._state.get("strategy_locked_today")):
            self._state["strategy_locked_today"] = True
            self._state["lock_reason"] = f"consecutive_losses_circuit_breaker: {self._state['consecutive_losses']}"
            logger.warning("[dummy-order] %d consecutive losses circuit breaker hit. Strategy locked.", self._state["consecutive_losses"])

        tot_pnl = float(self._state.get("daily_realized_pnl_rs") or 0.0) + profit_rs
        self._state["daily_realized_pnl_rs"] = round(tot_pnl, 2)

        # Check Daily Max Drawdown Guard
        max_dd_rs = float(getattr(config, "STRATEGY_RISK_DAILY_MAX_DRAWDOWN_RS", 3000.0))
        if tot_pnl <= -max_dd_rs and not bool(self._state.get("strategy_locked_today")):
            self._state["strategy_locked_today"] = True
            self._state["lock_reason"] = f"daily_drawdown_limit_exceeded: -₹{abs(tot_pnl):.2f}"
            logger.warning("[dummy-order] Daily max drawdown exceeded (-₹%.2f). Strategy locked.", abs(tot_pnl))

        logger.info(
            "[dummy-order] PATH B closed %s | reason=%s price=%.2f "
            "profit_rs=%.2f pnl=%+.2f%% (consec_losses=%d, tot_pnl_rs=%.2f)",
            order.get("order_id"), reason, price, profit_rs, pnl_pct * 100,
            self._state.get("consecutive_losses", 0), tot_pnl,
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

    def _reconcile_from_bars(
        self,
        order: dict[str, Any],
        now: datetime,
        complete_day: bool,
    ) -> None:
        """Bar-level reconcile: pull intraday candles, replay unseen bars."""
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

    def _close_order(
        self,
        order: dict[str, Any],
        reason: str,
        price: float,
        now: datetime,
    ) -> None:
        entry = _safe_float(order.get("entry_price"))
        stop = _safe_float(order.get("stop_price"))
        if entry is None or entry <= 0:
            return
        pnl = (price - entry) / entry
        stop_pct = ((entry - stop) / entry) if stop is not None else 0.0
        r = (pnl / stop_pct) if stop_pct > 0 else 0.0

        order["status"] = reason
        order["closed_at"] = now.isoformat()
        order["closed_price"] = round(price, 4)
        order["closed_pnl_pct"] = round(pnl, 6)
        order["closed_r_multiple"] = round(r, 4)
        order["closed_mfe"] = round(_safe_float(order.get("mfe")) or 0.0, 4)
        order["closed_mae"] = round(_safe_float(order.get("mae")) or 0.0, 4)
        order["current_ltp"] = round(price, 4)
        order["current_ltp_at"] = now.isoformat()

        # Update consecutive loss counter & daily drawdown
        is_loss = (pnl < 0)
        if is_loss:
            self._state["consecutive_losses"] = int(self._state.get("consecutive_losses") or 0) + 1
        else:
            self._state["consecutive_losses"] = 0

        halt_limit = int(getattr(config, "STRATEGY_RISK_MAX_CONSECUTIVE_LOSSES_HALT", 5))
        if self._state["consecutive_losses"] >= halt_limit and not bool(self._state.get("strategy_locked_today")):
            self._state["strategy_locked_today"] = True
            self._state["lock_reason"] = f"consecutive_losses_circuit_breaker: {self._state['consecutive_losses']}"
            logger.warning("[dummy-order] %d consecutive losses circuit breaker hit. Strategy locked.", self._state["consecutive_losses"])

        logger.info(
            "[dummy-order] closed %s | reason=%s price=%.2f pnl=%+.2f%% R=%+.2f (consec_losses=%d)",
            order.get("order_id"), reason, price, pnl * 100, r, self._state.get("consecutive_losses", 0),
        )

        # Notify (once per order)
        self._notify_closed(order)

    def _auto_close_eod(self, now: datetime) -> int:
        """Close any OPEN orders at EOD. Returns count closed."""
        closed = 0
        for order in self._state["orders"]:
            if not isinstance(order, dict) or order.get("status") != "OPEN":
                continue

            self._reconcile_from_bars(order, now, complete_day=True)
            if order.get("status") != "OPEN":
                closed += 1
                continue

            last_price = self._last_bar_close(order)
            if last_price is not None:
                self._close_order(order, "END_OF_DAY", last_price, now)
                closed += 1
        return closed

    def _last_bar_close(self, order: dict[str, Any]) -> float | None:
        try:
            from services.live_sr_calc_service import live_sr_calc_service
        except Exception:
            return None
        key = str(order.get("instrument_key") or "")
        if not key:
            return None
        candles = live_sr_calc_service.fetch_intraday_candles_for_entry(key)
        if not candles:
            return None
        return _safe_float(candles[-1].get("close"))

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

            warnings = order.get("warnings") or []
            notes_lines = warnings if warnings else ["—"]

            title = f"Dummy Order · Created · {scenario}"
            msg = build(
                emoji="📝",
                title=title,
                fields=[
                    (
                        "Instrument",
                        order.get("trading_symbol") or order.get("instrument_key"),
                    ),
                    ("Key", order.get("instrument_key")),
                    ("Side / Scenario", f"{side} · {scenario}"),
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
                        f"₹{target:.2f} (+{target_pct * 100:.1f}%)",
                    ),
                    (
                        "R:R (nominal)",
                        f"{rr:.2f} : 1" if rr is not None else "—",
                    ),
                ],
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
            status = str(order.get("status") or "")
            scenario = str(order.get("scenario") or "")
            side = str(order.get("side") or "")

            status_label = status.replace("_", " ")
            log_level = {
                "TARGET_HIT": "SUCCESS",
                "STOP_HIT": "WARNING",
                "END_OF_DAY": "INFO",
            }.get(status, "INFO")
            build_status = "success" if pnl >= 0 else "warning"

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
                    ("Side / Scenario", f"{side} · {scenario}"),
                    (
                        "Exit",
                        f"{status_label} @ ₹{exit_price:.2f}",
                    ),
                    ("Closed at", _fmt_time(order.get("closed_at"))),
                    ("PnL", f"{pnl * 100:+.2f}%"),
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

        by_scenario: dict[str, dict[str, Any]] = {}
        for scenario in ("signal_v2", "regime"):
            subset = [o for o in orders if o.get("scenario") == scenario]
            subset_closed = [o for o in subset if o.get("status") != "OPEN"]
            pnls = [_safe_float(o.get("closed_pnl_pct")) for o in subset_closed]
            pnls = [p for p in pnls if p is not None]
            rs = [_safe_float(o.get("closed_r_multiple")) for o in subset_closed]
            rs = [r for r in rs if r is not None]
            by_scenario[scenario] = {
                "count": len(subset),
                "open": sum(1 for o in subset if o.get("status") == "OPEN"),
                "closed": len(subset_closed),
                "day_pnl_pct": round(sum(pnls), 6) if pnls else 0.0,
                "day_r_multiple": round(sum(rs), 4) if rs else 0.0,
            }

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
            "day_pnl_pct": round(sum(closed_pnls), 6) if closed_pnls else 0.0,
            "day_r_multiple": round(sum(closed_rs), 4) if closed_rs else 0.0,
            "avg_pnl_pct": (
                round(sum(closed_pnls) / len(closed_pnls), 6)
                if closed_pnls else 0.0
            ),
            "avg_r_multiple": (
                round(sum(closed_rs) / len(closed_rs), 4)
                if closed_rs else 0.0
            ),
            "by_scenario": by_scenario,
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