#!/usr/bin/env python3
"""
simulate_path_a.py

Unified Path A strategy simulator (STANDARD + EARLY_EXTREME_REVERSAL).

Inputs:
  isolated_instrument.json   – isolated option leg (1-min candles + 15/3 S/R levels)
  opposite_instrument.json   – opposite option leg (same strike, same underlying)
  isolation.json             – isolation metadata object
  simulation_results.json    – (primary output)

Per-run artifacts bundle (auto-created):
  results/{trading_symbol}_{isolation_date}_{expiry}/
      isolated_instrument.json   (copy of input)
      opposite_instrument.json   (copy of input)
      isolation.json             (copy of input)
      simulation_results.json    (this run's output)

Usage:
  python simulate_path_a.py
  python simulate_path_a.py isolated_instrument.json opposite_instrument.json isolation.json simulation_results.json
  python simulate_path_a.py isolated_instrument.json opposite_instrument.json isolation.json simulation_results.json results
"""

from __future__ import annotations

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
}

IST = timezone(timedelta(hours=5, minutes=30))

# Default location for the per-trade artifacts bundles
DEFAULT_RESULTS_ROOT = Path("results")


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
    """Turn an arbitrary trading symbol into a filesystem-safe directory name."""
    out = (name or "").strip()
    for ch in (" ", "/", "\\", ":", "*", "?", '"', "<", ">", "|"):
        out = out.replace(ch, "_")
    while "__" in out:
        out = out.replace("__", "_")
    return out.strip("_") or "unknown"


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
    """
    Create results/{trading_symbol}_{date}_{expiry}/, copy the two instrument
    JSONs and isolation.json into it, and write simulation_results.json there.
    Returns a dict of saved paths.
    """
    bundle_name = bundle_dir_name(result)
    bundle_dir = results_root / bundle_name
    bundle_dir.mkdir(parents=True, exist_ok=True)

    isolated_dst = bundle_dir / "isolated_instrument.json"
    opposite_dst = bundle_dir / "opposite_instrument.json"
    isolation_dst = bundle_dir / "isolation.json"
    results_dst = bundle_dir / "simulation_results.json"

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
        output_path.write_text(json.dumps(result, indent=2, default=str))
        try:
            bundle = save_artifacts_bundle(
                isolated_path, opposite_path, isolation_path, result, results_root
            )
            result["artifacts"] = bundle
            output_path.write_text(json.dumps(result, indent=2, default=str))
        except Exception as exc:  # pragma: no cover - defensive
            result["artifacts"] = {"error": str(exc)}
        return result

    # 3) Routing
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
            "strategy": "unified Path A",
            "notes": "Deterministic candle replay; gross arithmetic only.",
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

    # 9) Save artifacts bundle (results/{symbol}_{date}_{expiry}/)
    try:
        bundle = save_artifacts_bundle(
            isolated_path, opposite_path, isolation_path, result, results_root
        )
        result["artifacts"] = bundle
    except Exception as exc:  # pragma: no cover - defensive
        result["artifacts"] = {"error": str(exc)}

    # 10) Write the primary output (same content, now including artifacts)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=2, default=str))
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: list[str]) -> int:
    iso = Path(argv[1]) if len(argv) > 1 else Path("isolated_instrument.json")
    opp = Path(argv[2]) if len(argv) > 2 else Path("opposite_instrument.json")
    isob = Path(argv[3]) if len(argv) > 3 else Path("isolation.json")
    out = Path(argv[4]) if len(argv) > 4 else Path("simulation_results.json")
    results_root = Path(argv[5]) if len(argv) > 5 else DEFAULT_RESULTS_ROOT

    for p in (iso, opp, isob):
        if not p.exists():
            print(f"[error] missing input file: {p}", file=sys.stderr)
            return 2

    result = run_simulation(iso, opp, isob, out, results_root)
    print(json.dumps(result, indent=2, default=str))

    bundle = result.get("artifacts", {})
    if "bundle_dir" in bundle:
        print(f"\n[ok] primary output : {out}", file=sys.stderr)
        print(f"[ok] artifacts bundle: {bundle['bundle_dir']}", file=sys.stderr)
        for key in (
            "isolated_instrument",
            "opposite_instrument",
            "isolation",
            "simulation_results",
        ):
            if key in bundle:
                print(f"     - {key}: {bundle[key]}", file=sys.stderr)
    else:
        print(f"\n[ok] wrote {out}", file=sys.stderr)
        if "error" in bundle:
            print(f"[warn] bundle skipped: {bundle['error']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))