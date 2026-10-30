# routes/upstox_proxy.py
"""Proxy routes for Upstox API.

These endpoints forward the request to Upstox with the stored access
token and return the JSON body — no candle processing, no token
exposure.

Selector values are normalized on the way in so callers can use any
case / spacing / alias form.

    GET  /api/upstox/token                       -> masked token info
    GET  /api/upstox/search                      -> instrument search
    GET  /api/upstox/instrument                  -> lookup by key
    GET  /api/upstox/intraday                    -> intraday 1-min (single)
    GET  /api/upstox/historical                  -> historical 1-min (single)
    GET  /api/upstox/intraday/multi              -> intraday candles for N keys
    GET  /api/upstox/historical/multi            -> merged historical candles for N keys
    GET  /api/upstox/subscription-status         -> registry check
    POST /api/upstox/subscribe                   -> on-demand subscribe

Multi-instrument routes accept either repeated query params or a
comma-separated list:

    ?instrument_keys=NSE_EQ|INE002A01018&instrument_keys=NSE_FO|12345
    ?instrument_keys=NSE_EQ|INE002A01018,NSE_FO|12345

They always respond with an object keyed by instrument key:

    {
      "status": "success",
      "count": 2,
      "data": {
        "NSE_EQ|INE002A01018": {"status": "success", "candle_count": 375, "candles": [...]},
        "NSE_FO|12345":        {"status": "error", "status_code": 400,
                                "detail": "...", "candles": []}
      }
    }

A failure for one instrument never aborts the whole batch; it is
reported inside that instrument's own entry.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from typing import Any

import requests
from fastapi import APIRouter, HTTPException, Query

from services.instrument_registry import instrument_registry
from services.live_feed_service import live_feed_service
from services.normalization import (
    normalize_strike,
    normalize_strike_type,
    normalize_underlying,
)
from services.token_service import token_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/upstox", tags=["upstox"])

_UPSTOX_BASE = "https://api.upstox.com"
_TIMEOUT = 30

# --- multi-instrument tuning ------------------------------------------------
_MAX_INSTRUMENTS_PER_REQUEST = 25
_MAX_WORKERS = 8
# Upstox caps the lookback per request, so long ranges are split into
# windows and the candles are merged back together.
_HISTORICAL_CHUNK_DAYS: dict[str, int] = {"minutes": 30, "hours": 90}
_MAX_CHUNKS_PER_INSTRUMENT = 40
_DEFAULT_HISTORICAL_DAYS = 30


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _auth_headers() -> dict[str, str]:
    token = token_service.get_access_token()
    if not token:
        raise HTTPException(
            status_code=503,
            detail="Upstox access token not available",
        )
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }


def _upstox_get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    url = f"{_UPSTOX_BASE}{path}"
    try:
        resp = requests.get(
            url,
            headers=_auth_headers(),
            params=params or {},
            timeout=_TIMEOUT,
        )
    except requests.RequestException as exc:
        logger.exception("Upstox proxy request failed: %s", url)
        raise HTTPException(
            status_code=502, detail=f"Upstox request failed: {exc}"
        )

    if resp.status_code >= 400:
        logger.warning(
            "Upstox returned %s for %s: %s",
            resp.status_code, url, resp.text[:300],
        )
        raise HTTPException(
            status_code=resp.status_code, detail=resp.text[:500]
        )

    try:
        return resp.json()
    except ValueError:
        raise HTTPException(
            status_code=502,
            detail="Upstox returned non-JSON response",
        )


# ---------------------------------------------------------------------------
# Multi-instrument helpers
# ---------------------------------------------------------------------------
def _collect_instrument_keys(raw_keys: list[str]) -> list[str]:
    """Flatten repeated params + comma-separated values, preserving order."""
    ordered: dict[str, None] = {}
    for raw in raw_keys:
        for part in str(raw).split(","):
            key = part.strip()
            if key:
                ordered.setdefault(key, None)
    return list(ordered)


def _validate_instrument_keys(raw_keys: list[str]) -> list[str]:
    keys = _collect_instrument_keys(raw_keys)
    if not keys:
        raise HTTPException(
            status_code=400,
            detail="At least one instrument_key is required",
        )
    if len(keys) > _MAX_INSTRUMENTS_PER_REQUEST:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Too many instrument keys ({len(keys)}); "
                f"max {_MAX_INSTRUMENTS_PER_REQUEST} per request"
            ),
        )
    too_short = [k for k in keys if len(k) < 3]
    if too_short:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid instrument_key(s): {', '.join(too_short)}",
        )
    return keys


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid date {value!r}; expected YYYY-MM-DD",
        )


def _extract_candles(payload: dict[str, Any]) -> list[list[Any]]:
    """Pull the raw candle rows out of an Upstox candles response."""
    data = payload.get("data")
    if not isinstance(data, dict):
        return []
    candles = data.get("candles")
    return candles if isinstance(candles, list) else []


def _merge_candles(batches: list[list[Any]]) -> list[list[Any]]:
    """Merge candle batches, de-duplicating by timestamp and sorting asc.

    Upstox timestamps are ISO-8601 strings with a fixed offset, so plain
    string ordering matches chronological ordering.
    """
    merged: dict[str, list[Any]] = {}
    for candles in batches:
        for candle in candles:
            if not isinstance(candle, (list, tuple)) or not candle:
                continue
            merged[str(candle[0])] = list(candle)
    return [merged[ts] for ts in sorted(merged)]


def _error_result(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, HTTPException):
        return {
            "status": "error",
            "status_code": exc.status_code,
            "detail": exc.detail,
            "candles": [],
            "candle_count": 0,
        }
    logger.exception("Batch instrument fetch failed")
    return {
        "status": "error",
        "status_code": 502,
        "detail": str(exc),
        "candles": [],
        "candle_count": 0,
    }


def _fetch_intraday_batch(
    instrument_key: str, interval: str, unit: str
) -> dict[str, Any]:
    path = f"/v3/historical-candle/intraday/{instrument_key}/{unit}/{interval}"
    try:
        payload = _upstox_get(path)
    except Exception as exc:  # noqa: BLE001 - isolated per instrument
        return _error_result(exc)

    candles = _extract_candles(payload)
    return {
        "status": "success",
        "candle_count": len(candles),
        "candles": candles,
    }


def _historical_chunks(
    from_date: date, to_date: date, unit: str
) -> list[tuple[str, str]]:
    """Split the range into (to_date, from_date) windows Upstox accepts."""
    window = _HISTORICAL_CHUNK_DAYS.get(unit.lower())
    if not window:
        return [(to_date.isoformat(), from_date.isoformat())]

    chunks: list[tuple[str, str]] = []
    end = to_date
    while end >= from_date and len(chunks) < _MAX_CHUNKS_PER_INSTRUMENT:
        start = max(from_date, end - timedelta(days=window - 1))
        chunks.append((end.isoformat(), start.isoformat()))
        end = start - timedelta(days=1)
    chunks.reverse()
    return chunks


def _fetch_historical_batch(
    instrument_key: str,
    interval: str,
    unit: str,
    chunks: list[tuple[str, str]],
) -> dict[str, Any]:
    batches: list[list[Any]] = []
    errors: list[dict[str, Any]] = []

    for to_d, from_d in chunks:
        path = (
            f"/v3/historical-candle/{instrument_key}/{unit}/{interval}"
            f"/{to_d}/{from_d}"
        )
        try:
            batches.append(_extract_candles(_upstox_get(path)))
        except Exception as exc:  # noqa: BLE001 - one bad window is survivable
            if isinstance(exc, HTTPException):
                errors.append(
                    {"status_code": exc.status_code, "detail": exc.detail}
                )
            else:
                logger.exception(
                    "Historical window failed for %s (%s..%s)",
                    instrument_key, from_d, to_d,
                )
                errors.append({"status_code": 502, "detail": str(exc)})

    candles = _merge_candles(batches)

    if not errors:
        status = "success"
    elif candles:
        status = "partial"
    else:
        status = "error"

    result: dict[str, Any] = {
        "status": status,
        "candle_count": len(candles),
        "chunks_requested": len(chunks),
        "chunks_failed": len(errors),
        "candles": candles,
    }
    if errors:
        result["errors"] = errors
    return result


def _run_batch(fetcher, keys: list[str]) -> dict[str, Any]:
    """Run ``fetcher`` per key in a small pool, keeping request order."""
    results: dict[str, Any] = {}
    with ThreadPoolExecutor(max_workers=min(_MAX_WORKERS, len(keys))) as pool:
        futures = {key: pool.submit(fetcher, key) for key in keys}
        for key in keys:
            future = futures[key]
            try:
                results[key] = future.result()
            except Exception as exc:  # noqa: BLE001 - defensive
                results[key] = _error_result(exc)
    return results


# ---------------------------------------------------------------------------
# Token info (never raw)
# ---------------------------------------------------------------------------
@router.get("/token")
def token_info() -> dict[str, Any]:
    token = token_service.get_access_token()
    return {
        "available": bool(token),
        "masked": (token[:6] + "…" + token[-4:]) if token else None,
    }


# ---------------------------------------------------------------------------
# Instrument search
# ---------------------------------------------------------------------------
@router.get("/search")
def search_instruments(
    query: str = Query(..., min_length=1, max_length=50),
    exchanges: str | None = Query(default=None),
    segments: str | None = Query(default=None),
    instrument_types: str | None = Query(default=None),
    expiry: str | None = Query(default=None),
    atm_offset: int | None = Query(default=None),
    page_number: int = Query(default=1, ge=1),
    records: int = Query(default=20, ge=1, le=30),
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "query": query,
        "page_number": page_number,
        "records": records,
    }
    if exchanges:
        params["exchanges"] = exchanges
    if segments:
        params["segments"] = segments
    if instrument_types:
        # Normalize so "ce", "call" both land as "CE" upstream.
        params["instrument_types"] = (
            normalize_strike_type(instrument_types) or instrument_types
        )
    if expiry:
        params["expiry"] = expiry
    if atm_offset is not None:
        params["atm_offset"] = atm_offset

    return _upstox_get("/v2/instruments/search", params=params)


@router.get("/instrument")
def lookup_instrument(
    instrument_key: str = Query(..., min_length=3),
) -> dict[str, Any]:
    """Return metadata for one instrument by exact key."""
    entry = instrument_registry.get(instrument_key)
    if entry:
        return {
            "status": "success",
            "source": "registry",
            "data": {
                "instrument_key": entry["instrument_key"],
                "trading_symbol": entry.get("trading_symbol"),
                "underlying_symbol":
                    normalize_underlying(entry.get("underlying")) or
                    entry.get("underlying"),
                "strike_price": normalize_strike(entry.get("strike_price")) or
                    entry.get("strike_price"),
                "instrument_type":
                    normalize_strike_type(entry.get("strike_type")) or
                    entry.get("strike_type"),
                "expiry": entry.get("expiry"),
                "segment": (
                    "NSE_FO" if not entry.get("is_index") else "NSE_INDEX"
                ),
                "exchange": "NSE",
            },
        }

    local = instrument_key.split("|", 1)[-1]
    data = _upstox_get(
        "/v2/instruments/search",
        params={"query": local, "records": 30, "page_number": 1},
    )
    for item in data.get("data", []):
        if item.get("instrument_key") == instrument_key:
            return {"status": "success", "source": "upstox", "data": item}
    return {"status": "not_found", "data": None}


# ---------------------------------------------------------------------------
# Candles (proxy only — no processing) — single instrument
# ---------------------------------------------------------------------------
@router.get("/intraday")
def intraday_candles(
    instrument_key: str = Query(..., min_length=3),
    interval: str = Query(default="1"),
    unit: str = Query(default="minutes"),
) -> dict[str, Any]:
    path = f"/v3/historical-candle/intraday/{instrument_key}/{unit}/{interval}"
    return _upstox_get(path)


@router.get("/historical")
def historical_candles(
    instrument_key: str = Query(..., min_length=3),
    interval: str = Query(default="1"),
    unit: str = Query(default="minutes"),
    from_date: str | None = Query(default=None),
    to_date: str | None = Query(default=None),
) -> dict[str, Any]:
    if not to_date:
        to_date = date.today().isoformat()
    if not from_date:
        from_date = (date.today() - timedelta(days=30)).isoformat()
    path = (
        f"/v3/historical-candle/{instrument_key}/{unit}/{interval}"
        f"/{to_date}/{from_date}"
    )
    return _upstox_get(path)


# ---------------------------------------------------------------------------
# Candles — multiple instruments
# ---------------------------------------------------------------------------
@router.get("/intraday/multi")
def intraday_candles_multi(
    instrument_keys: list[str] = Query(
        ...,
        description=(
            "Instrument keys. Repeat the param or pass a comma-separated "
            "list, e.g. ?instrument_keys=A,B,C"
        ),
    ),
    interval: str = Query(default="1"),
    unit: str = Query(default="minutes"),
) -> dict[str, Any]:
    """Intraday candles for several instruments in one call.

    Response shape: ``data[<instrument_key>] = {status, candle_count, candles}``.
    """
    keys = _validate_instrument_keys(instrument_keys)

    data = _run_batch(
        lambda key: _fetch_intraday_batch(key, interval, unit),
        keys,
    )

    failed = sum(1 for entry in data.values() if entry.get("status") == "error")
    return {
        "status": "success" if failed == 0 else ("partial" if failed < len(keys) else "error"),
        "count": len(keys),
        "failed": failed,
        "interval": interval,
        "unit": unit,
        "data": data,
    }


@router.get("/historical/multi")
def historical_candles_multi(
    instrument_keys: list[str] = Query(
        ...,
        description=(
            "Instrument keys. Repeat the param or pass a comma-separated "
            "list, e.g. ?instrument_keys=A,B,C"
        ),
    ),
    interval: str = Query(default="1"),
    unit: str = Query(default="minutes"),
    from_date: str | None = Query(default=None),
    to_date: str | None = Query(default=None),
) -> dict[str, Any]:
    """Merged historical candles for several instruments in one call.

    Long ranges are split into Upstox-acceptable windows, fetched per
    instrument, then merged, de-duplicated by timestamp and sorted
    ascending. Response shape:
    ``data[<instrument_key>] = {status, candle_count, candles, ...}``.
    """
    keys = _validate_instrument_keys(instrument_keys)

    resolved_to = _parse_date(to_date) or date.today()
    resolved_from = _parse_date(from_date) or (
        resolved_to - timedelta(days=_DEFAULT_HISTORICAL_DAYS)
    )
    if resolved_from > resolved_to:
        raise HTTPException(
            status_code=400,
            detail="from_date must be on or before to_date",
        )

    chunks = _historical_chunks(resolved_from, resolved_to, unit)

    data = _run_batch(
        lambda key: _fetch_historical_batch(key, interval, unit, chunks),
        keys,
    )

    failed = sum(1 for entry in data.values() if entry.get("status") == "error")
    return {
        "status": "success" if failed == 0 else ("partial" if failed < len(keys) else "error"),
        "count": len(keys),
        "failed": failed,
        "interval": interval,
        "unit": unit,
        "from_date": resolved_from.isoformat(),
        "to_date": resolved_to.isoformat(),
        "chunks": len(chunks),
        "data": data,
    }

# ---------------------------------------------------------------------------
# Live feed subscription
# ---------------------------------------------------------------------------
@router.get("/subscription-status")
def subscription_status(
    instrument_key: str = Query(..., min_length=3),
) -> dict[str, Any]:
    entry = instrument_registry.get(instrument_key)
    if entry is None:
        return {"subscribed": False, "in_registry": False}
    return {
        "subscribed": True,
        "in_registry": True,
        "underlying": normalize_underlying(entry.get("underlying"))
            or entry.get("underlying"),
        "strike_price": entry.get("strike_price"),
        "strike_type": normalize_strike_type(entry.get("strike_type"))
            or entry.get("strike_type"),
        "trading_symbol": entry.get("trading_symbol"),
    }

@router.post("/subscribe")
def subscribe_instrument(payload: dict[str, Any]) -> dict[str, Any]:
    """Request on-demand subscription to the live feed.

    Pins the key in the feed-subscription manager's LRU so it is
    protected from eviction until the LRU is drained, then ensures the
    streamer is actually subscribed to it.
    """
    instrument_key = str(payload.get("instrument_key") or "").strip()
    if not instrument_key:
        raise HTTPException(
            status_code=400, detail="instrument_key is required"
        )

    try:
        from services.feed_subscription_manager import (
            feed_subscription_manager,
        )
        feed_subscription_manager.pin(instrument_key)
    except Exception:
        logger.exception(
            "[subscribe] feed-sub manager pin failed for %s",
            instrument_key,
        )

    ok = False
    try:
        ok = live_feed_service.subscribe_instrument(instrument_key)
    except Exception:
        logger.exception(
            "Dynamic subscribe failed for %s", instrument_key,
        )

    try:
        from core import config as _cfg
        max_total = int(
            getattr(_cfg, "LIVE_FEED_MAX_TOTAL_INSTRUMENTS", 1800)
        )
    except Exception:
        max_total = None

    return {
        "subscribed": ok,
        "instrument_key": instrument_key,
        "max_total_instruments": max_total,
        "currently_subscribed": (
            live_feed_service.status().get("instrument_count")
        ),
    }