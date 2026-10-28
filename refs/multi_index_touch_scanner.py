# core/config.py
# ============================================================
# Multi-Index 30-Day Historical Level Engine & Touch Tracker
#   + NIFTY post-trigger S3/R3 touch check (10-minute buffer)
# ============================================================
"""
Multi-Index 30-Day Historical Level Engine & Touch Tracker
Fetches historical 1-minute candle data for 4 main indices:
  1. NIFTY (NSE_INDEX|Nifty 50)
  2. BANKNIFTY (NSE_INDEX|Nifty Bank)
  3. MIDCPNIFTY (NSE_INDEX|NIFTY MID SELECT)
  4. SENSEX (BSE_INDEX|SENSEX)

Workflow:
  1. Fetches last N calendar days of 1-minute candles from Upstox HistoryV3Api for each index.
  2. Partitions intraday candles day-by-day.
  3. Calculates Opening Range (09:15-09:16) formulas for each trading day.
  4. Scans trading days where ANY of the 4 indices touched R3 or S3.
  5. Determines the earliest triggering index of the day.
  6. If the earliest triggering index is NOT NIFTY, looks up NIFTY's spot price and candle at that exact timestamp.
  7. Computes the nearest ATM CE/PE strikes for NIFTY (strike step = 50).
  8. NEW: After the trigger, waits a 10-minute buffer, then checks if NIFTY itself
     touches its own R3 or S3 later that day. Records time, price and delay.
  9. Saves the complete summary into a structured JSON file.
 10. Prints a clean formatted summary table in console.
"""

import json
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

import upstox_client
from upstox_client.rest import ApiException

# ---------------------------------------------------------------------------
# Configuration & Constants
# ---------------------------------------------------------------------------
INDEX_MAP = {
    "NIFTY": "NSE_INDEX|Nifty 50",
    "BANKNIFTY": "NSE_INDEX|Nifty Bank",
    "MIDCPNIFTY": "NSE_INDEX|NIFTY MID SELECT",
    "SENSEX": "BSE_INDEX|SENSEX"
}

NIFTY_STRIKE_STEP = 50
MAX_DAYS_PER_CALL = 7
SLEEP_BETWEEN_CALLS = 1.0
OUT_ROOT = Path("data/multi_index_30d")
UNIT = "minutes"
INTERVAL = "1"

# NEW: how many minutes to skip after the trigger before checking NIFTY's own touch
NIFTY_BUFFER_MINUTES = 10


# ---------------------------------------------------------------------------
# Helpers for API chunking
# ---------------------------------------------------------------------------
def is_weekend(d: date) -> bool:
    return d.weekday() >= 5


def daterange_chunks(start: date, end: date, chunk_days: int):
    """Yield (from_date, to_date) tuples <= chunk_days walking backwards."""
    cur_end = end
    while cur_end >= start:
        cur_from = max(start, cur_end - timedelta(days=chunk_days - 1))
        yield cur_from, cur_end
        cur_end = cur_from - timedelta(days=1)


def all_weekend(from_d: date, to_d: date) -> bool:
    span = (to_d - from_d).days + 1
    return all(is_weekend(from_d + timedelta(days=i)) for i in range(span))


def fetch_chunk(api, instrument_key: str, from_d: date, to_d: date):
    """Fetch one chunk using HistoryV3Api."""
    try:
        response = api.get_historical_candle_data1(
            instrument_key, UNIT, INTERVAL,
            to_d.isoformat(), from_d.isoformat(),
        )
    except ApiException as e:
        print(f"  [WARN] ApiException {from_d}..{to_d} ({instrument_key}): {e}")
        return []
    except Exception as e:
        print(f"  [WARN] Unexpected error {from_d}..{to_d} ({instrument_key}): {e}")
        return []

    data = getattr(response, "data", None)
    if data is None:
        return []
    return getattr(data, "candles", None) or []


def fetch_instrument_candles(instrument_key: str, name: str, end_d: date, lookback_days: int, api):
    """Fetch all lookback_days candles for one instrument and sort chronologically."""
    start_d = end_d - timedelta(days=lookback_days - 1)
    all_candles, seen_ts = [], set()

    chunks = list(daterange_chunks(start_d, end_d, MAX_DAYS_PER_CALL))
    total_batches = len(chunks)

    print(f"\n[{name}] Fetching {lookback_days} days ({start_d} .. {end_d}) in {total_batches} batches...")

    for idx, (chunk_from, chunk_to) in enumerate(chunks, start=1):
        if all_weekend(chunk_from, chunk_to):
            continue

        candles = fetch_chunk(api, instrument_key, chunk_from, chunk_to)
        print(f"  Batch {idx}/{total_batches} ({chunk_from}..{chunk_to}): {len(candles)} candles")

        for c in candles:
            ts = c[0]
            if ts not in seen_ts:
                seen_ts.add(ts)
                all_candles.append(c)

        time.sleep(SLEEP_BETWEEN_CALLS)

    # Sort ascending chronologically (oldest to newest)
    all_candles.sort(key=lambda x: x[0])
    print(f"[{name}] Finished. Total valid 1-min candles: {len(all_candles)}")
    return all_candles


# ---------------------------------------------------------------------------
# Formula & Level Computations
# ---------------------------------------------------------------------------
def calculate_or_levels(first_candle):
    """
    Computes Opening Range levels from the first candle (09:15-09:16).
    Candle format: [timestamp, open, high, low, close, volume, oi]
    """
    H = float(first_candle[2])  # High
    L = float(first_candle[3])  # Low
    A = (H + L) / 2.0
    D_H = abs(H - A)
    D_L = abs(A - L)

    R1 = A + D_H / 2.0
    S1 = A - D_L / 2.0
    R2 = H + D_H
    S2 = L - D_L
    R3 = R2 + D_H
    S3 = S2 - D_L
    R3_Threshold = (R2 + R3) / 2.0
    S3_Threshold = (S2 + S3) / 2.0

    return {
        "H": round(H, 2),
        "L": round(L, 2),
        "A": round(A, 2),
        "D_H": round(D_H, 2),
        "D_L": round(D_L, 2),
        "R1": round(R1, 2),
        "S1": round(S1, 2),
        "R2": round(R2, 2),
        "S2": round(S2, 2),
        "R3": round(R3, 2),
        "S3": round(S3, 2),
        "R3_Threshold": round(R3_Threshold, 2),
        "S3_Threshold": round(S3_Threshold, 2),
    }


def find_first_index_touch(candles_day, or_levels):
    """
    Finds the earliest candle in the day (after 09:15) where R3 or S3 is touched.
    Returns touch record dict or None.
    """
    r3 = or_levels["R3"]
    s3 = or_levels["S3"]

    # Skip first candle (09:15)
    for c in candles_day[1:]:
        ts = c[0]
        h = float(c[2])
        l = float(c[3])

        r3_hit = h >= r3
        s3_hit = l <= s3

        if r3_hit and s3_hit:
            # Both breached within the same candle - resolve to R3 if open was higher, else S3
            level = "R3" if float(c[4]) >= float(c[1]) else "S3"
            touch_price = r3 if level == "R3" else s3
            return {"level": level, "timestamp": ts, "touch_price": touch_price, "candle": c}
        elif r3_hit:
            return {"level": "R3", "timestamp": ts, "touch_price": r3, "candle": c}
        elif s3_hit:
            return {"level": "S3", "timestamp": ts, "touch_price": s3, "candle": c}

    return None


def find_nifty_post_trigger_touch(nifty_day_candles, nifty_or_levels, trigger_ts, buffer_minutes=10):
    """
    NEW: After `trigger_ts`, skip `buffer_minutes` of NIFTY candles, then scan forward
    for the first NIFTY candle that touches NIFTY's own R3 or S3.
    Returns dict with level, timestamp, touch_price, delay_minutes, or None.
    """
    if not nifty_day_candles:
        return None

    r3 = nifty_or_levels["R3"]
    s3 = nifty_or_levels["S3"]

    # Parse trigger timestamp (ISO with T)
    try:
        trigger_dt = datetime.fromisoformat(trigger_ts)
    except ValueError:
        return None

    buffer_cutoff = trigger_dt + timedelta(minutes=buffer_minutes)

    for c in nifty_day_candles:
        # Skip the opening candle and anything at/before the buffer cutoff
        try:
            c_dt = datetime.fromisoformat(c[0])
        except ValueError:
            continue
        if c_dt <= buffer_cutoff:
            continue

        h = float(c[2])
        l = float(c[3])

        r3_hit = h >= r3
        s3_hit = l <= s3

        if r3_hit and s3_hit:
            level = "R3" if float(c[4]) >= float(c[1]) else "S3"
            touch_price = r3 if level == "R3" else s3
            delay = int((c_dt - trigger_dt).total_seconds() // 60)
            return {
                "level": level,
                "timestamp": c[0],
                "touch_price": touch_price,
                "delay_minutes": delay,
                "candle": c,
            }
        elif r3_hit:
            delay = int((c_dt - trigger_dt).total_seconds() // 60)
            return {
                "level": "R3",
                "timestamp": c[0],
                "touch_price": r3,
                "delay_minutes": delay,
                "candle": c,
            }
        elif s3_hit:
            delay = int((c_dt - trigger_dt).total_seconds() // 60)
            return {
                "level": "S3",
                "timestamp": c[0],
                "touch_price": s3,
                "delay_minutes": delay,
                "candle": c,
            }

    return None


def partition_by_day(candles):
    """Group candles by YYYY-MM-DD date string."""
    by_day = defaultdict(list)
    for c in candles:
        day_str = c[0].split("T")[0]
        by_day[day_str].append(c)
    return by_day


def get_nearest_nifty_strikes(spot_price: float, step: int = 50):
    """Calculates ATM nearest round strike and recommended CE/PE strike."""
    atm_strike = int(round(spot_price / step) * step)
    return {
        "atm_strike": atm_strike,
        "ce_strike": atm_strike,
        "pe_strike": atm_strike,
        "ce_symbol_ref": f"NIFTY {atm_strike} CE",
        "pe_symbol_ref": f"NIFTY {atm_strike} PE"
    }


# ---------------------------------------------------------------------------
# Main Execution Pipeline
# ---------------------------------------------------------------------------
def run_multi_index_analysis(lookback_days: int = 30, end_date: str | None = None):
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    api = upstox_client.HistoryV3Api()

    end_d = date.fromisoformat(end_date) if end_date else date.today() - timedelta(days=1)

    # 1. Fetch raw candles for all 4 indices
    raw_index_candles = {}
    day_partitioned = {}
    for name, key in INDEX_MAP.items():
        candles = fetch_instrument_candles(key, name, end_d, lookback_days, api)
        raw_index_candles[name] = candles
        day_partitioned[name] = partition_by_day(candles)

    # Identify all common trading dates across indices
    all_trading_days = sorted(
        set.intersection(*(set(day_partitioned[idx].keys()) for idx in INDEX_MAP))
    )

    print(f"\nProcessing {len(all_trading_days)} common trading days for OR & Touch Analysis...")

    daily_analysis_results = []
    touched_days_summary = []

    # Priority ranking when simultaneous touches happen
    PRIORITY_ORDER = ["NIFTY", "BANKNIFTY", "MIDCPNIFTY", "SENSEX"]

    for d_str in all_trading_days:
        day_record = {
            "date": d_str,
            "indices": {},
            "any_touch": False,
            "earliest_touch": None,
            "nifty_cross_strike_context": None
        }

        touches_today = []

        # Calculate OR levels and detect touches for each index on date d_str
        for name in PRIORITY_ORDER:
            day_candles = day_partitioned[name].get(d_str, [])
            if not day_candles:
                continue

            first_candle = day_candles[0]
            or_levels = calculate_or_levels(first_candle)
            touch_info = find_first_index_touch(day_candles, or_levels)

            day_record["indices"][name] = {
                "first_candle_time": first_candle[0],
                "or_levels": or_levels,
                "first_touch": touch_info
            }

            if touch_info:
                touches_today.append({
                    "index_name": name,
                    "level": touch_info["level"],
                    "timestamp": touch_info["timestamp"],
                    "touch_price": touch_info["touch_price"],
                    "candle_close": float(touch_info["candle"][4])
                })

        if touches_today:
            day_record["any_touch"] = True

            # Sort by earliest timestamp, then break ties by priority ranking
            touches_today.sort(
                key=lambda x: (x["timestamp"], PRIORITY_ORDER.index(x["index_name"]))
            )
            earliest_touch = touches_today[0]
            day_record["earliest_touch"] = earliest_touch

            trigger_index = earliest_touch["index_name"]
            trigger_time = earliest_touch["timestamp"]

            # If the index that touched first is NOT NIFTY, look up NIFTY spot at that time
            nifty_candles_today = day_partitioned["NIFTY"].get(d_str, [])
            nifty_match_candle = None

            # Find matching or most recent prior candle in NIFTY
            for c in nifty_candles_today:
                if c[0] == trigger_time:
                    nifty_match_candle = c
                    break
                elif c[0] < trigger_time:
                    nifty_match_candle = c
                else:
                    break

            if nifty_match_candle:
                nifty_spot_close = float(nifty_match_candle[4])
                strikes_info = get_nearest_nifty_strikes(nifty_spot_close, step=NIFTY_STRIKE_STEP)

                bias_direction = "CE" if earliest_touch["level"] == "R3" else "PE"
                counter_direction = "PE" if bias_direction == "CE" else "CE"

                # NEW: Compute NIFTY's own OR levels for the day (from its first candle)
                nifty_first_candle = nifty_candles_today[0]
                nifty_or_levels = calculate_or_levels(nifty_first_candle)

                # NEW: Check if NIFTY itself touches R3 or S3 after the trigger + buffer
                nifty_post_touch = find_nifty_post_trigger_touch(
                    nifty_candles_today,
                    nifty_or_levels,
                    trigger_time,
                    buffer_minutes=NIFTY_BUFFER_MINUTES,
                )

                cross_context = {
                    "trigger_index": trigger_index,
                    "trigger_time": trigger_time,
                    "trigger_level": earliest_touch["level"],
                    "bias_side": bias_direction,
                    "counter_side": counter_direction,
                    "nifty_candle_time": nifty_match_candle[0],
                    "nifty_spot_price": nifty_spot_close,
                    "nifty_or_levels": nifty_or_levels,
                    "strikes": strikes_info,
                    "nifty_post_trigger_touch": nifty_post_touch,
                    "notes": (
                        f"{trigger_index} touched {earliest_touch['level']} at {trigger_time}. "
                        f"NIFTY spot was {nifty_spot_close} -> Selected Strike: {strikes_info['atm_strike']} "
                        f"({bias_direction} Bias / {counter_direction} Counter). "
                        + (
                            f"NIFTY touched {nifty_post_touch['level']} at "
                            f"{nifty_post_touch['timestamp']} "
                            f"({nifty_post_touch['delay_minutes']} min after trigger) "
                            f"@ {nifty_post_touch['touch_price']}."
                            if nifty_post_touch else
                            "NIFTY did NOT touch its own R3/S3 after trigger."
                        )
                    )
                }
                day_record["nifty_cross_strike_context"] = cross_context
            else:
                day_record["nifty_cross_strike_context"] = {
                    "trigger_index": trigger_index,
                    "trigger_time": trigger_time,
                    "error": "No matching NIFTY candle found at trigger timestamp."
                }

            ctx = day_record["nifty_cross_strike_context"] or {}
            nifty_post = ctx.get("nifty_post_trigger_touch") if isinstance(ctx, dict) else None

            touched_days_summary.append({
                "date": d_str,
                "earliest_index": trigger_index,
                "level": earliest_touch["level"],
                "trigger_time": trigger_time.split("T")[-1][:5] if "T" in trigger_time else trigger_time,
                "trigger_price": earliest_touch["touch_price"],
                "nifty_spot": ctx.get("nifty_spot_price", "N/A") if ctx else "N/A",
                "isolated_strike": ctx.get("strikes", {}).get("atm_strike", "N/A") if ctx else "N/A",
                "bias_side": ctx.get("bias_side", "N/A") if ctx else "N/A",
                # NEW columns
                "nifty_touched_after_trigger": bool(nifty_post),
                "nifty_touch_level": nifty_post["level"] if nifty_post else "-",
                "nifty_touch_time": (
                    nifty_post["timestamp"].split("T")[-1][:5]
                    if nifty_post and "T" in nifty_post["timestamp"]
                    else "-"
                ),
                "nifty_touch_price": nifty_post["touch_price"] if nifty_post else "-",
                "nifty_touch_delay_min": nifty_post["delay_minutes"] if nifty_post else "-",
            })

        daily_analysis_results.append(day_record)

    # 2. Save comprehensive JSON result
    output_payload = {
        "generated_at": datetime.now().isoformat(),
        "lookback_days": lookback_days,
        "end_date": end_d.isoformat(),
        "nifty_buffer_minutes": NIFTY_BUFFER_MINUTES,
        "total_trading_days": len(all_trading_days),
        "days_with_touches": len(touched_days_summary),
        "touched_days_summary": touched_days_summary,
        "daily_details": daily_analysis_results
    }

    out_file = OUT_ROOT / f"multi_index_touches_30d_{lookback_days}d.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(output_payload, f, indent=2, ensure_ascii=False)

    print(f"[SAVED] Analysis complete. JSON output written to: {out_file}")

    # 3. Print clean formatted table (now with NIFTY post-trigger columns)
    header = (
        f"{'DATE':<12} | {'TRIG IDX':<12} | {'LVL':<3} | {'TIME':<5} | "
        f"{'TRIG PRICE':<11} | {'NIFTY SPOT':<11} | {'STRIKE':<7} | {'BIAS':<4} | "
        f"{'NIFTY TOUCH':<11} | {'NIFTY LVL':<9} | {'NIFTY TIME':<10} | "
        f"{'NIFTY PRICE':<11} | {'DELAY':<5}"
    )
    sep = "=" * len(header)
    dash = "-" * len(header)

    print("\n" + sep)
    print(header)
    print(dash)

    for row in touched_days_summary:
        trig_p = f"{row['trigger_price']:.2f}" if isinstance(row['trigger_price'], (int, float)) else str(row['trigger_price'])
        nifty_p = f"{row['nifty_spot']:.2f}" if isinstance(row['nifty_spot'], (int, float)) else str(row['nifty_spot'])

        touch_flag = "YES" if row["nifty_touched_after_trigger"] else "NO"
        n_lvl = row["nifty_touch_level"]
        n_time = row["nifty_touch_time"]
        n_price = row["nifty_touch_price"]
        if isinstance(n_price, (int, float)):
            n_price = f"{n_price:.2f}"
        else:
            n_price = str(n_price)
        delay = str(row["nifty_touch_delay_min"])

        print(
            f"{row['date']:<12} | {row['earliest_index']:<12} | {row['level']:<3} | {row['trigger_time']:<5} | "
            f"{trig_p:<11} | {nifty_p:<11} | {str(row['isolated_strike']):<7} | {row['bias_side']:<4} | "
            f"{touch_flag:<11} | {n_lvl:<9} | {n_time:<10} | {n_price:<11} | {delay:<5}"
        )

    print(sep)
    print(
        f"Total Trading Days Scanned: {len(all_trading_days)} | "
        f"Days with R3/S3 Touches: {len(touched_days_summary)} | "
        f"NIFTY post-trigger touch buffer: {NIFTY_BUFFER_MINUTES} min"
    )

    return output_payload


# ---------------------------------------------------------------------------
# Direct Run
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    # Example execution: 30 days lookback
    run_multi_index_analysis(lookback_days=30)