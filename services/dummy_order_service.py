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
      "orders": [ { ...order record... }, ... ]
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
    # Order creation                                                      #
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
        logger.info(
            "[dummy-order] closed %s | reason=%s price=%.2f pnl=%+.2f%% R=%+.2f",
            order.get("order_id"), reason, price, pnl * 100, r,
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