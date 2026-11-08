#!/usr/bin/env python3
"""
simulate_path_a_original.py

Unified Path A strategy simulator (STANDARD + EARLY_EXTREME_REVERSAL)
with MODIFIED routing/filters (suggestions 3 and 4):

  - Suggestion 3: bias-aware 09:30 backup reversal
      * PE backup reversal same-leg is SUPPRESSED (safe-mode).
      * CE backup reversal same-leg requires positive EMA(period) slope,
        computed from the isolated leg's own day candles (first-close
        seeded so early-session entries are handled).

  - Suggestion 4: EER same-leg premium band
      * EARLY_EXTREME_REVERSAL same-leg entries are only taken when
        eer_premium_min <= entry_price <= eer_premium_max.

  - Output filenames are <modified_simulation_results.json> for single
    mode and <modified_simulation_results_merged.json> for batch mode,
    so the original clean simulation_results.json files are preserved.

Default input folder (auto-detected):
  input_data/
      isolated_instrument.json   – isolated option leg (1-min candles + 15/3 S/R levels)
      opposite_instrument.json   – opposite option leg (same strike, same underlying)
      isolation.json             – isolation metadata object

Outputs:

  SINGLE-FOLDER MODE (default):
    input_data/modified_simulation_results.json
    results/{trading_symbol}_{date}_{expiry}/
      isolated_instrument.json      (copy of input)
      opposite_instrument.json      (copy of input)
      isolation.json                (copy of input)
      modified_simulation_results.json

  BATCH MODE (--batch):
    Scans --batch-dir (default: cwd) for subfolders that contain the three
    input files. For each bundle folder:
        <bundle>/modified_simulation_results.json
    At the batch root (unless --no-merged):
        modified_simulation_results_merged.json

Usage:
  # Single-folder mode
  python backtest_order_strategy.py
  python backtest_order_strategy.py --input-dir path/to/input_data
  python backtest_order_strategy.py --isolated a.json --opposite b.json --isolation c.json
  python backtest_order_strategy.py --output out.json --results-root my_results
  python backtest_order_strategy.py --no-artifacts

  # Batch mode
  python backtest_order_strategy.py --batch
  python backtest_order_strategy.py --batch --batch-dir results
  python backtest_order_strategy.py --batch --merged-output <path>
  python backtest_order_strategy.py --batch --no-merged
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
CONFIG: dict[str, Any] = {
    # ----- original Path A config -----
    "early_extreme_cutoff_time": time(10, 30, 0),
    "early_extreme_price_high": 250.0,
    "early_extreme_price_low": 100.0,
    "standard_backup_reversal_cutoff": time(9, 30, 0),
    "stop_loss_pct": 0.30,
    "target_band_low_max": 100.0,
    "target_band_mid_max": 200.0,
    "target_div_low": 3.0,
    "target_div_mid": 4.0,
    "target_div_high": 5.0,
    "eer_next_target_multiplier": 1.30,
    "quantity": 65,
    "eod_time": time(15, 35, 0),
    "sr_left_bars": 15,
    "sr_right_bars": 3,
    "eps": 1e-9,

    # ----- Suggestion 3: bias-aware 09:30 backup reversal -----
    "backup_pe_suppress": True,
    "backup_ce_require_positive_slope": True,
    "backup_ce_ema_period": 9,
    "backup_ce_ema_slope_lookback": 2,

    # ----- Suggestion 4: EER same-leg premium band -----
    "eer_apply_premium_band": True,
    "eer_premium_min": 80.0,
    "eer_premium_max": 100.0,
}

IST = timezone(timedelta(hours=5, minutes=30))

# ---------------------------------------------------------------------------
# Path defaults
# ---------------------------------------------------------------------------
DEFAULT_INPUT_DIR    = Path("input_data")
DEFAULT_RESULTS_ROOT = Path("results")

INPUT_ISOLATED_FILENAME  = "isolated_instrument.json"
INPUT_OPPOSITE_FILENAME  = "opposite_instrument.json"
INPUT_ISOLATION_FILENAME = "isolation.json"
OUTPUT_FILENAME          = "modified_simulation_results.json"
MERGED_OUTPUT_FILENAME   = "modified_simulation_results_merged.json"

REQUIRED_INPUTS = (
    INPUT_ISOLATED_FILENAME,
    INPUT_OPPOSITE_FILENAME,
    INPUT_ISOLATION_FILENAME,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def parse_ts(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt


def fmt_ts(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt is not None else None


def candle_at(candles: list[dict], ts: datetime) -> Optional[dict]:
    for c in candles:
        if parse_ts(c["timestamp"]) == ts:
            return c
    return None


def candles_after(candles: list[dict], ts: datetime) -> list[dict]:
    return [c for c in candles if parse_ts(c["timestamp"]) > ts]


def candles_upto(candles: list[dict], ts: datetime) -> list[dict]:
    return [c for c in candles if parse_ts(c["timestamp"]) <= ts]


def snapshot_close_at(candles: list[dict], ts: datetime) -> float:
    snap = None
    for c in candles:
        if parse_ts(c["timestamp"]) <= ts:
            snap = c
        else:
            break
    if snap is None:
        raise RuntimeError(f"No candle at or before {ts}")
    return float(snap["close"])


def target_divisor_and_price(entry: float) -> tuple[float, float]:
    if 0 < entry <= CONFIG["target_band_low_max"]:
        div = CONFIG["target_div_low"]
    elif entry <= CONFIG["target_band_mid_max"]:
        div = CONFIG["target_div_mid"]
    else:
        div = CONFIG["target_div_high"]
    return div, entry + entry / div


def sanitize_dirname(name: str) -> str:
    out = (name or "").strip()
    for ch in (" ", "/", "\\", ":", "*", "?", '"', "<", ">", "|"):
        out = out.replace(ch, "_")
    while "__" in out:
        out = out.replace("__", "_")
    return out.strip("_") or "unknown"


# ---------------------------------------------------------------------------
# EMA helpers (used by Suggestion 3 - CE backup reversal slope filter)
# ---------------------------------------------------------------------------
def _compute_ema_values(
    candles: list[dict], period: int
) -> list[tuple[datetime, float]]:
    """EMA(period) using first-close seed (handles early-session entries)."""
    if not candles:
        return []
    if period < 1:
        raise ValueError("period must be >= 1")
    k = 2.0 / (period + 1)
    out: list[tuple[datetime, float]] = []
    ema: Optional[float] = None
    for c in candles:
        close = float(c["close"])
        ts = parse_ts(c["timestamp"])
        if ema is None:
            ema = close
        else:
            ema = close * k + ema * (1.0 - k)
        out.append((ts, ema))
    return out


def _ema_slope_positive(
    candles: list[dict],
    up_to_ts: datetime,
    period: int,
    lookback: int,
) -> Optional[bool]:
    """True if EMA(period) rose from `lookback` bars ago to the bar at `up_to_ts`.

    Returns None if there are fewer than `lookback + 1` EMA values at/before
    `up_to_ts` (caller treats None as "insufficient data" pass-through).
    """
    if lookback < 1:
        raise ValueError("lookback must be >= 1")
    emas = _compute_ema_values(candles, period)
    eligible = [(t, v) for t, v in emas if t <= up_to_ts]
    if len(eligible) < lookback + 1:
        return None
    current = eligible[-1][1]
    prior = eligible[-(lookback + 1)][1]
    return current > prior


# ---------------------------------------------------------------------------
# Isolation → pinned execution mode
# ---------------------------------------------------------------------------
def decide_mode(
    isolation: dict, isolated_candles: list[dict]
) -> tuple[str, float, str, datetime]:
    touch_ts = parse_ts(f"{isolation['date']}T{isolation['touch_time']}+05:30")
    price = snapshot_close_at(isolated_candles, touch_ts)

    time_ok = touch_ts.timetz().replace(tzinfo=None) < CONFIG["early_extreme_cutoff_time"]
    price_ok = (
        price > CONFIG["early_extreme_price_high"]
        or price < CONFIG["early_extreme_price_low"]
    )

    if time_ok and price_ok:
        return (
            "EARLY_EXTREME_REVERSAL",
            price,
            "time < 10:30 IST AND price strictly >250 or <100",
            touch_ts,
        )

    reasons: list[str] = []
    if not time_ok:
        reasons.append(
            f"time {touch_ts.timetz().replace(tzinfo=None)} not strictly before 10:30 IST"
        )
    if not price_ok:
        reasons.append(f"snapshot price ₹{price:.2f} is not strictly >250 or <100")
    return "STANDARD", price, " | ".join(reasons), touch_ts


# ---------------------------------------------------------------------------
# First qualifying post-isolation 15/3 S/R
# ---------------------------------------------------------------------------
def first_qualifying_sr(
    levels: list[dict], isolation_ts: datetime
) -> Optional[dict]:
    qualified: list[tuple[datetime, dict]] = []
    for lv in levels:
        pivot_ts = parse_ts(lv["pivot_time"])
        conf_ts = parse_ts(lv["confirmed_time"])
        if pivot_ts > isolation_ts and conf_ts > isolation_ts:
            qualified.append((conf_ts, lv))
    qualified.sort(key=lambda x: x[0])
    return qualified[0][1] if qualified else None


# ---------------------------------------------------------------------------
# Routing matrix
# ---------------------------------------------------------------------------
def check_backup_reversal(
    first_sr: dict, isolated_levels: list[dict], isolation_ts: datetime
) -> bool:
    if first_sr["level"] != "resistance":
        return False
    cutoff = CONFIG["standard_backup_reversal_cutoff"]
    if parse_ts(first_sr["confirmed_time"]).timetz().replace(tzinfo=None) > cutoff:
        return False
    for lv in isolated_levels:
        if lv["level"] != "support":
            continue
        if parse_ts(lv["confirmed_time"]) <= isolation_ts:
            return True
    return False


def resolve_route(
    pinned_mode: str,
    first_sr: dict,
    isolated_levels: list[dict],
    isolation_ts: datetime,
    isolated_instrument: dict,
    opposite_instrument: dict,
) -> tuple[dict, str]:
    is_support = first_sr["level"] == "support"

    if pinned_mode == "STANDARD":
        if check_backup_reversal(first_sr, isolated_levels, isolation_ts):
            return isolated_instrument, "same-leg (STANDARD + 09:30 backup reversal)"
        if is_support:
            return isolated_instrument, "same-leg (STANDARD + Support)"
        return opposite_instrument, "opposite-leg (STANDARD + Resistance)"

    if is_support:
        return opposite_instrument, "opposite-leg (EARLY_EXTREME_REVERSAL + Support)"
    return isolated_instrument, "same-leg (EARLY_EXTREME_REVERSAL + Resistance)"


# ---------------------------------------------------------------------------
# Modified filters (Suggestions 3 and 4)
# ---------------------------------------------------------------------------
def check_modified_filters(
    pinned_mode: str,
    first_sr: dict,
    isolated_levels: list[dict],
    isolation_ts: datetime,
    bias: str,
    routing_mode: str,
    entry_price: float,
    isolated_candles: list[dict],
    entry_ts: datetime,
) -> tuple[bool, str]:
    """Apply modified filter rules (Suggestions 3 and 4).

    Returns (should_suppress, notes).
    """
    # --- Suggestion 3: bias-aware 09:30 backup reversal ---
    if pinned_mode == "STANDARD":
        is_backup = check_backup_reversal(first_sr, isolated_levels, isolation_ts)
        if is_backup:
            if bias == "PE" and CONFIG["backup_pe_suppress"]:
                return True, (
                    "CANCEL_FILTERED: STANDARD + 09:30 backup reversal on PE bias "
                    "is suppressed (safe-mode; in-sample PE backup was 0/2, "
                    "CE backup preserved)."
                )
            if bias == "CE" and CONFIG["backup_ce_require_positive_slope"]:
                slope = _ema_slope_positive(
                    isolated_candles,
                    entry_ts,
                    CONFIG["backup_ce_ema_period"],
                    CONFIG["backup_ce_ema_slope_lookback"],
                )
                if slope is False:
                    return True, (
                        f"CANCEL_FILTERED: STANDARD + 09:30 backup reversal on CE "
                        f"bias requires EMA{CONFIG['backup_ce_ema_period']} slope "
                        f"(lookback={CONFIG['backup_ce_ema_slope_lookback']}) "
                        f"positive; got non-positive."
                    )
                # slope is None → insufficient data → pass-through

    # --- Suggestion 4: EER same-leg premium band ---
    if (
        pinned_mode == "EARLY_EXTREME_REVERSAL"
        and CONFIG["eer_apply_premium_band"]
        and routing_mode.startswith("same-leg")
    ):
        lo = CONFIG["eer_premium_min"]
        hi = CONFIG["eer_premium_max"]
        if not (lo <= entry_price <= hi):
            return True, (
                f"CANCEL_FILTERED: EARLY_EXTREME_REVERSAL same-leg entry price "
                f"₹{entry_price:.2f} outside [{lo:.2f}, {hi:.2f}] premium band."
            )

    return False, ""


def _build_suppressed_result(
    isolation: dict,
    pinned_mode: str,
    routing_mode: str,
    entry_ts: datetime,
    entry_price: float,
    reason: str,
    snap_price: float,
    mode_reason: str,
    first_sr: dict,
) -> dict:
    """Build the ORDER_SUPPRESSED result payload (matches the existing sample shape)."""
    return {
        "simulation": {
            "generated_at": datetime.now(IST).isoformat(),
            "status": "ORDER_SUPPRESSED",
            "notes": reason,
            "strategy": "unified Path A (modified)",
            "guide": "STRATEGY_AND_ORDER_EXECUTION_GUIDE.md",
        },
        "date": isolation["date"],
        "isolation": isolation,
        "filter_context": {
            "pinned_mode": pinned_mode,
            "routing_reason": routing_mode,
            "bias": isolation["bias"],
            "snapshot_price": round(snap_price, 4),
            "mode_reason": mode_reason,
            "trigger_level": first_sr.get("level"),
            "trigger_price": first_sr.get("price"),
            "trigger_pivot_time": first_sr.get("pivot_time"),
            "trigger_confirmed_time": first_sr.get("confirmed_time"),
            "entry_time_attempted": fmt_ts(entry_ts),
            "entry_price_attempted": round(entry_price, 4),
        },
        "pnl": {"points": 0.0, "gross_pnl": 0.0, "return_pct": 0.0},
        "config": CONFIG,
    }


# ---------------------------------------------------------------------------
# New Resistance after a milestone
# ---------------------------------------------------------------------------
def next_resistance_after(
    isolated_levels: list[dict], after_ts: datetime
) -> Optional[dict]:
    candidates = [
        lv
        for lv in isolated_levels
        if lv["level"] == "resistance"
        and parse_ts(lv["confirmed_time"]) > after_ts
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda lv: parse_ts(lv["confirmed_time"]))
    return candidates[0]


# ---------------------------------------------------------------------------
# STANDARD exit engine
# ---------------------------------------------------------------------------
def simulate_standard(
    traded_candles: list[dict],
    isolated_levels: list[dict],
    entry_ts: datetime,
    entry_price: float,
    initial_stop: float,
    initial_target: float,
    eod_ts: datetime,
) -> dict:
    active_stop = initial_stop
    active_target = initial_target

    target_hit_ts: Optional[datetime] = None
    target_exit_ts: Optional[datetime] = None
    events: list[dict] = []

    exit_ts: Optional[datetime] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None

    for c in candles_after(traded_candles, entry_ts):
        cts = parse_ts(c["timestamp"])
        if cts > eod_ts:
            break

        if c["low"] <= active_stop + CONFIG["eps"]:
            exit_ts, exit_price, exit_reason = cts, active_stop, "STOP_HIT"
            events.append(
                {
                    "time": fmt_ts(cts),
                    "event": "stop_hit",
                    "stop_price": round(active_stop, 4),
                    "candle_low": c["low"],
                }
            )
            break

        if target_hit_ts is None and c["high"] >= active_target - CONFIG["eps"]:
            target_hit_ts = cts
            events.append(
                {
                    "time": fmt_ts(cts),
                    "event": "target_1_milestone",
                    "target_price": round(active_target, 4),
                    "candle_high": c["high"],
                }
            )
            nxt_res = next_resistance_after(isolated_levels, target_hit_ts)
            if nxt_res is not None:
                target_exit_ts = parse_ts(nxt_res["confirmed_time"])
                events.append(
                    {
                        "time": fmt_ts(target_exit_ts),
                        "event": "target_resistance_confirmed",
                        "resistance_price": nxt_res["price"],
                    }
                )

        if target_exit_ts is not None and cts == target_exit_ts:
            exit_ts, exit_price, exit_reason = (
                cts,
                c["close"],
                "TARGET_MILESTONE_RESISTANCE_EXIT",
            )
            events.append(
                {
                    "time": fmt_ts(cts),
                    "event": "target_resistance_exit",
                    "close": c["close"],
                }
            )
            break

    if exit_ts is None:
        upto = candles_upto(traded_candles, eod_ts)
        if not upto:
            raise RuntimeError("No candle available for EOD close")
        exit_ts = parse_ts(upto[-1]["timestamp"])
        exit_price = float(upto[-1]["close"])
        exit_reason = "END_OF_DAY"
        events.append(
            {"time": fmt_ts(exit_ts), "event": "eod_close", "close": exit_price}
        )

    return {
        "target_hit_time": fmt_ts(target_hit_ts),
        "target_exit_time": fmt_ts(target_exit_ts),
        "active_stop_final": round(active_stop, 4),
        "active_target_final": round(active_target, 4),
        "events": events,
        "exit_time": fmt_ts(exit_ts),
        "exit_price": exit_price,
        "exit_reason": exit_reason,
    }


# ---------------------------------------------------------------------------
# EARLY_EXTREME_REVERSAL ladder engine
# ---------------------------------------------------------------------------
def simulate_eer(
    traded_candles: list[dict],
    entry_ts: datetime,
    entry_price: float,
    initial_stop: float,
    initial_target: float,
    eod_ts: datetime,
) -> dict:
    active_stop = initial_stop
    active_target = initial_target
    prev_target: Optional[float] = None
    pending_stop: Optional[float] = None
    ladder_stage = 0
    ladder_history: list[dict] = []
    events: list[dict] = []

    exit_ts: Optional[datetime] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None

    for c in candles_after(traded_candles, entry_ts):
        cts = parse_ts(c["timestamp"])
        if cts > eod_ts:
            break

        if c["low"] <= active_stop + CONFIG["eps"]:
            exit_ts = cts
            exit_price = active_stop
            exit_reason = "STOP_HIT" if ladder_stage == 0 else "TARGET_TRAIL_STOP_HIT"
            events.append(
                {
                    "time": fmt_ts(cts),
                    "event": "stop_hit",
                    "stop_price": round(active_stop, 4),
                    "candle_low": c["low"],
                    "ladder_stage": ladder_stage,
                }
            )
            break

        while c["high"] >= active_target - CONFIG["eps"]:
            hit_target = active_target
            ladder_stage += 1
            if ladder_stage == 1:
                new_stop = entry_price
            else:
                assert prev_target is not None
                new_stop = prev_target
            next_target = active_target * CONFIG["eer_next_target_multiplier"]

            ladder_history.append(
                {
                    "stage": ladder_stage,
                    "target_hit": round(hit_target, 4),
                    "hit_time": fmt_ts(cts),
                    "new_protective_stop": round(new_stop, 4),
                    "next_target": round(next_target, 4),
                }
            )
            events.append(
                {
                    "time": fmt_ts(cts),
                    "event": f"target_{ladder_stage}_hit",
                    "target_price": round(hit_target, 4),
                    "new_stop": round(new_stop, 4),
                    "next_target": round(next_target, 4),
                }
            )

            pending_stop = new_stop
            prev_target = hit_target
            active_target = next_target

        if pending_stop is not None:
            active_stop = pending_stop
            pending_stop = None

    if exit_ts is None:
        upto = candles_upto(traded_candles, eod_ts)
        if not upto:
            raise RuntimeError("No candle available for EOD close")
        exit_ts = parse_ts(upto[-1]["timestamp"])
        exit_price = float(upto[-1]["close"])
        exit_reason = "END_OF_DAY"
        events.append(
            {"time": fmt_ts(exit_ts), "event": "eod_close", "close": exit_price}
        )

    return {
        "target_ladder_stage": ladder_stage,
        "target_ladder_history": ladder_history,
        "active_stop_final": round(active_stop, 4),
        "active_target_final": round(active_target, 4),
        "events": events,
        "exit_time": fmt_ts(exit_ts),
        "exit_price": exit_price,
        "exit_reason": exit_reason,
    }


# ---------------------------------------------------------------------------
# Artifacts bundle (per-trade directory)
# ---------------------------------------------------------------------------
def bundle_dir_name(result: dict) -> str:
    trading_symbol = result["isolation"]["trading_symbol"]
    isolation_date = result["date"]
    expiry = result["isolation"]["expiry"]
    return f"{sanitize_dirname(trading_symbol)}_{isolation_date}_{expiry}"


def save_artifacts_bundle(
    isolated_path: Path,
    opposite_path: Path,
    isolation_path: Path,
    result: dict,
    results_root: Path,
) -> dict:
    bundle_name = bundle_dir_name(result)
    bundle_dir = results_root / bundle_name
    bundle_dir.mkdir(parents=True, exist_ok=True)

    isolated_dst = bundle_dir / "isolated_instrument.json"
    opposite_dst = bundle_dir / "opposite_instrument.json"
    isolation_dst = bundle_dir / "isolation.json"
    results_dst = bundle_dir / "modified_simulation_results.json"

    shutil.copy2(isolated_path, isolated_dst)
    shutil.copy2(opposite_path, opposite_dst)
    shutil.copy2(isolation_path, isolation_dst)
    results_dst.write_text(json.dumps(result, indent=2, default=str))

    return {
        "bundle_name": bundle_name,
        "bundle_dir": str(bundle_dir),
        "isolated_instrument": str(isolated_dst),
        "opposite_instrument": str(opposite_dst),
        "isolation": str(isolation_dst),
        "simulation_results": str(results_dst),
    }


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def validate_instruments(isolation: dict, isolated: dict, opposite: dict) -> None:
    iso_meta = isolated["instrument_metadata"]
    opp_meta = opposite["instrument_metadata"]

    if iso_meta["strike_price"] != isolation["strike"]:
        raise ValueError(
            f"Isolated strike mismatch: {iso_meta['strike_price']} != {isolation['strike']}"
        )
    if opp_meta["strike_price"] != isolation["strike"]:
        raise ValueError(
            f"Opposite strike mismatch: {opp_meta['strike_price']} != {isolation['strike']}"
        )
    if iso_meta["instrument_type"] != isolation["bias"]:
        raise ValueError(
            f"Isolated option type {iso_meta['instrument_type']} != bias {isolation['bias']}"
        )
    if opp_meta["instrument_type"] == iso_meta["instrument_type"]:
        raise ValueError("Opposite leg has the same option type as the isolated leg")
    if iso_meta["instrument_key"] != isolation["instrument_key"]:
        raise ValueError("Isolated instrument_key does not match isolation object")


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def run_simulation(
    isolated_path: Path,
    opposite_path: Path,
    isolation_path: Path,
    output_path: Path,
    results_root: Path = DEFAULT_RESULTS_ROOT,
    save_artifacts: bool = True,
) -> dict:
    isolated = json.loads(isolated_path.read_text())
    opposite = json.loads(opposite_path.read_text())
    isolation = json.loads(isolation_path.read_text())

    validate_instruments(isolation, isolated, opposite)

    isolated_candles: list[dict] = isolated["candles"]
    isolated_levels: list[dict] = isolated.get("levels", [])

    # 1) Pinned mode
    pinned_mode, snap_price, mode_reason, isolation_ts = decide_mode(
        isolation, isolated_candles
    )

    # 2) First qualifying post-isolation S/R
    first_sr = first_qualifying_sr(isolated_levels, isolation_ts)
    if first_sr is None:
        result = {
            "error": "No qualifying post-isolation 15/3 S/R on isolated instrument",
            "isolation": isolation,
            "pinned_mode": pinned_mode,
            "date": isolation["date"],
        }
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(result, indent=2, default=str))
        if save_artifacts:
            try:
                bundle = save_artifacts_bundle(
                    isolated_path, opposite_path, isolation_path, result, results_root
                )
                result["artifacts"] = bundle
                output_path.write_text(json.dumps(result, indent=2, default=str))
            except Exception as exc:  # pragma: no cover - defensive
                result["artifacts"] = {"error": str(exc)}
        return result

    # 3) Routing (unmodified matrix)
    traded_instrument, routing_mode = resolve_route(
        pinned_mode,
        first_sr,
        isolated_levels,
        isolation_ts,
        isolated,
        opposite,
    )
    traded_meta = traded_instrument["instrument_metadata"]
    traded_candles: list[dict] = traded_instrument["candles"]

    # 4) Entry price = traded instrument close at confirmation minute
    entry_ts = parse_ts(first_sr["confirmed_time"])
    entry_candle = candle_at(traded_candles, entry_ts)
    if entry_candle is None:
        raise RuntimeError(
            f"No 1-min candle on {traded_meta['trading_symbol']} at {entry_ts}"
        )
    entry_price = float(entry_candle["close"])

    # 4.5) MODIFIED FILTERS (Suggestions 3 and 4)
    should_suppress, suppress_reason = check_modified_filters(
        pinned_mode=pinned_mode,
        first_sr=first_sr,
        isolated_levels=isolated_levels,
        isolation_ts=isolation_ts,
        bias=isolation["bias"],
        routing_mode=routing_mode,
        entry_price=entry_price,
        isolated_candles=isolated_candles,
        entry_ts=entry_ts,
    )

    if should_suppress:
        result = _build_suppressed_result(
            isolation=isolation,
            pinned_mode=pinned_mode,
            routing_mode=routing_mode,
            entry_ts=entry_ts,
            entry_price=entry_price,
            reason=suppress_reason,
            snap_price=snap_price,
            mode_reason=mode_reason,
            first_sr=first_sr,
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(result, indent=2, default=str))
        if save_artifacts:
            try:
                bundle = save_artifacts_bundle(
                    isolated_path, opposite_path, isolation_path, result, results_root
                )
                result["artifacts"] = bundle
                output_path.write_text(json.dumps(result, indent=2, default=str))
            except Exception as exc:  # pragma: no cover - defensive
                result["artifacts"] = {"error": str(exc)}
        return result

    # 5) Initial stop / target
    initial_stop = entry_price * (1.0 - CONFIG["stop_loss_pct"])
    divisor, initial_target = target_divisor_and_price(entry_price)

    # 6) EOD bound
    eod_ts = parse_ts(
        f"{isolation['date']}T{CONFIG['eod_time'].strftime('%H:%M:%S')}+05:30"
    )

    # 7) Lifecycle
    if pinned_mode == "STANDARD":
        lifecycle = simulate_standard(
            traded_candles,
            isolated_levels,
            entry_ts,
            entry_price,
            initial_stop,
            initial_target,
            eod_ts,
        )
    else:
        lifecycle = simulate_eer(
            traded_candles,
            entry_ts,
            entry_price,
            initial_stop,
            initial_target,
            eod_ts,
        )

    exit_price = float(lifecycle["exit_price"])
    pnl_points = exit_price - entry_price
    gross_pnl = pnl_points * CONFIG["quantity"]
    return_pct = ((exit_price - entry_price) / entry_price) * 100.0

    # 8) Result
    result = {
        "simulation": {
            "generated_at": datetime.now(IST).isoformat(),
            "guide": "STRATEGY_AND_ORDER_EXECUTION_GUIDE.md",
            "strategy": "unified Path A (modified)",
            "notes": (
                "Deterministic candle replay; gross arithmetic only. "
                "Modified logic: bias-aware backup reversal + EER premium band."
            ),
        },
        "date": isolation["date"],
        "isolation": {
            "trigger_index": isolation["trigger_index"],
            "touch_time": isolation["touch_time"],
            "bias": isolation["bias"],
            "strike": isolation["strike"],
            "expiry": isolation["expiry"],
            "instrument_key": isolation["instrument_key"],
            "exchange_token": isolation.get("exchange_token"),
            "trading_symbol": isolation["trading_symbol"],
            "isolation_ts": fmt_ts(isolation_ts),
            "snapshot_price": round(snap_price, 4),
            "pinned_mode": pinned_mode,
            "mode_reason": mode_reason,
        },
        "trigger": {
            "level": first_sr["level"],
            "price": first_sr["price"],
            "pivot_time": first_sr["pivot_time"],
            "confirmed_time": first_sr["confirmed_time"],
            "left_bars": first_sr.get("left_bars"),
            "right_bars": first_sr.get("right_bars"),
            "qualified_after_isolation": True,
        },
        "routing": {
            "pinned_mode": pinned_mode,
            "routing_reason": routing_mode,
            "traded_instrument_key": traded_meta["instrument_key"],
            "traded_trading_symbol": traded_meta["trading_symbol"],
            "traded_option_type": traded_meta["instrument_type"],
            "isolated_instrument_key": isolated["instrument_metadata"]["instrument_key"],
            "isolated_trading_symbol": isolated["instrument_metadata"]["trading_symbol"],
            "opposite_instrument_key": opposite["instrument_metadata"]["instrument_key"],
            "opposite_trading_symbol": opposite["instrument_metadata"]["trading_symbol"],
        },
        "entry": {
            "time": fmt_ts(entry_ts),
            "price": round(entry_price, 4),
            "price_source": "traded instrument 1-minute candle close at confirmation timestamp",
            "quantity": CONFIG["quantity"],
        },
        "initial": {
            "stop_price": round(initial_stop, 4),
            "target_price": round(initial_target, 4),
            "target_divisor": divisor,
            "stop_loss_pct": CONFIG["stop_loss_pct"],
        },
        "lifecycle": lifecycle,
        "exit": {
            "time": lifecycle["exit_time"],
            "price": round(exit_price, 4),
            "reason": lifecycle["exit_reason"],
        },
        "pnl": {
            "points": round(pnl_points, 4),
            "gross_pnl": round(gross_pnl, 2),
            "return_pct": round(return_pct, 4),
        },
        "config": CONFIG,
    }

    # 9) Save artifacts bundle (single-folder mode only)
    if save_artifacts:
        try:
            bundle = save_artifacts_bundle(
                isolated_path, opposite_path, isolation_path, result, results_root
            )
            result["artifacts"] = bundle
        except Exception as exc:  # pragma: no cover - defensive
            result["artifacts"] = {"error": str(exc)}

    # 10) Write the primary output
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=2, default=str))
    return result


# ---------------------------------------------------------------------------
# Batch helpers
# ---------------------------------------------------------------------------
def is_bundle_folder(folder: Path) -> bool:
    if not folder.is_dir():
        return False
    return all((folder / name).is_file() for name in REQUIRED_INPUTS)


def discover_bundles(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return [
        entry
        for entry in sorted(root.iterdir(), key=lambda p: p.name.lower())
        if is_bundle_folder(entry)
    ]


def _short_result_summary(result: dict) -> dict:
    if "error" in result:
        return {"status": "ERROR", "error": result["error"]}
    sim = result.get("simulation", {})
    if sim.get("status") == "ORDER_SUPPRESSED":
        return {"status": "SUPPRESSED", "notes": sim.get("notes")}
    entry = result.get("entry", {}) or {}
    exit_ = result.get("exit", {}) or {}
    pnl = result.get("pnl", {}) or {}
    return {
        "status": "OK",
        "entry_time": entry.get("time"),
        "entry_price": entry.get("price"),
        "exit_time": exit_.get("time"),
        "exit_price": exit_.get("price"),
        "exit_reason": exit_.get("reason"),
        "pnl_points": pnl.get("points"),
        "pnl_gross": pnl.get("gross_pnl"),
        "return_pct": pnl.get("return_pct"),
    }


def _build_aggregate(records: list[dict]) -> dict:
    ok_records = [r for r in records if r["status"] == "OK"]
    pnl_points = [r["pnl_points"] for r in ok_records if r.get("pnl_points") is not None]
    gross_pnls = [r["pnl_gross"] for r in ok_records if r.get("pnl_gross") is not None]
    returns = [r["return_pct"] for r in ok_records if r.get("return_pct") is not None]

    wins = [r for r in ok_records if (r.get("pnl_points") or 0) > 0]
    losses = [r for r in ok_records if (r.get("pnl_points") or 0) < 0]
    flats = [r for r in ok_records if (r.get("pnl_points") or 0) == 0]

    reason_counts: dict[str, int] = {}
    for r in ok_records:
        reason = r.get("exit_reason") or "UNKNOWN"
        reason_counts[reason] = reason_counts.get(reason, 0) + 1

    n_ok = len(ok_records)
    win_rate = (len(wins) / n_ok * 100.0) if n_ok else 0.0

    return {
        "total_bundles": len(records),
        "ok_count": n_ok,
        "suppressed_count": sum(1 for r in records if r["status"] == "SUPPRESSED"),
        "failed_count": sum(1 for r in records if r["status"] in ("FAILED", "ERROR")),
        "win_count": len(wins),
        "loss_count": len(losses),
        "flat_count": len(flats),
        "win_rate_pct": round(win_rate, 4),
        "total_pnl_points": round(sum(pnl_points), 4),
        "total_gross_pnl": round(sum(gross_pnls), 2),
        "avg_return_pct": round(sum(returns) / len(returns), 4) if returns else 0.0,
        "best_return_pct": round(max(returns), 4) if returns else 0.0,
        "worst_return_pct": round(min(returns), 4) if returns else 0.0,
        "exit_reason_counts": reason_counts,
    }


def write_merged_results(
    merged_path: Path,
    root: Path,
    records: list[dict],
    full_results: dict[str, dict],
) -> dict:
    aggregate = _build_aggregate(records)

    merged_records: list[dict] = []
    for rec in records:
        bundle_name = rec["bundle"]
        full = full_results.get(bundle_name)
        merged_records.append({
            "bundle": bundle_name,
            "status": rec["status"],
            "summary": {k: v for k, v in rec.items() if k not in ("bundle", "status")},
            "simulation_results": full if full is not None else None,
        })

    merged = {
        "simulation": {
            "generated_at": datetime.now(IST).isoformat(),
            "strategy": "unified Path A (modified)",
            "mode": "batch",
            "root": str(root),
            "notes": (
                "Merged batch summary (modified logic: bias-aware backup reversal "
                "+ EER same-leg premium band) — per-bundle results embedded."
            ),
        },
        "aggregate": aggregate,
        "results": merged_records,
        "config": CONFIG,
    }

    merged_path.parent.mkdir(parents=True, exist_ok=True)
    merged_path.write_text(json.dumps(merged, indent=2, default=str))
    return merged


def run_batch(
    root: Path,
    merged_output: Optional[Path] = None,
    write_merged: bool = True,
) -> int:
    bundles = discover_bundles(root)
    if not bundles:
        print(f"[batch] No bundle folders with required inputs found under: {root}")
        return 1

    print(f"[batch] Root: {root}")
    print(f"[batch] Bundles found: {len(bundles)}")
    print("")

    records: list[dict] = []
    full_results: dict[str, dict] = {}
    ok = 0
    failed = 0
    suppressed = 0

    for folder in bundles:
        output_path = folder / OUTPUT_FILENAME  # modified_simulation_results.json
        print(f"[batch] → {folder.name}")
        try:
            result = run_simulation(
                isolated_path=folder / INPUT_ISOLATED_FILENAME,
                opposite_path=folder / INPUT_OPPOSITE_FILENAME,
                isolation_path=folder / INPUT_ISOLATION_FILENAME,
                output_path=output_path,
                save_artifacts=False,
            )
            full_results[folder.name] = result
            short = _short_result_summary(result)
            if short.get("status") == "OK":
                ok += 1
                print(
                    f"        OK  entry={short['entry_price']} "
                    f"exit={short['exit_price']} "
                    f"reason={short['exit_reason']} "
                    f"pnl_points={short['pnl_points']} "
                    f"return={short['return_pct']}%"
                )
            elif short.get("status") == "SUPPRESSED":
                suppressed += 1
                print(f"        SUPPRESSED  {short.get('notes')}")
            else:
                failed += 1
                print(f"        ERROR  {short.get('error')}")
            records.append({"bundle": folder.name, **short})
        except Exception as exc:  # noqa: BLE001
            failed += 1
            msg = f"{type(exc).__name__}: {exc}"
            print(f"        FAILED  {msg}")
            records.append({"bundle": folder.name, "status": "FAILED", "error": msg})
        print("")

    total = len(bundles)
    print("=" * 70)
    print(f"[batch] Done.  total={total}  ok={ok}  suppressed={suppressed}  failed={failed}")
    print("=" * 70)

    if write_merged:
        target = merged_output if merged_output is not None else (root / MERGED_OUTPUT_FILENAME)
        merged = write_merged_results(target, root, records, full_results)
        agg = merged["aggregate"]
        print(f"[batch] Merged file: {target}")
        print(
            f"[batch] Aggregate: win_rate={agg['win_rate_pct']}%  "
            f"total_pnl_points={agg['total_pnl_points']}  "
            f"total_gross_pnl={agg['total_gross_pnl']}  "
            f"avg_return_pct={agg['avg_return_pct']}%"
        )

    print("")
    print(json.dumps(records, indent=2, default=str))
    return 0 if failed == 0 else 2


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _resolve_paths(args: argparse.Namespace) -> dict[str, Path]:
    input_dir = Path(args.input_dir).expanduser()

    iso   = Path(args.isolated).expanduser()  if args.isolated  else input_dir / INPUT_ISOLATED_FILENAME
    opp   = Path(args.opposite).expanduser()  if args.opposite  else input_dir / INPUT_OPPOSITE_FILENAME
    isob  = Path(args.isolation).expanduser() if args.isolation else input_dir / INPUT_ISOLATION_FILENAME
    out   = Path(args.output).expanduser()    if args.output    else input_dir / OUTPUT_FILENAME
    root  = Path(args.results_root).expanduser()

    return {
        "isolated":     iso,
        "opposite":     opp,
        "isolation":    isob,
        "output":       out,
        "results_root": root,
    }


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Unified Path A simulator (modified: bias-aware backup reversal "
            "+ EER premium band). Reads inputs from input_data/ by default; "
            "supports --batch mode."
        )
    )

    # ---- Batch controls ----
    ap.add_argument(
        "--batch", "-b", action="store_true",
        help="Process every subfolder of --batch-dir that contains the 3 input files.",
    )
    ap.add_argument(
        "--batch-dir", default=None,
        help="Parent folder to scan in --batch mode (default: cwd).",
    )
    ap.add_argument(
        "--merged-output", default=None,
        help=f"Merged file path (default: <batch-dir>/{MERGED_OUTPUT_FILENAME}).",
    )
    ap.add_argument(
        "--no-merged", action="store_true",
        help="Skip writing the merged batch results file.",
    )

    # ---- Single-folder controls ----
    ap.add_argument(
        "--input-dir",
        default=str(DEFAULT_INPUT_DIR),
        help=f"Folder containing the three input JSONs (default: {DEFAULT_INPUT_DIR}).",
    )
    ap.add_argument("--isolated",     default=None, help="Override isolated_instrument.json path.")
    ap.add_argument("--opposite",     default=None, help="Override opposite_instrument.json path.")
    ap.add_argument("--isolation",    default=None, help="Override isolation.json path.")
    ap.add_argument("--output",       default=None, help="Override modified_simulation_results.json output path.")
    ap.add_argument(
        "--results-root",
        default=str(DEFAULT_RESULTS_ROOT),
        help=f"Root folder for per-trade artifact bundles (default: {DEFAULT_RESULTS_ROOT}).",
    )
    ap.add_argument(
        "--no-artifacts", action="store_true",
        help="Skip copying inputs + output into a results/<bundle>/ folder (single mode only).",
    )

    args = ap.parse_args(argv[1:])

    # ---------- Batch mode ----------
    if args.batch:
        root = Path(args.batch_dir).expanduser() if args.batch_dir else Path.cwd()
        merged_path = Path(args.merged_output).expanduser() if args.merged_output else None
        return run_batch(
            root,
            merged_output=merged_path,
            write_merged=not args.no_merged,
        )

    # ---------- Single-folder mode ----------
    paths = _resolve_paths(args)

    missing = [p for key in ("isolated", "opposite", "isolation")
               for p in [paths[key]] if not p.exists()]
    if missing:
        for p in missing:
            print(f"[error] missing input file: {p}", file=sys.stderr)
        print(
            f"\nExpected default files inside '{paths['isolated'].parent}':\n"
            f"  - {INPUT_ISOLATED_FILENAME}\n"
            f"  - {INPUT_OPPOSITE_FILENAME}\n"
            f"  - {INPUT_ISOLATION_FILENAME}",
            file=sys.stderr,
        )
        return 2

    result = run_simulation(
        paths["isolated"],
        paths["opposite"],
        paths["isolation"],
        paths["output"],
        paths["results_root"],
        save_artifacts=not args.no_artifacts,
    )
    print(json.dumps(result, indent=2, default=str))

    bundle = result.get("artifacts", {})
    if "bundle_dir" in bundle:
        print(f"\n[ok] primary output : {paths['output']}", file=sys.stderr)
        print(f"[ok] artifacts bundle: {bundle['bundle_dir']}", file=sys.stderr)
        for key in ("isolated_instrument", "opposite_instrument", "isolation", "simulation_results"):
            if key in bundle:
                print(f"     - {key}: {bundle[key]}", file=sys.stderr)
    else:
        print(f"\n[ok] wrote {paths['output']}", file=sys.stderr)
        if "error" in bundle:
            print(f"[warn] bundle skipped: {bundle['error']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))