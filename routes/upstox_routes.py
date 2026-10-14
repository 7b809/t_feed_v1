"""Proxy routes for Upstox API.

These endpoints do NOT process candles — they simply forward the request
to Upstox with the stored access token and return the JSON body. This
avoids exposing the token to the browser and sidesteps CORS issues.

    GET  /api/upstox/token                       -> masked token info (never raw)
    GET  /api/upstox/search                      -> instrument search
    GET  /api/upstox/instrument                  -> single instrument lookup by key
    GET  /api/upstox/intraday                    -> intraday 1-min candles
    GET  /api/upstox/historical                  -> historical 1-min candles
    GET  /api/upstox/subscription-status         -> is the key in the live registry?
    POST /api/upstox/subscribe                   -> subscribe on-demand
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

import requests
from fastapi import APIRouter, HTTPException, Query

from core import config
from services.instrument_registry import instrument_registry
from services.live_feed_service import live_feed_service
from services.token_service import token_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/upstox", tags=["upstox"])

_UPSTOX_BASE = "https://api.upstox.com"
_TIMEOUT = 30


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _auth_headers() -> dict[str, str]:
    token = token_service.get_access_token()
    if not token:
        raise HTTPException(status_code=503, detail="Upstox access token not available")
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }


def _upstox_get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    url = f"{_UPSTOX_BASE}{path}"
    try:
        resp = requests.get(url, headers=_auth_headers(), params=params or {}, timeout=_TIMEOUT)
    except requests.RequestException as exc:
        logger.exception("Upstox proxy request failed: %s", url)
        raise HTTPException(status_code=502, detail=f"Upstox request failed: {exc}")

    if resp.status_code >= 400:
        logger.warning("Upstox returned %s for %s: %s", resp.status_code, url, resp.text[:300])
        raise HTTPException(status_code=resp.status_code, detail=resp.text[:500])

    try:
        return resp.json()
    except ValueError:
        raise HTTPException(status_code=502, detail="Upstox returned non-JSON response")


# ---------------------------------------------------------------------------
# Token info (never raw)
# ---------------------------------------------------------------------------

@router.get("/token")
def token_info() -> dict[str, Any]:
    """Return a masked token indicator — the raw token is never exposed."""
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
    if exchanges:         params["exchanges"] = exchanges
    if segments:          params["segments"] = segments
    if instrument_types:  params["instrument_types"] = instrument_types
    if expiry:            params["expiry"] = expiry
    if atm_offset is not None: params["atm_offset"] = atm_offset

    return _upstox_get("/v2/instruments/search", params=params)


@router.get("/instrument")
def lookup_instrument(
    instrument_key: str = Query(..., min_length=3),
) -> dict[str, Any]:
    """Return metadata for one instrument by exact key.

    Reuses the search endpoint with a targeted filter — the response is
    cached by the browser for a short period.
    """
    # Try the registry first — it's the fastest source for known keys.
    entry = instrument_registry.get(instrument_key)
    if entry:
        return {
            "status": "success",
            "source": "registry",
            "data": {
                "instrument_key": entry["instrument_key"],
                "trading_symbol": entry.get("trading_symbol"),
                "underlying_symbol": entry.get("underlying"),
                "strike_price": entry.get("strike_price"),
                "instrument_type": entry.get("strike_type"),
                "expiry": entry.get("expiry"),
                "segment": "NSE_FO" if not entry.get("is_index") else "NSE_INDEX",
                "exchange": "NSE",
            },
        }

    # Fall back to the search endpoint using the local part of the key.
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
# Candles (proxy only — no processing)
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
        "underlying": entry.get("underlying"),
        "strike_price": entry.get("strike_price"),
        "strike_type": entry.get("strike_type"),
        "trading_symbol": entry.get("trading_symbol"),
    }


@router.post("/subscribe")
def subscribe_instrument(payload: dict[str, Any]) -> dict[str, Any]:
    """Request on-demand subscription to the live feed."""
    instrument_key = str(payload.get("instrument_key") or "").strip()
    if not instrument_key:
        raise HTTPException(status_code=400, detail="instrument_key is required")

    ok = False
    try:
        ok = live_feed_service.subscribe_instrument(instrument_key)
    except Exception:
        logger.exception("Dynamic subscribe failed for %s", instrument_key)

    return {"subscribed": ok, "instrument_key": instrument_key}