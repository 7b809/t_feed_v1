# backtest_order_strategy.py
"""Standalone backtest for the t_feed_v1-market-engine-v3 order strategy.

Takes the two SR-export files that the live engine emits
(``isolated_instrument.json`` for the bias side and
``opposite_instrument.json`` for the counter side) plus the isolation
touch time, replays the day against the strategy spec, and writes a
single JSON result file.

The payload shape matches ``services/simulation_service.run()`` so a
result can be diffed against the live ``/admin/simulation/run`` response
for the same session, and additionally carries a top-level
``order_summary`` block with the P&L essentials:

    order_summary.entry.{price, time}
    order_summary.stop.{price, time_hit}          (only when hit)
    order_summary.exit.{price, time, reason}
    order_summary.points.{gross, per_contract}
    order_summary.pnl.{lot_size, contracts, total_points, invested,
                       gross_profit_inr, gross_loss_inr,
                       net_profit_inr, return_pct}

and an ``order_summary.lot_breakdown`` array with the same numbers
for each lot multiplier in ``LOT_MULTIPLIERS`` (default ``[1, 2, 5, 10]``).

Behaviour matches the spec's §1–§3:

    §1  Opening-range grid is computed from the traded instrument's own
        09:15 first candle.
    §2.1 Trigger = first 15/3 SR level on the bias side with
         ``pivot_time > T_iso`` AND ``confirmed_time > T_iso``.
    §2.2 09:30 backup reversal — if the post-isolation confirmation is
         on or before the cutoff, a prior Support was already confirmed
         before T_iso, AND the current post-isolation level is
         Resistance → BUY the SAME isolated leg. Otherwise:
           Support (post-isolation)   → BUY same leg.
           Resistance (post-isolation) → BUY opposite leg.
    §2.3 Regime check against the traded instrument's own OR grid:
           P_entry > R2                  → SKIP
           P_entry < S2                  → milestones: OR Low / +gain
           S2 ≤ P_entry < R1             → R2, R3_Threshold, R3
           R1 ≤ P_entry ≤ R2 (Zone 2)    → R3_Threshold, R3
    §2.4 Stop-loss = P_entry × (1 − stop_pct)   (default 0.30).
    §3   Stage 1 = milestone touch OR +gain_pts. Stage 2 = exit at the
         NEXT 15/3 Resistance confirmation close on the traded
         instrument. EOD close if no exit fires.

§4  1st follow-through order (SMALL-WIN MIRROR)
    When the primary order exits on a WINNER (STAGE_2_RESISTANCE_EXIT or
    STAGE_1_RESISTANCE_EXIT) with profit_points < MAX_WIN_POINTS_FOR_
    FOLLOWUP (default 20.0), we immediately place a SECOND order on the
    OPPOSITE instrument (the side that was NOT traded first) at the SAME
    exit timestamp/price of the primary order.

    The 1st follow-up uses a SIMPLIFIED exit rule:

        Entry   : opposite instrument's candle close at primary exit time
        Stop    : entry_price × (1 − stop_pct)  (same stop % as primary)
        Exit    : the FIRST post-entry 15/1 RESISTANCE confirmation close
                  on the opposite instrument — regardless of whether
                  Stage-1 / Stage-2 armed. No milestone / gain-point
                  arming is required; no EOD fallback fires as long as
                  at least one post-entry resistance level confirms.

    A 1st follow-up is NOT placed when:
      - primary exited with STOP_HIT, END_OF_DAY, OPEN, UNRESOLVED, or
      - primary exited with reason indicating a SKIP, or
      - primary profit_points <= 0 (a loss is not a winner), or
      - primary profit_points >= MAX_WIN_POINTS_FOR_FOLLOWUP, or
      - the opposite instrument has no candle at the primary exit minute
        (no valid entry price), or
      - the opposite instrument's entry would be classified as SKIP
        (entry > R2 against the opposite instrument's own OR grid).

§5  2nd follow-through order (LATE-MORNING RE-MIRROR)
    When the 1st follow-up exits on a WINNER with ALL of these true:

      * exit_reason is a winner exit (FOLLOWUP_RESISTANCE_EXIT_15_1, or
        any WINNER verdict from the 1st follow-up replay),
      * exit_time < 11:59 IST (strict cutoff — 11:59:59 still allowed
        if the minute bucket is 11:59; we use ``time() < 11:59``),
      * exit_price lies strictly between S3 and S2 of the instrument
        that the 1st follow-up traded (i.e. the opposite instrument's
        OR grid: ``S3 < exit_price < S2``),

    then a THIRD order is placed on the ORIGINAL traded instrument (the
    one the primary used) at the 1st follow-up's exit time/price.

    The 2nd follow-up uses a SIMPLIFIED exit rule:

        Entry   : original-traded instrument's candle close at 1st
                  follow-up exit time
        Stop    : entry_price × (1 − stop_pct)
        Exit    : the FIRST post-entry 15/30 RESISTANCE confirmation
                  close on the original-traded instrument — no
                  Stage-1 / Stage-2 arming required.
        Fallback: EOD / last available close if no 15/30 resistance
                  confirms after entry.

    A 2nd follow-up is NOT placed when:
      - 1st follow-up exited with STOP_HIT / EOD / OPEN / UNRESOLVED, or
      - 1st follow-up exit outcome is not a WIN, or
      - 1st follow-up exit time is >= 11:59 IST, or
      - 1st follow-up exit price is NOT strictly between S3 and S2 of
        the opposite instrument's own OR grid, or
      - the original traded instrument has no candle at the 1st
        follow-up exit minute.

Exit codes: 0 on any completed run (even if the order was SKIPPED or
no trigger was found); 1 on hard input errors.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


IST = ZoneInfo("Asia/Kolkata")


# =========================================================================== #
# Parsing helpers                                                            #
# =========================================================================== #
def _parse_iso(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt.astimezone(IST)


def _safe_float(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _candle_time(c: dict[str, Any]) -> Any:
    return c.get("timestamp") or c.get("time") or c.get("ts")


def _candle_close_at(
    candles: list[dict[str, Any]],
    target_minute: datetime,
    tolerance_minutes: int = 0,
) -> float | None:
    """Return the close of the candle whose minute bucket matches
    ``target_minute``. If ``tolerance_minutes`` > 0, the closest bar
    within that window is returned when no exact match exists."""
    target_minute = target_minute.replace(second=0, microsecond=0)
    best_delta = None
    best_close = None
    for c in candles:
        dt = _parse_iso(_candle_time(c))
        if dt is None:
            continue
        dt = dt.replace(second=0, microsecond=0)
        delta = abs((dt - target_minute).total_seconds())
        if delta == 0:
            return _safe_float(c.get("close"))
        if tolerance_minutes > 0 and delta <= tolerance_minutes * 60:
            if best_delta is None or delta < best_delta:
                best_delta = delta
                best_close = _safe_float(c.get("close"))
    return best_close


def _first_candle_for_day(
    candles: list[dict[str, Any]],
    target_date,
) -> dict[str, Any] | None:
    best: dict[str, Any] | None = None
    best_dt: datetime | None = None
    for c in candles:
        dt = _parse_iso(_candle_time(c))
        if dt is None or dt.date() != target_date:
            continue
        if best_dt is None or dt < best_dt:
            best_dt = dt
            best = c
    return best


def _sorted_candles(candles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def key(c):
        dt = _parse_iso(_candle_time(c))
        return dt.timestamp() if dt else 0.0
    return sorted(candles, key=key)


# =========================================================================== #
# §1 — Opening-range grid                                                   #
# =========================================================================== #
def compute_or_levels(first_candle: dict[str, Any]) -> dict[str, float] | None:
    """Compute the full OR grid from the traded instrument's first bar."""
    H = _safe_float(first_candle.get("high"))
    L = _safe_float(first_candle.get("low"))
    if H is None or L is None or H <= 0 or L <= 0:
        return None
    A = (H + L) / 2.0
    D_H = abs(H - A)
    D_L = abs(A - L)
    R1 = A + D_H / 2.0
    S1 = A - D_L / 2.0
    R2 = H + D_H
    S2 = L - D_L
    R3 = R2 + D_H
    S3 = S2 - D_L
    return {
        "H": H, "L": L, "A": A,
        "D_H": D_H, "D_L": D_L,
        "R1": R1, "R2": R2, "R3": R3,
        "S1": S1, "S2": S2, "S3": S3,
        "R3_Threshold": (R2 + R3) / 2.0,
        "S3_Threshold": (S2 + S3) / 2.0,
    }


# =========================================================================== #
# §2.3 — Regime / zone classification                                        #
# =========================================================================== #
def classify_zone(entry_price: float, or_levels: dict[str, float]) -> dict[str, Any]:
    R1, R2, R3 = or_levels["R1"], or_levels["R2"], or_levels["R3"]
    R3T = or_levels["R3_Threshold"]
    L, S2 = or_levels["L"], or_levels["S2"]

    if entry_price > R2:
        return {
            "zone": "ABOVE_R2",
            "milestones": [],
            "skip": True,
            "reason": (
                f"entry {entry_price:.4f} > R2 {R2:.4f} — "
                f"no valid logic present above R2 level"
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


# =========================================================================== #
# §2.1 / §2.2 — Trigger detection + routing                                 #
# =========================================================================== #
def find_trigger(
    levels: list[dict[str, Any]],
    t_iso: datetime,
) -> dict[str, Any] | None:
    """First 15/3 level whose pivot_time AND confirmed_time are both
    strictly after ``t_iso``. Earliest confirmed wins on ties."""
    candidates: list[tuple[datetime, datetime, dict[str, Any]]] = []
    for lvl in levels or []:
        pv = _parse_iso(lvl.get("pivot_time"))
        cf = _parse_iso(lvl.get("confirmed_time"))
        if pv is None or cf is None:
            continue
        if pv <= t_iso or cf <= t_iso:
            continue
        candidates.append((cf, pv, lvl))
    if not candidates:
        return None
    candidates.sort(key=lambda t: (t[0], t[1]))
    cf, pv, lvl = candidates[0]
    return {
        "level": str(lvl.get("level") or "").lower(),
        "price": _safe_float(lvl.get("price")),
        "pivot_time": pv.isoformat(),
        "confirmed_time": cf.isoformat(),
    }


def find_prior_support(
    levels: list[dict[str, Any]],
    t_iso: datetime,
) -> dict[str, Any] | None:
    """Latest support confirmed on or before ``t_iso``."""
    candidates: list[tuple[datetime, dict[str, Any]]] = []
    for lvl in levels or []:
        if str(lvl.get("level") or "").lower() != "support":
            continue
        cf = _parse_iso(lvl.get("confirmed_time"))
        if cf is None or cf > t_iso:
            continue
        candidates.append((cf, lvl))
    if not candidates:
        return None
    candidates.sort(key=lambda t: t[0], reverse=True)
    cf, lvl = candidates[0]
    return {
        "price": _safe_float(lvl.get("price")),
        "confirmed_time": cf.isoformat(),
    }


def decide_routing(
    trigger: dict[str, Any],
    prior_support: dict[str, Any] | None,
    bias_type: str,
    cutoff_dt: datetime | None,
) -> dict[str, Any]:
    post_conf = _parse_iso(trigger["confirmed_time"])
    reversal = (
        cutoff_dt is not None
        and post_conf is not None
        and post_conf <= cutoff_dt
        and prior_support is not None
        and trigger["level"] == "resistance"
    )
    if reversal:
        return {
            "mode": "reversal_same_leg",
            "traded_side": bias_type,
            "reason": (
                f"09:30 reversal — prior support @ {prior_support['confirmed_time']} "
                f"+ post-isolation resistance @ {trigger['confirmed_time']} "
                f"on or before cutoff"
            ),
        }
    if trigger["level"] == "support":
        return {
            "mode": "support_same_leg",
            "traded_side": bias_type,
            "reason": "post-isolation support → BUY the isolated instrument",
        }
    return {
        "mode": "resistance_opposite_leg",
        "traded_side": "PE" if bias_type == "CE" else "CE",
        "reason": (
            "post-isolation resistance → BUY the opposite leg "
            "(no reversal: prior support missing or confirmation after 09:30)"
        ),
    }


# =========================================================================== #
# §3 — Two-stage replay                                                      #
# =========================================================================== #
def build_resistance_exit_map(
    traded_levels: list[dict[str, Any]],
    traded_candles: list[dict[str, Any]],
) -> dict[str, float]:
    out: dict[str, float] = {}
    for lvl in traded_levels or []:
        if str(lvl.get("level") or "").lower() != "resistance":
            continue
        cf = _parse_iso(lvl.get("confirmed_time"))
        if cf is None:
            continue
        close = _candle_close_at(traded_candles, cf)
        if close is not None:
            out[cf.isoformat()] = close
    return out


def replay(
    traded_candles: list[dict[str, Any]],
    entry_time_iso: str,
    entry_price: float,
    stop_price: float,
    milestones: list[dict[str, Any]],
    resistance_exit_map: dict[str, float],
    gain_threshold: float,
    eod_dt: dtime,
) -> dict[str, Any]:
    entry_dt = _parse_iso(entry_time_iso)
    if entry_dt is None:
        return {"status": "invalid_entry_time"}

    sorted_exits: list[tuple[datetime, str, float]] = []
    for iso, price in resistance_exit_map.items():
        dt = _parse_iso(iso)
        if dt is not None:
            sorted_exits.append((dt, iso, price))
    sorted_exits.sort(key=lambda t: t[0])

    max_high = entry_price
    min_low = entry_price
    stage2_dt: datetime | None = None
    stage2_reason: str | None = None
    stage1_dt: datetime | None = None
    stage1_reason: str | None = None
    exit_reason: str | None = None
    exit_price: float | None = None
    exit_time: str | None = None
    stop_hit_time_iso: str | None = None
    stop_hit_price: float | None = None
    scanned = 0

    for c in traded_candles:
        c_dt = _parse_iso(_candle_time(c))
        if c_dt is None or c_dt <= entry_dt:
            continue
        scanned += 1
        hi = _safe_float(c.get("high"))
        lo = _safe_float(c.get("low"))
        if hi is None or lo is None:
            continue
        max_high = max(max_high, hi)
        min_low = min(min_low, lo)

        # 1. Stop-loss
        if lo <= stop_price:
            exit_reason = "STOP_HIT"
            exit_price = stop_price
            exit_time = c_dt.isoformat()
            stop_hit_time_iso = c_dt.isoformat()
            stop_hit_price = stop_price
            break

        # 2. Stage-1 arming
        if stage2_dt is None:
            armed_reason: str | None = None
            for m in milestones:
                m_price = _safe_float(m.get("price"))
                if m_price is not None and hi >= m_price:
                    armed_reason = f"MILESTONE_{m.get('name')}"
                    break
            if armed_reason is None:
                gain = hi - entry_price
                if gain >= gain_threshold:
                    armed_reason = f"GAIN_{gain:.2f}PT"
            if armed_reason is not None:
                stage2_dt = c_dt
                stage2_reason = armed_reason
                stage1_dt = c_dt
                stage1_reason = armed_reason

        # 3. Stage-2 exit
        if stage2_dt is not None:
            for conf_dt, conf_iso, conf_close in sorted_exits:
                if conf_dt <= stage2_dt:
                    continue
                if conf_dt <= c_dt:
                    exit_reason = "STAGE_2_RESISTANCE_EXIT"
                    exit_price = conf_close
                    exit_time = conf_iso
                    break
            if exit_reason:
                break

    # Unresolved — fall back to last close
    if exit_price is None:
        last_close: float | None = None
        last_time_iso: str | None = None
        for c in reversed(traded_candles):
            c_dt = _parse_iso(_candle_time(c))
            if c_dt is None or c_dt <= entry_dt:
                continue
            lc = _safe_float(c.get("close"))
            if lc is not None:
                last_close = lc
                last_time_iso = c_dt.isoformat()
                break
        if last_close is None:
            exit_price = entry_price
            exit_reason = "UNRESOLVED_NO_DATA"
        else:
            exit_price = last_close
            exit_time = last_time_iso
            last_dt = _parse_iso(last_time_iso)
            if last_dt is not None and last_dt.time() >= eod_dt:
                exit_reason = "END_OF_DAY"
            else:
                exit_reason = "OPEN"

    pnl_pct = (exit_price - entry_price) / entry_price if entry_price > 0 else 0.0
    profit_points = exit_price - entry_price
    stop_pct = (entry_price - stop_price) / entry_price if entry_price > 0 else 0.0
    r_mult = pnl_pct / stop_pct if stop_pct > 0 else None

    return {
        "status": "replayed",
        "candles_scanned": scanned,
        "exit_reason": exit_reason,
        "exit_time": exit_time,
        "exit_price": round(exit_price, 4),
        "pnl_pct": round(pnl_pct, 6),
        "profit_points": round(profit_points, 4),
        "r_multiple": round(r_mult, 4) if r_mult is not None else None,
        "max_high": round(max_high, 4),
        "min_low": round(min_low, 4),
        "mfe": round(max_high - entry_price, 4),
        "mae": round(min_low - entry_price, 4),
        "stage_1_triggered_at": stage1_dt.isoformat() if stage1_dt else None,
        "stage_1_trigger_reason": stage1_reason,
        "stage_2_triggered_at": stage2_dt.isoformat() if stage2_dt else None,
        "stage_2_trigger_reason": stage2_reason,
        "stop_hit_time": stop_hit_time_iso,
        "stop_hit_price": stop_hit_price,
    }


# =========================================================================== #
# §4/§5 — Simplified single-exit replays for follow-up orders               #
# =========================================================================== #
def replay_single_resistance_exit(
    traded_candles: list[dict[str, Any]],
    entry_time_iso: str,
    entry_price: float,
    stop_price: float,
    resistance_exit_map: dict[str, float],
    eod_dt: dtime,
    *,
    exit_reason_label: str,
    replay_mode_label: str,
) -> dict[str, Any]:
    """Simplified replay for follow-up orders.

    Exit rules:
      1. Stop-loss: hit if any bar's LOW touches ``stop_price``.
      2. Primary exit: the FIRST post-entry RESISTANCE confirmation close
         on the traded instrument (no Stage arming required). Exit reason
         is ``exit_reason_label``.
      3. Fallback: if no post-entry resistance confirms before EOD, the
         position is closed at the EOD / last available close.
    """
    entry_dt = _parse_iso(entry_time_iso)
    if entry_dt is None:
        return {"status": "invalid_entry_time"}

    sorted_exits: list[tuple[datetime, str, float]] = []
    for iso, price in resistance_exit_map.items():
        dt = _parse_iso(iso)
        if dt is not None:
            sorted_exits.append((dt, iso, price))
    sorted_exits.sort(key=lambda t: t[0])

    max_high = entry_price
    min_low = entry_price
    exit_reason: str | None = None
    exit_price: float | None = None
    exit_time: str | None = None
    stop_hit_time_iso: str | None = None
    stop_hit_price: float | None = None
    resistance_confirmed_time_iso: str | None = None
    scanned = 0

    for c in traded_candles:
        c_dt = _parse_iso(_candle_time(c))
        if c_dt is None or c_dt <= entry_dt:
            continue
        scanned += 1
        hi = _safe_float(c.get("high"))
        lo = _safe_float(c.get("low"))
        if hi is None or lo is None:
            continue
        max_high = max(max_high, hi)
        min_low = min(min_low, lo)

        # 1. Stop-loss always wins first
        if lo <= stop_price:
            exit_reason = "STOP_HIT"
            exit_price = stop_price
            exit_time = c_dt.isoformat()
            stop_hit_time_iso = c_dt.isoformat()
            stop_hit_price = stop_price
            break

        # 2. First post-entry resistance confirmation close
        if exit_reason is None:
            for conf_dt, conf_iso, conf_close in sorted_exits:
                if conf_dt <= entry_dt:
                    continue
                if conf_dt <= c_dt:
                    exit_reason = exit_reason_label
                    exit_price = conf_close
                    exit_time = conf_iso
                    resistance_confirmed_time_iso = conf_iso
                    break
            if exit_reason:
                break

    # Unresolved — fall back to last close
    if exit_price is None:
        last_close: float | None = None
        last_time_iso: str | None = None
        for c in reversed(traded_candles):
            c_dt = _parse_iso(_candle_time(c))
            if c_dt is None or c_dt <= entry_dt:
                continue
            lc = _safe_float(c.get("close"))
            if lc is not None:
                last_close = lc
                last_time_iso = c_dt.isoformat()
                break
        if last_close is None:
            exit_price = entry_price
            exit_reason = "UNRESOLVED_NO_DATA"
        else:
            exit_price = last_close
            exit_time = last_time_iso
            last_dt = _parse_iso(last_time_iso)
            if last_dt is not None and last_dt.time() >= eod_dt:
                exit_reason = "END_OF_DAY"
            else:
                exit_reason = "OPEN"

    pnl_pct = (exit_price - entry_price) / entry_price if entry_price > 0 else 0.0
    profit_points = exit_price - entry_price
    stop_pct = (entry_price - stop_price) / entry_price if entry_price > 0 else 0.0
    r_mult = pnl_pct / stop_pct if stop_pct > 0 else None

    return {
        "status": "replayed",
        "replay_mode": replay_mode_label,
        "candles_scanned": scanned,
        "exit_reason": exit_reason,
        "exit_time": exit_time,
        "exit_price": round(exit_price, 4),
        "pnl_pct": round(pnl_pct, 6),
        "profit_points": round(profit_points, 4),
        "r_multiple": round(r_mult, 4) if r_mult is not None else None,
        "max_high": round(max_high, 4),
        "min_low": round(min_low, 4),
        "mfe": round(max_high - entry_price, 4),
        "mae": round(min_low - entry_price, 4),
        "stage_1_triggered_at": None,
        "stage_1_trigger_reason": None,
        "stage_2_triggered_at": None,
        "stage_2_trigger_reason": None,
        "resistance_confirmed_time": resistance_confirmed_time_iso,
        "stop_hit_time": stop_hit_time_iso,
        "stop_hit_price": stop_hit_price,
    }


def replay_followup(
    traded_candles: list[dict[str, Any]],
    entry_time_iso: str,
    entry_price: float,
    stop_price: float,
    resistance_exit_map: dict[str, float],
    eod_dt: dtime,
) -> dict[str, Any]:
    """1st follow-up replay — exits on the first post-entry 15/1
    resistance confirmation (or EOD fallback)."""
    return replay_single_resistance_exit(
        traded_candles=traded_candles,
        entry_time_iso=entry_time_iso,
        entry_price=entry_price,
        stop_price=stop_price,
        resistance_exit_map=resistance_exit_map,
        eod_dt=eod_dt,
        exit_reason_label="FOLLOWUP_RESISTANCE_EXIT_15_1",
        replay_mode_label="followup_next_15_1_resistance",
    )


def replay_second_followup(
    traded_candles: list[dict[str, Any]],
    entry_time_iso: str,
    entry_price: float,
    stop_price: float,
    resistance_exit_map: dict[str, float],
    eod_dt: dtime,
) -> dict[str, Any]:
    """2nd follow-up replay — exits on the first post-entry 15/30
    resistance confirmation (or EOD fallback)."""
    return replay_single_resistance_exit(
        traded_candles=traded_candles,
        entry_time_iso=entry_time_iso,
        entry_price=entry_price,
        stop_price=stop_price,
        resistance_exit_map=resistance_exit_map,
        eod_dt=eod_dt,
        exit_reason_label="SECOND_FOLLOWUP_RESISTANCE_EXIT_15_30",
        replay_mode_label="second_followup_next_15_30_resistance",
    )


# =========================================================================== #
# Lot-wise P&L summary                                                       #
# =========================================================================== #
def build_order_summary(
    traded_meta: dict[str, Any],
    entry_time_iso: str,
    entry_price: float,
    stop_price: float,
    replay_result: dict[str, Any],
    *,
    lot_size: int,
    lot_multipliers: list[int],
) -> dict[str, Any]:
    """Assemble the P&L-ready order_summary block."""
    exit_price = _safe_float(replay_result.get("exit_price")) or entry_price
    exit_time_iso = replay_result.get("exit_time")
    exit_reason = replay_result.get("exit_reason") or "OPEN"

    per_contract_points = round(exit_price - entry_price, 4)
    return_pct = (
        round((exit_price - entry_price) / entry_price, 6)
        if entry_price > 0 else 0.0
    )

    stop_hit_time_iso = replay_result.get("stop_hit_time")
    stop_hit_price = replay_result.get("stop_hit_price")

    def _row(mult: int) -> dict[str, Any]:
        qty = lot_size * mult
        invested = round(entry_price * qty, 2)
        total_points = round(per_contract_points * qty, 2)
        return {
            "lots": mult,
            "contracts": qty,
            "invested_inr": invested,
            "total_points": total_points,
            "net_profit_inr": round(total_points, 2),
            "return_pct": return_pct,
        }

    lot_breakdown = [_row(m) for m in lot_multipliers]
    base_row = _row(1)
    net_profit_inr_1lot = base_row["net_profit_inr"]

    return {
        "instrument": {
            "instrument_key": traded_meta.get("instrument_key"),
            "trading_symbol": traded_meta.get("trading_symbol"),
            "side": "BUY",
        },
        "entry": {
            "price": round(entry_price, 4),
            "time": entry_time_iso,
        },
        "stop": {
            "price": round(stop_price, 4),
            "hit": stop_hit_price is not None,
            "time_hit": stop_hit_time_iso,
        },
        "exit": {
            "price": round(exit_price, 4),
            "time": exit_time_iso,
            "reason": exit_reason,
        },
        "points": {
            "per_contract": per_contract_points,
            "return_pct": return_pct,
        },
        "pnl": {
            "lot_size": lot_size,
            "contracts": lot_size,
            "invested_inr": base_row["invested_inr"],
            "gross_profit_inr": round(per_contract_points * lot_size, 2),
            "net_profit_inr": net_profit_inr_1lot,
            "return_pct": return_pct,
        },
        "lot_breakdown": lot_breakdown,
        "stage_1": {
            "triggered_at": replay_result.get("stage_1_triggered_at"),
            "trigger_reason": replay_result.get("stage_1_trigger_reason"),
        },
        "stage_2": {
            "triggered_at": replay_result.get("stage_2_triggered_at"),
            "trigger_reason": replay_result.get("stage_2_trigger_reason"),
        },
    }


# =========================================================================== #
# §4 — 1st follow-through order (small-win mirror)                          #
# =========================================================================== #
WIN_EXIT_REASONS = {"STAGE_2_RESISTANCE_EXIT", "STAGE_1_RESISTANCE_EXIT"}

# Cached per-run thresholds used by both follow-up gates. Set in backtest().
_SECOND_FOLLOWUP_CUTOFF = dtime(11, 59)  # 11:59 IST


def _is_followup_eligible(
    primary_replay: dict[str, Any],
    max_win_points: float,
) -> tuple[bool, str]:
    """Gate for the 1st follow-up (small-win mirror)."""
    if not primary_replay or primary_replay.get("status") != "replayed":
        return False, "primary order did not replay"
    exit_reason = str(primary_replay.get("exit_reason") or "")
    if exit_reason in {"STOP_HIT", "END_OF_DAY", "OPEN",
                       "UNRESOLVED_NO_DATA", "UNRESOLVED"}:
        return False, f"primary exit {exit_reason} is not a winner exit"
    if exit_reason not in WIN_EXIT_REASONS:
        return (
            False,
            f"primary exit {exit_reason} is not a recognised winner exit",
        )
    profit_points = _safe_float(primary_replay.get("profit_points"))
    if profit_points is None:
        return False, "primary profit_points missing"
    if profit_points <= 0:
        return (
            False,
            f"primary profit_points {profit_points:.2f} <= 0 "
            f"— a loss is not a winner",
        )
    if profit_points >= max_win_points:
        return (
            False,
            f"primary profit_points {profit_points:.2f} >= "
            f"{max_win_points:.2f} — large win, move already played out, "
            f"follow-up skipped",
        )
    return (
        True,
        f"small winner exit ({exit_reason}) with +{profit_points:.2f} pts "
        f"(< {max_win_points:.2f}) — mirroring on the opposite leg",
    )


def run_followup_order(
    *,
    opposite_meta: dict[str, Any],
    opposite_candles: list[dict[str, Any]],
    opposite_levels: list[dict[str, Any]],
    primary_replay: dict[str, Any],
    target_date,
    stop_pct: float,
    eod_hour: int,
    eod_minute: int,
    lot_size: int,
    lot_multipliers: list[int],
) -> dict[str, Any]:
    """1st follow-up: mirror on the OPPOSITE instrument, exit on first
    post-entry 15/1 resistance confirmation close."""
    entry_time_iso = primary_replay.get("exit_time")
    entry_dt = _parse_iso(entry_time_iso)
    if entry_dt is None:
        return {
            "status": "skipped",
            "reason": "primary exit_time missing — no follow-up placed",
        }

    entry_price = _candle_close_at(
        opposite_candles, entry_dt, tolerance_minutes=1,
    )
    if entry_price is None or entry_price <= 0:
        return {
            "status": "skipped",
            "reason": (
                f"no opposite-instrument candle close at primary exit "
                f"minute {entry_time_iso}"
            ),
        }

    first_candle = _first_candle_for_day(opposite_candles, target_date)
    or_levels = compute_or_levels(first_candle) if first_candle else None
    if or_levels is None:
        return {
            "status": "skipped",
            "reason": "cannot compute opposite instrument OR levels",
        }

    zone = classify_zone(entry_price, or_levels)
    if zone.get("skip"):
        return {
            "status": "skipped",
            "reason": (
                f"opposite entry ₹{entry_price:.4f} > "
                f"R2 ₹{or_levels['R2']:.4f} — skipped"
            ),
            "entry_price": round(entry_price, 4),
            "entry_time": entry_time_iso,
            "or_levels": {k: round(v, 4) for k, v in or_levels.items()},
            "regime": zone,
        }

    stop_price = round(entry_price * (1.0 - stop_pct), 4)
    resistance_exit_map = build_resistance_exit_map(
        opposite_levels, opposite_candles,
    )
    replay_result = replay_followup(
        traded_candles=opposite_candles,
        entry_time_iso=entry_time_iso,
        entry_price=entry_price,
        stop_price=stop_price,
        resistance_exit_map=resistance_exit_map,
        eod_dt=dtime(eod_hour, eod_minute),
    )

    pnl = _safe_float(replay_result.get("pnl_pct")) or 0.0
    exit_reason = replay_result.get("exit_reason")
    if exit_reason == "STOP_HIT":
        outcome = "LOSS"
    elif exit_reason in ("FOLLOWUP_RESISTANCE_EXIT_15_1",
                         "STAGE_2_RESISTANCE_EXIT"):
        outcome = "WIN" if pnl >= 0 else "LOSS"
    elif exit_reason in ("END_OF_DAY", "OPEN"):
        outcome = "WIN" if pnl > 0 else ("LOSS" if pnl < 0 else "UNRESOLVED")
    else:
        outcome = "UNRESOLVED"

    summary = build_order_summary(
        traded_meta=opposite_meta,
        entry_time_iso=entry_time_iso,
        entry_price=entry_price,
        stop_price=stop_price,
        replay_result=replay_result,
        lot_size=lot_size,
        lot_multipliers=lot_multipliers,
    )

    return {
        "status": "ok",
        "placed_at_primary_exit": {
            "time": entry_time_iso,
            "price": round(entry_price, 4),
            "reason": "primary_small_win_followup",
        },
        "instrument": {
            "instrument_key": opposite_meta.get("instrument_key"),
            "trading_symbol": opposite_meta.get("trading_symbol"),
            "side": "BUY",
        },
        "or_levels": {k: round(v, 4) for k, v in or_levels.items()},
        "regime": zone,
        "exit_rule": "next_post_entry_15_1_resistance_confirmation",
        "order": {
            "status": replay_result.get("exit_reason") or "OPEN",
            "entry_time": entry_time_iso,
            "entry_price": round(entry_price, 4),
            "stop_price": stop_price,
            "stop_pct": stop_pct,
        },
        "replay": replay_result,
        "verdict": {
            "outcome": outcome,
            "reason": (
                f"{exit_reason} @ ₹{replay_result.get('exit_price')} "
                f"({pnl * 100:+.2f}%)"
            ),
        },
        "order_summary": summary,
    }


# =========================================================================== #
# §5 — 2nd follow-through order (late-morning re-mirror)                    #
# =========================================================================== #
def _is_second_followup_eligible(
    first_followup_order: dict[str, Any] | None,
    first_followup_or_levels: dict[str, float] | None,
) -> tuple[bool, str]:
    """Gate for the 2nd follow-up.

    All three must hold on the 1st follow-up's result:
      1. WIN outcome (replay produced a positive profit and did not stop).
      2. Exit time strictly before 11:59 IST.
      3. Exit price strictly between S3 and S2 of the 1st follow-up
         instrument's OR grid.
    """
    if not first_followup_order or first_followup_order.get("status") != "ok":
        return False, "1st follow-up was not placed"
    replay = first_followup_order.get("replay") or {}
    verdict = first_followup_order.get("verdict") or {}
    outcome = str(verdict.get("outcome") or "").upper()
    if outcome != "WIN":
        return (
            False,
            f"1st follow-up outcome is {outcome or 'UNKNOWN'} — "
            f"2nd follow-up requires a WIN",
        )
    exit_reason = str(replay.get("exit_reason") or "")
    if exit_reason in {"STOP_HIT", "END_OF_DAY", "OPEN",
                       "UNRESOLVED_NO_DATA", "UNRESOLVED"}:
        return (
            False,
            f"1st follow-up exit {exit_reason} is not a winner exit",
        )
    profit_points = _safe_float(replay.get("profit_points"))
    if profit_points is None or profit_points <= 0:
        return (
            False,
            f"1st follow-up profit_points {profit_points} <= 0 — "
            f"2nd follow-up requires a positive winner",
        )

    exit_dt = _parse_iso(replay.get("exit_time"))
    if exit_dt is None:
        return False, "1st follow-up exit_time missing"
    if exit_dt.time() >= _SECOND_FOLLOWUP_CUTOFF:
        return (
            False,
            f"1st follow-up exit time {exit_dt.strftime('%H:%M')} "
            f">= {_SECOND_FOLLOWUP_CUTOFF.strftime('%H:%M')} — "
            f"2nd follow-up skipped (too late)",
        )

    exit_price = _safe_float(replay.get("exit_price"))
    if exit_price is None:
        return False, "1st follow-up exit_price missing"
    if not first_followup_or_levels:
        return False, "1st follow-up OR levels missing"
    S2 = _safe_float(first_followup_or_levels.get("S2"))
    S3 = _safe_float(first_followup_or_levels.get("S3"))
    if S2 is None or S3 is None:
        return False, "1st follow-up OR S2/S3 missing"
    if not (S3 < exit_price < S2):
        return (
            False,
            f"1st follow-up exit price ₹{exit_price:.4f} is not strictly "
            f"between S3 ₹{S3:.4f} and S2 ₹{S2:.4f} — 2nd follow-up skipped",
        )

    return (
        True,
        f"1st follow-up WIN ({outcome}) before 11:59 with exit ₹"
        f"{exit_price:.4f} in (S3 ₹{S3:.4f}, S2 ₹{S2:.4f}) — "
        f"placing 2nd follow-up on the original traded instrument",
    )


def run_second_followup_order(
    *,
    original_traded_meta: dict[str, Any],
    original_traded_candles: list[dict[str, Any]],
    original_traded_levels: list[dict[str, Any]],
    first_followup_order: dict[str, Any],
    target_date,
    stop_pct: float,
    eod_hour: int,
    eod_minute: int,
    lot_size: int,
    lot_multipliers: list[int],
) -> dict[str, Any]:
    """2nd follow-up: re-mirror back onto the ORIGINAL traded instrument
    at the 1st follow-up's exit time/price, exit on first post-entry
    15/30 resistance confirmation close.
    """
    first_replay = first_followup_order.get("replay") or {}
    entry_time_iso = first_replay.get("exit_time")
    entry_dt = _parse_iso(entry_time_iso)
    if entry_dt is None:
        return {
            "status": "skipped",
            "reason": "1st follow-up exit_time missing — no 2nd follow-up",
        }

    entry_price = _candle_close_at(
        original_traded_candles, entry_dt, tolerance_minutes=1,
    )
    if entry_price is None or entry_price <= 0:
        return {
            "status": "skipped",
            "reason": (
                f"no original-instrument candle close at 1st follow-up "
                f"exit minute {entry_time_iso}"
            ),
        }

    first_candle = _first_candle_for_day(
        original_traded_candles, target_date,
    )
    or_levels = compute_or_levels(first_candle) if first_candle else None
    if or_levels is None:
        return {
            "status": "skipped",
            "reason": "cannot compute original-instrument OR levels",
        }

    zone = classify_zone(entry_price, or_levels)
    if zone.get("skip"):
        return {
            "status": "skipped",
            "reason": (
                f"original-instrument entry ₹{entry_price:.4f} > "
                f"R2 ₹{or_levels['R2']:.4f} — 2nd follow-up skipped"
            ),
            "entry_price": round(entry_price, 4),
            "entry_time": entry_time_iso,
            "or_levels": {k: round(v, 4) for k, v in or_levels.items()},
            "regime": zone,
        }

    stop_price = round(entry_price * (1.0 - stop_pct), 4)
    resistance_exit_map = build_resistance_exit_map(
        original_traded_levels, original_traded_candles,
    )
    replay_result = replay_second_followup(
        traded_candles=original_traded_candles,
        entry_time_iso=entry_time_iso,
        entry_price=entry_price,
        stop_price=stop_price,
        resistance_exit_map=resistance_exit_map,
        eod_dt=dtime(eod_hour, eod_minute),
    )

    pnl = _safe_float(replay_result.get("pnl_pct")) or 0.0
    exit_reason = replay_result.get("exit_reason")
    if exit_reason == "STOP_HIT":
        outcome = "LOSS"
    elif exit_reason == "SECOND_FOLLOWUP_RESISTANCE_EXIT_15_30":
        outcome = "WIN" if pnl >= 0 else "LOSS"
    elif exit_reason in ("END_OF_DAY", "OPEN"):
        outcome = "WIN" if pnl > 0 else ("LOSS" if pnl < 0 else "UNRESOLVED")
    else:
        outcome = "UNRESOLVED"

    summary = build_order_summary(
        traded_meta=original_traded_meta,
        entry_time_iso=entry_time_iso,
        entry_price=entry_price,
        stop_price=stop_price,
        replay_result=replay_result,
        lot_size=lot_size,
        lot_multipliers=lot_multipliers,
    )

    return {
        "status": "ok",
        "placed_at_first_followup_exit": {
            "time": entry_time_iso,
            "price": round(entry_price, 4),
            "reason": "first_followup_win_in_s2_s3_before_1159",
        },
        "instrument": {
            "instrument_key": original_traded_meta.get("instrument_key"),
            "trading_symbol": original_traded_meta.get("trading_symbol"),
            "side": "BUY",
        },
        "or_levels": {k: round(v, 4) for k, v in or_levels.items()},
        "regime": zone,
        "exit_rule": "next_post_entry_15_30_resistance_confirmation",
        "order": {
            "status": replay_result.get("exit_reason") or "OPEN",
            "entry_time": entry_time_iso,
            "entry_price": round(entry_price, 4),
            "stop_price": stop_price,
            "stop_pct": stop_pct,
        },
        "replay": replay_result,
        "verdict": {
            "outcome": outcome,
            "reason": (
                f"{exit_reason} @ ₹{replay_result.get('exit_price')} "
                f"({pnl * 100:+.2f}%)"
            ),
        },
        "order_summary": summary,
    }


# =========================================================================== #
# Orchestrator                                                               #
# =========================================================================== #
def backtest(
    isolated_path: str | Path,
    opposite_path: str | Path,
    touch_time: str,
    *,
    stop_pct: float = 0.30,
    gain_points: float = 50.0,
    reversal_cutoff_hour: int = 9,
    reversal_cutoff_minute: int = 30,
    eod_hour: int = 15,
    eod_minute: int = 35,
    lot_size: int = 65,
    lot_multipliers: list[int] | None = None,
    max_win_points_for_followup: float = 20.0,
    enable_followup: bool = True,
    enable_second_followup: bool = True,
    second_followup_cutoff_hour: int = 11,
    second_followup_cutoff_minute: int = 59,
) -> dict[str, Any]:
    """Run the full backtest and return the result dict."""
    global _SECOND_FOLLOWUP_CUTOFF
    _SECOND_FOLLOWUP_CUTOFF = dtime(
        second_followup_cutoff_hour, second_followup_cutoff_minute,
    )

    if lot_multipliers is None:
        lot_multipliers = [1, 2, 5, 10]

    with open(isolated_path, "r", encoding="utf-8") as f:
        isolated = json.load(f)
    with open(opposite_path, "r", encoding="utf-8") as f:
        opposite = json.load(f)

    t_iso = _parse_iso(touch_time)
    if t_iso is None:
        return {
            "status": "error",
            "error": f"invalid touch_time: {touch_time!r}",
        }
    target_date = t_iso.date()

    iso_meta = isolated.get("metadata") or {}
    opp_meta = opposite.get("metadata") or {}

    iso_inst = iso_meta.get("instrument") or {}
    bias_type = str(
        iso_inst.get("instrument_type")
        or iso_inst.get("option_type")
        or ""
    ).upper()
    if bias_type not in {"CE", "PE"}:
        return {
            "status": "error",
            "error": (
                f"isolated instrument type unknown — need CE/PE, "
                f"got {bias_type!r}"
            ),
        }

    iso_candles = _sorted_candles(isolated.get("candles") or [])
    opp_candles = _sorted_candles(opposite.get("candles") or [])
    iso_levels = isolated.get("levels") or []
    opp_levels = opposite.get("levels") or []

    trigger = find_trigger(iso_levels, t_iso)
    if trigger is None:
        return {
            "status": "no_trigger",
            "detail": "no post-isolation 15/3 SR level on the bias side",
            "day": target_date.isoformat(),
            "isolated": {
                "instrument_key": iso_meta.get("instrument_key"),
                "trading_symbol": iso_meta.get("trading_symbol"),
                "bias_type": bias_type,
                "t_iso": t_iso.isoformat(),
            },
            "opposite": {
                "instrument_key": opp_meta.get("instrument_key"),
                "trading_symbol": opp_meta.get("trading_symbol"),
            },
            "counts": {
                "isolated_levels": len(iso_levels),
                "opposite_levels": len(opp_levels),
            },
        }

    prior_support = find_prior_support(iso_levels, t_iso)

    cutoff_dt = t_iso.replace(
        hour=reversal_cutoff_hour,
        minute=reversal_cutoff_minute,
        second=0, microsecond=0,
    )
    routing = decide_routing(trigger, prior_support, bias_type, cutoff_dt)

    if routing["traded_side"] == bias_type:
        traded_meta = iso_meta
        traded_candles = iso_candles
        traded_levels = iso_levels
        followup_meta = opp_meta
        followup_candles = opp_candles
        followup_levels = opp_levels
        traded_role = "isolated"
    else:
        traded_meta = opp_meta
        traded_candles = opp_candles
        traded_levels = opp_levels
        followup_meta = iso_meta
        followup_candles = iso_candles
        followup_levels = iso_levels
        traded_role = "opposite"

    traded_key = traded_meta.get("instrument_key")

    first_candle = _first_candle_for_day(traded_candles, target_date)
    if first_candle is None:
        return {
            "status": "error",
            "error": (
                f"no first candle for day {target_date.isoformat()} "
                f"on traded instrument {traded_key}"
            ),
            "routing": routing,
        }
    or_levels = compute_or_levels(first_candle)
    if or_levels is None:
        return {
            "status": "error",
            "error": "cannot compute OR levels (bad first candle)",
            "routing": routing,
            "first_candle": first_candle,
        }

    trigger_conf_dt = _parse_iso(trigger["confirmed_time"])
    entry_time_iso = trigger["confirmed_time"]
    entry_price = _candle_close_at(
        traded_candles, trigger_conf_dt, tolerance_minutes=1,
    )
    if entry_price is None or entry_price <= 0:
        return {
            "status": "error",
            "error": (
                f"no traded-instrument candle close at trigger minute "
                f"{trigger_conf_dt.isoformat()}"
            ),
            "routing": routing,
            "trigger": trigger,
        }

    zone = classify_zone(entry_price, or_levels)

    base: dict[str, Any] = {
        "day": target_date.isoformat(),
        "isolated": {
            "instrument_key": iso_meta.get("instrument_key"),
            "trading_symbol": iso_meta.get("trading_symbol"),
            "bias_type": bias_type,
            "t_iso": t_iso.isoformat(),
        },
        "opposite": {
            "instrument_key": opp_meta.get("instrument_key"),
            "trading_symbol": opp_meta.get("trading_symbol"),
        },
        "trigger": trigger,
        "prior_support": prior_support,
        "routing": routing,
        "traded_instrument": {
            "instrument_key": traded_key,
            "trading_symbol": traded_meta.get("trading_symbol"),
            "side": "BUY",
        },
        "or_levels": {k: round(v, 4) for k, v in or_levels.items()},
        "regime": zone,
        "params": {
            "stop_pct": stop_pct,
            "gain_points": gain_points,
            "reversal_cutoff": f"{reversal_cutoff_hour:02d}:{reversal_cutoff_minute:02d}",
            "eod": f"{eod_hour:02d}:{eod_minute:02d}",
            "lot_size": lot_size,
            "lot_multipliers": lot_multipliers,
            "max_win_points_for_followup": max_win_points_for_followup,
            "enable_followup": enable_followup,
            "enable_second_followup": enable_second_followup,
            "second_followup_cutoff": (
                f"{second_followup_cutoff_hour:02d}:"
                f"{second_followup_cutoff_minute:02d}"
            ),
        },
    }

    if zone.get("skip"):
        base["status"] = "ok"
        base["order"] = {
            "status": "SKIPPED",
            "entry_time": entry_time_iso,
            "entry_price": round(entry_price, 4),
            "stop_price": None,
            "stop_pct": None,
            "skip_reason": zone.get("reason"),
        }
        base["replay"] = None
        base["verdict"] = {
            "outcome": "SKIPPED",
            "reason": zone.get("reason"),
        }
        base["notes"] = [
            f"Routing: {routing['mode']} — {routing['reason']}",
            f"Trigger: {trigger['level'].upper()} @ {trigger['price']} "
            f"confirmed {entry_time_iso}",
            f"Entry {entry_price:.4f} > R2 {or_levels['R2']:.4f} — "
            f"order rejected.",
        ]
        base["candles_total"] = len(traded_candles)
        base["order_summary"] = None
        base["followup_order"] = None
        base["followup_order_summary"] = None
        base["followup_gate"] = {
            "placed": False,
            "reason": "primary order was SKIPPED",
        }
        base["second_followup_order"] = None
        base["second_followup_order_summary"] = None
        base["second_followup_gate"] = {
            "placed": False,
            "reason": "primary order was SKIPPED",
        }
        base["combined_pnl"] = {
            "lot_size": lot_size,
            "primary_net_inr": 0.0,
            "followup_net_inr": 0.0,
            "second_followup_net_inr": 0.0,
            "combined_net_inr": 0.0,
            "followup_placed": False,
            "second_followup_placed": False,
        }
        return base

    stop_price = round(entry_price * (1.0 - stop_pct), 4)
    resistance_exit_map = build_resistance_exit_map(traded_levels, traded_candles)

    replay_result = replay(
        traded_candles=traded_candles,
        entry_time_iso=entry_time_iso,
        entry_price=entry_price,
        stop_price=stop_price,
        milestones=zone.get("milestones") or [],
        resistance_exit_map=resistance_exit_map,
        gain_threshold=gain_points,
        eod_dt=dtime(eod_hour, eod_minute),
    )

    base["status"] = "ok"
    base["order"] = {
        "status": replay_result.get("exit_reason") or "OPEN",
        "entry_time": entry_time_iso,
        "entry_price": round(entry_price, 4),
        "stop_price": stop_price,
        "stop_pct": stop_pct,
    }
    base["replay"] = replay_result

    pnl = _safe_float(replay_result.get("pnl_pct")) or 0.0
    exit_reason = replay_result.get("exit_reason")
    if exit_reason == "STOP_HIT":
        outcome = "LOSS"
    elif exit_reason == "STAGE_2_RESISTANCE_EXIT":
        outcome = "WIN" if pnl >= 0 else "LOSS"
    elif exit_reason in ("END_OF_DAY", "OPEN"):
        outcome = "WIN" if pnl > 0 else ("LOSS" if pnl < 0 else "UNRESOLVED")
    else:
        outcome = "UNRESOLVED"

    base["verdict"] = {
        "outcome": outcome,
        "reason": (
            f"{exit_reason} @ ₹{replay_result.get('exit_price')} "
            f"({pnl * 100:+.2f}%)"
        ),
    }

    base["order_summary"] = build_order_summary(
        traded_meta=traded_meta,
        entry_time_iso=entry_time_iso,
        entry_price=entry_price,
        stop_price=stop_price,
        replay_result=replay_result,
        lot_size=lot_size,
        lot_multipliers=lot_multipliers,
    )

    # ---- §4 1st follow-up order on the opposite instrument ---------- #
    followup_result: dict[str, Any] | None = None
    followup_summary: dict[str, Any] | None = None

    if not enable_followup:
        followup_gate = {
            "placed": False,
            "reason": "follow-up disabled via enable_followup=False",
        }
    else:
        eligible, gate_reason = _is_followup_eligible(
            replay_result, max_win_points_for_followup,
        )
        if not eligible:
            followup_gate = {"placed": False, "reason": gate_reason}
        else:
            followup_result = run_followup_order(
                opposite_meta=followup_meta,
                opposite_candles=followup_candles,
                opposite_levels=followup_levels,
                primary_replay=replay_result,
                target_date=target_date,
                stop_pct=stop_pct,
                eod_hour=eod_hour,
                eod_minute=eod_minute,
                lot_size=lot_size,
                lot_multipliers=lot_multipliers,
            )
            if followup_result.get("status") == "ok":
                followup_summary = followup_result.get("order_summary")
                followup_gate = {
                    "placed": True,
                    "reason": gate_reason,
                    "exit_rule": "next_post_entry_15_1_resistance_confirmation",
                    "placed_at_primary_exit":
                        followup_result.get("placed_at_primary_exit"),
                    "instrument": followup_result.get("instrument"),
                    "entry": followup_result["order"],
                    "verdict": followup_result.get("verdict"),
                }
            else:
                followup_gate = {
                    "placed": False,
                    "reason": followup_result.get("reason")
                        or "follow-up order could not be placed",
                    "detail": followup_result,
                }

    base["followup_order"] = followup_result
    base["followup_order_summary"] = followup_summary
    base["followup_gate"] = followup_gate

    # ---- §5 2nd follow-up order on the ORIGINAL traded instrument --- #
    second_followup_result: dict[str, Any] | None = None
    second_followup_summary: dict[str, Any] | None = None

    if not enable_second_followup:
        second_followup_gate = {
            "placed": False,
            "reason": "2nd follow-up disabled via enable_second_followup=False",
        }
    else:
        first_followup_or_levels = (
            (followup_result or {}).get("or_levels") if followup_result else None
        )
        eligible2, gate_reason2 = _is_second_followup_eligible(
            followup_result, first_followup_or_levels,
        )
        if not eligible2:
            second_followup_gate = {
                "placed": False, "reason": gate_reason2,
            }
        else:
            second_followup_result = run_second_followup_order(
                original_traded_meta=traded_meta,
                original_traded_candles=traded_candles,
                original_traded_levels=traded_levels,
                first_followup_order=followup_result,
                target_date=target_date,
                stop_pct=stop_pct,
                eod_hour=eod_hour,
                eod_minute=eod_minute,
                lot_size=lot_size,
                lot_multipliers=lot_multipliers,
            )
            if second_followup_result.get("status") == "ok":
                second_followup_summary = (
                    second_followup_result.get("order_summary")
                )
                second_followup_gate = {
                    "placed": True,
                    "reason": gate_reason2,
                    "exit_rule":
                        "next_post_entry_15_30_resistance_confirmation",
                    "placed_at_first_followup_exit":
                        second_followup_result
                        .get("placed_at_first_followup_exit"),
                    "instrument": second_followup_result.get("instrument"),
                    "entry": second_followup_result["order"],
                    "verdict": second_followup_result.get("verdict"),
                }
            else:
                second_followup_gate = {
                    "placed": False,
                    "reason": second_followup_result.get("reason")
                        or "2nd follow-up order could not be placed",
                    "detail": second_followup_result,
                }

    base["second_followup_order"] = second_followup_result
    base["second_followup_order_summary"] = second_followup_summary
    base["second_followup_gate"] = second_followup_gate

    # ---- Combined P&L across primary + 1st + 2nd follow-ups ---------- #
    primary_net = (
        (_safe_float(replay_result.get("profit_points")) or 0.0) * lot_size
    )
    followup_net = 0.0
    if followup_summary and (followup_summary.get("pnl") or {}):
        followup_net = _safe_float(
            followup_summary["pnl"].get("net_profit_inr")
        ) or 0.0
    second_followup_net = 0.0
    if second_followup_summary and (second_followup_summary.get("pnl") or {}):
        second_followup_net = _safe_float(
            second_followup_summary["pnl"].get("net_profit_inr")
        ) or 0.0

    base["combined_pnl"] = {
        "lot_size": lot_size,
        "primary_net_inr": round(primary_net, 2),
        "followup_net_inr": round(followup_net, 2),
        "second_followup_net_inr": round(second_followup_net, 2),
        "combined_net_inr": round(
            primary_net + followup_net + second_followup_net, 2,
        ),
        "followup_placed": bool(followup_summary),
        "second_followup_placed": bool(second_followup_summary),
    }

    # ---- Human-readable notes ---------------------------------------- #
    notes: list[str] = [
        f"Routing: {routing['mode']} — {routing['reason']}",
        f"Trigger: {trigger['level'].upper()} @ {trigger['price']} "
        f"confirmed {entry_time_iso}",
    ]
    if prior_support:
        notes.append(
            f"Prior support: ₹{prior_support['price']} "
            f"@ {prior_support['confirmed_time']}"
        )
    notes.append(
        f"Entry ₹{entry_price:.4f} on "
        f"{traded_meta.get('trading_symbol') or traded_key} "
        f"({traded_role})"
    )
    notes.append(
        f"Zone {zone['zone']} — milestones: " +
        ", ".join(
            f"{m['name']}=₹{m['price']}"
            for m in (zone.get("milestones") or [])
        )
    )
    notes.append(f"Stop-loss ₹{stop_price:.4f} ({-stop_pct * 100:.1f}%)")
    if replay_result.get("stage_2_triggered_at"):
        notes.append(
            f"Stage 2 armed @ {replay_result['stage_2_triggered_at']} "
            f"({replay_result.get('stage_2_trigger_reason')})"
        )
    notes.append(
        f"Exit: {exit_reason} @ ₹{replay_result.get('exit_price')} "
        f"({pnl * 100:+.2f}%)"
    )
    notes.append(
        f"Lot {lot_size} × 1 = {lot_size} qty | "
        f"Primary net ₹{primary_net:+,.2f}"
    )
    if followup_gate.get("placed"):
        fo = followup_summary.get("pnl") or {}
        foreplay = followup_result.get("replay") or {}
        notes.append(
            f"1st follow-up placed on "
            f"{(followup_result.get('instrument') or {}).get('trading_symbol')} "
            f"@ ₹{followup_result['order']['entry_price']:.4f} "
            f"(at primary exit {followup_result['order']['entry_time']}); "
            f"exit rule = first post-entry 15/1 resistance confirmation"
        )
        if foreplay.get("resistance_confirmed_time"):
            notes.append(
                f"1st follow-up resistance confirmed at "
                f"{foreplay['resistance_confirmed_time']}"
            )
        notes.append(
            f"1st follow-up exit "
            f"{(followup_result.get('verdict') or {}).get('reason')} | "
            f"Net ₹{(fo.get('net_profit_inr') or 0):+,.2f}"
        )
    else:
        notes.append(
            f"1st follow-up order not placed — "
            f"{followup_gate.get('reason')}"
        )

    if second_followup_gate.get("placed"):
        so = second_followup_summary.get("pnl") or {}
        sreplay = second_followup_result.get("replay") or {}
        notes.append(
            f"2nd follow-up placed on "
            f"{(second_followup_result.get('instrument') or {}).get('trading_symbol')} "
            f"@ ₹{second_followup_result['order']['entry_price']:.4f} "
            f"(at 1st follow-up exit "
            f"{second_followup_result['order']['entry_time']}); "
            f"exit rule = first post-entry 15/30 resistance confirmation"
        )
        if sreplay.get("resistance_confirmed_time"):
            notes.append(
                f"2nd follow-up resistance confirmed at "
                f"{sreplay['resistance_confirmed_time']}"
            )
        notes.append(
            f"2nd follow-up exit "
            f"{(second_followup_result.get('verdict') or {}).get('reason')} | "
            f"Net ₹{(so.get('net_profit_inr') or 0):+,.2f}"
        )
    else:
        notes.append(
            f"2nd follow-up order not placed — "
            f"{second_followup_gate.get('reason')}"
        )

    notes.append(
        f"Combined net (primary + 1st + 2nd follow-ups, 1 lot each): "
        f"₹{base['combined_pnl']['combined_net_inr']:+,.2f}"
    )
    base["notes"] = notes
    base["candles_total"] = len(traded_candles)

    return base


# =========================================================================== #
# CLI summary printer                                                        #
# =========================================================================== #
def _print_summary(result: dict[str, Any]) -> None:
    status = result.get("status")
    if status not in {"ok", "no_trigger"}:
        print(f"  Status:  {status}")
        if result.get("error"):
            print(f"  Error:   {result['error']}")
        return

    if status == "no_trigger":
        print("  Status:  no trigger — no post-isolation 15/3 SR on bias side")
        return

    order = result.get("order") or {}
    replay = result.get("replay") or {}
    verdict = result.get("verdict") or {}
    summary = result.get("order_summary") or {}
    print(f"  Routing:  {result['routing']['mode']}")
    print(f"  Traded:   {result['traded_instrument']['trading_symbol']}")
    print(f"  Entry:    ₹{order.get('entry_price')} @ {order.get('entry_time')}")
    if order.get("status") == "SKIPPED":
        print(f"  SKIPPED:  {order.get('skip_reason')}")
        return
    stop = summary.get("stop") or {}
    print(
        f"  Stop:     ₹{order.get('stop_price')} "
        f"({'-hit @ ' + str(stop.get('time_hit')) if stop.get('hit') else 'not hit'})"
    )
    print(f"  Zone:     {result['regime']['zone']}")
    print(
        f"  Exit:     {replay.get('exit_reason')} @ "
        f"₹{replay.get('exit_price')} "
        f"({(replay.get('pnl_pct') or 0) * 100:+.2f}%, "
        f"{replay.get('profit_points')} pts)"
    )
    print(f"  Verdict:  {verdict.get('outcome')}")

    pnl = summary.get("pnl") or {}
    if pnl:
        print()
        print(f"  ── Primary P&L (lot size = {pnl.get('lot_size')}) ──")
        print(f"  Invested: ₹{pnl.get('invested_inr'):,.2f}")
        print(f"  Net PnL:  ₹{pnl.get('net_profit_inr'):+,.2f} "
              f"({(pnl.get('return_pct') or 0) * 100:+.2f}%)")

    gate = result.get("followup_gate") or {}
    fo = result.get("followup_order") or {}
    fos = result.get("followup_order_summary") or {}
    print()
    print("  ── 1st follow-up (small-win mirror · next 15/1 resistance exit) ──")
    if gate.get("placed"):
        placed = gate.get("placed_at_primary_exit") or {}
        print(f"  Placed:   {gate['instrument'].get('trading_symbol')} "
              f"(mirror of primary exit)")
        print(f"  Entry:    ₹{fo['order']['entry_price']} "
              f"@ {placed.get('time')}")
        foreplay = fo.get("replay") or {}
        foverdict = fo.get("verdict") or {}
        print(f"  Exit:     {foreplay.get('exit_reason')} @ "
              f"₹{foreplay.get('exit_price')} "
              f"({(foreplay.get('pnl_pct') or 0) * 100:+.2f}%, "
              f"{foreplay.get('profit_points')} pts)")
        if foreplay.get("resistance_confirmed_time"):
            print(f"  Res 15/1: {foreplay['resistance_confirmed_time']}")
        print(f"  Verdict:  {foverdict.get('outcome')}")
        fp = fos.get("pnl") or {}
        if fp:
            print(f"  Net PnL:  ₹{fp.get('net_profit_inr'):+,.2f} "
                  f"({(fp.get('return_pct') or 0) * 100:+.2f}%)")
    else:
        print(f"  Not placed — {gate.get('reason')}")

    gate2 = result.get("second_followup_gate") or {}
    fo2 = result.get("second_followup_order") or {}
    fos2 = result.get("second_followup_order_summary") or {}
    print()
    print("  ── 2nd follow-up (late-morning re-mirror · next 15/30 resistance exit) ──")
    if gate2.get("placed"):
        placed2 = gate2.get("placed_at_first_followup_exit") or {}
        print(f"  Placed:   {gate2['instrument'].get('trading_symbol')} "
              f"(mirror of 1st follow-up exit)")
        print(f"  Entry:    ₹{fo2['order']['entry_price']} "
              f"@ {placed2.get('time')}")
        foreplay2 = fo2.get("replay") or {}
        foverdict2 = fo2.get("verdict") or {}
        print(f"  Exit:     {foreplay2.get('exit_reason')} @ "
              f"₹{foreplay2.get('exit_price')} "
              f"({(foreplay2.get('pnl_pct') or 0) * 100:+.2f}%, "
              f"{foreplay2.get('profit_points')} pts)")
        if foreplay2.get("resistance_confirmed_time"):
            print(f"  Res 15/30: {foreplay2['resistance_confirmed_time']}")
        print(f"  Verdict:  {foverdict2.get('outcome')}")
        fp2 = fos2.get("pnl") or {}
        if fp2:
            print(f"  Net PnL:  ₹{fp2.get('net_profit_inr'):+,.2f} "
                  f"({(fp2.get('return_pct') or 0) * 100:+.2f}%)")
    else:
        print(f"  Not placed — {gate2.get('reason')}")

    combined = result.get("combined_pnl") or {}
    if combined:
        print()
        print("  ── Combined (1 lot each) ──")
        print(f"  Primary:  ₹{combined.get('primary_net_inr'):+,.2f}")
        print(f"  1st f/u:  ₹{combined.get('followup_net_inr'):+,.2f}")
        print(f"  2nd f/u:  ₹{combined.get('second_followup_net_inr'):+,.2f}")
        print(f"  Total:    ₹{combined.get('combined_net_inr'):+,.2f}")


# =========================================================================== #
# Static main — edit values, run, done                                       #
# =========================================================================== #
def main() -> None:
    # ==================================================================== #
    # EDIT HERE — static inputs. No CLI arguments.                        #
    # ==================================================================== #
    ISOLATED_PATH = "isolated_instrument.json"       # bias-side file
    OPPOSITE_PATH = "opposite_instrument.json"       # counter-side file

    TOUCH_DATE = "2026-10-27"                        # YYYY-MM-DD
    TOUCH_TIME = "09.40"                             # HH.MM (IST)

    OUTPUT_PATH = "simulation_results.json"

    STOP_PCT               = 0.30
    GAIN_POINTS            = 50.0
    REVERSAL_CUTOFF_HOUR   = 9
    REVERSAL_CUTOFF_MINUTE = 30
    EOD_HOUR               = 15
    EOD_MINUTE             = 35

    LOT_SIZE        = 65
    LOT_MULTIPLIERS = [1, 2, 5, 10]

    # ---- §4 / §5 follow-up settings ----------------------------------- #
    MAX_WIN_POINTS_FOR_FOLLOWUP = 20.0
    ENABLE_FOLLOWUP             = True

    # ---- §5 2nd follow-up settings ------------------------------------ #
    # 2nd follow-up fires ONLY when the 1st follow-up exits on a WIN,
    # strictly before 11:59 IST, with its exit price strictly between
    # the 1st follow-up instrument's own S3 and S2.
    ENABLE_SECOND_FOLLOWUP         = True
    SECOND_FOLLOWUP_CUTOFF_HOUR    = 11
    SECOND_FOLLOWUP_CUTOFF_MINUTE  = 59
    # ==================================================================== #

    def _parse_time_token(tok: str) -> tuple[int, int]:
        s = str(tok).strip().replace(":", ".").replace(" ", "")
        if "." in s:
            hh, mm = s.split(".", 1)
        else:
            hh, mm = s[:-2], s[-2:]
        return int(hh), int(mm)

    try:
        hh, mm = _parse_time_token(TOUCH_TIME)
    except Exception as exc:
        print(f"Invalid TOUCH_TIME {TOUCH_TIME!r}: {exc}", file=sys.stderr)
        sys.exit(1)

    touch_iso = f"{TOUCH_DATE}T{hh:02d}:{mm:02d}:00+05:30"

    try:
        result = backtest(
            isolated_path=ISOLATED_PATH,
            opposite_path=OPPOSITE_PATH,
            touch_time=touch_iso,
            stop_pct=STOP_PCT,
            gain_points=GAIN_POINTS,
            reversal_cutoff_hour=REVERSAL_CUTOFF_HOUR,
            reversal_cutoff_minute=REVERSAL_CUTOFF_MINUTE,
            eod_hour=EOD_HOUR,
            eod_minute=EOD_MINUTE,
            lot_size=LOT_SIZE,
            lot_multipliers=LOT_MULTIPLIERS,
            max_win_points_for_followup=MAX_WIN_POINTS_FOR_FOLLOWUP,
            enable_followup=ENABLE_FOLLOWUP,
            enable_second_followup=ENABLE_SECOND_FOLLOWUP,
            second_followup_cutoff_hour=SECOND_FOLLOWUP_CUTOFF_HOUR,
            second_followup_cutoff_minute=SECOND_FOLLOWUP_CUTOFF_MINUTE,
        )
    except FileNotFoundError as exc:
        print(f"Missing input file: {exc}", file=sys.stderr)
        sys.exit(1)
    except json.JSONDecodeError as exc:
        print(f"Invalid JSON in an input file: {exc}", file=sys.stderr)
        sys.exit(1)

    out = Path(OUTPUT_PATH)
    out.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")

    print(f"Wrote {out}  (touch_time={touch_iso}, lot_size={LOT_SIZE})")
    _print_summary(result)

    if result.get("status") == "error":
        sys.exit(1)


if __name__ == "__main__":
    main()