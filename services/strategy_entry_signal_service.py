# services/strategy_entry_signal_service.py
"""R3/S3-touch -> buffered fast-pivot -> LTP-tier stop-loss entry signal (G1 v2).

Reads today's isolation record (from `level_isolated_instrument_service`)
and the persisted SR levels of both the bias side and the counter side,
runs a FAST pivot scan (left=15, right=3) on the live candle series for
both sides, and produces the entry-signal block that powers the "Entry
Signal" card on `/isolated`.

Signal chain (see core/config.py STRATEGY_ENTRY_* block for tunables):

    touch_time  ─┐
                 │   window = [touch_time, touch_time + BUFFER]
                 ▼
    ┌───────────────────────────┐
    │  FAST SUPPORT pivot in    │   for each candidate side
    │  window (left=15, right=3)│   pivot_time ∈ window
    └────────────┬──────────────┘
                 │
                 ▼
    ┌───────────────────────────┐
    │  Not blocked by a         │   resistance pivot
    │  resistance?              │   pivot_time   > support.pivot_time
    │                           │   price        > support.price
    │                           │   confirmed_fast ≤ support.confirmed_fast
    └────────────┬──────────────┘
                 │
                 ▼
    ┌───────────────────────────┐
    │  Wait for confirmed_fast  │   ARMED when confirmed_time_fast ≤ now
    │  → entry price = candle   │   (candle close, fallback: live LTP)
    │    close at confirmed_fast│
    └────────────┬──────────────┘
                 │
                 ▼
    ┌───────────────────────────┐
    │  LTP-tier stop-loss       │   sl_pct = tier_lookup(entry_price)
    │  stop = entry × (1 - sl)  │   stop_pct = (entry − stop) / entry
    └───────────────────────────┘

G1 v2 changes (compared to v1)
------------------------------
* Dual confirmation windows: `confirmed_time` (display, 15 bars) is
  still surfaced for the chart / SR card / Telegram. The entry uses
  `confirmed_time_fast` (default 3 bars), so a pivot that formed at
  10:04 arms at 10:07 (not 10:19).
* LTP-tier stop-loss: stop = entry_price × (1 − sl_pct), sl_pct from
  `STRATEGY_ENTRY_STOP_LTP_TIERS`, not from the support price.
* New hard blocks: `invalid_entry_price` (≤0), `no_entry_price`
  (candle close and LTP both missing), `entry_below_support`
  (entry_price < support.price).
* New soft warning: `support_far_below_entry` (support >20% below
  entry — informational, does not block).
* `STRATEGY_ENTRY_PRICE_MODE` removed. Entry is always "close at
  confirmed_time_fast, else live LTP".

Crossover
---------
When `STRATEGY_ENTRY_ALLOW_CROSSOVER` is True, BOTH the bias side and
the counter side are evaluated every poll. Whichever reaches ARMED with
the earliest `confirmed_time_fast` becomes the chosen candidate; the
other (if it also arms) is marked "discarded".

Promotion
---------
When the CHOSEN side differs from the official isolated instrument
(bias side), the response includes a `promotion` block that tells the
caller to promote the counter-side instrument to the active/official
pick.  Applying the promotion is delegated to
`isolated_instrument_promotion_service` — this service stays a pure
read/compute routine (no side effects).

Once-per-day
------------
`STRATEGY_ENTRY_ONLY_ONCE_PER_DAY` (default True) means that once a
candidate side is ARMED and shown, that side is not re-armed even if a
new support confirms later. `chosen_locked` is the flag the UI uses to
display the final pick.

Threading
---------
All state is recomputed on every `compute()` call. There is no mutable
state on the service itself, so multiple concurrent readers are safe.
The service holds no locks. The fast scan reads the SR engine's candle
state under the SR engine's guard — a brief, non-blocking read.
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
    """Parse an ISO-8601 string into a tz-aware datetime (or None)."""
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
    """Return ``(touch_side, bias_type)`` from an isolation reason string.

    Recognised substrings (case-insensitive):
      * ``r3`` -> touch_side="R3", bias_type="CE"
      * ``s3`` -> touch_side="S3", bias_type="PE"
    """
    r = str(reason or "").lower()
    if "r3" in r:
        return "R3", "CE"
    if "s3" in r:
        return "S3", "PE"
    return None, None

# --------------------------------------------------------------------------- #
# G1 v2 — LTP-tier stop-loss helpers                                          #
# --------------------------------------------------------------------------- #
_DEFAULT_STOP_TIERS: list[dict[str, Any]] = [
    {"max_ltp": 100.0, "sl_pct": 0.15},
    {"max_ltp": 200.0, "sl_pct": 0.12},
    {"max_ltp": 300.0, "sl_pct": 0.10},
    {"max_ltp": None,  "sl_pct": 0.05},
]

def _parse_stop_tiers() -> list[dict[str, Any]]:
    """Parse STRATEGY_ENTRY_STOP_LTP_TIERS defensively.

    Bad JSON, non-list, or entries with invalid ``sl_pct`` fall back to
    the default tier table. Called on every arm (cheap; the raw string
    is short).
    """
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

    # Ensure an open-ended (max_ltp=None) tier is present; if not,
    # append the last tier with max_ltp set to None as a fallback.
    if not any(t.get("max_ltp") is None for t in out):
        last_sl = out[-1]["sl_pct"]
        out.append({"max_ltp": None, "sl_pct": last_sl})

    return out

def _tier_lookup(entry_price: float) -> float:
    """Return the configured sl_pct for this entry price."""
    tiers = _parse_stop_tiers()
    for tier in tiers:
        max_ltp = tier.get("max_ltp")
        if max_ltp is None or entry_price < max_ltp:
            return float(tier["sl_pct"])
    return float(tiers[-1]["sl_pct"]) if tiers else 0.15

def _get_tick_size(instrument_key: str) -> float:
    """Return the tick size for rounding.

    Falls back to 0.05 (NSE/BSE F&O options default) when the registry
    entry does not carry a usable tick_size.
    """
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
    """Round value to the nearest tick based on mode."""
    if tick <= 0:
        return float(value)
    mode = (mode or "down").lower()
    if mode == "up":
        return math.ceil(value / tick) * tick
    if mode == "nearest":
        return round(value / tick) * tick
    # default: down
    return math.floor(value / tick) * tick

def _compute_ltp_tier_stop(
    entry_price: float,
    instrument_key: str,
) -> tuple[float, float, float | None, str]:
    """Compute the LTP-tier stop-loss.

    Returns ``(stop_price, sl_pct, stop_pct, stop_source)``.

    Applies the STRATEGY_MAX_STOP_PCT hard cap (logs ERROR when the
    tiers request more than the cap). Rounds to the instrument's tick
    via STRATEGY_ENTRY_STOP_TICK_ROUNDING.
    """
    sl_pct = _tier_lookup(entry_price)

    hard_cap = float(getattr(config, "STRATEGY_MAX_STOP_PCT", 0.35))
    if sl_pct > hard_cap:
        logger.error(
            "[strategy-entry] sl_pct %.4f exceeds hard cap %.4f; clamping. "
            "Fix STRATEGY_ENTRY_STOP_LTP_TIERS.",
            sl_pct, hard_cap,
        )
        sl_pct = hard_cap

    stop_raw = entry_price * (1.0 - sl_pct)
    tick = _get_tick_size(instrument_key)
    mode = str(getattr(config, "STRATEGY_ENTRY_STOP_TICK_ROUNDING", "down")).lower()
    stop = _round_to_tick(stop_raw, tick, mode)

    if stop <= 0:
        # Pathological data — fall back to the raw computed value so the
        # caller still sees a positive number (the entry block's
        # entry_below_support / invalid_entry_price blocks already ran).
        stop = stop_raw

    stop_pct = ((entry_price - stop) / entry_price) if entry_price > 0 else None
    return float(stop), float(sl_pct), (
        round(stop_pct, 6) if stop_pct is not None else None
    ), "ltp_tier"

# --------------------------------------------------------------------------- #
# Candle-close-at-time lookup                                                 #
# --------------------------------------------------------------------------- #
def _candle_close_at(instrument_key: str, iso_time: str) -> float | None:
    """Return the close of the 1-min candle that ended at ``iso_time``.

    Prefers the live SR engine's in-memory candle buffer (which is seeded
    from the merged historical + intraday series by ``sr_backfill_service``
    on every refresh, so it holds the full session's candles for backfill
    and live mode both).

    Falls back to ``None`` on any failure — the caller then falls back to
    the live LTP.
    """
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
    """Return ``(price, source)`` for the entry using FAST confirmation.

    Source is one of:
      * ``"candle_close_at_confirmed_fast"`` — exact candle close at
        ``confirmed_time_fast``
      * ``"live_ltp"``                       — fallback when the bar
                                               isn't yet finalized or
                                               the candle isn't in state
      * ``None``                             — no price available
    """
    close = _candle_close_at(instrument_key, confirmed_time_iso)
    if close is not None:
        return close, "candle_close_at_confirmed_fast"
    ltp = _live_ltp(instrument_key)
    if ltp is not None:
        return ltp, "live_ltp"
    return None, None

# --------------------------------------------------------------------------- #
# G1 v2 — Fast pivot scan on the live SR engine's candle series              #
# --------------------------------------------------------------------------- #
def _run_pivot_scan(
    instrument_key: str,
    left_bars: int,
    right_bars: int,
) -> list[dict[str, Any]]:
    """Run a strict pivot scan on the SR engine's in-memory candles.

    Returns a list of pivot records::

        {
          "level":             "support" | "resistance",
          "price":             <float>,
          "pivot_bar_index":   <int>,
          "pivot_time":        <ISO>,
          "confirmed_bar_index": <int>,
          "confirmed_time":    <ISO>,   # = FAST confirmed time
          "left_bars":         <int>,
          "right_bars":        <int>,
        }

    Pivot rule (same as live_sr_levels_service, but with the caller's
    ``right_bars``)::

        support   pivot at bar i  iff  C[i].low  < C[j].low  for every
                                       j in [i-left, i-1] and [i+1, i+right]
        resistance pivot at bar i  iff  C[i].high > C[j].high for every
                                       j in [i-left, i-1] and [i+1, i+right]

    Never raises. Returns [] on any failure (missing state, unreadable
    candles, too few bars).
    """
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

        # ---- Support pivot? ---- #
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

        # ---- Resistance pivot? ---- #
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
    """Return the display (15-bar) ``confirmed_time`` for a fast pivot.

    Uses the same candle series as `_run_pivot_scan` but adds
    ``LIVE_SR_RIGHT_BARS`` (default 15) to the pivot index. Returns
    None when the display bar hasn't finalized yet (i.e. the pivot is
    not yet a display-confirmed SR level).
    """
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

    display_right = int(getattr(config, "LIVE_SR_RIGHT_BARS", 15))
    confirm_idx = pivot_bar_index + display_right
    if confirm_idx < 0 or confirm_idx >= len(candles):
        return None
    try:
        return str(candles[confirm_idx]["time"])
    except (KeyError, TypeError):
        return None

# --------------------------------------------------------------------------- #
# Counterpart lookup                                                          #
# --------------------------------------------------------------------------- #
def _both_sides(
    underlying: str,
    strike: Any,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Return ``(ce_entry, pe_entry)`` at the given (underlying, strike)."""
    try:
        from services.instrument_registry import instrument_registry
    except Exception:
        return None, None

    ce = instrument_registry.resolve(underlying, strike, "CE")
    pe = instrument_registry.resolve(underlying, strike, "PE")
    return ce, pe

# --------------------------------------------------------------------------- #
# Per-candidate signal                                                        #
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
        # waiting | waiting_confirmation | blocked | armed | discarded | error
        "state": "waiting",
        "state_note": None,
        "entry_support": None,
        "blocking_resistance": None,
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
) -> dict[str, Any]:
    """Compute the signal for ONE candidate side (CE or PE) under G1 v2."""
    if entry is None:
        return _empty_candidate(side, None, None, is_bias_side)

    key = str(entry.get("instrument_key") or "").strip()
    symbol = entry.get("trading_symbol")
    cand = _empty_candidate(side, key, symbol, is_bias_side)

    # ---- 1. FAST pivot scan --------------------------------------- #
    left_bars = int(getattr(
        config, "STRATEGY_ENTRY_PIVOT_LEFT_BARS", config.LIVE_SR_LEFT_BARS
    ))
    right_bars = int(getattr(config, "STRATEGY_ENTRY_PIVOT_RIGHT_BARS", 3))
    levels = _run_pivot_scan(key, left_bars, right_bars)

    if not levels:
        cand["state_note"] = "no_sr_levels"
        return cand

    # ---- 2. Supports whose pivot_time is inside the window --------- #
    supports_in_window: list[dict[str, Any]] = []
    for lvl in levels:
        if str(lvl.get("level") or "").lower() != "support":
            continue
        p_dt = _parse_iso(lvl.get("pivot_time"))
        if p_dt is None:
            continue
        if p_dt < touch_dt:
            continue
        if p_dt > window_end:
            continue
        try:
            price = float(lvl.get("price"))
        except (TypeError, ValueError):
            continue
        supports_in_window.append({
            "price": price,
            "pivot_time": _iso(p_dt),
            "confirmed_time_fast": lvl.get("confirmed_time"),
            "pivot_bar_index": lvl.get("pivot_bar_index"),
            "pivot_dt": p_dt,
            "raw": lvl,
        })

    if not supports_in_window:
        cand["state"] = "waiting"
        cand["state_note"] = "no_support_pivot_in_window"
        return cand

    # ---- 3. Iterate in pivot_time order ---------------------------- #
    supports_in_window.sort(key=lambda s: s["pivot_dt"])

    for s in supports_in_window:
        support_pivot_dt = s["pivot_dt"]
        support_confirmed_fast_iso = s["confirmed_time_fast"]
        support_confirmed_fast_dt = _parse_iso(support_confirmed_fast_iso)
        if support_confirmed_fast_dt is None:
            continue

        # ---- 4. Resistance blocker (unchanged rule, fast window) --- #
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

        # ---- 5. Derive display (15-bar) confirmed_time ------------- #
        display_confirmed_iso = _derive_display_confirmed_time(
            key, s["pivot_bar_index"] or -1,
        )

        minutes_from_touch = _minutes_between(touch_dt, support_pivot_dt)
        entry_support = {
            "price": s["price"],
            "pivot_time": s["pivot_time"],
            "confirmed_time_fast": support_confirmed_fast_iso,
            "confirmed_time": display_confirmed_iso,
            "minutes_from_touch": round(minutes_from_touch, 2),
        }
        cand["entry_support"] = entry_support
        cand["blocking_resistance"] = None

        # ---- 6. Waiting for fast confirmation? -------------------- #
        if support_confirmed_fast_dt > now:
            cand["state"] = "waiting_confirmation"
            cand["state_note"] = "support_found_waiting_for_confirmation"
            cand["seconds_until_confirmation"] = int(
                (support_confirmed_fast_dt - now).total_seconds()
            )
            return cand

        # ---- 7. ARMED — resolve entry price ----------------------- #
        entry_price, price_source = _entry_price_for(
            key, support_confirmed_fast_iso,
        )

        # Hard block: no price at all
        if entry_price is None:
            cand["state"] = "blocked"
            cand["state_note"] = "no_entry_price"
            cand["entry"] = None
            return cand

        # Hard block: invalid price
        if entry_price <= 0:
            cand["state"] = "blocked"
            cand["state_note"] = "invalid_entry_price"
            cand["entry"] = None
            return cand

        # Hard block: entry price broke the support
        if entry_price < s["price"]:
            cand["state"] = "blocked"
            cand["state_note"] = "entry_below_support"
            cand["entry"] = None
            return cand

        # ---- 8. LTP-tier stop-loss -------------------------------- #
        stop_price, sl_pct, stop_pct, stop_source = _compute_ltp_tier_stop(
            entry_price, key,
        )

        # ---- 9. Soft warning: support far below entry ------------- #
        warning: str | None = None
        warning_detail: str | None = None
        support_gap_pct = (entry_price - s["price"]) / entry_price
        if support_gap_pct > 0.20:
            warning = "support_far_below_entry"
            warning_detail = (
                f"Support {s['price']:.2f} is "
                f"{support_gap_pct * 100:.1f}% below entry "
                f"{entry_price:.2f}; stop is "
                f"{sl_pct * 100:.1f}% below entry."
            )

        # ---- 10. Target: next resistance above entry -------------- #
        target_price: float | None = None
        target_pivot_time: str | None = None
        best_r: dict[str, Any] | None = None
        for lvl in levels:
            if str(lvl.get("level") or "").lower() != "resistance":
                continue
            try:
                r_price = float(lvl.get("price"))
            except (TypeError, ValueError):
                continue
            if r_price <= entry_price:
                continue
            if best_r is None or r_price < float(best_r.get("price") or 0.0):
                best_r = lvl
        if best_r is not None:
            try:
                target_price = float(best_r.get("price"))
            except (TypeError, ValueError):
                target_price = None
            target_pivot_time = best_r.get("pivot_time")

        cand["state"] = "armed"
        cand["state_note"] = "entry_support_confirmed_fast"
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

    # All supports in window were blocked.
    cand["state"] = "blocked"
    cand["state_note"] = "all_supports_in_window_blocked"
    return cand

# --------------------------------------------------------------------------- #
# Service                                                                     #
# --------------------------------------------------------------------------- #
class StrategyEntrySignalService:
    """Stateless computation of the entry-signal block for /isolated (G1 v2)."""

    def compute(self) -> dict[str, Any]:
        # ---- 0. Feature toggle ---------------------------------- #
        if not getattr(config, "STRATEGY_ENTRY_ENABLED", True):
            return {
                "status": "disabled",
                "reason": "STRATEGY_ENTRY_ENABLED is False",
                "generated_at": _now_utc_iso(),
            }

        # ---- 1. Read the isolation record ----------------------- #
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

        # ---- 2. Touch time -------------------------------------- #
        touch_time_iso = (
            isolated.get("touched_at")
            or isolated.get("isolated_at")
            or isolated.get("trigger", {}).get("touched_at")
        )
        touch_dt = _parse_iso(touch_time_iso)
        if touch_dt is None:
            return {
                "status": "no_touch_time",
                "note": (
                    "Isolation record has no touched_at / isolated_at, "
                    "cannot anchor the strategy window."
                ),
                "instrument_key": instrument_key,
                "generated_at": _now_utc_iso(),
            }

        # ---- 3. Side / bias ------------------------------------- #
        touch_side, bias_type = _parse_touch_reason(isolated.get("reason"))
        if touch_side is None or bias_type is None:
            return {
                "status": "no_touch_side",
                "note": (
                    "Isolation reason does not encode R3 or S3; "
                    "cannot determine the bias side."
                ),
                "instrument_key": instrument_key,
                "reason": isolated.get("reason"),
                "generated_at": _now_utc_iso(),
            }

        # ---- 4. Resolve isolated registry entry + both sides ----- #
        try:
            from services.instrument_registry import instrument_registry
            iso_entry = instrument_registry.get(instrument_key)
        except Exception:
            iso_entry = None

        if iso_entry is None:
            return {
                "status": "no_registry_entry",
                "note": (
                    "The isolated instrument is not in the registry — "
                    "cannot resolve strike / underlying."
                ),
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
                "underlying": underlying or None,
                "strike": strike,
                "generated_at": _now_utc_iso(),
            }

        ce_entry, pe_entry = _both_sides(underlying, strike)

        if bias_type == "CE":
            bias_entry = ce_entry
            counter_entry = pe_entry
        else:  # bias_type == "PE"
            bias_entry = pe_entry
            counter_entry = ce_entry

        # ---- 5. Build candidates -------------------------------- #
        now = _now_market()
        window_end = touch_dt + timedelta(
            minutes=int(getattr(config, "STRATEGY_ENTRY_BUFFER_MINUTES", 10))
        )

        candidate_specs: list[tuple[str, dict[str, Any] | None, bool]] = []

        if bias_entry is not None:
            candidate_specs.append((bias_type, bias_entry, True))

        allow_crossover = bool(
            getattr(config, "STRATEGY_ENTRY_ALLOW_CROSSOVER", True)
        )
        if allow_crossover and counter_entry is not None:
            opposite = "PE" if bias_type == "CE" else "CE"
            candidate_specs.append((opposite, counter_entry, False))

        candidates: list[dict[str, Any]] = []
        for side, entry, is_bias in candidate_specs:
            try:
                cand = _compute_candidate(
                    side=side,
                    entry=entry,
                    is_bias_side=is_bias,
                    touch_dt=touch_dt,
                    window_end=window_end,
                    now=now,
                )
            except Exception as exc:
                logger.exception(
                    "[strategy-entry] candidate compute failed for side=%s",
                    side,
                )
                cand = _empty_candidate(side, None, None, is_bias)
                cand["state"] = "error"
                cand["state_note"] = f"{type(exc).__name__}: {exc}"
            candidates.append(cand)

        # ---- 6. First-to-confirm winner (fast timestamps) ------- #
        chosen: dict[str, Any] | None = None
        armed = [c for c in candidates if c.get("state") == "armed"]

        first_wins = bool(
            getattr(config, "STRATEGY_ENTRY_FIRST_TO_CONFIRM_WINS", True)
        )
        if armed:
            if first_wins and len(armed) > 1:
                def _confirmed_dt(c: dict[str, Any]) -> str:
                    es = c.get("entry_support") or {}
                    return str(es.get("confirmed_time_fast") or "")
                armed.sort(key=_confirmed_dt)
                chosen = armed[0]
                for c in armed[1:]:
                    c["state"] = "discarded"
                    c["state_note"] = "first_to_confirm_won_by_other_side"
            else:
                chosen = armed[0]

        for c in candidates:
            c["chosen"] = (chosen is not None and c is chosen)

        # ---- 7. Assemble response ------------------------------- #
        response: dict[str, Any] = {
            "status": "ok",
            "day": iso_status.get("day"),
            "isolated": {
                "instrument_key": instrument_key,
                "trading_symbol": isolated.get("trading_symbol"),
                "underlying": underlying,
                "strike": strike,
                "reason": isolated.get("reason"),
                "touched_at": touch_time_iso,
                # Trigger metadata — the promotion service needs these
                # to build the promoted record with the original touch
                # context preserved (level, value, index).
                "trigger_level": (
                    isolated.get("trigger_level")
                    or isolated.get("trigger_side")
                ),
                "trigger_level_value": isolated.get("trigger_level_value"),
                "trigger_index": isolated.get("trigger_index"),
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
            "chosen_side": chosen.get("side") if chosen else None,
            "chosen_instrument_key": (
                chosen.get("instrument_key") if chosen else None
            ),
            "warnings": self._warnings(candidates, touch_dt, now),
            "generated_at": _now_utc_iso(),
        }

        # ---- 8. Promotion decision block ------------------------ #
        # Pure read-only check.  Actual promotion is applied by
        # `isolated_instrument_promotion_service.apply_promotion(...)`.
        try:
            from services.isolated_instrument_promotion_service import (
                isolated_instrument_promotion_service,
            )
            active_state = isolated_instrument_promotion_service.read_active()
        except Exception:
            active_state = None

        chosen_key = response.get("chosen_instrument_key")
        active_key = (
            active_state.get("to_instrument_key")
            if active_state else None
        )

        already_applied = (
            active_key is not None
            and chosen_key is not None
            and str(active_key) == str(chosen_key)
        )

        allow_promotion = bool(
            getattr(config, "STRATEGY_ENTRY_ALLOW_PROMOTION", True)
        )
        allow_re_promotion = bool(
            getattr(config, "STRATEGY_ENTRY_ALLOW_RE_PROMOTION", False)
        )

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

    # ------------------------------------------------------------------ #
    # Warnings                                                           #
    # ------------------------------------------------------------------ #
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
                    out.append(
                        f"{side}: no fast SR pivots available yet for this "
                        f"instrument (candle series too short or empty)"
                    )
                elif note == "no_support_pivot_in_window":
                    out.append(
                        f"{side}: no support pivot inside "
                        f"[{_iso(touch_dt)} → +"
                        f"{int(getattr(config, 'STRATEGY_ENTRY_BUFFER_MINUTES', 10))}min]"
                    )
            elif state == "waiting_confirmation":
                es = c.get("entry_support") or {}
                out.append(
                    f"{side}: support at {es.get('price')} found — "
                    f"waiting for fast confirmation at "
                    f"{es.get('confirmed_time_fast')}"
                )
            elif state == "blocked":
                note = c.get("state_note") or ""
                if note == "entry_below_support":
                    out.append(
                        f"{side}: entry price is below the support — "
                        f"the support did not hold during the "
                        f"confirmation wait."
                    )
                elif note == "invalid_entry_price":
                    out.append(
                        f"{side}: invalid entry price (≤ 0) — bad tick "
                        f"or dead instrument."
                    )
                elif note == "no_entry_price":
                    out.append(
                        f"{side}: no entry price available "
                        f"(candle close and live LTP both missing)."
                    )
                elif note == "all_supports_in_window_blocked":
                    out.append(
                        f"{side}: all support pivots inside the window "
                        f"were blocked by resistances."
                    )
                else:
                    blocker = c.get("blocking_resistance") or {}
                    out.append(
                        f"{side}: blocked by resistance "
                        f"{blocker.get('price')} "
                        f"(confirmed {blocker.get('confirmed_time')})"
                    )
            elif state == "armed" and c.get("warning"):
                out.append(
                    f"{side}: {c.get('warning')} — "
                    f"{c.get('warning_detail')}"
                )

        if not any(c.get("state") == "armed" for c in candidates):
            if all(c.get("state") == "waiting" for c in candidates):
                out.append(
                    "No candidate has produced a support pivot inside "
                    "the post-touch buffer yet."
                )

        _ = now  # reserved for future timing warnings
        return out

# --------------------------------------------------------------------------- #
# Singleton                                                                   #
# --------------------------------------------------------------------------- #
strategy_entry_signal_service = StrategyEntrySignalService()