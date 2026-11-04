# services/dummy_order_service.py
"""Post-isolation dummy order execution engine — t_feed_v1-market-engine-v3.

Implements the *Isolated Instrument Order Strategy & Execution Engine*
specification:

    1. Once an instrument is ISOLATED at ``T_iso`` (bias-side CE for R3
       or PE for S3 touch), watch its 15/3 SR levels.
    2. Trigger = first SR level on the bias-side with
       ``pivot_time > T_iso`` AND ``confirmed_time > T_iso``.
    3. Routing (with 09:30 IST backup reversal):
         * 09:30 reversal — post-isolation confirmation is on or before
           09:30 IST, AND a prior Support was confirmed before T_iso,
           AND the current post-isolation level is Resistance
           → buy the SAME isolated instrument.
         * Post-isolation Support → buy SAME isolated instrument.
         * Post-isolation Resistance → buy OPPOSITE leg instrument
           (CE ↔ PE at the same strike).
    4. Regime validation using the traded instrument's OWN OR grid
       (option ``first_candle.json`` → ``or_levels``):
         * P_entry > R2                    → SKIP (no valid logic above R2)
         * P_entry < S2                    → milestones: OR Low,
                                              +50 pt gain
         * S2 ≤ P_entry < R1               → milestones: R2, R3_Threshold, R3
         * R1 ≤ P_entry ≤ R2 (Zone 2)      → milestones: R3_Threshold, R3
    5. Stop-loss = P_entry × (1 − 0.30) — fixed 30 % below fill.
    6. Stage 1: price touches any armed milestone OR net gain ≥ 50 pts.
       Stage 2: exit at the NEXT 15/3 resistance confirmation close on
       the traded instrument (strictly after the Stage 2 trigger time).
    7. EOD: auto-close any surviving OPEN order at the last available
       bar close.

This service NEVER places a real Upstox order. Every record carries
``"dummy": true`` so audits cannot confuse it with a real order.

Persistence
-----------
``<LIVE_DATA_DIR>/dummy_orders.json`` — single JSON document per day,
reset on IST date rollover.  Structure::

    {
      "day": "2026-10-29",
      "execution_path": "A_self_trigger" | "B_external_trigger",
      "isolated_key": "NSE_FO|53792",
      "bias_type": "CE",
      "t_iso": "2026-10-29T09:18:00+05:30",
      "order": { ... } | null,
      "orders": [ ... ]  # 0- or 1-element compat list
    }

The ``order`` record is self-contained — routing mode, trigger level,
prior support, traded key, entry price + source, stop, OR levels,
zone, milestones, profit_state, Stage 2 trigger time/reason, tracking
watermarks, close state, and the two Telegram notification flags.

Never raises. Every failure mode lands in the returned summary dict
and in the state file.
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

def _log_calc_regime(
    zone: dict[str, Any],
    entry_price: float,
    or_levels: dict[str, Any],
) -> None:
    logger.info(
        "[CALC][REGIME] zone=%s entry=%.4f\n"
        "    OR  H=%s L=%s A=%s\n"
        "    R   R1=%s R2=%s R3=%s R3T=%s\n"
        "    S   S1=%s S2=%s S3=%s S3T=%s\n"
        "    MILESTONES %s\n"
        "    SKIP %s (%s)",
        zone.get("zone"), entry_price,
        _fmt_price(or_levels.get("H")),
        _fmt_price(or_levels.get("L")),
        _fmt_price(or_levels.get("A")),
        _fmt_price(or_levels.get("R1")),
        _fmt_price(or_levels.get("R2")),
        _fmt_price(or_levels.get("R3")),
        _fmt_price(or_levels.get("R3_Threshold")),
        _fmt_price(or_levels.get("S1")),
        _fmt_price(or_levels.get("S2")),
        _fmt_price(or_levels.get("S3")),
        _fmt_price(or_levels.get("S3_Threshold")),
        [f"{m['name']}={_fmt_price(m['price'])}"
         for m in (zone.get("milestones") or [])],
        zone.get("skip"),
        zone.get("reason") or "—",
    )

# --------------------------------------------------------------------------- #
# Service                                                                     #
# --------------------------------------------------------------------------- #
class DummyOrderService:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._state: dict[str, Any] = self._empty_state()
        self._load_from_disk()

    # ------------------------------------------------------------------ #
    # Public API                                                         #
    # ------------------------------------------------------------------ #
    def run_once(self, reason: str = "manual") -> dict[str, Any]:
        """One full scan. Never raises."""
        t0 = time.monotonic()
        with self._lock:
            try:
                if not bool(getattr(config, "DUMMY_ORDER_ENABLED", True)):
                    return self._skipped(reason, "disabled", t0)

                now = _now_market()
                window = self._classify_window(now)
                if window == "before_start":
                    return self._skipped(reason, "before_start", t0)

                self._reset_if_new_day(now)

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

                # Record the execution path the isolation engine chose.
                path = str(iso.get("execution_path") or "").strip()
                if path:
                    self._state["execution_path"] = path
                self._state.setdefault("isolated_key", iso.get("instrument_key"))
                self._state.setdefault("bias_type", iso.get("type"))
                self._state.setdefault(
                    "t_iso",
                    iso.get("touched_at") or iso.get("isolated_at"),
                )

                # ---- 2. Ensure order (created at most once) ------- #
                created = 0
                order = self._state.get("order")
                if order is None:
                    order = self._try_create_order(iso, now, window)
                    if order is not None:
                        self._state["order"] = order
                        created = 1

                # ---- 3. Update OPEN order ------------------------- #
                updated = 0
                closed = 0
                if isinstance(order, dict) and order.get("status") == "OPEN":
                    before_status = order.get("status")
                    self._update_order(order, now, window)
                    updated = 1
                    if order.get("status") != before_status:
                        closed = 1

                self._save(now)

                logger.info(
                    "[dummy-order] %s — created=%d updated=%d closed=%d "
                    "order_status=%s mode=%s",
                    reason, created, updated, closed,
                    (order or {}).get("status") if order else "none",
                    window,
                )
                return self._summary(
                    reason, "ok", now, window, t0,
                    created=created, updated=updated, closed=closed,
                )

            except Exception:
                logger.exception("[dummy-order] run_once failed")
                return self._error(reason, "unexpected", t0)

    def get_state(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._state)

    def get_summary(self) -> dict[str, Any]:
        with self._lock:
            return self._summary(
                "read", "ok", _now_market(),
                self._classify_window(_now_market()),
                time.monotonic(),
            )

    def reset(self) -> dict[str, Any]:
        with self._lock:
            self._state = self._empty_state()
            self._save(_now_market())
            logger.info("[dummy-order] state reset")
            return {"status": "reset", "day": self._state.get("day")}

    # ------------------------------------------------------------------ #
    # Window / state plumbing                                            #
    # ------------------------------------------------------------------ #
    def _classify_window(self, now: datetime) -> str:
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
            self._state["orders"] = (
                [self._state["order"]]
                if self._state.get("order") else []
            )
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(self._state, indent=2, default=str),
                encoding="utf-8",
            )
            tmp.replace(path)
        except Exception:
            logger.exception("[dummy-order] save failed")

    # ------------------------------------------------------------------ #
    # OR-level / candle access                                           #
    # ------------------------------------------------------------------ #
    def _read_or_levels(self, instrument_key: str) -> dict[str, Any] | None:
        try:
            from services.instrument_registry import instrument_registry
            entry = instrument_registry.get(instrument_key)
        except Exception:
            return None
        if not entry:
            return None
        underlying = str(entry.get("underlying") or "").strip().lower()
        strike = entry.get("strike_price")
        strike_type = str(entry.get("strike_type") or "").upper()
        if not underlying or strike is None or not strike_type:
            return None
        try:
            strike_int = int(float(strike))
        except (TypeError, ValueError):
            return None
        path = (
            Path(config.LIVE_DATA_DIR)
            / underlying
            / f"{strike_int}_{strike_type}"
            / "first_candle.json"
        )
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("[dummy-order] unreadable %s", path)
            return None
        if not isinstance(payload, dict):
            return None
        if str(payload.get("date") or "") != _today_iso():
            return None
        orl = payload.get("or_levels")
        return orl if isinstance(orl, dict) else None

    def _resolve_entry_price(
        self,
        instrument_key: str,
        confirmed_time_iso: str,
    ) -> tuple[float | None, str | None]:
        """Return (price, source) for the traded instrument at trigger time.

        Priority:
          1. candle_close_at_confirmed_fast  (live_sr_levels_service)
          2. candle_close_from_intraday      (fresh API fetch, cached)
          3. live_ltp                        (degraded — logged WARNING)
        """
        try:
            from services.live_sr_levels_service import live_sr_levels_service
            close = live_sr_levels_service.get_candle_close_at(
                instrument_key, confirmed_time_iso,
            )
            if close is not None:
                return float(close), "candle_close_at_confirmed_fast"
        except Exception:
            pass

        # Intraday fallback
        try:
            from services.live_sr_calc_service import live_sr_calc_service
            target = _parse_iso(confirmed_time_iso)
            if target is not None:
                target_min = target.replace(second=0, microsecond=0)
                candles = live_sr_calc_service.fetch_intraday_candles_for_entry(
                    instrument_key,
                )
                for c in candles or []:
                    c_dt = _parse_iso(c.get("time"))
                    if c_dt is None:
                        continue
                    if c_dt.replace(second=0, microsecond=0) == target_min:
                        close = _safe_float(c.get("close"))
                        if close is not None:
                            return close, "candle_close_from_intraday"
        except Exception:
            logger.exception(
                "[dummy-order] intraday fallback failed for %s",
                instrument_key,
            )

        # LTP fallback
        try:
            from services.live_feed_service import live_feed_service
            ltp = live_feed_service.get_latest_ltp(instrument_key)
            if ltp is not None:
                logger.warning(
                    "[dummy-order] entry price for %s fell back to "
                    "live_ltp (no candle close at %s) — may be wrong in "
                    "simulate mode",
                    instrument_key, confirmed_time_iso,
                )
                return float(ltp), "live_ltp"
        except Exception:
            pass
        return None, None

    def _get_candles_for(self, instrument_key: str) -> list[dict[str, Any]]:
        try:
            from services.live_sr_levels_service import live_sr_levels_service
            candles = live_sr_levels_service.get_candles(instrument_key)
            if candles:
                return candles
        except Exception:
            pass
        try:
            from services.live_sr_calc_service import live_sr_calc_service
            return live_sr_calc_service.fetch_intraday_candles_for_entry(
                instrument_key,
            )
        except Exception:
            return []

    def _build_resistance_exit_map(
        self, traded_key: str,
    ) -> dict[str, float]:
        """Return {confirmed_time_iso: close_price} for every resistance
        on ``traded_key`` whose confirming bar's close is available."""
        out: dict[str, float] = {}
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
            try:
                close = live_sr_levels_service.get_candle_close_at(
                    traded_key, conf_iso,
                )
            except Exception:
                close = None
            close_f = _safe_float(close)
            if close_f is not None:
                out[str(conf_iso)] = close_f
        return out

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

        # First qualified post-isolation level + most recent prior support.
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

        # Resolve bias-side metadata from the registry.
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

        # 09:30 reversal cutoff.
        cutoff_hour = int(getattr(config, "STRATEGY_ORDER_REVERSAL_CUTOFF_HOUR", 9))
        cutoff_min = int(getattr(config, "STRATEGY_ORDER_REVERSAL_CUTOFF_MINUTE", 30))
        post_conf_dt = _parse_iso(post_trigger["confirmed_time"])
        cutoff_dt = post_conf_dt.replace(
            hour=cutoff_hour, minute=cutoff_min, second=0, microsecond=0,
        ) if post_conf_dt is not None else None

        post_is_resistance = post_trigger["level"] == "resistance"
        reversal_active = (
            cutoff_dt is not None
            and post_conf_dt is not None
            and post_conf_dt <= cutoff_dt
            and prior_support is not None
            and post_is_resistance
        )

        # Routing decision.
        if reversal_active:
            routing_mode = "reversal_same_leg"
            traded_key = bias_key
            traded_symbol = bias_entry.get("trading_symbol")
        elif post_trigger["level"] == "support":
            routing_mode = "support_same_leg"
            traded_key = bias_key
            traded_symbol = bias_entry.get("trading_symbol")
        else:  # resistance → opposite leg
            routing_mode = "resistance_opposite_leg"
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
            "entry_time_iso": post_trigger["confirmed_time"],
        }

    # ------------------------------------------------------------------ #
    # Regime classification                                              #
    # ------------------------------------------------------------------ #
    def _classify_zone(
        self,
        entry_price: float,
        or_levels: dict[str, Any],
    ) -> dict[str, Any]:
        R1 = _safe_float(or_levels.get("R1"))
        R2 = _safe_float(or_levels.get("R2"))
        R3 = _safe_float(or_levels.get("R3"))
        R3T = _safe_float(or_levels.get("R3_Threshold"))
        L = _safe_float(or_levels.get("L"))
        S1 = _safe_float(or_levels.get("S1"))
        S2 = _safe_float(or_levels.get("S2"))

        if None in (R1, R2, R3, R3T, L, S1, S2):
            return {
                "zone": "UNCLASSIFIABLE",
                "milestones": [],
                "skip": True,
                "reason": "incomplete_or_levels",
            }

        if entry_price > R2:
            return {
                "zone": "ABOVE_R2",
                "milestones": [],
                "skip": True,
                "reason": (
                    f"entry {entry_price:.4f} > R2 {R2:.4f} — no valid "
                    f"logic present above R2 level"
                ),
            }

        if entry_price < S2:
            return {
                "zone": "BELOW_S2",
                "milestones": [{"name": "OR_LOW", "price": L}],
                "skip": False,
            }

        if entry_price < R1:
            return {
                "zone": "BELOW_R1",
                "milestones": [
                    {"name": "R2", "price": R2},
                    {"name": "R3_Threshold", "price": R3T},
                    {"name": "R3", "price": R3},
                ],
                "skip": False,
            }

        return {
            "zone": "ZONE_2",
            "milestones": [
                {"name": "R3_Threshold", "price": R3T},
                {"name": "R3", "price": R3},
            ],
            "skip": False,
        }

    # ------------------------------------------------------------------ #
    # Order creation                                                     #
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

        entry_price, entry_source = self._resolve_entry_price(
            traded_key, entry_time_iso,
        )
        if entry_price is None or entry_price <= 0:
            logger.warning(
                "[dummy-order] trigger confirmed but no entry price for "
                "%s @ %s — will retry next poll",
                traded_key, entry_time_iso,
            )
            return None

        or_levels = self._read_or_levels(traded_key)
        if not or_levels:
            logger.warning(
                "[dummy-order] no OR levels for traded instrument %s — "
                "cannot evaluate regime",
                traded_key,
            )
            return self._skipped_order(
                iso, routing, entry_time_iso, entry_price, entry_source,
                now, reason="missing_or_levels",
            )

        zone = self._classify_zone(entry_price, or_levels)
        _log_calc_regime(zone, entry_price, or_levels)

        if zone.get("skip"):
            logger.warning(
                "[dummy-order] SKIP order — %s (entry=%.4f, zone=%s, "
                "instrument=%s)",
                zone.get("reason"), entry_price, zone.get("zone"),
                traded_key,
            )
            return self._skipped_order(
                iso, routing, entry_time_iso, entry_price, entry_source,
                now, zone=zone, or_levels=or_levels,
            )

        stop_pct = float(
            getattr(config, "STRATEGY_ORDER_STOP_LOSS_PCT", 0.30),
        )
        stop_price = round(entry_price * (1.0 - stop_pct), 4)

        # Ensure the SR engine is tracking the traded instrument so that
        # the resistance-exit map has its candle series available.
        self._ensure_sr_tracking(traded_key)

        order = {
            "order_id": f"{traded_key}|{entry_time_iso}",
            "dummy": True,
            "status": "OPEN",
            "instrument_key": traded_key,
            "trading_symbol": routing["traded_symbol"],
            "side": "BUY",
            "routing_mode": routing["routing_mode"],
            "isolated_key": iso.get("instrument_key"),
            "bias_type": iso.get("type"),
            "t_iso": iso.get("touched_at") or iso.get("isolated_at"),
            "trigger": routing["trigger"],
            "prior_support": routing.get("prior_support"),
            "entry_time": entry_time_iso,
            "entry_price": round(entry_price, 4),
            "entry_price_source": entry_source,
            "stop_price": stop_price,
            "stop_pct": stop_pct,
            "or_levels": or_levels,
            "zone": zone.get("zone"),
            "milestones": zone.get("milestones") or [],
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

        logger.info(
            "[dummy-order] CREATED %s | routing=%s entry=%.4f stop=%.4f "
            "zone=%s milestones=%s",
            order["order_id"], routing["routing_mode"],
            entry_price, stop_price, zone.get("zone"),
            [m.get("name") for m in (zone.get("milestones") or [])],
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
    # Order update (bar replay + Stage 2 exit)                           #
    # ------------------------------------------------------------------ #
    def _update_order(
        self,
        order: dict[str, Any],
        now: datetime,
        window: str,
    ) -> None:
        if order.get("status") != "OPEN":
            return

        candles = self._get_candles_for(order.get("instrument_key"))
        if not candles:
            return

        entry_dt = _parse_iso(order.get("entry_time"))
        if entry_dt is None:
            return

        entry_price = _safe_float(order.get("entry_price")) or 0.0
        stop_price = _safe_float(order.get("stop_price")) or 0.0
        if entry_price <= 0 or stop_price <= 0:
            return

        milestones = order.get("milestones") or []
        traded_key = order.get("instrument_key")
        gain_threshold = _safe_float(
            getattr(config, "STRATEGY_ORDER_MILESTONE_GAIN_POINTS", 50.0),
        ) or 50.0

        # Resistance-exit map for Stage 2.
        resistance_exits = self._build_resistance_exit_map(traded_key)

        stage_2_dt = _parse_iso(order.get("stage_2_triggered_at"))
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
                triggered_name: str | None = None
                for m in milestones:
                    m_price = _safe_float(m.get("price"))
                    if m_price is not None and hi >= m_price:
                        triggered_name = f"MILESTONE_{m.get('name')}"
                        break
                if triggered_name is None:
                    gain = hi - entry_price
                    if gain >= gain_threshold:
                        triggered_name = f"GAIN_{gain:.2f}PT"
                if triggered_name is not None:
                    stage_2_dt = c_dt
                    order["stage_2_triggered_at"] = _iso(c_dt)
                    order["stage_2_trigger_reason"] = triggered_name
                    logger.info(
                        "[dummy-order] Stage 2 ARMED %s | %s @ %s",
                        order.get("order_id"),
                        triggered_name,
                        _fmt_time(_iso(c_dt)),
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
                        exit_reason = "STAGE_2_RESISTANCE_EXIT"
                        exit_price = conf_close
                        exit_time_iso = conf_iso
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
        last_time_iso = str(candles[-1].get("time")) if candles else now.isoformat()
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
                    ("Stop-loss", f"₹{stop:.2f} (−{stop_pct * 100:.1f}%)"),
                    ("Zone", order.get("zone")),
                ],
                sections=[
                    ("Milestones (Stage 1)", "🎯",
                     milestones or ["+50 pt gain"]),
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
                    ("Exit", f"{status_label} @ ₹{exit_price:.2f}"),
                    ("Closed at", _fmt_time(
                        order.get("closed_time") or order.get("closed_at"),
                    )),
                    ("PnL", f"{pnl * 100:+.2f}%"),
                    ("Profit", f"{pts:+.2f} pts"),
                    ("Stage 2 trigger", order.get("stage_2_trigger_reason") or "—"),
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
    ) -> dict[str, Any]:
        order = self._state.get("order")
        out: dict[str, Any] = {
            "status": status,
            "reason": reason,
            "day": self._state.get("day"),
            "mode": window,
            "created": created,
            "updated": updated,
            "closed": closed,
            "has_order": bool(order),
            "order_status": order.get("status") if order else None,
            "execution_path": self._state.get("execution_path"),
            "elapsed_seconds": round(time.monotonic() - t0, 3),
            "generated_at": _now_utc_iso(),
        }
        if isinstance(order, dict) and order.get("status") == "OPEN":
            out["open_order"] = {
                "instrument_key": order.get("instrument_key"),
                "routing_mode": order.get("routing_mode"),
                "entry_price": order.get("entry_price"),
                "stop_price": order.get("stop_price"),
                "current_ltp": order.get("current_ltp"),
                "profit_state": order.get("profit_state"),
                "stage_2_triggered_at": order.get("stage_2_triggered_at"),
            }
        return out

    def _skipped(self, reason: str, why: str, t0: float) -> dict[str, Any]:
        return {
            "status": "skipped",
            "reason": reason,
            "skip_reason": why,
            "day": self._state.get("day"),
            "elapsed_seconds": round(time.monotonic() - t0, 3),
            "generated_at": _now_utc_iso(),
        }

    def _no_signal(self, reason: str, why: str, t0: float) -> dict[str, Any]:
        return {
            "status": "no_signal",
            "reason": reason,
            "detail": why,
            "day": self._state.get("day"),
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
# Singleton                                                                   #
# --------------------------------------------------------------------------- #
dummy_order_service = DummyOrderService() 