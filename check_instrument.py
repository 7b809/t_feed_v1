"""
Manual diagnostic for a single instrument_key.

Usage:
    python check_instrument.py NSE_FO|56957

What it does:
  1. Dumps the Mongo doc for the instrument_key (if any).
  2. Calls the normal historical candle endpoint with a WIDE window
     (START_DATE -> END_DATE) to see if ANY candles exist at all.
  3. Groups returned candles by date so you can see which days are present.
  4. Optionally checks the expired-contracts API for the same expiry.
  5. Optionally queries Upstox "instrument search" for the underlying.
"""

import os
import sys
import json
import urllib.parse
from datetime import datetime, timedelta
from collections import defaultdict

import requests
from pymongo import MongoClient
from dotenv import load_dotenv

from db import get_upstox_access_token


# ---------------------------------------------------------------------------
# CONFIG  (edit these if you want a different window)
# ---------------------------------------------------------------------------

load_dotenv()
MONGO_ATLAS_URL = os.getenv("MONGO_ATLAS_URL")

DB_NAME = "UPSTOX_APP"
COLLECTION_NAME = "isolated_instrumentevent"

# Wide probe window (independent of the script's 7-day window).
START_DATE = "2026-08-25"   # a bit before the doc's date
END_DATE   = "2026-09-30"   # a bit after expiry
INTERVAL   = "1minute"

NORMAL_CANDLE_URL = (
    "https://api.upstox.com/v2/historical-candle/"
    "{encoded_key}/{interval}/{to_date}/{from_date}"
)
EXPIRED_CANDLE_URL = (
    "https://api.upstox.com/v2/expired-instruments/historical-candle/"
    "{encoded_key}/{interval}/{to_date}/{from_date}"
)
EXPIRED_CONTRACTS_URL = (
    "https://api.upstox.com/v2/expired-instruments/option/contract"
)
INSTRUMENT_SEARCH_URL = "https://api.upstox.com/v2/instruments/search"

REQUEST_TIMEOUT = 30
UNDERLYING_KEY = "NSE_INDEX|Nifty 50"


# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------

def headers(token):
    return {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
    }


def get_mongo_db():
    if not MONGO_ATLAS_URL:
        raise ValueError("MONGO_ATLAS_URL not set in .env")
    return MongoClient(MONGO_ATLAS_URL)[DB_NAME]


def dump_doc(instrument_key):
    col = get_mongo_db()[COLLECTION_NAME]
    print("=" * 70)
    print(f"MONGO DOC for {instrument_key}")
    print("=" * 70)

    doc = col.find_one({"instrument_key": instrument_key})
    if not doc:
        # try _id suffix pattern "YYYY-MM-DD::NSE_FO|56957"
        doc = col.find_one({"_id": {"$regex": f"::" + instrument_key.replace("|", r"\|") + "$"}})

    if not doc:
        print("No document found in Mongo for this instrument_key.")
        print("(You may be on a different DB or the key is stored differently.)")
        return None

    # Print a trimmed view
    trimmed = {
        "_id": doc.get("_id"),
        "date": doc.get("date"),
        "expiry": doc.get("expiry"),
        "instrument_key": doc.get("instrument_key"),
        "instrument_name": doc.get("instrument_name"),
        "instrument_type": doc.get("instrument_type"),
        "option_type": doc.get("option_type"),
        "strike_price": doc.get("strike_price"),
        "lot_size": doc.get("lot_size"),
        "underlying_symbol": doc.get("underlying_symbol"),
        "created_at": doc.get("created_at"),
    }
    print(json.dumps(trimmed, indent=2, default=str))

    # Also look inside the first event's payload for the instrument block
    events = doc.get("events") or {}
    for bucket_key, bucket in events.items():
        if isinstance(bucket, list) and bucket:
            payload = bucket[0].get("payload") or {}
            inst = payload.get("instrument") or {}
            if inst:
                print("\nEvent payload instrument block:")
                print(json.dumps(inst, indent=2, default=str))
                break
    return doc


def fetch_candles(url_template, instrument_key, from_date, to_date, token, label):
    encoded = urllib.parse.quote(instrument_key, safe="")
    url = url_template.format(
        encoded_key=encoded,
        interval=INTERVAL,
        to_date=to_date,
        from_date=from_date,
    )
    print(f"\n--- {label} ---")
    print(f"URL: {url}")
    resp = requests.get(url, headers=headers(token), timeout=REQUEST_TIMEOUT)
    print(f"HTTP {resp.status_code}")
    if resp.status_code != 200:
        print("Body:", resp.text[:500])
        return None
    data = resp.json()
    candles = (data.get("data") or {}).get("candles") or []
    print(f"Candles returned: {len(candles)}")
    return data


def summarise_candles_by_day(candles):
    """candles: list of [ts, o, h, l, c, v, oi]"""
    per_day = defaultdict(list)
    for row in candles:
        ts = row[0]
        day = ts[:10]
        per_day[day].append(ts)
    if not per_day:
        print("  (no candles to summarise)")
        return
    print("  Candles per day:")
    for day in sorted(per_day):
        first = min(per_day[day])
        last = max(per_day[day])
        print(f"    {day}  count={len(per_day[day]):4d}  {first[11:16]} -> {last[11:16]}")


def lookup_expired_contracts(underlying_key, expiry_date, token):
    print(f"\n--- EXPIRED CONTRACTS: {underlying_key} @ {expiry_date} ---")
    url = EXPIRED_CONTRACTS_URL
    params = {"instrument_key": underlying_key, "expiry_date": expiry_date}
    resp = requests.get(url, headers=headers(token), params=params, timeout=REQUEST_TIMEOUT)
    print(f"URL: {resp.url}")
    print(f"HTTP {resp.status_code}")
    if resp.status_code != 200:
        print("Body:", resp.text[:500])
        return []
    data = resp.json()
    contracts = (data.get("data") or [])
    print(f"Contracts returned: {len(contracts)}")
    return contracts


def instrument_search(query, token, records=20):
    """Search the Upstox instrument master (useful to find underlying for a key)."""
    print(f"\n--- INSTRUMENT SEARCH: '{query}' ---")
    params = {"query": query, "records": records}
    resp = requests.get(INSTRUMENT_SEARCH_URL, headers=headers(token), params=params, timeout=REQUEST_TIMEOUT)
    print(f"URL: {resp.url}")
    print(f"HTTP {resp.status_code}")
    if resp.status_code != 200:
        print("Body:", resp.text[:500])
        return []
    data = resp.json()
    items = data.get("data") or []
    print(f"Results: {len(items)}")
    for it in items[:records]:
        print("  -", json.dumps(it, default=str))
    return items


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 2:
        print("Usage: python check_instrument.py <instrument_key>")
        print('Example: python check_instrument.py "NSE_FO|56957"')
        sys.exit(1)

    instrument_key = sys.argv[1]
    token = get_upstox_access_token()
    print("Access token loaded.\n")

    # 1) Mongo doc
    doc = dump_doc(instrument_key)

    # Figure out expiry from doc (or CLI override)
    expiry = None
    if doc:
        expiry = doc.get("expiry")

    # 2) Wide-window probe on the NORMAL endpoint
    normal = fetch_candles(
        NORMAL_CANDLE_URL, instrument_key,
        from_date=START_DATE, to_date=END_DATE,
        token=token, label="NORMAL endpoint (wide window)",
    )
    if normal:
        candles = (normal.get("data") or {}).get("candles") or []
        summarise_candles_by_day(candles)

    # 3) Wide-window probe on the EXPIRED endpoint (using its expired key)
    #    We need to first find the expired key by matching trading_symbol.
    if doc and expiry:
        contracts = lookup_expired_contracts(UNDERLYING_KEY, expiry, token)
        target_symbol = (doc.get("instrument_name") or "").strip().upper()

        # find matching contract
        match = None
        for c in contracts:
            if (c.get("trading_symbol") or "").strip().upper() == target_symbol:
                match = c
                break
        if not match and target_symbol:
            # strike + type fallback
            parts = target_symbol.split()
            if len(parts) >= 4:
                try:
                    strike = float(parts[1])
                    opt_type = parts[2].upper()
                except ValueError:
                    strike, opt_type = None, None
                if strike is not None and opt_type:
                    for c in contracts:
                        if (c.get("strike_price") == strike
                                and (c.get("instrument_type") or "").upper() == opt_type
                                and c.get("expiry") == expiry):
                            match = c
                            break

        if match:
            print(f"\nMatched expired contract: {json.dumps(match, indent=2, default=str)}")
            expired_key = match.get("instrument_key")
            if expired_key:
                exp = fetch_candles(
                    EXPIRED_CANDLE_URL, expired_key,
                    from_date=START_DATE, to_date=END_DATE,
                    token=token, label="EXPIRED endpoint (wide window)",
                )
                if exp:
                    ecandles = (exp.get("data") or {}).get("candles") or []
                    summarise_candles_by_day(ecandles)
        else:
            print(f"\nNo expired contract matched trading_symbol '{target_symbol}' @ {expiry}")
    else:
        print("\n(Skipping expired-contract probe — no expiry in doc.)")

    # 4) Instrument search by partial key (last part of the key)
    token_part = instrument_key.split("|")[-1]
    instrument_search(token_part, token, records=10)


if __name__ == "__main__":
    main()