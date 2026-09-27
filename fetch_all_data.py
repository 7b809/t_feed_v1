"""
UPSTOX EXPIRED HISTORICAL CANDLE EXPORT (with expired contract fallback)

For each unique instrument found in MongoDB:
  - expiry_date  = doc["expiry"]
  - end_date     = doc["date"]
  - start_date   = the 10th most recent NON-ZERO trading day before end_date

The Upstox historical endpoint only returns up to 7 calendar days per call.
To collect 10 non-zero trading days we split the range into batches of at
most 7 calendar days and merge the candle arrays, deduplicating by timestamp.

The expired fallback is tried whenever the NORMAL endpoint fails to produce
candles for any of these reasons:
  - HTTP 400 UDAPI100011 "Invalid Instrument key"
  - HTTP 200 but 0 candles (expired contract accepted by key but no data)
  - any other 4xx (e.g. UDAPI1148 "Invalid date range")

Fallback steps:
  1. GET /v2/expired-instruments/option/contract?instrument_key=...&expiry_date=...
  2. Match by trading_symbol (with a strike + option_type fallback)
  3. Use the returned expired_instrument_key with the expired-candle endpoint.

No upstox_client SDK is used — every call goes through `requests`, which
avoids the SDK's import-time circular-import bug.

Only files with >= 1 candle are saved.
Token is loaded from MongoDB via db.get_upstox_access_token().
Filenames use the instrument's trading symbol with a serial-number prefix
ordered by the instrument's saved date.
"""

import os
import json
import time
import urllib.parse
from datetime import datetime, timedelta

import requests
from pymongo import MongoClient
from dotenv import load_dotenv

from db import get_upstox_access_token


# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

load_dotenv()

MONGO_ATLAS_URL = os.getenv("MONGO_ATLAS_URL")

DB_NAME = "UPSTOX_APP"
COLLECTION_NAME = "isolated_instrumentevent"

INTERVAL = "1minute"

# How many NON-ZERO trading days to look back.
# Weekends / holidays return 0 candles and are skipped.
LOOKBACK_TRADING_DAYS = 10

# Safety cap: never scan more than this many calendar days back
# while probing for non-zero trading days.
MAX_CALENDAR_LOOKBACK_DAYS = 45

# Upstox historical-candle API caps a single request at 7 calendar days.
MAX_DAYS_PER_REQUEST = 7

OUTPUT_FOLDER = "historical_data"

UPSTOX_HISTORICAL_URL = (
    "https://api.upstox.com/v2/historical-candle/"
    "{encoded_key}/{interval}/{to_date}/{from_date}"
)

UPSTOX_EXPIRED_HISTORICAL_URL = (
    "https://api.upstox.com/v2/expired-instruments/historical-candle/"
    "{encoded_key}/{interval}/{to_date}/{from_date}"
)

UPSTOX_EXPIRED_OPTION_CONTRACTS_URL = (
    "https://api.upstox.com/v2/expired-instruments/option/contract"
)

REQUEST_TIMEOUT = 30
SLEEP_BETWEEN_REQUESTS = 0.35

UNDERLYING_KEY = "NSE_INDEX|Nifty 50"


# ---------------------------------------------------------------------------
# MONGO
# ---------------------------------------------------------------------------

def get_mongo_db():
    if not MONGO_ATLAS_URL:
        raise ValueError("MONGO_ATLAS_URL is not configured in .env")
    client = MongoClient(MONGO_ATLAS_URL)
    return client[DB_NAME]


# ---------------------------------------------------------------------------
# INSTRUMENT KEY / EXPIRY / SYMBOL / DATE EXTRACTION
# ---------------------------------------------------------------------------

def extract_instrument_key(doc):
    if not isinstance(doc, dict):
        return None

    top = doc.get("instrument_key")
    if top:
        return top

    events = doc.get("events")
    if isinstance(events, dict):
        for _bucket_key, bucket in events.items():
            if not isinstance(bucket, list):
                continue
            for evt in bucket:
                if not isinstance(evt, dict):
                    continue

                payload = evt.get("payload") or {}
                if isinstance(payload, dict):
                    inst = payload.get("instrument") or {}
                    if isinstance(inst, dict):
                        key = inst.get("instrument_key")
                        if key:
                            return key

                    raw = payload.get("raw_ema_event") or {}
                    if isinstance(raw, dict):
                        key = raw.get("instrument_key")
                        if key:
                            return key

                        raw_inst = raw.get("contract_info") or raw.get("info") or {}
                        if isinstance(raw_inst, dict):
                            key = raw_inst.get("instrument_key")
                            if key:
                                return key

                key = evt.get("instrument_key")
                if key:
                    return key

    return _deep_find_instrument_key(doc)


def _deep_find_instrument_key(node):
    if isinstance(node, dict):
        ik = node.get("instrument_key")
        if ik and isinstance(ik, str) and "|" in ik:
            return ik
        for v in node.values():
            found = _deep_find_instrument_key(v)
            if found:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _deep_find_instrument_key(item)
            if found:
                return found
    return None


def _extract_expiry(doc):
    if not isinstance(doc, dict):
        return None

    if doc.get("expiry"):
        return doc["expiry"]

    events = doc.get("events")
    if isinstance(events, dict):
        for _bucket_key, bucket in events.items():
            if not isinstance(bucket, list):
                continue
            for evt in bucket:
                if not isinstance(evt, dict):
                    continue
                payload = evt.get("payload") or {}
                if isinstance(payload, dict):
                    inst = payload.get("instrument") or {}
                    if isinstance(inst, dict) and inst.get("expiry"):
                        return inst["expiry"]
                    ms = payload.get("market_snapshot") or {}
                    if isinstance(ms, dict) and ms.get("option_chain_expiry"):
                        return ms["option_chain_expiry"]
                    raw = payload.get("raw_ema_event") or {}
                    if isinstance(raw, dict):
                        ci = raw.get("contract_info") or raw.get("info") or {}
                        if isinstance(ci, dict) and ci.get("expiry"):
                            return ci["expiry"]
    return None


def _extract_trading_symbol(doc):
    """Extract the human-readable trading symbol like 'NIFTY 23200 CE 22 SEP 26'."""
    if not isinstance(doc, dict):
        return None

    if doc.get("instrument_name"):
        return doc["instrument_name"]

    events = doc.get("events")
    if isinstance(events, dict):
        for _bucket_key, bucket in events.items():
            if not isinstance(bucket, list):
                continue
            for evt in bucket:
                if not isinstance(evt, dict):
                    continue
                payload = evt.get("payload") or {}
                if isinstance(payload, dict):
                    inst = payload.get("instrument") or {}
                    if isinstance(inst, dict) and inst.get("trading_symbol"):
                        return inst["trading_symbol"]

                    raw = payload.get("raw_ema_event") or {}
                    if isinstance(raw, dict):
                        ci = raw.get("contract_info") or raw.get("info") or {}
                        if isinstance(ci, dict) and ci.get("trading_symbol"):
                            return ci["trading_symbol"]
    return None


def _extract_date(doc):
    if not isinstance(doc, dict):
        return None

    if doc.get("date"):
        return doc["date"]

    _id = doc.get("_id")
    if isinstance(_id, str) and "::" in _id:
        prefix = _id.split("::", 1)[0]
        if len(prefix) == 10 and prefix[4] == "-" and prefix[7] == "-":
            return prefix

    created = doc.get("created_at")
    if isinstance(created, str) and len(created) >= 10:
        return created[:10]

    return None


# ---------------------------------------------------------------------------
# LOAD INSTRUMENTS WITH PER-DOC META
# ---------------------------------------------------------------------------

def load_instruments():
    db = get_mongo_db()
    col = db[COLLECTION_NAME]

    results = {}
    scanned = 0
    skipped = 0

    cursor = col.find({}, {"instrument_key": 1, "events": 1, "date": 1,
                           "expiry": 1, "created_at": 1, "_id": 1,
                           "instrument_name": 1})

    for doc in cursor:
        scanned += 1

        key = extract_instrument_key(doc)
        if not key:
            skipped += 1
            print("Skipping document - instrument_key not found")
            continue

        end_date = _extract_date(doc)
        if not end_date:
            skipped += 1
            print(f"Skipping document - date not found for {key}")
            continue

        expiry = _extract_expiry(doc)
        symbol = _extract_trading_symbol(doc)

        try:
            end_dt = datetime.strptime(end_date, "%Y-%m-%d").date()
        except ValueError:
            skipped += 1
            print(f"Skipping document - bad date '{end_date}' for {key}")
            continue

        entry = {
            "expiry": expiry,
            "trading_symbol": symbol,
            "end_date": end_dt.isoformat(),
        }

        existing = results.get(key)
        if existing is None or entry["end_date"] > existing["end_date"]:
            results[key] = entry

    print(f"Documents scanned  : {scanned}")
    print(f"Documents skipped  : {skipped}")
    print(f"Unique instruments : {len(results)}")
    return results


# ---------------------------------------------------------------------------
# UPSTOX API (all via requests — no SDK)
# ---------------------------------------------------------------------------

def build_headers(access_token):
    return {
        "Accept": "application/json",
        "Authorization": f"Bearer {access_token}",
    }


class InvalidInstrumentKeyError(Exception):
    def __init__(self, instrument_key, body):
        self.instrument_key = instrument_key
        self.body = body
        super().__init__(f"Invalid Instrument key: {instrument_key}")


def fetch_candles_normal(instrument_key, interval, from_date, to_date, access_token):
    """
    Returns the raw payload dict.

    Raises:
      - InvalidInstrumentKeyError on UDAPI100011
      - ValueError on any other non-200 response
    """
    encoded_key = urllib.parse.quote(instrument_key, safe="")
    url = UPSTOX_HISTORICAL_URL.format(
        encoded_key=encoded_key,
        interval=interval,
        to_date=to_date,
        from_date=from_date,
    )

    resp = requests.get(
        url,
        headers=build_headers(access_token),
        timeout=REQUEST_TIMEOUT,
    )

    if resp.status_code != 200:
        body = resp.text or ""
        if "UDAPI100011" in body or "Invalid Instrument key" in body:
            raise InvalidInstrumentKeyError(instrument_key, body)
        raise ValueError(f"HTTP {resp.status_code} for {instrument_key}: {body[:200]}")

    return resp.json()


def fetch_candles_expired(expired_instrument_key, interval, from_date, to_date, access_token):
    """Fetch candles using the expired-instruments endpoint."""
    encoded_key = urllib.parse.quote(expired_instrument_key, safe="")
    url = UPSTOX_EXPIRED_HISTORICAL_URL.format(
        encoded_key=encoded_key,
        interval=interval,
        to_date=to_date,
        from_date=from_date,
    )

    resp = requests.get(
        url,
        headers=build_headers(access_token),
        timeout=REQUEST_TIMEOUT,
    )

    if resp.status_code != 200:
        raise ValueError(
            f"HTTP {resp.status_code} for {expired_instrument_key}: {resp.text[:200]}"
        )

    return resp.json()


def fetch_expired_option_contracts(underlying_key, expiry_date, access_token):
    """
    GET /v2/expired-instruments/option/contract
    Returns the list of contract dicts for the given underlying + expiry.
    """
    params = {
        "instrument_key": underlying_key,
        "expiry_date": expiry_date,
    }

    resp = requests.get(
        UPSTOX_EXPIRED_OPTION_CONTRACTS_URL,
        headers=build_headers(access_token),
        params=params,
        timeout=REQUEST_TIMEOUT,
    )

    if resp.status_code != 200:
        raise ValueError(
            f"HTTP {resp.status_code} for expired contracts "
            f"({underlying_key}, {expiry_date}): {resp.text[:200]}"
        )

    data = resp.json()
    if isinstance(data, dict):
        return data.get("data") or []
    return []


def lookup_expired_instrument_key(underlying_key, expiry_date, trading_symbol, access_token):
    """
    Call the expired option contracts REST endpoint, match by trading_symbol
    (with a strike/type fallback), and return the expired_instrument_key
    like 'NSE_FO|56980|22-09-2026' or None.
    """
    try:
        contracts = fetch_expired_option_contracts(
            underlying_key=underlying_key,
            expiry_date=expiry_date,
            access_token=access_token,
        )
    except Exception as e:
        print(f"    Expired contracts API failed: {e}")
        return None

    if not contracts:
        print("    Expired contracts API returned 0 contracts")
        return None

    target = (trading_symbol or "").strip().upper()

    # Pass 1: exact match on trading_symbol
    for c in contracts:
        sym = (c.get("trading_symbol") or "").strip().upper()
        if sym == target:
            return c.get("instrument_key")

    # Pass 2: match by strike + option_type + expiry
    parts = target.split()
    if len(parts) >= 4:
        try:
            strike = float(parts[1])
            opt_type = parts[2].upper()
        except ValueError:
            strike = None
            opt_type = None

        if strike is not None and opt_type:
            for c in contracts:
                if (
                    c.get("strike_price") == strike
                    and (c.get("instrument_type") or "").upper() == opt_type
                    and c.get("expiry") == expiry_date
                ):
                    return c.get("instrument_key")

    return None


def safe_filename(trading_symbol, instrument_key, expiry):
    """
    Build a filesystem-safe base name using the instrument's trading symbol.
    Falls back to instrument_key if trading_symbol is missing.
    """
    base = trading_symbol or instrument_key
    for ch in ['|', '/', '\\', ':', '*', '?', '"', '<', '>', ' ', '\t', '\n']:
        base = base.replace(ch, '_')
    while '__' in base:
        base = base.replace('__', '_')
    base = base.strip('_')
    if not base:
        base = instrument_key.replace("|", "_").replace("/", "_")
    if expiry:
        base = f"{base}_{expiry}"
    return base


def save_candles(filename_base, payload, output_folder):
    os.makedirs(output_folder, exist_ok=True)
    fname = f"{filename_base}.json"
    fpath = os.path.join(output_folder, fname)
    with open(fpath, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    return fpath


def _count_candles(payload):
    if not isinstance(payload, dict):
        return 0
    return len((payload.get("data") or {}).get("candles") or [])


# ---------------------------------------------------------------------------
# BATCHED FETCHING (Upstox caps at 7 calendar days per request)
# ---------------------------------------------------------------------------

def build_date_batches(from_date_str, to_date_str, max_days=MAX_DAYS_PER_REQUEST):
    """
    Split [from_date, to_date] into consecutive windows of at most `max_days`
    calendar days each. Returns a list of (batch_from, batch_to) ISO strings,
    ordered ascending (oldest first).
    """
    try:
        start = datetime.strptime(from_date_str, "%Y-%m-%d").date()
        end = datetime.strptime(to_date_str, "%Y-%m-%d").date()
    except ValueError:
        return []

    if start > end:
        start, end = end, start

    batches = []
    cursor = start
    # Each window is inclusive of both endpoints, so span = max_days.
    step = timedelta(days=max_days - 1)
    while cursor <= end:
        batch_end = cursor + step
        if batch_end > end:
            batch_end = end
        batches.append((cursor.isoformat(), batch_end.isoformat()))
        cursor = batch_end + timedelta(days=1)

    return batches


def merge_candle_payloads(payloads):
    """
    Merge multiple Upstox candle payloads into one, deduplicating candles by
    timestamp. Preserves the first payload's non-candle metadata (status etc.).
    """
    merged_candles_by_ts = {}
    base = None

    for p in payloads:
        if not isinstance(p, dict):
            continue
        if base is None:
            base = p
        candles = (p.get("data") or {}).get("candles") or []
        for c in candles:
            if not isinstance(c, list) or len(c) < 6:
                continue
            ts = c[0]
            merged_candles_by_ts[ts] = c

    if base is None:
        return {"status": "success", "data": {"candles": []}}

    merged_list = [merged_candles_by_ts[k] for k in sorted(merged_candles_by_ts.keys())]

    # Shallow clone base payload and swap in merged candles.
    out = dict(base)
    out_data = dict(out.get("data") or {})
    out_data["candles"] = merged_list
    out["data"] = out_data
    return out


def fetch_candles_batched_normal(instrument_key, interval, from_date, to_date,
                                 access_token, max_days=MAX_DAYS_PER_REQUEST):
    """
    Fetch candles from the NORMAL endpoint over [from_date, to_date] by
    splitting the range into <= max_days windows and merging the results.

    Raises InvalidInstrumentKeyError if any batch reports the key as invalid.
    """
    batches = build_date_batches(from_date, to_date, max_days=max_days)
    if not batches:
        return {"status": "success", "data": {"candles": []}}

    payloads = []
    for b_from, b_to in batches:
        payload = fetch_candles_normal(
            instrument_key=instrument_key,
            interval=interval,
            from_date=b_from,
            to_date=b_to,
            access_token=access_token,
        )
        payloads.append(payload)
        time.sleep(SLEEP_BETWEEN_REQUESTS)

    return merge_candle_payloads(payloads)


def fetch_candles_batched_expired(expired_instrument_key, interval, from_date, to_date,
                                  access_token, max_days=MAX_DAYS_PER_REQUEST):
    """
    Fetch candles from the EXPIRED endpoint over [from_date, to_date] by
    splitting the range into <= max_days windows and merging the results.
    """
    batches = build_date_batches(from_date, to_date, max_days=max_days)
    if not batches:
        return {"status": "success", "data": {"candles": []}}

    payloads = []
    for b_from, b_to in batches:
        payload = fetch_candles_expired(
            expired_instrument_key=expired_instrument_key,
            interval=interval,
            from_date=b_from,
            to_date=b_to,
            access_token=access_token,
        )
        payloads.append(payload)
        time.sleep(SLEEP_BETWEEN_REQUESTS)

    return merge_candle_payloads(payloads)


# ---------------------------------------------------------------------------
# NON-ZERO TRADING DAY DISCOVERY
# ---------------------------------------------------------------------------

def find_nonzero_trading_days_batched(instrument_key, end_date_str, access_token,
                                      needed=LOOKBACK_TRADING_DAYS,
                                      max_calendar_days=MAX_CALENDAR_LOOKBACK_DAYS,
                                      max_days_per_call=MAX_DAYS_PER_REQUEST,
                                      use_expired=False,
                                      expired_instrument_key=None):
    """
    Walk backwards from end_date_str in windows of <= max_days_per_call
    calendar days and collect the distinct dates that contain >= 1 candle.

    Stops when `needed` distinct non-zero days are collected or the
    `max_calendar_days` cap is reached.

    Returns a sorted list of ISO date strings (ascending).
    """
    try:
        end_dt = datetime.strptime(end_date_str, "%Y-%m-%d").date()
    except ValueError:
        return []

    earliest_limit = end_dt - timedelta(days=max_calendar_days)

    nonzero_set = set()
    cursor_end = end_dt

    while len(nonzero_set) < needed and cursor_end >= earliest_limit:
        cursor_start = cursor_end - timedelta(days=max_days_per_call - 1)
        if cursor_start < earliest_limit:
            cursor_start = earliest_limit

        b_from = cursor_start.isoformat()
        b_to = cursor_end.isoformat()

        try:
            if use_expired and expired_instrument_key:
                payload = fetch_candles_expired(
                    expired_instrument_key=expired_instrument_key,
                    interval=INTERVAL,
                    from_date=b_from,
                    to_date=b_to,
                    access_token=access_token,
                )
            else:
                payload = fetch_candles_normal(
                    instrument_key=instrument_key,
                    interval=INTERVAL,
                    from_date=b_from,
                    to_date=b_to,
                    access_token=access_token,
                )
        except InvalidInstrumentKeyError:
            raise
        except Exception as e:
            print(f"    Probe stopped at {b_from}..{b_to}: {e}")
            break

        candles = (payload.get("data") or {}).get("candles") or []
        for c in candles:
            if not isinstance(c, list) or not c:
                continue
            ts = c[0]
            # Upstox returns ISO like "2026-09-17T09:15:00+05:30"
            day = str(ts)[:10]
            if len(day) == 10 and day[4] == "-" and day[7] == "-":
                nonzero_set.add(day)

        time.sleep(SLEEP_BETWEEN_REQUESTS)
        cursor_end = cursor_start - timedelta(days=1)

    nonzero_days = sorted(nonzero_set)
    # Keep only the most recent `needed` days (ascending order).
    if len(nonzero_days) > needed:
        nonzero_days = nonzero_days[-needed:]
    return nonzero_days


def build_window_from_nonzero_days(nonzero_days):
    """
    Given a list of non-zero trading days (ascending), return (from_date, to_date)
    covering the whole span so batched requests can fetch all of them at once.
    """
    if not nonzero_days:
        return None, None
    return nonzero_days[0], nonzero_days[-1]


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    line = "=" * 70
    print(line)
    print("CONNECTING TO MONGODB")
    print(line)
    print(f"Database   : {DB_NAME}")
    print(f"Collection : {COLLECTION_NAME}")
    print(line)
    print()

    print(line)
    print("UPSTOX EXPIRED HISTORICAL CANDLE EXPORT")
    print(line)
    print(f"Database            : {DB_NAME}")
    print(f"Collection          : {COLLECTION_NAME}")
    print(f"Interval            : {INTERVAL}")
    print(f"Lookback (trading)  : {LOOKBACK_TRADING_DAYS} non-zero days")
    print(f"Max calendar scan   : {MAX_CALENDAR_LOOKBACK_DAYS} days")
    print(f"Max days per request: {MAX_DAYS_PER_REQUEST}")
    print(f"Output Folder       : {OUTPUT_FOLDER}")
    print(line)
    print()

    print("Loading Upstox access token...")
    access_token = get_upstox_access_token()
    print("Access token loaded successfully.")
    print()

    print("Loading isolated instruments from MongoDB...")
    instruments = load_instruments()
    print()

    if not instruments:
        print(line)
        print("PROCESS COMPLETED")
        print(line)
        print("Unique Instruments : 0")
        print("Successful         : 0")
        print("Skipped (0 candles): 0")
        print("Failed             : 0")
        print(f"Output Folder      : {OUTPUT_FOLDER}")
        print(line)
        return

    os.makedirs(OUTPUT_FOLDER, exist_ok=True)

    successful = 0
    skipped_zero = 0
    failed = 0
    recovered = 0

    print(line)
    print(f"Fetching candles for {len(instruments)} instrument(s)")
    print(line)

    # Sort instruments by their saved date so the serial number prefix
    # reflects chronological order of saving.
    items = sorted(
        instruments.items(),
        key=lambda kv: (kv[1]["end_date"], kv[0]),
    )
    total = len(items)

    # Serial counter — only incremented when a file is actually saved.
    serial_no = 0

    for idx, (instrument_key, meta) in enumerate(items, start=1):
        isolation_date = meta["end_date"]
        expiry = meta["expiry"]
        symbol = meta["trading_symbol"]

        print(f"[{idx}/{total}] {instrument_key}")
        print(f"    isolation date: {isolation_date}  expiry: {expiry}")

        # -----------------------------------------------------------------
        # STEP 1: discover the last N non-zero trading days using batched
        # requests. Try the NORMAL endpoint first. If the key is rejected
        # as invalid (expired contract), try the EXPIRED endpoint.
        # -----------------------------------------------------------------
        nonzero_days = []
        used_expired_probe = False
        expired_key_for_probe = None

        try:
            print(f"    Probing last {LOOKBACK_TRADING_DAYS} non-zero trading days (normal)...")
            nonzero_days = find_nonzero_trading_days_batched(
                instrument_key=instrument_key,
                end_date_str=isolation_date,
                access_token=access_token,
                needed=LOOKBACK_TRADING_DAYS,
                max_calendar_days=MAX_CALENDAR_LOOKBACK_DAYS,
                max_days_per_call=MAX_DAYS_PER_REQUEST,
                use_expired=False,
            )
        except InvalidInstrumentKeyError:
            used_expired_probe = True
            print("    Normal endpoint rejected the key (expired contract).")

        # If the normal probe yielded nothing and we have the metadata,
        # try the expired endpoint probe.
        if not nonzero_days and expiry and symbol:
            expired_key_for_probe = lookup_expired_instrument_key(
                underlying_key=UNDERLYING_KEY,
                expiry_date=expiry,
                trading_symbol=symbol,
                access_token=access_token,
            )
            if expired_key_for_probe:
                used_expired_probe = True
                print(f"    Probing via expired endpoint ({expired_key_for_probe})...")
                try:
                    nonzero_days = find_nonzero_trading_days_batched(
                        instrument_key=instrument_key,
                        end_date_str=isolation_date,
                        access_token=access_token,
                        needed=LOOKBACK_TRADING_DAYS,
                        max_calendar_days=MAX_CALENDAR_LOOKBACK_DAYS,
                        max_days_per_call=MAX_DAYS_PER_REQUEST,
                        use_expired=True,
                        expired_instrument_key=expired_key_for_probe,
                    )
                except Exception as e:
                    print(f"    Expired probe failed: {e}")

        if not nonzero_days:
            failed += 1
            print("    FAILED - could not determine any non-zero trading day")
            time.sleep(SLEEP_BETWEEN_REQUESTS)
            continue

        from_date, to_date = build_window_from_nonzero_days(nonzero_days)
        print(f"    Non-zero trading days found: {len(nonzero_days)}")
        print(f"    window: {from_date} -> {to_date}")

        # -----------------------------------------------------------------
        # STEP 2: fetch the full candle range for the computed window,
        # batched into <= 7 calendar-day requests.
        # -----------------------------------------------------------------
        payload = None
        n = 0
        needs_expired_fallback = used_expired_probe
        fallback_reason = "key rejected on normal probe" if used_expired_probe else ""

        if not used_expired_probe:
            try:
                payload = fetch_candles_batched_normal(
                    instrument_key=instrument_key,
                    interval=INTERVAL,
                    from_date=from_date,
                    to_date=to_date,
                    access_token=access_token,
                    max_days=MAX_DAYS_PER_REQUEST,
                )
                n = _count_candles(payload)
                if n == 0:
                    needs_expired_fallback = True
                    fallback_reason = "0 candles on normal endpoint for computed window"
            except InvalidInstrumentKeyError:
                needs_expired_fallback = True
                fallback_reason = "UDAPI100011 (invalid key on normal endpoint)"
            except Exception as e:
                msg = str(e)
                if "UDAPI" in msg or "HTTP 4" in msg:
                    needs_expired_fallback = True
                    fallback_reason = msg[:120]
                else:
                    failed += 1
                    print(f"    FAILED - {e}")
                    time.sleep(SLEEP_BETWEEN_REQUESTS)
                    continue

        # --- Attempt via expired endpoint if needed ---
        if needs_expired_fallback:
            if not expiry or not symbol:
                failed += 1
                print(
                    f"    FAILED - {fallback_reason}; "
                    f"missing expiry/trading_symbol to try expired fallback"
                )
                time.sleep(SLEEP_BETWEEN_REQUESTS)
                continue

            expired_key = expired_key_for_probe
            if not expired_key:
                print(f"    {fallback_reason}. Looking up expired contract...")
                expired_key = lookup_expired_instrument_key(
                    underlying_key=UNDERLYING_KEY,
                    expiry_date=expiry,
                    trading_symbol=symbol,
                    access_token=access_token,
                )

            if not expired_key:
                failed += 1
                print(f"    FAILED - no matching expired contract for '{symbol}' @ {expiry}")
                time.sleep(SLEEP_BETWEEN_REQUESTS)
                continue

            print(f"    Using expired key: {expired_key}")

            try:
                payload = fetch_candles_batched_expired(
                    expired_instrument_key=expired_key,
                    interval=INTERVAL,
                    from_date=from_date,
                    to_date=to_date,
                    access_token=access_token,
                    max_days=MAX_DAYS_PER_REQUEST,
                )
                recovered += 1
                n = _count_candles(payload)
            except Exception as e:
                failed += 1
                print(f"    FAILED on expired endpoint - {e}")
                time.sleep(SLEEP_BETWEEN_REQUESTS)
                continue

        # -----------------------------------------------------------------
        # STEP 3: save if candles exist.
        # -----------------------------------------------------------------
        if n == 0:
            skipped_zero += 1
            print("    SKIPPED - 0 candles (no file written)")
        else:
            serial_no += 1
            base_name = safe_filename(symbol, instrument_key, expiry)
            filename_base = f"{serial_no}_{base_name}"

            fpath = save_candles(filename_base, payload, OUTPUT_FOLDER)
            successful += 1
            print(f"    OK - saved {n} candles -> {fpath}")

        time.sleep(SLEEP_BETWEEN_REQUESTS)

    print()
    print(line)
    print("PROCESS COMPLETED")
    print(line)
    print(f"Unique Instruments : {len(instruments)}")
    print(f"Successful         : {successful}")
    print(f"Recovered (expired): {recovered}")
    print(f"Skipped (0 candles): {skipped_zero}")
    print(f"Failed             : {failed}")
    print(f"Output Folder      : {OUTPUT_FOLDER}")
    print(line)


if __name__ == "__main__":
    main()