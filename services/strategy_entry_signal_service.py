# services/strategy_entry_signal_service.py
"""G1 v4.1 — Dual-sided fast pivot + auto-reverse entry signal.

For the isolated NIFTY strike, this service:

1. Runs a fast pivot scan (left=15, right=3) on the bias side's
   candle series.
2. Decision tree:
     * Fast SUPPORT exists on bias side → ARM BIAS SIDE.
     * Fast RESISTANCE exists on bias side with NO support → TRIGGER
       AUTO-REVERSE to the counter side at the counter instrument's
       candle close at rejection time (or, as a last resort, its
       live LTP).
     * Nothing confirms inside the buffer window → HARD BLOCK
       "all_candidates_in_window_blocked".
3. Applies the LTP-tier stop-loss (unchanged from G1 v2).
4. First-to-confirm tiebreak uses `confirmed_time_fast`.

G1 v4.1 changes (2026-10-29)
----------------------------
Fixes the three bugs surfaced by the 2026-10-29 SIMULATE-mode replay:

  1. **Entry price source** — ``_entry_price_for()`` now falls back to
     a fresh ``HistoryV3Api.get_intra_day_candle_data`` fetch BEFORE
     falling back to the live LTP. This closes the "entry price is the
     15:31 wall-clock LTP instead of the 09:23 candle close" bug that
     appeared when the counter-side instrument was not yet registered
     with the SR engine (see ``level_isolated_instrument_service
     ._register_isolated_pair_with_sr``).

  2. **Auto-reverse price priority** — ``_trigger_auto_reverse()`` now
     uses ``_entry_price_for()``, so it prefers the counter-side candle
     close at ``bias_resistance_confirmed_time_fast`` over the live LTP.
     A degraded live-LTP fallback is still permitted (logged at WARNING)
     so a genuine "confirming bar not yet in the series" race in live
     mode does not hard-block the trade.

  3. **Target selection** — new ``_pick_target_resistance()`` helper
     returns the **first resistance confirmed strictly AFTER** the
     entry confirmation, among those priced above entry. Falls back to
     the lowest-above-entry resistance only when no time-ordered
     candidate exists. This replaces the previous "lowest price above
     entry" selection, which on a day-long down trend picked a
     resistance that only formed hours AFTER entry (e.g. 63.00 @ 15:24
     for a 09:23 entry, instead of the correct 110.65 @ 10:56).
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
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

def _parse_iso(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    try:
        n = float(value)
        if n > 1e12:
            n /= 1000.0
        if n > 1e9:
            return datetime.fromtimestamp(n, tz=_market_tz())
    except (TypeError, ValueError):
        pass
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_market_tz())
    return dt.astimezone(_market_tz())

def _iso(dt: datetime) -> str:
    return dt.astimezone(_market_tz()).isoformat()

def _minutes_between(a: datetime, b: datetime) -> float:
    return (b - a).total_seconds() / 60.0

# --------------------------------------------------------------------------- #
# Touch-side parsing                                                          #
# --------------------------------------------------------------------------- #
def _parse_touch_reason(reason: Any) -> tuple[str | None, str | None]:
    r = str(reason or "").lower()
    if "r3" in r:
        return "R3", "CE"
    if "s3" in r:
        return "S3", "PE"
    return None, None

# --------------------------------------------------------------------------- #
# LTP-tier stop-loss (unchanged from G1 v2)                                   #
# --------------------------------------------------------------------------- #
_DEFAULT_STOP_TIERS: list[dict[str, Any]] = [
    {"max_ltp": 100.0, "sl_pct": 0.15},
    {"max_ltp": 200.0, "sl_pct": 0.12},
    {"max_ltp": 300.0, "sl_pct": 0.10},
    {"max_ltp": None,  "sl_pct": 0.05},
]

def _parse_stop_tiers() -> list[dict[str, Any]]:
    raw = (getattr(config, "STRATEGY_ENTRY_STOP_LTP_TIERS", "") or "").strip()
    if not raw:
        return _DEFAULT_STOP_TIERS
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning(
            "[strategy-entry] STRATEGY_ENTRY_STOP_LTP_TIERS is not valid "
            "JSON; using default tiers."
        )
        return _DEFAULT_STOP_TIERS
    if not isinstance(parsed, list) or not parsed:
        return _DEFAULT_STOP_TIERS
    out: list[dict[str, Any]] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        try:
            sl_pct = float(item.get("sl_pct"))
        except (TypeError, ValueError):
            continue
        if sl_pct <= 0 or sl_pct >= 1:
            continue
        max_ltp_raw = item.get("max_ltp")
        max_ltp: float | None
        if max_ltp_raw is None:
            max_ltp = None
        else:
            try:
                max_ltp = float(max_ltp_raw)
            except (TypeError, ValueError):
                continue
            if max_ltp <= 0:
                continue
        out.append({"max_ltp": max_ltp, "sl_pct": sl_pct})
    if not out:
        return _DEFAULT_STOP_TIERS
    if not any(t.get("max_ltp") is None for t in out):
        last_sl = out[-1]["sl_pct"]
        out.append({"max_ltp": None, "sl_pct": last_sl})
    return out

def _tier_lookup(entry_price: float) -> float:
    tiers = _parse_stop_tiers()
    for tier in tiers:
        max_ltp = tier.get("max_ltp")
        if max_ltp is None or entry_price < max_ltp:
            return float(tier["sl_pct"])
    return float(tiers[-1]["sl_pct"]) if tiers else 0.15

def _get_tick_size(instrument_key: str) -> float:
    try:
        from services.instrument_registry import instrument_registry
        entry = instrument_registry.get(instrument_key)
        if entry:
            ts = entry.get("tick_size")
            if ts is not None:
                ts_f = float(ts)
                if ts_f > 0:
                    return ts_f
    except Exception:
        pass
    return 0.05

def _round_to_tick(value: float, tick: float, mode: str) -> float:
    if tick <= 0:
        return float(value)
    mode = (mode or "down").lower()
    if mode == "up":
        return math.ceil(value / tick) * tick
    if mode == "nearest":
        return round(value / tick) * tick
    return math.floor(value / tick) * tick

def _compute_ltp_tier_stop(
    entry_price: float,
    instrument_key: str,
) -> tuple[float, float, float | None, str]:
    sl_pct = _tier_lookup(entry_price)
    hard_cap = float(getattr(config, "STRATEGY_MAX_STOP_PCT", 0.35))
    if sl_pct > hard_cap:
        logger.error(
            "[strategy-entry] sl_pct %.4f exceeds hard cap %.4f; clamping.",
            sl_pct, hard_cap,
        )
        sl_pct = hard_cap
    stop_raw = entry_price * (1.0 - sl_pct)
    tick = _get_tick_size(instrument_key)
    mode = str(getattr(config, "STRATEGY_ENTRY_STOP_TICK_ROUNDING", "down")).lower()
    stop = _round_to_tick(stop_raw, tick, mode)
    if stop <= 0:
        stop = stop_raw
    stop_pct = ((entry_price - stop) / entry_price) if entry_price > 0 else None
    return float(stop), float(sl_pct), (
        round(stop_pct, 6) if stop_pct is not None else None
    ), "ltp_tier"

# --------------------------------------------------------------------------- #
# Candle lookup + LTP                                                         #
# --------------------------------------------------------------------------- #
def _candle_close_at(instrument_key: str, iso_time: str) -> float | None:
    """Return the close of the 1-min candle at ``iso_time`` from the
    SR engine's in-memory state (per-instrument ``_states`` dict)."""
    target = _parse_iso(iso_time)
    if target is None:
        return None
    target_minute = target.replace(second=0, microsecond=0)
    try:
        from services.live_sr_levels_service import live_sr_levels_service
        with live_sr_levels_service._guard:  # type: ignore[attr-defined]
            state = live_sr_levels_service._states.get(instrument_key)  # type: ignore[attr-defined]
    except Exception:
        return None
    if not state:
        return None
    for candle in state.get("candles") or []:
        c_dt = _parse_iso(candle.get("time"))
        if c_dt is None:
            continue
        if c_dt.replace(second=0, microsecond=0) == target_minute:
            try:
                return float(candle["close"])
            except (KeyError, TypeError, ValueError):
                return None
    return None

def _candle_close_from_intraday(
    instrument_key: str,
    iso_time: str,
) -> float | None:
    """G1 v4.1 — fallback path when the SR engine has no candle state
    for ``instrument_key`` at ``iso_time``.

    Fetches today's intraday 1-min candles via the stateless
    ``live_sr_calc_service`` loader and returns the close of the bar
    whose minute matches ``iso_time``.

    This path is what makes the entry price correct in simulate mode
    when the counter-side instrument was not pre-registered with the
    SR engine (see the companion fix in
    ``level_isolated_instrument_service._register_isolated_pair_with_sr``,
    which now registers BOTH sides up front — the two fixes together
    guarantee a correct entry price even if one of them is bypassed).

    Never raises. Returns ``None`` on any failure.
    """
    target = _parse_iso(iso_time)
    if target is None:
        return None
    target_minute = target.replace(second=0, microsecond=0)
    try:
        from services.live_sr_calc_service import live_sr_calc_service
        candles = live_sr_calc_service.fetch_intraday_candles_for_entry(
            instrument_key
        )
    except Exception:
        logger.exception(
            "[strategy-entry] intraday fallback fetch failed for %s",
            instrument_key,
        )
        return None
    for c in candles or []:
        c_dt = _parse_iso(c.get("time"))
        if c_dt is None:
            continue
        if c_dt.replace(second=0, microsecond=0) == target_minute:
            try:
                return float(c["close"])
            except (KeyError, TypeError, ValueError):
                return None
    return None

def _live_ltp(instrument_key: str) -> float | None:
    try:
        from services.live_feed_service import live_feed_service
        ltp = live_feed_service.get_latest_ltp(instrument_key)
        if ltp is None:
            return None
        return float(ltp)
    except Exception:
        return None

def _entry_price_for(
    instrument_key: str,
    confirmed_time_iso: str,
) -> tuple[float | None, str | None]:
    """Resolve the entry price for an armed candidate.

    G1 v4.1 resolution order (strictly descending preference):

        1. ``candle_close_at_confirmed_fast`` — SR engine in-memory state
        2. ``candle_close_from_intraday``    — fresh intraday API fetch
        3. ``live_ltp``                      — last resort (logged WARNING
                                               by the caller because the
                                               resulting price may be
                                               wrong in simulate mode)

    The previous revision fell straight through from (1) to (3), which
    produced the "entry price = 15:31 wall clock LTP" bug in simulate
    mode. Step (2) closes that gap.
    """
    close = _candle_close_at(instrument_key, confirmed_time_iso)
    if close is not None:
        return close, "candle_close_at_confirmed_fast"

    close = _candle_close_from_intraday(instrument_key, confirmed_time_iso)
    if close is not None:
        return close, "candle_close_from_intraday"

    ltp = _live_ltp(instrument_key)
    if ltp is not None:
        return ltp, "live_ltp"
    return None, None

# --------------------------------------------------------------------------- #
# Target resistance selection (G1 v4.1)                                       #
# --------------------------------------------------------------------------- #
def _pick_target_resistance(
    levels: list[dict[str, Any]],
    entry_price: float,
    entry_confirmed_iso: str,
) -> dict[str, Any] | None:
    """Return the target resistance for an armed entry.

    G1 v4.1 fix — the previous implementation picked the LOWEST-priced
    resistance above ``entry_price`` regardless of when it confirmed.
    On a day-long down trend this selected a resistance that only
    formed hours AFTER the entry (e.g. 63.00 @ 15:24 for a 09:23
    entry), which is not a real "next resistance" from the entry's
    perspective.

    Correct behaviour:

        1. Filter to resistances that confirmed **strictly AFTER**
           ``entry_confirmed_iso``.
        2. Filter to resistances priced **strictly ABOVE**
           ``entry_price``.
        3. Pick the EARLIEST confirmed among the survivors.

    Fallback: when no resistance survives the time filter (e.g. the
    option collapsed below every subsequent pivot), return the
    lowest-priced resistance above entry (previous behaviour) so the
    caller still gets a target when one exists. Returns ``None`` only
    when there is no resistance above ``entry_price`` at all.
    """
    entry_conf_dt = _parse_iso(entry_confirmed_iso)

    # ---- Primary: first confirmed-after-entry resistance above price #
    if entry_conf_dt is not None:
        time_filtered: list[tuple[datetime, dict[str, Any]]] = []
        for lvl in levels:
            if str(lvl.get("level") or "").lower() != "resistance":
                continue
            try:
                r_price = float(lvl.get("price"))
            except (TypeError, ValueError):
                continue
            if r_price <= entry_price:
                continue
            r_conf_dt = _parse_iso(lvl.get("confirmed_time"))
            if r_conf_dt is None:
                continue
            if r_conf_dt <= entry_conf_dt:
                continue
            time_filtered.append((r_conf_dt, lvl))
        if time_filtered:
            time_filtered.sort(key=lambda t: t[0])
            return time_filtered[0][1]

    # ---- Fallback: lowest-priced resistance above entry -------------- #
    best: dict[str, Any] | None = None
    best_price: float | None = None
    for lvl in levels:
        if str(lvl.get("level") or "").lower() != "resistance":
            continue
        try:
            r_price = float(lvl.get("price"))
        except (TypeError, ValueError):
            continue
        if r_price <= entry_price:
            continue
        if best_price is None or r_price < best_price:
            best = lvl
            best_price = r_price
    return best

# --------------------------------------------------------------------------- #
# Fast pivot scan (unchanged from G1 v2)                                      #
# --------------------------------------------------------------------------- #
def _run_pivot_scan(
    instrument_key: str,
    left_bars: int,
    right_bars: int,
) -> list[dict[str, Any]]:
    try:
        from services.live_sr_levels_service import live_sr_levels_service
        with live_sr_levels_service._guard:  # type: ignore[attr-defined]
            state = live_sr_levels_service._states.get(instrument_key)  # type: ignore[attr-defined]
            if state is None:
                return []
            candles = list(state.get("candles") or [])
    except Exception:
        logger.exception(
            "[strategy-entry] fast scan failed to read candle series for %s",
            instrument_key,
        )
        return []

    if not candles:
        return []
    if len(candles) < left_bars + right_bars + 1:
        return []

    out: list[dict[str, Any]] = []
    n = len(candles)

    for i in range(left_bars, n - right_bars):
        c = candles[i]
        try:
            c_high = float(c["high"])
            c_low = float(c["low"])
            c_time = c["time"]
        except (KeyError, TypeError, ValueError):
            continue

        is_support = True
        for j in range(i - left_bars, i):
            try:
                if float(candles[j]["low"]) <= c_low:
                    is_support = False
                    break
            except (KeyError, TypeError, ValueError):
                is_support = False
                break
        if is_support:
            for j in range(i + 1, i + right_bars + 1):
                try:
                    if float(candles[j]["low"]) <= c_low:
                        is_support = False
                        break
                except (KeyError, TypeError, ValueError):
                    is_support = False
                    break

        if is_support:
            confirm_idx = i + right_bars
            try:
                confirmed_time = candles[confirm_idx]["time"]
            except (IndexError, KeyError, TypeError):
                continue
            out.append({
                "level": "support",
                "price": c_low,
                "pivot_bar_index": i,
                "pivot_time": c_time,
                "confirmed_bar_index": confirm_idx,
                "confirmed_time": confirmed_time,
                "left_bars": left_bars,
                "right_bars": right_bars,
            })

        is_resistance = True
        for j in range(i - left_bars, i):
            try:
                if float(candles[j]["high"]) >= c_high:
                    is_resistance = False
                    break
            except (KeyError, TypeError, ValueError):
                is_resistance = False
                break
        if is_resistance:
            for j in range(i + 1, i + right_bars + 1):
                try:
                    if float(candles[j]["high"]) >= c_high:
                        is_resistance = False
                        break
                except (KeyError, TypeError, ValueError):
                    is_resistance = False
                    break

        if is_resistance:
            confirm_idx = i + right_bars
            try:
                confirmed_time = candles[confirm_idx]["time"]
            except (IndexError, KeyError, TypeError):
                continue
            out.append({
                "level": "resistance",
                "price": c_high,
                "pivot_bar_index": i,
                "pivot_time": c_time,
                "confirmed_bar_index": confirm_idx,
                "confirmed_time": confirmed_time,
                "left_bars": left_bars,
                "right_bars": right_bars,
            })

    return out

def _derive_display_confirmed_time(
    instrument_key: str,
    pivot_bar_index: int,
) -> str | None:
    if pivot_bar_index is None or pivot_bar_index < 0:
        return None
    try:
        from services.live_sr_levels_service import live_sr_levels_service
        with live_sr_levels_service._guard:  # type: ignore[attr-defined]
            state = live_sr_levels_service._states.get(instrument_key)  # type: ignore[attr-defined]
            if state is None:
                return None
            candles = list(state.get("candles") or [])
    except Exception:
        return None
    display_right = int(getattr(config, "LIVE_SR_RIGHT_BARS", 3))
    confirm_idx = pivot_bar_index + display_right
    if confirm_idx < 0 or confirm_idx >= len(candles):
        return None
    try:
        return str(candles[confirm_idx]["time"])
    except (KeyError, TypeError):
        return None

# --------------------------------------------------------------------------- #
# Both sides lookup                                                           #
# --------------------------------------------------------------------------- #
def _both_sides(
    underlying: str,
    strike: Any,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    try:
        from services.instrument_registry import instrument_registry
    except Exception:
        return None, None
    ce = instrument_registry.resolve(underlying, strike, "CE")
    pe = instrument_registry.resolve(underlying, strike, "PE")
    return ce, pe

# --------------------------------------------------------------------------- #
# Candidate factories                                                         #
# --------------------------------------------------------------------------- #
def _empty_candidate(
    side: str,
    instrument_key: str | None,
    trading_symbol: str | None,
    is_bias_side: bool,
) -> dict[str, Any]:
    return {
        "side": side,
        "instrument_key": instrument_key,
        "trading_symbol": trading_symbol,
        "is_bias_side": is_bias_side,
        "state": "waiting",
        "state_note": None,
        "entry_type": None,
        "entry_support": None,
        "blocking_resistance": None,
        "reversal_trigger": None,
        "entry": None,
        "warning": None,
        "warning_detail": None,
    }

def _compute_candidate(
    side: str,
    entry: dict[str, Any] | None,
    is_bias_side: bool,
    touch_dt: datetime,
    window_end: datetime,
    now: datetime,
    counter_entry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """G1 v3.2 — bias-side dual scan + auto-reverse.

    Decision priority on the bias side:
      1. Fast SUPPORT in window → ARM BIAS (highest priority).
      2. Fast RESISTANCE in window with NO support → AUTO-REVERSE.
      3. Window expired → BLOCK.

    When called with ``is_bias_side == False`` (counter side), the
    candidate is filled in by the auto-reverse branch of the bias-side
    candidate rather than being scanned independently.
    """
    if entry is None:
        return _empty_candidate(side, None, None, is_bias_side)

    key = str(entry.get("instrument_key") or "").strip()
    symbol = entry.get("trading_symbol")
    cand = _empty_candidate(side, key, symbol, is_bias_side)

    # ---- Auto-reverse path: not the bias side --------------------- #
    if not is_bias_side:
        # Marker candidate — populated by the bias-side auto-reverse
        # branch. Left in "waiting" until then.
        return cand

    left_bars = int(getattr(
        config, "STRATEGY_ENTRY_PIVOT_LEFT_BARS", config.LIVE_SR_LEFT_BARS
    ))
    right_bars = int(getattr(config, "STRATEGY_ENTRY_PIVOT_RIGHT_BARS", 3))
    levels = _run_pivot_scan(key, left_bars, right_bars)

    if not levels:
        cand["state_note"] = "no_sr_levels"
        return cand

    # ---- 1. Collect supports + resistances in the window --------- #
    supports_in_window: list[dict[str, Any]] = []
    resistances_in_window: list[dict[str, Any]] = []
    for lvl in levels:
        p_dt = _parse_iso(lvl.get("pivot_time"))
        if p_dt is None:
            continue
        if p_dt < touch_dt or p_dt > window_end:
            continue
        try:
            price = float(lvl.get("price"))
        except (TypeError, ValueError):
            continue
        rec = {
            "price": price,
            "pivot_time": _iso(p_dt),
            "confirmed_time_fast": lvl.get("confirmed_time"),
            "pivot_bar_index": lvl.get("pivot_bar_index"),
            "pivot_dt": p_dt,
            "raw": lvl,
        }
        if str(lvl.get("level") or "").lower() == "support":
            supports_in_window.append(rec)
        elif str(lvl.get("level") or "").lower() == "resistance":
            resistances_in_window.append(rec)

    supports_in_window.sort(key=lambda s: s["pivot_dt"])
    resistances_in_window.sort(key=lambda r: r["pivot_dt"])

    # ---- 2. Prioritize support over resistance ------------------- #
    # G1 v4.0 — the new flag names are aliased in core/config.py to
    # fall back to the v3.2 names when the new env vars are unset, so
    # existing .env files keep working without any edit.
    prioritize_support = bool(getattr(
        config, "STRATEGY_PRIORITIZE_SUPPORT_OVER_RESISTANCE", True
    ))
    allow_auto_reverse = bool(getattr(
        config, "STRATEGY_ALLOW_OPPOSITE_REVERSAL_ON_RESISTANCE", True
    ))

    # ---- CASE 1: SUPPORT (priority) ------------------------------ #
    if supports_in_window and (prioritize_support or not resistances_in_window):
        return _arm_bias_on_support(
            cand, key, symbol, supports_in_window, levels,
            left_bars, right_bars, now,
        )

    # ---- CASE 2: RESISTANCE only (auto-reverse) ------------------ #
    if (
        resistances_in_window
        and not supports_in_window
        and allow_auto_reverse
        and counter_entry is not None
    ):
        return _trigger_auto_reverse(
            cand, key, symbol, resistances_in_window,
            counter_entry, now,
        )

    # ---- CASE 3: window still open, nothing yet ------------------ #
    if now <= window_end:
        cand["state"] = "waiting"
        cand["state_note"] = (
            "no_support_pivot_in_window" if not supports_in_window
            else "waiting_for_fast_confirmation"
        )
        return cand

    # ---- CASE 4: window expired ---------------------------------- #
    cand["state"] = "blocked"
    cand["state_note"] = "all_candidates_in_window_blocked"
    return cand

# --------------------------------------------------------------------------- #
# Case 1: arm bias on support (target selection G1 v4.1)                     #
# --------------------------------------------------------------------------- #
def _arm_bias_on_support(
    cand: dict[str, Any],
    key: str,
    symbol: str | None,
    supports_in_window: list[dict[str, Any]],
    levels: list[dict[str, Any]],
    left_bars: int,
    right_bars: int,
    now: datetime,
) -> dict[str, Any]:
    for s in supports_in_window:
        support_pivot_dt = s["pivot_dt"]
        support_confirmed_fast_iso = s["confirmed_time_fast"]
        support_confirmed_fast_dt = _parse_iso(support_confirmed_fast_iso)
        if support_confirmed_fast_dt is None:
            continue

        # Resistance blocker (unchanged rule, fast window)
        blocker: dict[str, Any] | None = None
        if getattr(config, "STRATEGY_ENTRY_BLOCKED_BY_RESISTANCE", True):
            for lvl in levels:
                if str(lvl.get("level") or "").lower() != "resistance":
                    continue
                r_pivot_dt = _parse_iso(lvl.get("pivot_time"))
                r_conf_dt = _parse_iso(lvl.get("confirmed_time"))
                if r_pivot_dt is None or r_conf_dt is None:
                    continue
                try:
                    r_price = float(lvl.get("price"))
                except (TypeError, ValueError):
                    continue
                if r_price <= s["price"]:
                    continue
                if r_pivot_dt <= support_pivot_dt:
                    continue
                if r_conf_dt > support_confirmed_fast_dt:
                    continue
                if blocker is None or r_price < float(blocker.get("price") or 0.0):
                    blocker = {
                        "price": r_price,
                        "pivot_time": _iso(r_pivot_dt),
                        "confirmed_time": lvl.get("confirmed_time"),
                    }
        if blocker is not None:
            cand["blocking_resistance"] = blocker
            cand["state_note"] = "all_supports_blocked_so_far"
            continue

        display_confirmed_iso = _derive_display_confirmed_time(
            key, s["pivot_bar_index"] or -1,
        )
        entry_support = {
            "price": s["price"],
            "pivot_time": s["pivot_time"],
            "confirmed_time_fast": support_confirmed_fast_iso,
            "confirmed_time": display_confirmed_iso,
        }
        cand["entry_support"] = entry_support
        cand["blocking_resistance"] = None

        if support_confirmed_fast_dt > now:
            cand["state"] = "waiting_confirmation"
            cand["state_note"] = "support_found_waiting_for_confirmation"
            cand["seconds_until_confirmation"] = int(
                (support_confirmed_fast_dt - now).total_seconds()
            )
            return cand

        entry_price, price_source = _entry_price_for(
            key, support_confirmed_fast_iso,
        )
        if entry_price is None:
            cand["state"] = "blocked"
            cand["state_note"] = "no_entry_price"
            return cand
        if entry_price <= 0:
            cand["state"] = "blocked"
            cand["state_note"] = "invalid_entry_price"
            return cand
        if entry_price < s["price"]:
            cand["state"] = "blocked"
            cand["state_note"] = "entry_below_support"
            return cand

        stop_price, sl_pct, stop_pct, stop_source = _compute_ltp_tier_stop(
            entry_price, key,
        )

        warning = None
        warning_detail = None
        support_gap_pct = (entry_price - s["price"]) / entry_price
        if support_gap_pct > 0.20:
            warning = "support_far_below_entry"
            warning_detail = (
                f"Support {s['price']:.2f} is "
                f"{support_gap_pct * 100:.1f}% below entry "
                f"{entry_price:.2f}."
            )

        # G1 v4.1 — target: first resistance confirmed AFTER the entry
        # support's confirmed_time_fast, priced above entry_price.
        best_r = _pick_target_resistance(
            levels, entry_price, support_confirmed_fast_iso,
        )
        target_price = None
        target_pivot_time = None
        if best_r is not None:
            try:
                target_price = float(best_r.get("price"))
            except (TypeError, ValueError):
                target_price = None
            target_pivot_time = best_r.get("pivot_time")

        cand["state"] = "armed"
        cand["state_note"] = "entry_support_confirmed_fast"
        cand["entry_type"] = "bias_support_bounce"
        cand["warning"] = warning
        cand["warning_detail"] = warning_detail
        cand["entry"] = {
            "price": entry_price,
            "price_source": price_source,
            "time": support_confirmed_fast_iso,
            "stop": stop_price,
            "stop_pct": stop_pct,
            "sl_pct": sl_pct,
            "stop_source": stop_source,
            "target": target_price,
            "target_pivot_time": target_pivot_time,
        }
        return cand

    cand["state"] = "blocked"
    cand["state_note"] = "all_supports_in_window_blocked"
    return cand

# --------------------------------------------------------------------------- #
# Case 2: auto-reverse on bias-side rejection (G1 v4.1 fixes)                 #
# --------------------------------------------------------------------------- #
def _trigger_auto_reverse(
    cand: dict[str, Any],
    key: str,
    symbol: str | None,
    resistances_in_window: list[dict[str, Any]],
    counter_entry: dict[str, Any],
    now: datetime,
) -> dict[str, Any]:
    """Bias side confirmed a fast resistance with no support → flip.

    G1 v4.1 changes vs the previous revision
    ----------------------------------------
    * The counter-side entry price is now resolved via
      ``_entry_price_for()`` — i.e. the **candle close at the rejection
      minute** is preferred, with a fresh intraday fetch as fallback,
      and live LTP **only as a last resort** (logged WARNING, marked
      degraded in the entry block). The previous revision preferred
      the live LTP, which in simulate mode gave the wall-clock price
      (e.g. 61.65 at 15:31) instead of the historical price at the
      rejection moment (e.g. 186.55 at 09:23).

    * The target is now selected via ``_pick_target_resistance()``
      (first resistance confirmed AFTER the rejection), matching the
      bias-side arm path.
    """
    for r in resistances_in_window:
        r_conf_iso = r["confirmed_time_fast"]
        r_conf_dt = _parse_iso(r_conf_iso)
        if r_conf_dt is None:
            continue
        if r_conf_dt > now:
            cand["state"] = "waiting_confirmation"
            cand["state_note"] = "resistance_found_awaiting_auto_reverse"
            cand["seconds_until_confirmation"] = int(
                (r_conf_dt - now).total_seconds()
            )
            cand["blocking_resistance"] = {
                "price": r["price"],
                "pivot_time": r["pivot_time"],
                "confirmed_time": r_conf_iso,
            }
            return cand

        counter_key = str(counter_entry.get("instrument_key") or "")
        counter_symbol = counter_entry.get("trading_symbol")

        # G1 v4.1 — resolve counter price via _entry_price_for() so the
        # candle close at rejection time wins over the live LTP.
        counter_price, price_source = _entry_price_for(
            counter_key, r_conf_iso,
        )

        # Log the degraded fallback so simulate-mode drift is visible.
        if counter_price is not None and price_source == "live_ltp":
            logger.warning(
                "[strategy-entry] counter-side entry price for %s fell "
                "back to live_ltp (no candle at %s in SR state or "
                "intraday fetch) — entry price may be wrong in "
                "simulate mode",
                counter_key, r_conf_iso,
            )

        if counter_price is None or counter_price <= 0:
            cand["state"] = "blocked"
            cand["state_note"] = "invalid_counter_price"
            return cand

        stop_price, sl_pct, stop_pct, stop_source = _compute_ltp_tier_stop(
            counter_price, counter_key,
        )

        # Target: first counter-side resistance confirmed AFTER the
        # rejection minute, priced above the counter entry.
        counter_levels = _run_pivot_scan(
            counter_key,
            int(getattr(
                config, "STRATEGY_ENTRY_PIVOT_LEFT_BARS",
                config.LIVE_SR_LEFT_BARS,
            )),
            int(getattr(config, "STRATEGY_ENTRY_PIVOT_RIGHT_BARS", 3)),
        )
        best_r = _pick_target_resistance(
            counter_levels, counter_price, r_conf_iso,
        )
        target_price = None
        target_pivot_time = None
        if best_r is not None:
            try:
                target_price = float(best_r.get("price"))
            except (TypeError, ValueError):
                target_price = None
            target_pivot_time = best_r.get("pivot_time")

        cand["state"] = "armed"
        cand["state_note"] = "auto_reverse_on_bias_rejection"
        cand["entry_type"] = "auto_reverse_on_rejection"
        cand["reversal_trigger"] = {
            "bias_resistance_price": r["price"],
            "bias_resistance_pivot_time": r["pivot_time"],
            "bias_resistance_confirmed_time_fast": r_conf_iso,
            "rejected_side": (
                "CE" if str(cand.get("side") or "").upper() == "CE"
                else "PE"
            ),
        }
        # The actual armed instrument is the COUNTER side.
        cand["armed_instrument_key"] = counter_key
        cand["armed_trading_symbol"] = counter_symbol
        cand["armed_side"] = "PE" if str(cand.get("side") or "").upper() == "CE" else "CE"
        cand["entry"] = {
            "price": counter_price,
            "price_source": price_source,
            "time": r_conf_iso,
            "stop": stop_price,
            "stop_pct": stop_pct,
            "sl_pct": sl_pct,
            "stop_source": stop_source,
            "target": target_price,
            "target_pivot_time": target_pivot_time,
        }
        return cand

    cand["state"] = "blocked"
    cand["state_note"] = "auto_reverse_unresolvable"
    return cand

# --------------------------------------------------------------------------- #
# Service                                                                     #
# --------------------------------------------------------------------------- #
class StrategyEntrySignalService:
    def compute(self) -> dict[str, Any]:
        if not getattr(config, "STRATEGY_ENTRY_ENABLED", True):
            return {
                "status": "disabled",
                "reason": "STRATEGY_ENTRY_ENABLED is False",
                "generated_at": _now_utc_iso(),
            }

        try:
            from services.level_isolated_instrument_service import (
                level_isolated_instrument_service,
            )
            iso_status = level_isolated_instrument_service.last_status()
        except Exception as exc:
            logger.exception("[strategy-entry] last_status() raised")
            return {
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
                "generated_at": _now_utc_iso(),
            }

        isolated = (iso_status or {}).get("isolated") or {}
        if not isolated:
            return {
                "status": "no_isolation",
                "note": "No isolated instrument for today.",
                "generated_at": _now_utc_iso(),
            }

        instrument_key = str(isolated.get("instrument_key") or "").strip()
        if not instrument_key:
            return {
                "status": "no_isolation",
                "note": "Isolation record has no instrument_key.",
                "generated_at": _now_utc_iso(),
            }

        touch_time_iso = isolated.get("touched_at") or isolated.get("isolated_at")
        touch_dt = _parse_iso(touch_time_iso)
        if touch_dt is None:
            return {
                "status": "no_touch_time",
                "note": "Isolation record has no touched_at / isolated_at.",
                "instrument_key": instrument_key,
                "generated_at": _now_utc_iso(),
            }

        touch_side, bias_type = _parse_touch_reason(isolated.get("reason"))
        if touch_side is None or bias_type is None:
            return {
                "status": "no_touch_side",
                "note": "Isolation reason does not encode R3 or S3.",
                "instrument_key": instrument_key,
                "reason": isolated.get("reason"),
                "generated_at": _now_utc_iso(),
            }

        try:
            from services.instrument_registry import instrument_registry
            iso_entry = instrument_registry.get(instrument_key)
        except Exception:
            iso_entry = None

        if iso_entry is None:
            return {
                "status": "no_registry_entry",
                "note": "Isolated instrument not in the registry.",
                "instrument_key": instrument_key,
                "generated_at": _now_utc_iso(),
            }

        underlying = str(iso_entry.get("underlying") or "").strip()
        strike = iso_entry.get("strike_price")
        if not underlying or strike is None:
            return {
                "status": "incomplete_registry_entry",
                "note": "Registry entry missing underlying / strike_price.",
                "instrument_key": instrument_key,
                "generated_at": _now_utc_iso(),
            }

        ce_entry, pe_entry = _both_sides(underlying, strike)
        if bias_type == "CE":
            bias_entry, counter_entry = ce_entry, pe_entry
        else:
            bias_entry, counter_entry = pe_entry, ce_entry

        now = _now_market()
        window_end = touch_dt + timedelta(
            minutes=int(getattr(config, "STRATEGY_ENTRY_BUFFER_MINUTES", 10))
        )

        # Run the bias-side dual scan.
        bias_cand = _compute_candidate(
            side=bias_type,
            entry=bias_entry,
            is_bias_side=True,
            touch_dt=touch_dt,
            window_end=window_end,
            now=now,
            counter_entry=counter_entry,
        )

        candidates: list[dict[str, Any]] = [bias_cand]

        # If auto-reverse fired, add the counter side as an armed entry.
        if bias_cand.get("entry_type") == "auto_reverse_on_rejection":
            armed_key = str(bias_cand.get("armed_instrument_key") or "")
            armed_side = str(bias_cand.get("armed_side") or "")
            armed_symbol = bias_cand.get("armed_trading_symbol")
            counter_cand = _empty_candidate(
                armed_side, armed_key, armed_symbol, False,
            )
            counter_cand["state"] = "armed"
            counter_cand["state_note"] = "auto_reverse_counter_side"
            counter_cand["entry_type"] = "auto_reverse_on_rejection"
            counter_cand["entry"] = bias_cand.get("entry")
            counter_cand["reversal_trigger"] = bias_cand.get("reversal_trigger")
            counter_cand["chosen"] = True
            candidates.append(counter_cand)
            bias_cand["state"] = "discarded"
            bias_cand["state_note"] = "rejected_and_reversed"
            bias_cand["chosen"] = False
            chosen: dict[str, Any] | None = counter_cand
        else:
            # First-to-confirm tiebreak (only when bias side armed).
            armed = [c for c in candidates if c.get("state") == "armed"]
            chosen = armed[0] if armed else None

        response: dict[str, Any] = {
            "status": "ok",
            "day": iso_status.get("day"),
            "isolated": {
                "instrument_key": instrument_key,
                "trading_symbol": isolated.get("trading_symbol"),
                "underlying": underlying,
                "strike": strike,
                "reason": isolated.get("reason"),
                "source": isolated.get("source"),
                "trigger_index": isolated.get("trigger_index"),
                "routed_index": isolated.get("routed_index"),
                "touched_at": touch_time_iso,
                "trigger_level": (
                    isolated.get("trigger_level")
                    or isolated.get("trigger_side")
                ),
                "trigger_level_value": isolated.get("trigger_level_value"),
            },
            "touch": {
                "time": _iso(touch_dt),
                "side": touch_side,
                "bias_type": bias_type,
                "buffer_minutes": int(
                    getattr(config, "STRATEGY_ENTRY_BUFFER_MINUTES", 10)
                ),
                "window_start": _iso(touch_dt),
                "window_end": _iso(window_end),
            },
            "candidates": candidates,
            "chosen_side": (
                chosen.get("side") if chosen else None
            ),
            "chosen_instrument_key": (
                chosen.get("instrument_key") if chosen else None
            ),
            "chosen_entry_type": (
                chosen.get("entry_type") if chosen else None
            ),
            "warnings": self._warnings(candidates, touch_dt, now),
            "generated_at": _now_utc_iso(),
        }

        # Promotion block (unchanged behaviour — reads whether the
        # chosen key differs from the official isolation key).
        try:
            from services.isolated_instrument_promotion_service import (
                isolated_instrument_promotion_service,
            )
            active_state = isolated_instrument_promotion_service.read_active()
        except Exception:
            active_state = None

        chosen_key = response.get("chosen_instrument_key")
        active_key = (
            active_state.get("to_instrument_key") if active_state else None
        )
        already_applied = (
            active_key is not None
            and chosen_key is not None
            and str(active_key) == str(chosen_key)
        )
        allow_promotion = bool(getattr(
            config, "STRATEGY_ENTRY_ALLOW_PROMOTION", True
        ))
        allow_re_promotion = bool(getattr(
            config, "STRATEGY_ENTRY_ALLOW_RE_PROMOTION", False
        ))

        blockers: list[str] = []
        if not allow_promotion:
            blockers.append("disabled")
        if chosen_key is None:
            blockers.append("no_armed_candidate")
        elif chosen_key == instrument_key:
            blockers.append("chosen_is_bias_side")
        elif already_applied:
            blockers.append("already_applied")
        elif active_state is not None and not allow_re_promotion:
            blockers.append("already_promoted_today")

        response["promotion"] = {
            "recommended": len(blockers) == 0,
            "already_applied": already_applied,
            "blockers": blockers,
            "current_active_instrument_key": active_key,
            "target_instrument_key": chosen_key,
            "target_trading_symbol": (
                chosen.get("trading_symbol") if chosen else None
            ),
            "target_side": response.get("chosen_side"),
            "official_isolated_key": instrument_key,
            "promotion_allowed": allow_promotion,
            "re_promotion_allowed": allow_re_promotion,
        }
        response["active_instrument_key"] = active_key or instrument_key

        return response

    @staticmethod
    def _warnings(
        candidates: list[dict[str, Any]],
        touch_dt: datetime,
        now: datetime,
    ) -> list[str]:
        out: list[str] = []
        for c in candidates:
            side = str(c.get("side") or "")
            state = str(c.get("state") or "")
            if state == "waiting":
                note = c.get("state_note") or ""
                if note == "no_sr_levels":
                    out.append(f"{side}: no fast SR pivots yet.")
                elif note == "no_support_pivot_in_window":
                    out.append(
                        f"{side}: no support pivot inside the "
                        f"post-touch buffer yet."
                    )
            elif state == "waiting_confirmation":
                es = c.get("entry_support") or {}
                br = c.get("blocking_resistance") or {}
                if es.get("price"):
                    out.append(
                        f"{side}: support at {es.get('price')} — "
                        f"waiting for fast confirmation at "
                        f"{es.get('confirmed_time_fast')}"
                    )
                elif br.get("price"):
                    out.append(
                        f"{side}: resistance at {br.get('price')} — "
                        f"waiting for fast confirmation at "
                        f"{br.get('confirmed_time')}"
                    )
            elif state == "blocked":
                note = c.get("state_note") or ""
                if note == "entry_below_support":
                    out.append(
                        f"{side}: entry broke support during confirmation."
                    )
                elif note == "all_candidates_in_window_blocked":
                    out.append(
                        f"{side}: buffer window expired with no "
                        f"tradeable confirmation."
                    )
                elif note == "invalid_counter_price":
                    out.append(
                        f"{side}: auto-reverse failed — counter side "
                        f"price unavailable."
                    )
            elif state == "armed" and c.get("warning"):
                out.append(
                    f"{side}: {c.get('warning')} — {c.get('warning_detail')}"
                )
        _ = now
        return out

strategy_entry_signal_service = StrategyEntrySignalService()