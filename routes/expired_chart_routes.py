# routes/expired_chart_routes.py
"""Expired / Active instrument chart page + data endpoints.

Routes
------
    GET  /expired-chart                            -> HTML page (empty state)
    GET  /expired-chart/{instrument_key:path}      -> HTML page with key

    GET  /api/chart2/candles                       -> unified candles + meta
    GET  /9:17                                     -> legacy alias for the
                                                     same handler (kept so
                                                     existing callers still
                                                     resolve; the frontend
                                                     uses the /api/... form)

    GET  /api/chart2/search                        -> one-shot search (active)
    GET  /api/chart2/expired/expiries              -> expired expiries
    GET  /api/chart2/expired/contracts             -> expired contracts

Candle ordering
---------------
Upstox's **expired** historical candle endpoint returns candles in
**descending** (newest-first) order. The active HistoryV3Api endpoint
returns them **ascending** (oldest-first). The LWC frontend absolutely
requires ascending order — feeding descending data into
``series.setData()`` does not throw at set-time, but poisons the
series' internal lookup index so every subsequent chart render
(``updateAllViews``) throws ``Value is null``.

To eliminate that class of bug once and for all, every candle list
that leaves this module is routed through ``_sort_candles_ascending``,
which is idempotent and safe for all the Upstox payload shapes we
receive (list-of-lists). This is the authoritative sort — the frontend
sorts again as belt-and-suspenders, but a future caller that bypasses
the frontend (curl, test) will still get ascending data.

Trading-symbol enrichment (expired path)
----------------------------------------
The expired historical-candles endpoint returns only OHLCV rows and a
minimal meta dict whose ``trading_symbol`` is just the raw
``NSE_FO|<token>|<DD-MM-YYYY>`` key. Callers (charts, exports, strategy
backtests) want the human-readable symbol — e.g.
``"NIFTY 22750 CE 27 OCT 26"`` — plus strike / option-type / lot-size /
tick-size from the contract registry.

``_build_expired_meta`` therefore performs a best-effort lookup against
``expired_instruments_service``:

    1. Direct method (``get_contract_by_key`` / ``get_contract_meta`` /
       ``get_contract``) if the service exposes one.
    2. Any dict-shaped cache on the service (``_contracts_cache``,
       ``cache``, ``_by_key``, ...).
    3. Fallback: fetch ``get_expired_option_contracts`` for every known
       index whose segment matches the instrument key's segment
       (NSE_FO → NSE_INDEX|*, BSE_FO → BSE_INDEX|*), then pick the
       matching contract by ``instrument_key``.

Successful resolutions are memoised in ``_expired_meta_cache`` so
repeated chart loads for the same key do not re-hit the service.
Failures are *not* cached, so a transient upstream error will be
retried on the next request.

Security
--------
The access token is read by the *existing* services
(``services.token_service`` for active, ``upstox_app.services
.expired_instruments_service`` for expired). It never appears in any
log line or response payload produced here.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import requests
from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from core.logger import get_logger
from services.token_service import token_service
from upstox_app.services.expired_instruments_service import (
    expired_instruments_service,
)

logger = get_logger(__file__)

router = APIRouter(tags=["expired-chart"])

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
templates = Jinja2Templates(directory=str(_PROJECT_ROOT / "templates"))

_UPSTOX_BASE = "https://api.upstox.com"
_HTTP_TIMEOUT = 30

_KNOWN_INDEXES = [
    {"key": "NSE_INDEX|Nifty 50",           "label": "NIFTY 50"},
    {"key": "NSE_INDEX|Nifty Bank",         "label": "BANKNIFTY"},
    {"key": "NSE_INDEX|NIFTY MID SELECT",   "label": "MIDCPNIFTY"},
    {"key": "NSE_INDEX|NIFTY FIN SERVICE",  "label": "FINNIFTY"},
    {"key": "BSE_INDEX|SENSEX",             "label": "SENSEX"},
]

# Per-process memo: instrument_key -> resolved meta dict.  Only
# successful resolutions are stored so transient failures retry.
_expired_meta_cache: dict[str, dict[str, Any]] = {}


# --------------------------------------------------------------------------- #
# Small helpers                                                               #
# --------------------------------------------------------------------------- #
def _is_expired_key(key: str) -> bool:
    """True when ``key`` has >=3 pipe-separated parts (expired format)."""
    return len([p for p in str(key or "").split("|") if p.strip()]) >= 3


def _extract_expiry_from_key(key: str) -> str | None:
    """Return ``YYYY-MM-DD`` from ``NSE_FO|<token>|<DD-MM-YYYY>`` (or None)."""
    parts = str(key or "").split("|")
    if len(parts) < 3:
        return None
    try:
        dd, mm, yyyy = parts[2].strip().split("-")
        return f"{yyyy}-{mm}-{dd}"
    except Exception:
        return None


def _safe_dict(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if hasattr(raw, "to_dict"):
        try:
            d = raw.to_dict()
            if isinstance(d, dict):
                return d
        except Exception:
            pass
    try:
        return dict(raw)
    except Exception:
        return {}


def _sort_candles_ascending(candles: Any) -> list:
    """Return ``candles`` sorted ascending by their timestamp.

    Upstox candle arrays are ``[timestamp_iso, o, h, l, c, v, oi]``. The
    ISO strings sort lexicographically in chronological order as long
    as they are all in the same timezone format (which Upstox always
    guarantees). We still try a numeric parse as a fallback for any
    future payload shape that returns epoch-millis instead.

    Idempotent — safe to call on already-sorted data.
    """
    if not isinstance(candles, list):
        return []

    def _key(c: Any) -> Any:
        if not isinstance(c, list) or not c:
            return ""
        ts = c[0]
        # Numeric (epoch millis / seconds) sorts numerically
        try:
            n = float(ts)
            if n > 1e12:
                n /= 1000.0
            if n > 1e9:
                return n
        except (TypeError, ValueError):
            pass
        # Otherwise sort lexicographically as ISO strings
        return str(ts)

    try:
        return sorted(candles, key=_key)
    except Exception:
        logger.exception("[chart2] candle sort failed; returning as-is")
        return candles


def _extract_contracts(resp: Any) -> list:
    """Normalise an expired-contracts response to a flat list.

    The service can return a list, or a dict with ``data`` /
    ``contracts`` / ``instruments`` / ``items`` at either the top level
    or nested one level deep. Rather than guess, try all the shapes we
    have seen in the wild.
    """
    if isinstance(resp, list):
        return resp
    if not isinstance(resp, dict):
        return []
    for top in ("data", "contracts", "instruments", "items"):
        val = resp.get(top)
        if isinstance(val, list):
            return val
        if isinstance(val, dict):
            for sub in ("contracts", "instruments", "items"):
                subval = val.get(sub)
                if isinstance(subval, list):
                    return subval
    return []


def _index_candidates_for_segment(segment: str) -> list[dict[str, str]]:
    """Return the indexes whose exchange matches ``segment``'s exchange."""
    if segment.startswith("BSE"):
        return [i for i in _KNOWN_INDEXES if i["key"].startswith("BSE_INDEX")]
    if segment.startswith("NSE"):
        return [i for i in _KNOWN_INDEXES if i["key"].startswith("NSE_INDEX")]
    return list(_KNOWN_INDEXES)


# --------------------------------------------------------------------------- #
# HTML page routes                                                            #
# --------------------------------------------------------------------------- #
@router.get("/expired-chart", response_class=HTMLResponse)
def expired_chart_page_empty(request: Request) -> HTMLResponse:
    """Render the chart page with no instrument key — shows empty state."""
    return templates.TemplateResponse(
        request=request,
        name="expired_chart.html",
        context={
            "app_name":       "Market Data Foundation",
            "instrument_key": "",
            "known_indexes":  _KNOWN_INDEXES,
        },
    )


@router.get("/expired-chart/{instrument_key:path}", response_class=HTMLResponse)
def expired_chart_page(request: Request, instrument_key: str) -> HTMLResponse:
    """Render the chart page with a pre-set instrument key."""
    return templates.TemplateResponse(
        request=request,
        name="expired_chart.html",
        context={
            "app_name":       "Market Data Foundation",
            "instrument_key": instrument_key,
            "known_indexes":  _KNOWN_INDEXES,
        },
    )


# --------------------------------------------------------------------------- #
# Data: unified candles + meta                                                #
# --------------------------------------------------------------------------- #
# Both paths resolve to the same handler:
#   /api/chart2/candles  — canonical path used by the frontend
#   /9:17                — legacy alias kept for backward compatibility
@router.get("/api/chart2/candles")
@router.get("/9:17")
async def chart2_candles(
    instrument_key: str = Query(..., min_length=3),
    from_date: str | None = Query(default=None),
    to_date: str | None = Query(default=None),
    interval: str = Query(default="1minute"),
) -> dict[str, Any]:
    """Return candles + best-effort meta for one instrument.

    * **Expired** key  → ``/upstox-app/expired/candles``
                         default window ``[expiry - 7d, expiry]``
    * **Active**  key  → ``/api/upstox/historical``
                         default window ``[today - 10d, today]``

    Candles are always returned in **ascending** order (see module
    docstring — the expired endpoint returns descending by default,
    which poisons Lightweight Charts).
    """
    key = str(instrument_key or "").strip()
    if not key:
        return {"status": "invalid_input", "error": "instrument_key required"}

    today = date.today()

    # ---- EXPIRED PATH ----------------------------------------------- #
    if _is_expired_key(key):
        expiry = _extract_expiry_from_key(key)
        if not expiry:
            return {
                "status": "invalid_input",
                "error": "cannot parse expiry from key (expected DD-MM-YYYY)",
                "instrument_key": key,
            }
        try:
            expiry_dt = datetime.strptime(expiry, "%Y-%m-%d").date()
        except ValueError:
            return {
                "status": "invalid_input",
                "error": f"bad expiry parsed from key: {expiry}",
                "instrument_key": key,
            }

        fd = from_date or (expiry_dt - timedelta(days=7)).isoformat()
        td = to_date or expiry

        expired_interval = "1minute"
        if interval in ("1", "1min", "1minute"):
            expired_interval = "1minute"
        elif interval:
            expired_interval = interval

        result = await asyncio.to_thread(
            expired_instruments_service.get_expired_historical_candles,
            key, expired_interval, fd, td,
        )
        # CRITICAL: sort ascending — the expired endpoint returns
        # newest-first, which crashes Lightweight Charts.
        candles = _sort_candles_ascending(result.get("candles") or [])
        meta = await _build_expired_meta(key, expiry)

        return {
            "status":         "ok" if result.get("status") == "ok" else result.get("status"),
            "mode":           "expired",
            "instrument_key": key,
            "expiry":         expiry,
            "from_date":      fd,
            "to_date":        td,
            "interval":       expired_interval,
            "candles":        candles,
            "count":          len(candles),
            "meta":           meta,
            "error":          result.get("error"),
        }

    # ---- ACTIVE PATH ------------------------------------------------ #
    fd = from_date or (today - timedelta(days=10)).isoformat()
    td = to_date or today.isoformat()

    try:
        token = token_service.get_access_token()
    except Exception:
        token = None
    if not token:
        return {
            "status": "no_token",
            "error":  "access_token_missing",
            "mode":   "active",
            "instrument_key": key,
        }

    url = (
        f"{_UPSTOX_BASE}/v3/historical-candle/"
        f"{key}/minutes/1/{td}/{fd}"
    )
    try:
        resp = await asyncio.to_thread(
            requests.get, url,
            headers={
                "Accept":        "application/json",
                "Authorization": f"Bearer {token}",
            },
            timeout=_HTTP_TIMEOUT,
        )
    except requests.RequestException as e:
        return {
            "status": "error",
            "error":  f"{type(e).__name__}: {e}",
            "mode":   "active",
            "instrument_key": key,
        }
    if resp.status_code >= 400:
        return {
            "status": "error",
            "error":  f"Upstox HTTP {resp.status_code}: {resp.text[:200]}",
            "mode":   "active",
            "instrument_key": key,
        }
    try:
        body = resp.json()
    except ValueError:
        return {
            "status": "error",
            "error":  "Upstox returned non-JSON",
            "mode":   "active",
            "instrument_key": key,
        }

    candles = _sort_candles_ascending(
        ((body.get("data") or {}).get("candles")) or []
    )

    meta: dict[str, Any] = {}
    try:
        from services.instrument_registry import instrument_registry
        entry = instrument_registry.get(key)
        if entry:
            meta = entry
    except Exception:
        pass

    return {
        "status":         "ok",
        "mode":           "active",
        "instrument_key": key,
        "from_date":      fd,
        "to_date":        td,
        "interval":       "1minute",
        "candles":        candles,
        "count":          len(candles),
        "meta":           meta,
        "error":          None,
    }


# --------------------------------------------------------------------------- #
# Expired meta resolution                                                     #
# --------------------------------------------------------------------------- #
async def _resolve_contract_meta(
    key: str, expiry: str,
) -> dict[str, Any] | None:
    """Best-effort lookup of full contract meta by instrument key.

    Tries, in order:

    1. A direct method on ``expired_instruments_service`` named
       ``get_contract_by_key`` / ``get_contract_meta`` / ``get_contract``.
    2. Any dict-shaped cache attribute on the service
       (``_contracts_cache``, ``contracts_cache``, ``cache``, ...).
    3. ``get_expired_option_contracts(index_key, expiry)`` for every
       known index matching the key's exchange segment, then matches on
       ``instrument_key``.

    Returns the resolved contract dict (which may contain
    ``trading_symbol``, ``strike_price``, ``instrument_type``,
    ``lot_size``, ``tick_size``, ``underlying_symbol``, ...) or
    ``None`` when nothing could be resolved.
    """
    # ---- Strategy 1: direct method on the service ------------------ #
    for method_name in (
        "get_contract_by_key",
        "get_contract_meta",
        "get_contract",
    ):
        method = getattr(expired_instruments_service, method_name, None)
        if not callable(method):
            continue
        try:
            result = await asyncio.to_thread(method, key)
        except Exception:
            logger.debug(
                "[chart2] %s failed for %s", method_name, key, exc_info=True
            )
            continue
        d = _safe_dict(result)
        # Only accept if the returned object is clearly for *this* key
        if d and (d.get("instrument_key") in (None, key)):
            return d

    # ---- Strategy 2: any dict-shaped cache on the service ---------- #
    for attr in (
        "_contracts_cache", "contracts_cache",
        "_cache",          "cache",
        "_contracts",      "_by_key",
        "_meta_cache",     "meta_cache",
    ):
        cache = getattr(expired_instruments_service, attr, None)
        if not isinstance(cache, dict):
            continue
        candidate = cache.get(key)
        if not candidate:
            continue
        d = _safe_dict(candidate)
        if d:
            return d

    # ---- Strategy 3: fetch expired contracts for matching index ---- #
    segment = key.split("|")[0] if "|" in key else ""
    for idx in _index_candidates_for_segment(segment):
        try:
            resp = await asyncio.to_thread(
                expired_instruments_service.get_expired_option_contracts,
                idx["key"], expiry,
            )
        except Exception:
            logger.debug(
                "[chart2] expired contracts lookup failed for %s @ %s",
                idx["key"], expiry, exc_info=True,
            )
            continue

        for c in _extract_contracts(resp):
            cd = _safe_dict(c)
            if cd.get("instrument_key") == key:
                return cd

    return None


async def _build_expired_meta(key: str, expiry: str) -> dict[str, Any]:
    """Best-effort metadata for an expired instrument key.

    Ensures ``trading_symbol`` is the human-readable symbol when the
    underlying contract can be resolved, else falls back to the raw
    key. Successful resolutions are memoised in
    ``_expired_meta_cache``; failures retry on the next request.
    """
    cached = _expired_meta_cache.get(key)
    if cached is not None:
        # Return a shallow copy so callers cannot mutate our cache
        return dict(cached)

    parts = key.split("|")
    meta: dict[str, Any] = {
        "instrument_key":   key,
        "expiry":           expiry,
        "segment":          parts[0] if parts else "",
        "exchange_token":   parts[1] if len(parts) > 1 else "",
        # Fallback — overwritten below if resolution succeeds
        "trading_symbol":   key,
        "underlying_symbol": "",
        "_note": (
            "Expired contract — full metadata available via the "
            "⌛ Expired browser (or /upstox-app/expired/contracts)."
        ),
    }

    resolved = await _resolve_contract_meta(key, expiry)
    if resolved:
        # Merge resolved fields, but keep our canonical key/expiry
        for k, v in resolved.items():
            if v is None:
                continue
            if k in ("instrument_key", "expiry"):
                continue
            meta[k] = v
        # Guarantee canonical identity fields
        meta["instrument_key"] = key
        meta["expiry"]         = expiry
        meta["_note"] = (
            "Expired contract — metadata resolved from the expired "
            "contracts registry."
        )
        # Memoise only successful resolutions
        _expired_meta_cache[key] = dict(meta)

    return meta


# --------------------------------------------------------------------------- #
# Data: one-shot search (ACTIVE only, no live debounce)                       #
# --------------------------------------------------------------------------- #
@router.get("/api/chart2/search")
async def chart2_search(
    query: str = Query(..., min_length=2, max_length=50),
    records: int = Query(default=20, ge=1, le=30),
) -> dict[str, Any]:
    """One-shot search across active Upstox instruments.

    Fires only on explicit user action (Enter / Search button).
    """
    q = str(query or "").strip()
    if not q:
        return {"status": "ok", "results": [], "count": 0}

    try:
        token = token_service.get_access_token()
    except Exception:
        token = None
    if not token:
        return {"status": "no_token", "error": "access_token_missing", "results": []}

    try:
        resp = await asyncio.to_thread(
            requests.get,
            f"{_UPSTOX_BASE}/v2/instruments/search",
            headers={
                "Accept":        "application/json",
                "Authorization": f"Bearer {token}",
            },
            params={"query": q, "records": records, "page_number": 1},
            timeout=15,
        )
    except requests.RequestException as e:
        return {"status": "error", "error": f"{type(e).__name__}: {e}", "results": []}

    if resp.status_code >= 400:
        return {
            "status": "error",
            "error":  f"Upstox HTTP {resp.status_code}: {resp.text[:200]}",
            "results": [],
        }
    try:
        body = resp.json()
    except ValueError:
        return {"status": "error", "error": "non-JSON", "results": []}

    items = body.get("data") or []
    results = []
    for it in items:
        if not isinstance(it, dict):
            continue
        key = str(it.get("instrument_key") or "")
        results.append({
            "instrument_key":   key,
            "trading_symbol":   it.get("trading_symbol"),
            "strike_price":     it.get("strike_price"),
            "instrument_type":  it.get("instrument_type"),
            "expiry":           it.get("expiry"),
            "underlying_symbol": it.get("underlying_symbol") or it.get("name"),
            "_source":          "expired" if _is_expired_key(key) else "active",
        })
    return {"status": "ok", "results": results, "count": len(results)}


# --------------------------------------------------------------------------- #
# Data: expired browser (expiries + contracts)                                #
# --------------------------------------------------------------------------- #
@router.get("/api/chart2/expired/expiries")
async def chart2_expired_expiries(
    index_key: str = Query(default="NSE_INDEX|Nifty 50"),
) -> dict[str, Any]:
    """Return the expired expiry list for the given index."""
    return await asyncio.to_thread(
        expired_instruments_service.get_expiries, index_key,
    )


@router.get("/api/chart2/expired/contracts")
async def chart2_expired_contracts(
    index_key: str = Query(default="NSE_INDEX|Nifty 50"),
    expiry_date: str = Query(..., min_length=8),
) -> dict[str, Any]:
    """Return every expired contract for ``index_key`` at ``expiry_date``."""
    return await asyncio.to_thread(
        expired_instruments_service.get_expired_option_contracts,
        index_key, expiry_date,
    )