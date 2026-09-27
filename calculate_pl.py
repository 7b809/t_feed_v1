"""
calculate_pl.py

Loads one or more isolated instrument docs from a JSON array, fetches 1-min
candles for PE + CE legs via Upstox History V3, simulates EMA-cross trades,
and exports results (raw candles + Excel with trades + summary) under ./data.

Usage:
    python calculate_pl.py                      # auto-pick newest by date
    python calculate_pl.py --key NSE_FO|73924   # pick by instrument_key
    python calculate_pl.py --id 2026-09-24::NSE_FO|73924
    python calculate_pl.py --all                # process every doc in the file
    python calculate_pl.py --date 2026-09-24    # pick by date

Dependencies:
    pip install upstox-python-sdk pandas openpyxl pymongo python-dotenv
"""

import os
import re
import ast
import json
import argparse
import logging
from datetime import datetime, date as date_cls

import pandas as pd
import upstox_client
from upstox_client.rest import ApiException

from db import get_upstox_access_token

# ------------------------------------------------------------------
# Logging
# ------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger(__name__)

# ------------------------------------------------------------------
# Config
# ------------------------------------------------------------------
OUTPUT_DIR = "data"
os.makedirs(OUTPUT_DIR, exist_ok=True)

CANDIDATE_DOC_FILES = [
    "isolated_instrument_doc.json",
    "isolated_instrument_doc.txt",
]


# ==================================================================
# Document loading
# ==================================================================
def _clean_js_types(text: str) -> str:
    """
    Convert MongoDB-shell / Extended-JSON tokens to plain JSON:
        ISODate('...')      -> "..."
        NumberInt('13')     -> 13
        NumberLong('...')   -> 1234567890
        Double('23242.5')   -> 23242.5
    """
    text = re.sub(r"ISODate\('([^']*)'\)", r'"\1"', text)
    text = re.sub(r"NumberInt\('([^']*)'\)", r"\1", text)
    text = re.sub(r"NumberLong\('([^']*)'\)", r"\1", text)
    text = re.sub(r"Double\('([^']*)'\)", r"\1", text)
    return text


def _parse_doc_text(raw: str, path: str):
    """Parse the file content into Python objects (dict or list)."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        cleaned = _clean_js_types(raw)
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            try:
                return ast.literal_eval(cleaned)
            except Exception as e:
                raise ValueError(f"Could not parse {path}: {e}")


def load_all_docs():
    """
    Load every isolated instrument doc found in the JSON/TXT file.
    Always returns a list of dicts.
    """
    path = next((p for p in CANDIDATE_DOC_FILES if os.path.exists(p)), None)
    if path is None:
        raise FileNotFoundError(
            "Save the isolated instrument doc as "
            + " or ".join(CANDIDATE_DOC_FILES)
        )

    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()

    doc = _parse_doc_text(raw, path)

    if isinstance(doc, dict):
        docs = [doc]
    elif isinstance(doc, list):
        docs = [d for d in doc if isinstance(d, dict)]
    else:
        raise TypeError(f"Unexpected top-level type in {path}: {type(doc)}")

    if not docs:
        raise ValueError(f"{path} contained no usable documents.")

    log.info(f"Loaded {len(docs)} isolated doc(s) from {path}")
    return docs, path


def select_docs(docs, *, by_id=None, by_key=None, by_date=None, all_docs=False):
    """Filter docs by CLI criteria. Returns a list."""
    if all_docs:
        return docs

    # priority: id > key > date
    if by_id:
        out = [d for d in docs if d.get("_id") == by_id]
        if not out:
            raise ValueError(f"No doc with _id={by_id}")
        return out

    if by_key:
        out = [d for d in docs if d.get("instrument_key") == by_key]
        if not out:
            raise ValueError(f"No doc with instrument_key={by_key}")
        # if multiple (different dates), take the newest
        out.sort(key=lambda d: d.get("date", ""), reverse=True)
        return [out[0]]

    if by_date:
        out = [d for d in docs if d.get("date") == by_date]
        if not out:
            raise ValueError(f"No doc with date={by_date}")
        return out

    # default: newest by `date`, then updated_at
    docs_sorted = sorted(
        docs,
        key=lambda d: (d.get("date", ""), d.get("updated_at", "")),
        reverse=True,
    )
    log.info(
        f"Auto-selected newest doc: "
        f"{docs_sorted[0].get('instrument_name')} on {docs_sorted[0].get('date')}"
    )
    return [docs_sorted[0]]


# ==================================================================
# Upstox client
# ==================================================================
def configure_upstox():
    access_token = get_upstox_access_token()
    configuration = upstox_client.Configuration()
    configuration.access_token = access_token
    upstox_client.ApiClient(configuration)
    log.info("Upstox client configured.")
    return configuration


# ==================================================================
# Candle fetching (History V3)
# ==================================================================
def fetch_candles(instrument_key: str, from_date: str, to_date: str,
                  interval: str = "1", unit: str = "minutes"):
    api_instance = upstox_client.HistoryV3Api()
    try:
        response = api_instance.get_historical_candle_data1(
            instrument_key, unit, interval, to_date, from_date
        )
        data = response.to_dict() if hasattr(response, "to_dict") else response
        candles = data.get("data", {}).get("candles", [])
        log.info(f"Fetched {len(candles)} candles for {instrument_key}")
        return candles
    except ApiException as e:
        body = getattr(e, "body", b"")
        if isinstance(body, bytes):
            body = body.decode("utf-8", "ignore")
        log.error(
            f"Upstox API error for {instrument_key}: "
            f"HTTP {e.status} | {body[:200]}"
        )
        return []


# ==================================================================
# Helpers
# ==================================================================
def sanitize_filename(name: str) -> str:
    name = (name or "").replace(" ", "_").replace("/", "_")
    return re.sub(r"[^A-Za-z0-9_\-]", "", name)


def candles_to_df(candles):
    if not candles:
        return pd.DataFrame(
            columns=["timestamp", "open", "high", "low", "close", "volume", "oi"]
        )
    df = pd.DataFrame(
        candles,
        columns=["timestamp", "open", "high", "low", "close", "volume", "oi"],
    )
    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    for c in ["open", "high", "low", "close", "volume", "oi"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)


def find_candle_close_at(df: pd.DataFrame, ts: pd.Timestamp):
    if df.empty:
        return None
    # Normalize tz
    if ts.tzinfo is not None and df["timestamp"].dt.tz is None:
        ts = ts.tz_localize(None)
    elif ts.tzinfo is None and df["timestamp"].dt.tz is not None:
        ts = ts.tz_localize(df["timestamp"].dt.tz)

    match = df[df["timestamp"] == ts]
    if not match.empty:
        return float(match.iloc[0]["close"])

    prior = df[df["timestamp"] <= ts]
    if not prior.empty:
        return float(prior.iloc[-1]["close"])
    return None


def last_close_of_day(df: pd.DataFrame):
    if df.empty:
        return None
    return float(df.iloc[-1]["close"])


def is_expired(doc) -> bool:
    """Return True if the doc's expiry date is strictly before today (IST-naive)."""
    exp = doc.get("expiry")
    if not exp:
        return False
    try:
        exp_d = datetime.strptime(exp, "%Y-%m-%d").date()
    except ValueError:
        return False
    return exp_d < date_cls.today()


# ==================================================================
# Event extraction (handles multiple shapes)
# ==================================================================
def extract_events(doc):
    """
    Returns a flat, time-sorted list of events.

    Handles:
        - doc['events'] = {"09_41_06": [ev, ev, ...], ...}   (isolated docs)
        - doc['events'] = [ev, ev, ...]                       (array form)
        - each ev may have fields at top-level, or nested under ev['payload']
    """
    raw_events = doc.get("events") or {}

    flat = []
    if isinstance(raw_events, dict):
        for _, ev_list in raw_events.items():
            if isinstance(ev_list, list):
                flat.extend(ev_list)
    elif isinstance(raw_events, list):
        flat = list(raw_events)

    # Normalize each event: pull the ema/order_suggestion etc. from payload if needed
    normalized = []
    for ev in flat:
        if not isinstance(ev, dict):
            continue
        payload = ev.get("payload", {}) or {}

        ema_ts = ev.get("ema_timestamp") or payload.get("ema", {}).get("timestamp")
        side = (
            ev.get("suggested_order_side")
            or payload.get("order_suggestion", {}).get("suggested_order_side")
        )
        cross_type = (
            ev.get("cross_type")
            or payload.get("ema", {}).get("cross_type")
        )
        ema_block = payload.get("ema", {}) or {}
        nifty_ltp = ev.get("nifty_ltp") or payload.get("market_snapshot", {}).get("nifty_ltp")
        iso_ltp = (
            ev.get("isolated_instrument_ltp")
            or payload.get("market_snapshot", {}).get("isolated_instrument_ltp")
        )

        if not ema_ts or not side:
            continue

        normalized.append({
            "ema_timestamp": ema_ts,
            "suggested_order_side": side,
            "cross_type": cross_type,
            "ema_fast": ema_block.get("fast_value"),
            "ema_slow": ema_block.get("slow_value"),
            "nifty_ltp": nifty_ltp,
            "isolated_instrument_ltp": iso_ltp,
            "payload": payload,
        })

    normalized.sort(key=lambda e: e["ema_timestamp"])
    return normalized


# ==================================================================
# Resolve CE counterpart key
# ==================================================================
def resolve_ce_key(doc, events):
    pe_key = doc.get("instrument_key")
    strike = doc.get("strike_price")
    ce_key = None

    for ev in events:
        suggestions = (
            ev.get("payload", {})
              .get("order_suggestion", {})
              .get("nearest_instruments", [])
        )
        for inst in suggestions:
            if (
                inst.get("option_type") == "CE"
                and (strike is None or inst.get("strike_price") == strike)
            ):
                ce_key = inst["instrument_key"]
                break
        if ce_key:
            break

    if not ce_key:
        log.warning(
            "CE key not found in doc's order_suggestion. "
            "Cannot simulate bearish legs accurately."
        )
    return pe_key, ce_key


# ==================================================================
# Simulation
# ==================================================================
def simulate(doc, events, pe_df, ce_df):
    lot_size = int(doc.get("lot_size") or 65)
    trades = []
    open_trade = None

    for ev in events:
        side = ev["suggested_order_side"]
        ts = pd.Timestamp(ev["ema_timestamp"])
        ts_naive = ts.tz_localize(None) if ts.tzinfo else ts

        series = pe_df if side == "PE" else ce_df
        entry_price = find_candle_close_at(series, ts_naive)
        if entry_price is None:
            entry_price = float(ev.get("isolated_instrument_ltp") or 0.0)

        # Close previous
        if open_trade is not None:
            exit_price = entry_price
            pnl = (exit_price - open_trade["entry_price"]) * lot_size
            open_trade.update({
                "exit_time": str(ts_naive),
                "exit_price": exit_price,
                "pnl": round(pnl, 2),
                "exit_reason": "signal_flip",
            })
            trades.append(open_trade)
            open_trade = None

        open_trade = {
            "instrument_key": doc.get("instrument_key"),
            "instrument_name": doc.get("instrument_name"),
            "option_side": side,
            "entry_time": str(ts_naive),
            "entry_price": entry_price,
            "cross_type": ev.get("cross_type"),
            "ema_fast": ev.get("ema_fast"),
            "ema_slow": ev.get("ema_slow"),
            "nifty_ltp": ev.get("nifty_ltp"),
            "lot_size": lot_size,
        }

    # Close EOD
    if open_trade is not None:
        series = pe_df if open_trade["option_side"] == "PE" else ce_df
        exit_price = last_close_of_day(series)
        if exit_price is None:
            exit_price = open_trade["entry_price"]
        pnl = (exit_price - open_trade["entry_price"]) * lot_size
        open_trade.update({
            "exit_time": str(series.iloc[-1]["timestamp"]) if not series.empty else "",
            "exit_price": exit_price,
            "pnl": round(pnl, 2),
            "exit_reason": "end_of_day",
        })
        trades.append(open_trade)

    return trades


# ==================================================================
# Output
# ==================================================================
def save_candles_json(df, instrument_name, side):
    fname = f"{sanitize_filename(instrument_name)}_{side}_candles.json"
    path = os.path.join(OUTPUT_DIR, fname)
    out = df.copy()
    out["timestamp"] = out["timestamp"].astype(str)
    out.to_json(path, orient="records", indent=2)
    log.info(f"Saved candles -> {path}")
    return path


def save_trades_excel(trades, doc):
    fname = f"{sanitize_filename(doc.get('instrument_name'))}_simulation.xlsx"
    path = os.path.join(OUTPUT_DIR, fname)

    cols = [
        "instrument_name", "option_side", "entry_time", "entry_price",
        "exit_time", "exit_price", "pnl", "exit_reason",
        "cross_type", "ema_fast", "ema_slow", "nifty_ltp", "lot_size",
        "instrument_key",
    ]

    if not trades:
        empty = pd.DataFrame(columns=cols)
        summary = pd.DataFrame([{
            "total_trades": 0, "winning_trades": 0, "losing_trades": 0,
            "total_pnl": 0, "avg_pnl": 0, "max_win": 0, "max_loss": 0,
            "win_rate_%": 0,
        }])
        with pd.ExcelWriter(path, engine="openpyxl") as w:
            empty.to_excel(w, sheet_name="Trades", index=False)
            summary.to_excel(w, sheet_name="Summary", index=False)
        log.info(f"Saved empty simulation -> {path}")
        return path

    df = pd.DataFrame(trades)
    df = df[[c for c in cols if c in df.columns]]

    wins = df[df["pnl"] > 0]
    losses = df[df["pnl"] < 0]

    summary = pd.DataFrame([{
        "total_trades": len(df),
        "winning_trades": len(wins),
        "losing_trades": len(losses),
        "total_pnl": round(df["pnl"].sum(), 2),
        "avg_pnl": round(df["pnl"].mean(), 2),
        "max_win": round(df["pnl"].max(), 2),
        "max_loss": round(df["pnl"].min(), 2),
        "win_rate_%": round(len(wins) / len(df) * 100, 2),
    }])

    with pd.ExcelWriter(path, engine="openpyxl") as w:
        df.to_excel(w, sheet_name="Trades", index=False)
        summary.to_excel(w, sheet_name="Summary", index=False)

    log.info(f"Saved simulation -> {path}")
    return path


# ==================================================================
# Per-doc processing
# ==================================================================
def process_doc(doc):
    instrument_name = doc.get("instrument_name") or "UNKNOWN"
    date_str = doc.get("date")
    pe_key = doc.get("instrument_key")

    log.info("-" * 70)
    log.info(f"Processing: {instrument_name} | date={date_str} | key={pe_key}")

    if not pe_key or not date_str:
        log.warning("Skipping doc: missing instrument_key or date.")
        return None

    if is_expired(doc):
        log.warning(
            f"Skipping {instrument_name}: expired on {doc.get('expiry')}. "
            f"Upstox History V3 does not serve expired F&O contracts."
        )
        return None

    events = extract_events(doc)
    if not events:
        log.warning(f"No usable events for {instrument_name}. Skipping.")
        return None

    _, ce_key = resolve_ce_key(doc, events)
    if not ce_key:
        log.warning(f"No CE key resolved for {instrument_name}; CE legs will use 0 price.")

    log.info(f"PE key: {pe_key} | CE key: {ce_key}")

    pe_candles = fetch_candles(pe_key, date_str, date_str)
    ce_candles = fetch_candles(ce_key, date_str, date_str) if ce_key else []

    pe_df = candles_to_df(pe_candles)
    ce_df = candles_to_df(ce_candles)

    save_candles_json(pe_df, instrument_name, "PE")
    if ce_key:
        save_candles_json(ce_df, instrument_name, "CE")

    trades = simulate(doc, events, pe_df, ce_df)
    log.info(f"Simulated {len(trades)} trades.")

    path = save_trades_excel(trades, doc)

    # Console summary
    if trades:
        df = pd.DataFrame(trades)
        print(f"\n===== SIMULATION SUMMARY: {instrument_name} =====")
        print(f"Date            : {date_str}")
        print(f"Total Trades    : {len(df)}")
        print(f"Total P&L       : {df['pnl'].sum():.2f}")
        print(f"Wins / Losses   : {len(df[df.pnl>0])} / {len(df[df.pnl<0])}")
        print(f"Win Rate        : {len(df[df.pnl>0])/len(df)*100:.2f}%")
        print(f"Excel saved at  : {path}")
        print("=" * 60 + "\n")
    return path


# ==================================================================
# Main
# ==================================================================
def main():
    parser = argparse.ArgumentParser(description="Simulate isolated instrument EMA trades.")
    parser.add_argument("--id", help="Pick doc by _id")
    parser.add_argument("--key", help="Pick doc by instrument_key (newest if multiple)")
    parser.add_argument("--date", help="Pick doc(s) by date (YYYY-MM-DD)")
    parser.add_argument("--all", action="store_true", help="Process every doc in the file")
    args = parser.parse_args()

    configure_upstox()
    docs, path = load_all_docs()
    selected = select_docs(
        docs,
        by_id=args.id,
        by_key=args.key,
        by_date=args.date,
        all_docs=args.all,
    )
    log.info(f"Will process {len(selected)} doc(s).")

    for d in selected:
        try:
            process_doc(d)
        except Exception as e:
            log.exception(f"Failed to process doc {d.get('_id')}: {e}")


if __name__ == "__main__":
    main()