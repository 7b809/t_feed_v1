# temp/scrpt/fetch_option_candles.py
"""
Fetch NIFTY 50 spot + CE/PE option candles for every touched day in
    D:\\files\\novag7\\market-engine-v1\\temp\\data\\multi_index_30d\\multi_index_touches_30d_30d.json

The script talks ONLY to the local FastAPI app (default
http://127.0.0.1:8010) — no direct Upstox SDK usage:

    ACTIVE instruments  ->  /api/upstox/historical
                            /api/upstox/search
                            /api/upstox/instrument

    EXPIRED instruments ->  /upstox-app/expired/expiries
                            /upstox-app/expired/contracts
                            /upstox-app/expired/candles

Expiry-resolution rule (per touched day D):
    - candidates = [e for e in expired_expiries if e >= D]
    - if candidates:
          nearest_expiry = min(candidates)
          EXPIRED path (fetch via /upstox-app/expired/contracts + /candles)
    - else:
          nearest expiry on D was still in the future (or expiring today)
          -> ACTIVE path (search active contract, pick nearest future expiry)

Output layout (default):
    D:\\files\\novag7\\market-engine-v1\\temp\\data\\multi_index_30d\\option_candles\\
    ├── summary.json                          (consolidated across all days)
    └── by_date\\
        └── YYYY-MM-DD\\
            ├── day_summary.json
            ├── spot.json                     (NIFTY 50 index candles + meta)
            ├── ce_<STRIKE>.json              (CE candles + meta)
            └── pe_<STRIKE>.json              (PE candles + meta)

Environment overrides:
    API_BASE      Local FastAPI base URL          (default: http://127.0.0.1:8010)
    INPUT_JSON    Absolute path to input JSON     (default: the hardcoded path above)
    OUT_ROOT      Absolute path to output dir     (default: <multi_index_30d>\\option_candles)
    NIFTY_KEY     Underlying index key            (default: NSE_INDEX|Nifty 50)
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

import requests


# ============================================================
# Config
# ============================================================
API_BASE        = os.environ.get("API_BASE", "http://127.0.0.1:8010").rstrip("/")
HTTP_TIMEOUT    = 30
NIFTY_INDEX_KEY = os.environ.get("NIFTY_KEY", "NSE_INDEX|Nifty 50")
SLEEP_SECONDS   = 0.3

# ------------------------------------------------------------------
# Hard-coded paths (per requirement)
# ------------------------------------------------------------------
DEFAULT_INPUT_JSON = Path(
    r"D:\files\novag7\market-engine-v1\temp\data\multi_index_30d"
    r"\multi_index_touches_30d_30d.json"
)

INPUT_JSON = (
    Path(os.environ["INPUT_JSON"]).resolve()
    if os.environ.get("INPUT_JSON")
    else DEFAULT_INPUT_JSON
)

# Output lives next to the input file, in the same `multi_index_30d` folder
OUT_ROOT = (
    Path(os.environ["OUT_ROOT"]).resolve()
    if os.environ.get("OUT_ROOT")
    else DEFAULT_INPUT_JSON.parent / "option_candles"
)
BY_DATE_DIR = OUT_ROOT / "by_date"


# ============================================================
# Small HTTP helper
# ============================================================
def _get(path: str, params: dict | None = None) -> Any | None:
    """GET ``API_BASE + path`` with ``params``; return parsed JSON or None."""
    url = f"{API_BASE}{path}"
    try:
        r = requests.get(url, params=params or {}, timeout=HTTP_TIMEOUT)
    except requests.RequestException as e:
        print(f"  [HTTP-ERR] {path}: {type(e).__name__}: {e}")
        return None
    if r.status_code >= 400:
        snippet = (r.text or "")[:200].replace("\n", " ")
        print(f"  [HTTP-{r.status_code}] {path}: {snippet}")
        return None
    try:
        return r.json()
    except ValueError:
        print(f"  [HTTP-PARSE-ERR] {path}: non-JSON response")
        return None


# ============================================================
# Normalizers
# ============================================================
def _normalize_expiry(val: Any) -> str | None:
    """Return ``YYYY-MM-DD`` or None from a str / int / float expiry."""
    if val is None or val == "":
        return None
    if isinstance(val, str):
        s = val.strip()
        if len(s) >= 10 and s[4] == "-" and s[7] == "-":
            return s[:10]
        try:
            n = float(s)
        except (TypeError, ValueError):
            return None
        val = n
    if isinstance(val, (int, float)):
        n = float(val)
        if n > 1e12:
            n /= 1000.0
        if n < 1e9:
            return None
        try:
            return datetime.fromtimestamp(n).strftime("%Y-%m-%d")
        except (OSError, ValueError):
            return None
    return None


def _summarise_candles(candles: list) -> dict:
    """Return a compact summary from a raw Upstox candle list."""
    if not candles:
        return {"count": 0}
    try:
        highs = [float(c[2]) for c in candles if isinstance(c, list) and len(c) > 4]
        lows  = [float(c[3]) for c in candles if isinstance(c, list) and len(c) > 4]
        return {
            "count":       len(candles),
            "first_time":  candles[0][0],
            "first_open":  float(candles[0][1]),
            "first_close": float(candles[0][4]),
            "last_time":   candles[-1][0],
            "last_close":  float(candles[-1][4]),
            "high":        max(highs) if highs else None,
            "low":         min(lows)  if lows  else None,
        }
    except (IndexError, TypeError, ValueError):
        return {"count": len(candles)}


# ============================================================
# Per-API wrappers
# ============================================================
def api_health_check() -> None:
    """Abort early if the local FastAPI app is not reachable."""
    try:
        r = requests.get(f"{API_BASE}/live/status", timeout=5)
        if r.status_code != 200:
            print(f"WARN: /live/status returned HTTP {r.status_code}")
    except requests.RequestException as e:
        print(f"ERROR: FastAPI not reachable at {API_BASE}")
        print(f"       ({type(e).__name__}: {e})")
        print(f"       start the app with run.bat / start.sh first.")
        sys.exit(1)


def api_get_expired_expiries(index_key: str) -> list[str]:
    data = _get("/upstox-app/expired/expiries", {"instrument_key": index_key})
    if not data or data.get("status") != "ok":
        if data:
            print(f"  [expired/expiries] status={data.get('status')} "
                  f"err={data.get('error')}")
        return []
    out: list[str] = []
    for e in data.get("expiries") or []:
        s = _normalize_expiry(e)
        if s:
            out.append(s)
    out.sort()
    return out


def api_get_expired_contracts(index_key: str, expiry_date: str) -> list[dict]:
    data = _get("/upstox-app/expired/contracts", {
        "instrument_key": index_key,
        "expiry_date":    expiry_date,
    })
    if not data or data.get("status") != "ok":
        if data:
            print(f"  [expired/contracts {expiry_date}] "
                  f"status={data.get('status')} err={data.get('error')}")
        return []
    return [c for c in (data.get("contracts") or []) if isinstance(c, dict)]


def api_get_expired_candles(
    expired_key: str,
    from_date: str,
    to_date: str,
    interval: str = "1minute",
) -> list:
    data = _get("/upstox-app/expired/candles", {
        "expired_instrument_key": expired_key,
        "interval":               interval,
        "from_date":              from_date,
        "to_date":                to_date,
    })
    if not data or data.get("status") != "ok":
        if data:
            print(f"  [expired/candles {expired_key}] "
                  f"status={data.get('status')} err={data.get('error')}")
        return []
    return data.get("candles") or []


def api_search_active(
    underlying: str,
    strike: int,
    side: str,
    min_expiry: str,
) -> dict | None:
    """Search active Upstox contracts; return nearest valid one or None."""
    data = _get("/api/upstox/search", {
        "query":             f"{underlying} {strike} {side}",
        "records":           30,
        "page_number":       1,
        "exchanges":         "NSE",
        "segments":          "FO",
        "instrument_types":  side,
    })
    if not data:
        return None
    items = data.get("data") or []
    candidates: list[tuple[str, dict]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            sp = int(float(item.get("strike_price", 0)))
        except (TypeError, ValueError):
            continue
        it = str(item.get("instrument_type", "")).upper()
        if sp != int(strike) or it != side.upper():
            continue
        exp = _normalize_expiry(item.get("expiry"))
        if exp and exp >= min_expiry:
            candidates.append((exp, item))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])
    return candidates[0][1]


def api_get_active_candles(
    instrument_key: str,
    from_date: str,
    to_date: str,
) -> list:
    data = _get("/api/upstox/historical", {
        "instrument_key": instrument_key,
        "from_date":      from_date,
        "to_date":        to_date,
        "interval":       "1",
        "unit":           "minutes",
    })
    if not data:
        return []
    d = data.get("data") or {}
    if isinstance(d, dict):
        return d.get("candles") or []
    return []


def api_get_instrument_meta(instrument_key: str) -> dict:
    data = _get("/api/upstox/instrument", {"instrument_key": instrument_key})
    if data and data.get("status") == "success":
        return data.get("data") or {}
    return {}


# ============================================================
# Contracts cache (per-expiry) to avoid re-hitting the API
# ============================================================
_contracts_cache: dict[str, list[dict]] = {}

def _get_contracts_cached(expiry_date: str) -> list[dict]:
    if expiry_date not in _contracts_cache:
        _contracts_cache[expiry_date] = api_get_expired_contracts(
            NIFTY_INDEX_KEY, expiry_date,
        )
        time.sleep(SLEEP_SECONDS)
    return _contracts_cache[expiry_date]


# ============================================================
# Per-day fetchers
# ============================================================
def fetch_spot_for_day(trade_date: str) -> dict:
    """Fetch NIFTY 50 index candles + metadata for one trade date."""
    candles = api_get_active_candles(NIFTY_INDEX_KEY, trade_date, trade_date)
    time.sleep(SLEEP_SECONDS)
    meta = api_get_instrument_meta(NIFTY_INDEX_KEY)
    time.sleep(SLEEP_SECONDS)
    return {
        "instrument_key": NIFTY_INDEX_KEY,
        "trade_date":     trade_date,
        "from_date":      trade_date,
        "to_date":        trade_date,
        "meta":           meta,
        "summary":        _summarise_candles(candles),
        "candles":        candles,
    }


def fetch_option_for_day(
    trade_date: str,
    strike: int,
    side: str,
    expired_expiries: list[str],
    today_str: str,
) -> dict:
    """Fetch candles + metadata for one CE/PE option.

    Routes to the expired endpoints when the nearest expiry on
    ``trade_date`` is already past; otherwise uses the active
    ``/api/upstox/*`` endpoints.
    """
    result: dict[str, Any] = {
        "side":           side,
        "strike":         int(strike),
        "expiry":         None,
        "is_expired":     None,
        "instrument_key": None,
        "from_date":      trade_date,
        "to_date":        None,
        "meta":           {},
        "summary":        {"count": 0},
        "candles":        [],
        "error":          None,
    }

    # ---- 1. Determine nearest expiry on trade_date ----
    expired_candidates = [e for e in expired_expiries if e >= trade_date]

    # ---- 2. EXPIRED path ----
    if expired_candidates:
        expiry = min(expired_candidates)
        result["expiry"]     = expiry
        result["is_expired"] = True
        to_date              = min(expiry, today_str)
        result["to_date"]    = to_date

        contracts = _get_contracts_cached(expiry)
        inst_key  = None
        inst_meta: dict[str, Any] = {}
        for c in contracts:
            try:
                cs = int(float(c.get("strike_price", 0)))
            except (TypeError, ValueError):
                continue
            ct = str(c.get("instrument_type", "")).upper()
            if cs == int(strike) and ct == side.upper():
                inst_key  = c.get("instrument_key")
                inst_meta = c
                break

        if not inst_key:
            result["error"] = (
                f"no {side} expired contract for strike {strike} "
                f"expiry {expiry}"
            )
            return result

        result["instrument_key"] = inst_key
        result["meta"]           = inst_meta

        candles = api_get_expired_candles(
            inst_key, trade_date, to_date, interval="1minute",
        )
        time.sleep(SLEEP_SECONDS)
        result["summary"] = _summarise_candles(candles)
        result["candles"] = candles
        return result

    # ---- 3. ACTIVE path ----
    result["is_expired"] = False
    entry = api_search_active("NIFTY", int(strike), side, trade_date)
    time.sleep(SLEEP_SECONDS)
    if not entry:
        result["error"] = (
            f"no active {side} contract for strike {strike} "
            f"(expiry >= {trade_date})"
        )
        return result

    inst_key = entry.get("instrument_key")
    expiry   = _normalize_expiry(entry.get("expiry"))
    to_date  = min(expiry, today_str) if expiry else today_str

    result["instrument_key"] = inst_key
    result["expiry"]         = expiry
    result["to_date"]        = to_date
    result["meta"]           = entry

    candles = api_get_active_candles(inst_key, trade_date, to_date)
    time.sleep(SLEEP_SECONDS)
    result["summary"] = _summarise_candles(candles)
    result["candles"] = candles
    return result


# ============================================================
# IO helpers
# ============================================================
def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _fmt(v: Any, prec: int = 2) -> str:
    if isinstance(v, (int, float)):
        return f"{v:.{prec}f}"
    return "—"


# ============================================================
# Table output
# ============================================================
def _print_table(day_summaries: list[dict]) -> None:
    print("\n" + "=" * 176)
    print(
        f"{'DATE':<12} | {'TRIG':<11} | {'LVL':<3} | {'TIME':<5} | "
        f"{'STRIKE':<6} | {'EXPIRY':<11} | {'EXPIRED':<7} | "
        f"{'CE #':<5} | {'CE LAST':<9} | {'CE HIGH':<9} | "
        f"{'PE #':<5} | {'PE LAST':<9} | {'PE HIGH':<9} | "
        f"{'SPOT #':<6} | {'SPOT LAST':<10}"
    )
    print("-" * 176)

    for s in day_summaries:
        ce   = s.get("ce")   or {}
        pe   = s.get("pe")   or {}
        spot = s.get("spot") or {}

        ce_s   = ce.get("summary")   or {}
        pe_s   = pe.get("summary")   or {}
        sp_s   = spot.get("summary") or {}

        exp_flag = (
            "yes" if ce.get("is_expired") is True
            else "no" if ce.get("is_expired") is False
            else "—"
        )
        print(
            f"{s.get('trade_date', '—'):<12} | "
            f"{str(s.get('trigger_index') or '—'):<11} | "
            f"{str(s.get('level') or '—'):<3} | "
            f"{str(s.get('trigger_time') or '—'):<5} | "
            f"{str(s.get('isolated_strike', '—')):<6} | "
            f"{str(ce.get('expiry') or '—'):<11} | "
            f"{exp_flag:<7} | "
            f"{ce_s.get('count', 0):<5} | "
            f"{_fmt(ce_s.get('last_close')):<9} | "
            f"{_fmt(ce_s.get('high')):<9} | "
            f"{pe_s.get('count', 0):<5} | "
            f"{_fmt(pe_s.get('last_close')):<9} | "
            f"{_fmt(pe_s.get('high')):<9} | "
            f"{sp_s.get('count', 0):<6} | "
            f"{_fmt(sp_s.get('last_close')):<10}"
        )

    print("=" * 176)
    print(f"Total days processed: {len(day_summaries)}")


# ============================================================
# Main
# ============================================================
def main() -> None:
    print(f"API base : {API_BASE}")
    print(f"Input    : {INPUT_JSON}")
    print(f"Output   : {OUT_ROOT}")

    api_health_check()

    if not INPUT_JSON.exists():
        print(f"\nERROR: input JSON not found at {INPUT_JSON}")
        sys.exit(1)

    with INPUT_JSON.open("r", encoding="utf-8") as f:
        payload = json.load(f)

    touched = payload.get("touched_days_summary") or []
    print(f"\nLoaded {len(touched)} touched day(s) from input JSON.")

    today_str = date.today().isoformat()
    print(f"Today (system date) = {today_str}")

    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    BY_DATE_DIR.mkdir(parents=True, exist_ok=True)

    # ---- 1. Fetch expired expiries once ----
    print(f"\nFetching expired expiries for {NIFTY_INDEX_KEY} ...")
    expired_expiries = api_get_expired_expiries(NIFTY_INDEX_KEY)
    print(f"  Got {len(expired_expiries)} expired expiry date(s).")

    # ---- 2. Iterate every touched day ----
    day_summaries: list[dict] = []
    n = len(touched)

    for i, row in enumerate(touched, start=1):
        trade_date = row.get("date")
        strike     = row.get("isolated_strike")

        if not trade_date:
            print(f"\n[{i}/{n}] SKIP — missing date field")
            continue
        if not isinstance(strike, int):
            print(f"\n[{i}/{n}] {trade_date} — SKIP (invalid strike: {strike!r})")
            continue

        print(f"\n[{i}/{n}] {trade_date}  strike={strike}  "
              f"trigger={row.get('earliest_index')} {row.get('level')}")

        day_dir = BY_DATE_DIR / trade_date
        day_dir.mkdir(parents=True, exist_ok=True)

        # ---- Spot ----
        print(f"  spot  : fetching {NIFTY_INDEX_KEY} ...")
        spot = fetch_spot_for_day(trade_date)
        _write_json(day_dir / "spot.json", spot)
        print(f"          candles={spot['summary'].get('count', 0)}")

        # ---- CE ----
        print(f"  CE    : fetching {strike} CE ...")
        ce = fetch_option_for_day(
            trade_date, strike, "CE", expired_expiries, today_str,
        )
        _write_json(day_dir / f"ce_{strike}.json", ce)
        ce_err = f"  ERROR={ce['error']}" if ce.get("error") else ""
        print(f"          expiry={ce.get('expiry')}  "
              f"expired={ce.get('is_expired')}  "
              f"candles={ce['summary'].get('count', 0)}{ce_err}")

        # ---- PE ----
        print(f"  PE    : fetching {strike} PE ...")
        pe = fetch_option_for_day(
            trade_date, strike, "PE", expired_expiries, today_str,
        )
        _write_json(day_dir / f"pe_{strike}.json", pe)
        pe_err = f"  ERROR={pe['error']}" if pe.get("error") else ""
        print(f"          expiry={pe.get('expiry')}  "
              f"expired={pe.get('is_expired')}  "
              f"candles={pe['summary'].get('count', 0)}{pe_err}")

        # ---- Day summary ----
        day_summary = {
            "trade_date":         trade_date,
            "trigger_index":      row.get("earliest_index"),
            "level":              row.get("level"),
            "trigger_time":       row.get("trigger_time"),
            "trigger_price":      row.get("trigger_price"),
            "nifty_spot_at_touch": row.get("nifty_spot"),
            "isolated_strike":    strike,
            "bias_side":          row.get("bias_side"),
            "spot": {
                "instrument_key": spot["instrument_key"],
                "meta":           spot["meta"],
                "summary":        spot["summary"],
            },
            "ce": {
                "instrument_key": ce["instrument_key"],
                "expiry":         ce["expiry"],
                "is_expired":     ce["is_expired"],
                "meta":           ce["meta"],
                "summary":        ce["summary"],
                "error":          ce["error"],
            },
            "pe": {
                "instrument_key": pe["instrument_key"],
                "expiry":         pe["expiry"],
                "is_expired":     pe["is_expired"],
                "meta":           pe["meta"],
                "summary":        pe["summary"],
                "error":          pe["error"],
            },
        }
        _write_json(day_dir / "day_summary.json", day_summary)
        day_summaries.append(day_summary)

    # ---- 3. Consolidated summary ----
    overall = {
        "generated_at":          datetime.now().isoformat(),
        "today":                 today_str,
        "api_base":              API_BASE,
        "source_json":           str(INPUT_JSON),
        "nifty_index_key":       NIFTY_INDEX_KEY,
        "expired_expiry_count":  len(expired_expiries),
        "total_input_days":      len(touched),
        "processed_days":        len(day_summaries),
        "days":                  day_summaries,
    }
    summary_path = OUT_ROOT / "summary.json"
    _write_json(summary_path, overall)
    print(f"\n[SAVED] {summary_path}")
    print(f"[SAVED] per-day folders under {BY_DATE_DIR}")

    # ---- 4. Print table ----
    _print_table(day_summaries)


if __name__ == "__main__":
    main()