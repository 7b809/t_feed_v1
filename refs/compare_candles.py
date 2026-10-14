"""
compare_candles.py

Compares live WebSocket candle feeds (produced by nifty_socket.py) against
official Upstox intraday candles (produced by fetch_canldes.py).

For each (instrument, interval) pair:

    * Loads the feed file       : feeds/<inst>__<mode>.json
    * Loads the official file   : upstox_intraday_candles/<inst>__intraday_<N>m.json
    * Extracts every unique completed candle from the feed file
    * Looks up each feed candle in the official dataset by timestamp
    * Compares OHLC + volume + OI field by field
    * Writes a structured JSON report and prints a readable summary

Only the feed's candles are iterated; the official dataset is used
purely as a lookup table. This means only the minutes actually captured
during the live run are compared — everything else in the official file
is ignored.

Directory layout (must be run from `refs/`):

    refs/
      feeds/
        <INSTRUMENT>__default.json
        <INSTRUMENT>__1min.json
        <INSTRUMENT>__3min.json
        <INSTRUMENT>__5min.json
      upstox_intraday_candles/
        <INSTRUMENT>__intraday_1m.json
        <INSTRUMENT>__intraday_3m.json
        <INSTRUMENT>__intraday_5m.json

Run:

    python compare_candles.py
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any


# ==================================================================
# CONFIGURATION
# ==================================================================

FEEDS_DIR = Path("feeds")
UPSTOX_DIR = Path("upstox_intraday_candles")

REPORT_PATH = Path("candle_comparison_report.json")

# Feed-file mode  ->  suffix used by the official Upstox file
# ("default" is a raw tick feed and is intentionally not compared.)
MODE_TO_OFFICIAL_SUFFIX: dict[str, str] = {
    "1min": "intraday_1m",
    "3min": "intraday_3m",
    "5min": "intraday_5m",
}

# Instruments we expect to find (drives the comparison loop).
INSTRUMENT_LIST: list[str] = [
    "NSE_FO|47142",
    "NSE_INDEX|Nifty 50",
    "BSE_INDEX|SENSEX",
]

# Comparison tolerances. Prices are compared with a small epsilon to
# absorb float serialisation noise; volume and OI must match exactly.
PRICE_TOLERANCE = 0.01
VOLUME_TOLERANCE = 0.0
OI_TOLERANCE = 0.0

# How many detailed mismatches to print to the console per file.
CONSOLE_DETAIL_LIMIT = 10


# ==================================================================
# HELPERS
# ==================================================================

def sanitize(name: str) -> str:
    """Mirror of the sanitiser used by nifty_socket.py / fetch_canldes.py."""
    return (
        name.replace("|", "_")
        .replace(" ", "_")
        .replace("/", "_")
        .replace(":", "_")
    )


def normalize_ts(ts: Any) -> str:
    """
    Normalize an ISO-8601 timestamp to minute resolution, preserving its
    timezone. This makes string comparison safe even when one side has
    microseconds and the other does not.
    """
    if not isinstance(ts, str):
        return str(ts)

    try:
        dt = datetime.fromisoformat(ts)
        dt = dt.replace(second=0, microsecond=0)
        return dt.isoformat()
    except Exception:
        return ts


def load_official_candles(path: Path) -> dict[str, dict]:
    """
    Return { normalized_ts: candle_dict } from an official Upstox intraday
    file. candle_dict has keys: open, high, low, close, volume, oi, raw.
    """
    with path.open("r", encoding="utf-8") as fh:
        payload = json.load(fh)

    candles_list = (
        payload.get("data", {}).get("candles", [])
        if isinstance(payload, dict)
        else []
    )

    out: dict[str, dict] = {}
    for entry in candles_list:
        if not isinstance(entry, list) or len(entry) < 7:
            continue
        key = normalize_ts(entry[0])
        out[key] = {
            "open":   float(entry[1]),
            "high":   float(entry[2]),
            "low":    float(entry[3]),
            "close":  float(entry[4]),
            "volume": float(entry[5]),
            "oi":     float(entry[6]),
            "raw":    entry,
        }
    return out


def iter_feed_records(path: Path):
    """
    Stream-parse a feed file that contains concatenated, indented JSON
    objects. Yields each top-level object as a Python dict.
    """
    content = path.read_text(encoding="utf-8")
    decoder = json.JSONDecoder()
    idx = 0
    length = len(content)

    while idx < length:
        # Skip whitespace between records.
        while idx < length and content[idx] in " \t\r\n":
            idx += 1
        if idx >= length:
            break

        try:
            obj, end = decoder.raw_decode(content, idx)
        except json.JSONDecodeError:
            # Corrupt tail — stop parsing this file rather than crash.
            break

        yield obj
        idx = end


def extract_feed_candles(path: Path) -> dict[str, dict]:
    """
    Return { normalized_ts: candle_dict } built from every candle record
    found in a feed file. When the same timestamp appears more than once
    (e.g. after a reconnect), the last one wins.
    """
    candles: dict[str, dict] = {}

    for record in iter_feed_records(path):
        if not isinstance(record, dict):
            continue

        candle = record.get("candle")
        if not isinstance(candle, list) or len(candle) < 7:
            continue

        key = normalize_ts(candle[0])
        candles[key] = {
            "open":        float(candle[1]),
            "high":        float(candle[2]),
            "low":         float(candle[3]),
            "close":       float(candle[4]),
            "volume":      float(candle[5]),
            "oi":          float(candle[6]),
            "received_at": record.get("received_at"),
            "raw":         candle,
        }

    return candles


def diff_candles(feed: dict, official: dict) -> list[dict]:
    """Return a list of field-level differences (empty means a match)."""
    diffs: list[dict] = []

    for field in ("open", "high", "low", "close"):
        fv = float(feed[field])
        ov = float(official[field])
        if abs(fv - ov) > PRICE_TOLERANCE:
            diffs.append({
                "field":    field,
                "feed":     fv,
                "official": ov,
                "delta":    round(fv - ov, 6),
            })

    fv = float(feed["volume"])
    ov = float(official["volume"])
    if abs(fv - ov) > VOLUME_TOLERANCE:
        diffs.append({
            "field":    "volume",
            "feed":     fv,
            "official": ov,
            "delta":    round(fv - ov, 6),
        })

    fv = float(feed["oi"])
    ov = float(official["oi"])
    if abs(fv - ov) > OI_TOLERANCE:
        diffs.append({
            "field":    "oi",
            "feed":     fv,
            "official": ov,
            "delta":    round(fv - ov, 6),
        })

    return diffs


# ==================================================================
# COMPARISON DRIVER
# ==================================================================

def compare_one(instrument_key: str, mode: str) -> dict:
    """
    Compare the feed file for (instrument, mode) against the matching
    official intraday file. Returns a structured result dict.
    """
    inst_tag = sanitize(instrument_key)

    feed_file = FEEDS_DIR / f"{inst_tag}__{mode}.json"
    official_file = UPSTOX_DIR / f"{inst_tag}__{MODE_TO_OFFICIAL_SUFFIX[mode]}.json"

    result: dict[str, Any] = {
        "instrument_key":             instrument_key,
        "mode":                       mode,
        "feed_file":                  str(feed_file),
        "official_file":              str(official_file),
        "status":                     "unknown",
        "feed_candle_count":          0,
        "official_candle_count":      0,
        "matched_count":              0,
        "mismatched_count":           0,
        "missing_in_official_count":  0,
        "mismatches":                 [],
        "missing_in_official":        [],
    }

    if not feed_file.exists():
        result["status"] = "feed_file_missing"
        return result

    if not official_file.exists():
        result["status"] = "official_file_missing"
        return result

    feed_candles = extract_feed_candles(feed_file)
    official_candles = load_official_candles(official_file)

    result["feed_candle_count"] = len(feed_candles)
    result["official_candle_count"] = len(official_candles)

    if not feed_candles:
        result["status"] = "no_feed_candles"
        return result

    matched = 0
    mismatched = 0
    missing = 0

    # Iterate over feed candles only. The official dataset is used
    # purely as a lookup table, keyed by normalized timestamp.
    for ts in sorted(feed_candles.keys()):
        feed_c = feed_candles[ts]
        official_c = official_candles.get(ts)

        if official_c is None:
            missing += 1
            result["missing_in_official"].append({
                "timestamp": ts,
                "feed":      feed_c["raw"],
            })
            continue

        diffs = diff_candles(feed_c, official_c)
        if diffs:
            mismatched += 1
            result["mismatches"].append({
                "timestamp": ts,
                "feed":      feed_c["raw"],
                "official":  official_c["raw"],
                "diffs":     diffs,
            })
        else:
            matched += 1

    result["matched_count"] = matched
    result["mismatched_count"] = mismatched
    result["missing_in_official_count"] = missing
    result["status"] = "ok"

    return result


# ==================================================================
# REPORTING
# ==================================================================

def format_summary_line(r: dict) -> str:
    return (
        f"{r['instrument_key']:<24} | {r['mode']:<5} | "
        f"feed={r['feed_candle_count']:>5} | "
        f"official={r['official_candle_count']:>5} | "
        f"matched={r['matched_count']:>5} | "
        f"mismatched={r['mismatched_count']:>5} | "
        f"missing={r['missing_in_official_count']:>5} | "
        f"{r['status']}"
    )


def print_detail_section(r: dict) -> None:
    if not r["mismatches"] and not r["missing_in_official"]:
        return

    print()
    print(
        f"### {r['instrument_key']} [{r['mode']}] - "
        f"mismatches={len(r['mismatches'])} "
        f"missing_in_official={len(r['missing_in_official'])}"
    )

    for mm in r["mismatches"][:CONSOLE_DETAIL_LIMIT]:
        print(f"  TS {mm['timestamp']}")
        print(f"    feed     : {mm['feed']}")
        print(f"    official : {mm['official']}")
        for d in mm["diffs"]:
            print(
                f"    - {d['field']:<6} "
                f"feed={d['feed']} "
                f"official={d['official']} "
                f"delta={d['delta']}"
            )

    if len(r["mismatches"]) > CONSOLE_DETAIL_LIMIT:
        print(
            f"  ... and {len(r['mismatches']) - CONSOLE_DETAIL_LIMIT} "
            f"more mismatches"
        )

    for mi in r["missing_in_official"][:CONSOLE_DETAIL_LIMIT]:
        print(
            f"  MISSING IN OFFICIAL | ts={mi['timestamp']} | "
            f"feed={mi['feed']}"
        )

    if len(r["missing_in_official"]) > CONSOLE_DETAIL_LIMIT:
        print(
            f"  ... and "
            f"{len(r['missing_in_official']) - CONSOLE_DETAIL_LIMIT} "
            f"more missing candles"
        )


# ==================================================================
# MAIN
# ==================================================================

def main() -> None:
    print("=" * 140)
    print("CANDLE COMPARISON — LIVE FEED vs OFFICIAL UPSTOX INTRADAY")
    print("=" * 140)
    print(f"Feeds directory    : {FEEDS_DIR.resolve()}")
    print(f"Official directory : {UPSTOX_DIR.resolve()}")
    print(f"Intervals          : {sorted(MODE_TO_OFFICIAL_SUFFIX.keys())}")
    print(f"Price tolerance    : {PRICE_TOLERANCE}")
    print(f"Volume tolerance   : {VOLUME_TOLERANCE}")
    print(f"OI tolerance       : {OI_TOLERANCE}")
    print("=" * 140)

    all_results: list[dict] = []

    for instrument_key in INSTRUMENT_LIST:
        for mode in MODE_TO_OFFICIAL_SUFFIX.keys():
            result = compare_one(instrument_key, mode)
            all_results.append(result)
            print(format_summary_line(result))

    # -------------- Aggregate totals -------------------------------
    total_feed = sum(r["feed_candle_count"] for r in all_results)
    total_official = sum(r["official_candle_count"] for r in all_results)
    total_matched = sum(r["matched_count"] for r in all_results)
    total_mismatched = sum(r["mismatched_count"] for r in all_results)
    total_missing = sum(r["missing_in_official_count"] for r in all_results)

    print("-" * 140)
    print(
        f"TOTAL | feed={total_feed} | official={total_official} | "
        f"matched={total_matched} | mismatched={total_mismatched} | "
        f"missing_in_official={total_missing}"
    )
    print("=" * 140)

    # -------------- Detailed mismatches ----------------------------
    any_problem = False
    for r in all_results:
        if r["mismatches"] or r["missing_in_official"]:
            any_problem = True
            print_detail_section(r)

    if not any_problem:
        print()
        print(
            "ALL FEED CANDLES MATCH THE OFFICIAL UPSTOX DATA — "
            "no mismatches and no missing minutes."
        )

    # -------------- Write structured JSON report -------------------
    report_payload = {
        "generated_at": datetime.now().astimezone().isoformat(),
        "config": {
            "feeds_dir":        str(FEEDS_DIR.resolve()),
            "official_dir":     str(UPSTOX_DIR.resolve()),
            "instruments":      INSTRUMENT_LIST,
            "intervals":        sorted(MODE_TO_OFFICIAL_SUFFIX.keys()),
            "price_tolerance":  PRICE_TOLERANCE,
            "volume_tolerance": VOLUME_TOLERANCE,
            "oi_tolerance":     OI_TOLERANCE,
        },
        "totals": {
            "feed_candles":           total_feed,
            "official_candles":       total_official,
            "matched":                total_matched,
            "mismatched":             total_mismatched,
            "missing_in_official":    total_missing,
        },
        "results": all_results,
    }

    REPORT_PATH.write_text(
        json.dumps(report_payload, indent=4, ensure_ascii=False),
        encoding="utf-8",
    )

    print()
    print(f"JSON report written to : {REPORT_PATH.resolve()}")
    print("Done.")


if __name__ == "__main__":
    main()