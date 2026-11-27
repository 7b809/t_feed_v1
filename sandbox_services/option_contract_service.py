"""
option_contract_service.py

Given an underlying instrument key (index), fetch the nearest-expiry option
chain and return the CE and PE contracts whose strike_price lies inside a
pre-mapped range around the underlying's live spot price.

Data source
-----------
Primary:  GET /v2/option/chain   (``OptionsApi.get_put_call_option_chain``)
          → returns per-strike LTP, volume, OI, bid/ask, close, prev_oi AND
            option greeks (vega, theta, gamma, delta, iv, pop) AND the
            underlying spot price in one call.

Optional: GET /v2/option/contract  (metadata only — trading_symbol,
          tick_size, lot_size, freeze_quantity, minimum_lot, weekly, segment,
          exchange). Chain does not return these, so we merge them in when
          ``include_metadata=True`` (default).

Enrichment per contract
-----------------------
Every CE/PE entry carries:
    * identity      : instrument_key, expiry, strike_price, instrument_type
    * relative      : moneyness (ITM/ATM/OTM), strike_offset from spot
    * market_data   : ltp, volume, oi, close_price, bid_price, bid_qty,
                      ask_price, ask_qty, prev_oi    [include_market_data]
    * greeks        : vega, theta, gamma, delta, iv, pop   [include_greeks]
    * strike_pcr    : per-strike Put/Call ratio (from chain row)
    * metadata      : segment, exchange, trading_symbol, tick_size, lot_size,
                      freeze_quantity, minimum_lot, weekly, underlying_key,
                      underlying_symbol                       [include_metadata]

Pre-mapped strike ranges
------------------------
    NIFTY ±1000, BANKNIFTY ±2000, FINNIFTY ±500,
    MIDCPNIFTY ±300, SENSEX ±3000, BANKEX ±2000.

Expiry discovery
----------------
When the caller does NOT pass ``expiry``, chain keywords are tried in strict
chronological order:
    current_week → next_week → far_week
                 → current_month → next_month → far_month
The first keyword that returns non-empty chain data with a non-past expiry
wins; past expiries are always skipped.

Failure contract
----------------
{
    "status": "FAILED",
    "step":   "VALIDATION | API_CLIENT_INIT | CHAIN_FETCH | FILTERING |
               METADATA_ENRICH | EXPIRIES_FETCH",
    "error":  { "error_code": ..., "message": ..., "http_status": ..., ... },
    "context": { ... }
}
"""
from __future__ import annotations

import json
import urllib.parse
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Union

import upstox_client
from upstox_client.rest import ApiException

from core.config import UPSTOX_HTTP_TIMEOUT_SECONDS, UPSTOX_SANDBOX
from core.logger import get_logger
from core.token_service import fetch_access_token

logger = get_logger(__file__)

# ---------------------------------------------------------------------------
# Pre-mapping: underlying key → human symbol, strike range, ATM tolerance
# ---------------------------------------------------------------------------
UNDERLYING_SYMBOL_MAP: Dict[str, str] = {
    "NSE_INDEX|Nifty 50":          "NIFTY",
    "NSE_INDEX|Nifty Bank":        "BANKNIFTY",
    "NSE_INDEX|Nifty Fin Service": "FINNIFTY",
    "NSE_INDEX|Nifty Mid Select":  "MIDCPNIFTY",
    "BSE_INDEX|SENSEX":            "SENSEX",
    "BSE_INDEX|BANKEX":            "BANKEX",
}

STRIKE_RANGE_MAP: Dict[str, float] = {
    "NIFTY":      1000.0,
    "BANKNIFTY":  2000.0,
    "FINNIFTY":    500.0,
    "MIDCPNIFTY":  300.0,
    "SENSEX":     3000.0,
    "BANKEX":     2000.0,
}

ATM_TOLERANCE_MAP: Dict[str, float] = {
    "NIFTY":      25.0,
    "BANKNIFTY":  50.0,
    "FINNIFTY":   25.0,
    "MIDCPNIFTY": 12.5,
    "SENSEX":     50.0,
    "BANKEX":     50.0,
}

# ---------------------------------------------------------------------------
# Field sets
# ---------------------------------------------------------------------------
CHAIN_MARKET_DATA_FIELDS = (
    "ltp",
    "volume",
    "oi",
    "close_price",
    "bid_price",
    "bid_qty",
    "ask_price",
    "ask_qty",
    "prev_oi",
)

CHAIN_GREEKS_FIELDS = (
    "vega",
    "theta",
    "gamma",
    "delta",
    "iv",
    "pop",
)

# Metadata fields we pull from the contracts endpoint (chain lacks these)
METADATA_FIELDS = (
    "segment",
    "exchange",
    "trading_symbol",
    "tick_size",
    "lot_size",
    "freeze_quantity",
    "minimum_lot",
    "weekly",
    "underlying_key",
    "underlying_symbol",
)

# ---------------------------------------------------------------------------
# Expiry keywords (chronological order)
# ---------------------------------------------------------------------------
EXPIRY_KEYWORDS = [
    "current_week",
    "next_week",
    "far_week",
    "current_month",
    "next_month",
    "far_month",
]

# Step labels
STEP_VALIDATION = "VALIDATION"
STEP_API_CLIENT = "API_CLIENT_INIT"
STEP_CHAIN      = "CHAIN_FETCH"
STEP_FILTER     = "FILTERING"
STEP_METADATA   = "METADATA_ENRICH"
STEP_EXPIRIES   = "EXPIRIES_FETCH"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _normalize_key(key: str) -> str:
    return urllib.parse.unquote(key).strip()


def _today_iso() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _expiry_str(value: Union[str, datetime, date, None]) -> str:
    """Normalise an ``expiry`` value to an ISO string."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day).isoformat()
    return str(value)


def _get_upstox_api_client() -> upstox_client.ApiClient:
    access_token = fetch_access_token()
    if not access_token:
        raise RuntimeError("Failed to retrieve a valid Upstox access token.")

    configuration = upstox_client.Configuration(sandbox=UPSTOX_SANDBOX)
    configuration.access_token = access_token

    api_client = upstox_client.ApiClient(configuration)
    if hasattr(api_client, "rest_client") and hasattr(api_client.rest_client, "pool_manager"):
        api_client.rest_client.pool_manager.connection_pool_kw["timeout"] = (
            UPSTOX_HTTP_TIMEOUT_SECONDS
        )
    return api_client


def _resolve_symbol(underlying_key: str) -> Optional[str]:
    return UNDERLYING_SYMBOL_MAP.get(underlying_key)


def _strike_range_for(symbol: str, offset_override: Optional[float]) -> float:
    if offset_override is not None:
        return float(offset_override)
    return STRIKE_RANGE_MAP.get(symbol, 1000.0)


def _moneyness(opt_type: str, strike: float, spot: float, tolerance: float) -> str:
    diff = strike - spot
    if abs(diff) <= tolerance:
        return "ATM"
    if opt_type == "CE":
        return "ITM" if diff < 0 else "OTM"
    return "ITM" if diff > 0 else "OTM"


# ---------------------------------------------------------------------------
# Chain fetch + parse
# ---------------------------------------------------------------------------
def _fetch_option_chain(
    api_client: upstox_client.ApiClient,
    underlying_key: str,
    expiry: str,
) -> List[Dict[str, Any]]:
    """
    Call GET /v2/option/chain. Returns the ``data`` list (per-strike rows)
    or an empty list if the response has no rows.
    """
    options_api = upstox_client.OptionsApi(api_client)
    response = options_api.get_put_call_option_chain(
        instrument_key=underlying_key,
        expiry_date=expiry,
    )
    resp_dict = response.to_dict() if hasattr(response, "to_dict") else dict(response)
    data = resp_dict.get("data") or []
    logger.info(
        f"Fetched chain with {len(data)} strike rows for {underlying_key} "
        f"(expiry={expiry})"
    )
    return data


def _discover_nearest_expiry_chain(
    api_client: upstox_client.ApiClient,
    underlying_key: str,
    explicit_expiry: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Walk expiry keywords in chronological order and return the first chain
    that has at least one non-expired strike row.
    """
    today_iso = _today_iso()
    candidates = [explicit_expiry] if explicit_expiry else list(EXPIRY_KEYWORDS)

    attempts: List[Dict[str, Any]] = []
    last_error: Optional[Dict[str, Any]] = None

    for kw in candidates:
        try:
            rows = _fetch_option_chain(api_client, underlying_key, kw)
            if not rows:
                attempts.append({"keyword": kw, "status": "EMPTY"})
                continue

            # Extract distinct future expiries from the rows
            expiries_in_response = sorted(
                {
                    _expiry_str(r.get("expiry"))
                    for r in rows if r.get("expiry")
                }
            )
            future_expiries = [
                e for e in expiries_in_response if e[:10] >= today_iso
            ]
            attempts.append({
                "keyword": kw,
                "status": "OK",
                "expiries_in_response": expiries_in_response,
                "future_expiries": future_expiries,
            })

            if not future_expiries:
                logger.debug(
                    f"Keyword '{kw}' chain had only past expiries: "
                    f"{expiries_in_response}"
                )
                continue

            chosen = future_expiries[0]
            rows_for_expiry = [
                r for r in rows if _expiry_str(r.get("expiry")) == chosen
            ]
            if not rows_for_expiry:
                attempts[-1]["status"] = "EMPTY_AFTER_FILTER"
                continue

            return {
                "rows": rows_for_expiry,
                "expiry_iso": chosen,
                "expiry_keyword": kw if not explicit_expiry else explicit_expiry,
                "error": None,
                "attempts": attempts,
            }

        except ApiException as exc:
            err = _parse_api_exception(exc)
            attempts.append({"keyword": kw, "status": "API_ERROR", "error": err})
            last_error = err
            logger.warning(f"Chain keyword '{kw}' raised ApiException: {err}")
        except Exception as exc:
            err = {"error_code": "CHAIN_FETCH_ERROR", "message": str(exc)}
            attempts.append({"keyword": kw, "status": "ERROR", "error": err})
            last_error = err
            logger.warning(f"Chain keyword '{kw}' failed: {exc}")

    return {
        "rows": [],
        "expiry_iso": None,
        "expiry_keyword": None,
        "error": last_error or {
            "error_code": "NO_CHAIN_DATA",
            "message": "No non-expired option chain data for any expiry candidate.",
        },
        "attempts": attempts,
    }


# ---------------------------------------------------------------------------
# Optional metadata merge (contracts endpoint)
# ---------------------------------------------------------------------------
def _fetch_contracts_metadata(
    api_client: upstox_client.ApiClient,
    underlying_key: str,
    expiry: str,
) -> Dict[str, Dict[str, Any]]:
    """
    Fetch contract metadata for the given expiry and return a lookup keyed
    by ``instrument_key``. Returns an empty dict on failure (caller treats
    metadata as optional).
    """
    try:
        options_api = upstox_client.OptionsApi(api_client)
        response = options_api.get_option_contracts(
            instrument_key=underlying_key,
            expiry_date=expiry,
        )
        resp_dict = (
            response.to_dict() if hasattr(response, "to_dict") else dict(response)
        )
        data = resp_dict.get("data") or []
        lookup: Dict[str, Dict[str, Any]] = {}
        for c in data:
            key = c.get("instrument_key")
            if key:
                lookup[key] = c
        logger.info(
            f"Fetched {len(lookup)} contract metadata rows for {underlying_key} "
            f"(expiry={expiry})"
        )
        return lookup
    except Exception as exc:
        logger.warning(
            f"Metadata enrichment failed (chain-only response will be returned): {exc}"
        )
        return {}


# ---------------------------------------------------------------------------
# Per-side (CE/PE) entry builder
# ---------------------------------------------------------------------------
def _build_side_entry(
    strike_row: Dict[str, Any],
    side_key: str,                          # "call_options" | "put_options"
    instrument_type: str,                   # "CE" | "PE"
    underlying_ltp: float,
    atm_tolerance: float,
    metadata_lookup: Dict[str, Dict[str, Any]],
    include_market_data: bool,
    include_greeks: bool,
    include_metadata: bool,
) -> Optional[Dict[str, Any]]:
    """Build a single CE or PE entry from one chain strike row."""
    side = strike_row.get(side_key) or {}
    instrument_key = side.get("instrument_key")
    if not instrument_key:
        return None

    strike = strike_row.get("strike_price")
    try:
        strike_f = float(strike) if strike is not None else None
    except (TypeError, ValueError):
        strike_f = None

    entry: Dict[str, Any] = {
        "instrument_key": instrument_key,
        "expiry": _expiry_str(strike_row.get("expiry")),
        "strike_price": strike_f,
        "instrument_type": instrument_type,
        "moneyness": (
            _moneyness(instrument_type, strike_f, underlying_ltp, atm_tolerance)
            if strike_f is not None else None
        ),
        "strike_offset": (
            round(strike_f - underlying_ltp, 2)
            if strike_f is not None else None
        ),
        "strike_pcr": strike_row.get("pcr"),
    }

    if include_market_data:
        md_in = side.get("market_data") or {}
        entry["market_data"] = {
            k: md_in.get(k) for k in CHAIN_MARKET_DATA_FIELDS
        }

    if include_greeks:
        greeks_in = side.get("option_greeks") or {}
        entry["greeks"] = {
            k: greeks_in.get(k) for k in CHAIN_GREEKS_FIELDS
        }

    if include_metadata and metadata_lookup:
        meta = metadata_lookup.get(instrument_key)
        if meta:
            for k in METADATA_FIELDS:
                entry[k] = meta.get(k)

    return entry


# ---------------------------------------------------------------------------
# Public service #1 — nearest-expiry chain, filtered to strike range
# ---------------------------------------------------------------------------
def get_nearest_expiry_option_contracts(
    underlying_key: str,
    expiry: Optional[str] = None,
    offset_override: Optional[float] = None,
    include_market_data: bool = True,
    include_greeks: bool = True,
    include_metadata: bool = True,
) -> Dict[str, Any]:
    """
    Fetch the nearest-expiry option chain and return CE + PE contracts within
    the pre-mapped strike range around the underlying spot.

    See module docstring for the full entry shape.

    Parameters
    ----------
    underlying_key : str
    expiry : str, optional
        Upstox expiry keyword or ISO date. Defaults to auto-discovery.
    offset_override : float, optional
        Override the pre-mapped ±strike range.
    include_market_data : bool = True
        Include ltp, volume, oi, close_price, bid/ask, prev_oi per side.
    include_greeks : bool = True
        Include vega, theta, gamma, delta, iv, pop per side.
    include_metadata : bool = True
        Merge segment, exchange, trading_symbol, tick_size, lot_size,
        freeze_quantity, minimum_lot, weekly from the contracts endpoint.
    """
    clean_key = _normalize_key(underlying_key)

    # --- STEP 1 — VALIDATION --------------------------------------------
    try:
        if not clean_key:
            raise ValueError("underlying_key cannot be empty.")

        symbol = _resolve_symbol(clean_key)
        if not symbol:
            raise ValueError(
                f"Unknown underlying_key '{clean_key}'. "
                f"Known keys: {list(UNDERLYING_SYMBOL_MAP.keys())}"
            )

        offset = _strike_range_for(symbol, offset_override)
        atm_tol = ATM_TOLERANCE_MAP.get(symbol, 25.0)

        logger.info(
            f"Fetching option chain | underlying={clean_key} | symbol={symbol} | "
            f"offset=±{offset} | expiry={expiry or 'auto'} | "
            f"market_data={include_market_data} | greeks={include_greeks} | "
            f"metadata={include_metadata}"
        )
    except Exception as exc:
        logger.exception("Validation failed")
        return {
            "status": "FAILED",
            "step": STEP_VALIDATION,
            "error": {"error_code": "VALIDATION_ERROR", "message": str(exc)},
            "context": {"underlying_key": underlying_key},
        }

    # --- STEP 2 — API CLIENT --------------------------------------------
    try:
        api_client = _get_upstox_api_client()
    except Exception as exc:
        logger.exception("API client init failed")
        err = (
            _parse_api_exception(exc)
            if isinstance(exc, ApiException)
            else {"error_code": "CLIENT_INIT_ERROR", "message": str(exc)}
        )
        return {
            "status": "FAILED",
            "step": STEP_API_CLIENT,
            "error": err,
            "context": {"underlying_key": clean_key},
        }

    # --- STEP 3 — DISCOVER NEAREST EXPIRY + FETCH CHAIN -----------------
    discovery = _discover_nearest_expiry_chain(
        api_client, clean_key, explicit_expiry=expiry
    )

    if not discovery["rows"]:
        return {
            "status": "FAILED",
            "step": STEP_CHAIN,
            "error": discovery["error"],
            "context": {
                "underlying_key": clean_key,
                "expiry_requested": expiry,
                "attempts": discovery["attempts"],
            },
        }

    rows = discovery["rows"]
    expiry_iso = discovery["expiry_iso"]
    expiry_keyword = discovery["expiry_keyword"]

    # Spot price comes from the chain itself — no separate LTP call needed
    underlying_ltp: Optional[float] = None
    for r in rows:
        sp = r.get("underlying_spot_price")
        if sp is not None:
            try:
                underlying_ltp = float(sp)
                break
            except (TypeError, ValueError):
                continue

    if underlying_ltp is None:
        # Fallback — rare; only if chain somehow didn't carry the spot
        logger.warning(
            f"Chain response for {clean_key} missing underlying_spot_price — "
            f"falling back to dedicated LTP call."
        )
        try:
            quote_api = upstox_client.MarketQuoteApi(api_client)
            resp = quote_api.ltp(symbol=clean_key, api_version="2.0")
            rd = resp.to_dict() if hasattr(resp, "to_dict") else dict(resp)
            dm = rd.get("data", {})
            entry = dm.get(clean_key) or next(iter(dm.values()), None)
            if entry and "last_price" in entry:
                underlying_ltp = float(entry["last_price"])
        except Exception as exc:
            logger.exception(f"Fallback LTP fetch failed: {exc}")

    if underlying_ltp is None:
        return {
            "status": "FAILED",
            "step": STEP_CHAIN,
            "error": {
                "error_code": "SPOT_PRICE_UNAVAILABLE",
                "message": "Could not determine underlying spot price from chain or LTP call.",
            },
            "context": {"underlying_key": clean_key, "expiry_used": expiry_iso},
        }

    logger.info(
        f"Resolved nearest expiry for {clean_key}: "
        f"{expiry_iso} (keyword='{expiry_keyword}') | "
        f"spot={underlying_ltp} | rows={len(rows)}"
    )

    # --- STEP 4 — FILTER ROWS BY STRIKE RANGE ---------------------------
    try:
        lower = round(underlying_ltp - offset, 2)
        upper = round(underlying_ltp + offset, 2)

        filtered_rows = []
        for r in rows:
            try:
                strike = float(r.get("strike_price"))
            except (TypeError, ValueError):
                continue
            if lower <= strike <= upper:
                filtered_rows.append(r)

        logger.info(
            f"Filtered strikes | range=[{lower}, {upper}] | "
            f"rows_in_range={len(filtered_rows)}/{len(rows)}"
        )
    except Exception as exc:
        logger.exception("Filtering failed")
        return {
            "status": "FAILED",
            "step": STEP_FILTER,
            "error": {"error_code": "FILTER_ERROR", "message": str(exc)},
            "context": {
                "underlying_key": clean_key,
                "underlying_ltp": underlying_ltp,
                "offset": offset,
            },
        }

    # --- STEP 5 — OPTIONAL METADATA ENRICHMENT --------------------------
    metadata_lookup: Dict[str, Dict[str, Any]] = {}
    metadata_warning: Optional[str] = None
    if include_metadata and filtered_rows:
        try:
            metadata_lookup = _fetch_contracts_metadata(
                api_client, clean_key, expiry_iso
            )
            if not metadata_lookup:
                metadata_warning = (
                    "Contracts metadata endpoint returned no rows; "
                    "entries will contain chain data only."
                )
        except Exception as exc:
            logger.warning(f"Metadata enrichment failed: {exc}")
            metadata_warning = f"Metadata enrichment failed: {exc}"

    # --- STEP 6 — BUILD CE / PE LISTS -----------------------------------
    ce_contracts: List[Dict[str, Any]] = []
    pe_contracts: List[Dict[str, Any]] = []

    for r in filtered_rows:
        ce = _build_side_entry(
            r, "call_options", "CE",
            underlying_ltp, atm_tol, metadata_lookup,
            include_market_data, include_greeks, include_metadata,
        )
        if ce:
            ce_contracts.append(ce)

        pe = _build_side_entry(
            r, "put_options", "PE",
            underlying_ltp, atm_tol, metadata_lookup,
            include_market_data, include_greeks, include_metadata,
        )
        if pe:
            pe_contracts.append(pe)

    # Sort each list by strike ascending for predictable ordering
    ce_contracts.sort(key=lambda x: (x.get("strike_price") or 0))
    pe_contracts.sort(key=lambda x: (x.get("strike_price") or 0))

    # --- SUCCESS --------------------------------------------------------
    response: Dict[str, Any] = {
        "status": "SUCCESS",
        "underlying_key": clean_key,
        "underlying_symbol": symbol,
        "underlying_ltp": underlying_ltp,
        "expiry_requested": expiry,
        "expiry_keyword": expiry_keyword,
        "expiry_used": expiry_iso,
        "strike_range": {
            "lower": lower,
            "upper": upper,
            "offset": offset,
        },
        "total_strikes": len(filtered_rows),
        "total_contracts": len(ce_contracts) + len(pe_contracts),
        "enrichment": {
            "market_data": include_market_data,
            "greeks": include_greeks,
            "metadata": include_metadata,
        },
        "ce_contracts": ce_contracts,
        "pe_contracts": pe_contracts,
    }
    if metadata_warning:
        response["metadata_warning"] = metadata_warning
    return response


# ---------------------------------------------------------------------------
# Public service #2 — list all available expiries
# ---------------------------------------------------------------------------
def get_available_expiries(underlying_key: str) -> Dict[str, Any]:
    """
    Return every available expiry for the given underlying, nearest first.
    Uses the chain endpoint across all keywords (consistent with /contracts).
    """
    clean_key = _normalize_key(underlying_key)

    # --- STEP 1 — VALIDATION --------------------------------------------
    try:
        if not clean_key:
            raise ValueError("underlying_key cannot be empty.")
        symbol = _resolve_symbol(clean_key)
        if not symbol:
            raise ValueError(
                f"Unknown underlying_key '{clean_key}'. "
                f"Known keys: {list(UNDERLYING_SYMBOL_MAP.keys())}"
            )
        logger.info(f"Fetching available expiries | underlying={clean_key} | symbol={symbol}")
    except Exception as exc:
        logger.exception("Validation failed")
        return {
            "status": "FAILED",
            "step": STEP_VALIDATION,
            "error": {"error_code": "VALIDATION_ERROR", "message": str(exc)},
            "context": {"underlying_key": underlying_key},
        }

    # --- STEP 2 — API CLIENT --------------------------------------------
    try:
        api_client = _get_upstox_api_client()
    except Exception as exc:
        logger.exception("API client init failed")
        err = (
            _parse_api_exception(exc)
            if isinstance(exc, ApiException)
            else {"error_code": "CLIENT_INIT_ERROR", "message": str(exc)}
        )
        return {
            "status": "FAILED",
            "step": STEP_API_CLIENT,
            "error": err,
            "context": {"underlying_key": clean_key},
        }

    today_iso = _today_iso()
    expiry_map: Dict[str, Dict[str, Any]] = {}
    keyword_errors: List[Dict[str, Any]] = []

    for kw in EXPIRY_KEYWORDS:
        try:
            rows = _fetch_option_chain(api_client, clean_key, kw)
            if not rows:
                continue

            # Group by expiry; count only CE+PE pairs (i.e. strike rows)
            per_expiry: Dict[str, List[Dict[str, Any]]] = {}
            for r in rows:
                exp = _expiry_str(r.get("expiry"))
                if exp and exp[:10] >= today_iso:
                    per_expiry.setdefault(exp, []).append(r)

            for exp, strike_rows in per_expiry.items():
                if exp not in expiry_map:
                    expiry_map[exp] = {
                        "expiry": exp,
                        "is_weekly": exp[8:10] not in ("25", "30", "31"),  # heuristic
                        "strike_count": len(strike_rows),
                        "contract_count": len(strike_rows) * 2,  # CE + PE
                        "matched_keywords": [kw],
                    }
                else:
                    if kw not in expiry_map[exp]["matched_keywords"]:
                        expiry_map[exp]["matched_keywords"].append(kw)
        except ApiException as exc:
            err = _parse_api_exception(exc)
            keyword_errors.append({"keyword": kw, "error": err})
            logger.warning(f"Expiry keyword '{kw}' raised ApiException: {err}")
        except Exception as exc:
            keyword_errors.append({"keyword": kw, "error": str(exc)})
            logger.warning(f"Expiry keyword '{kw}' failed: {exc}")

    if not expiry_map and keyword_errors and len(keyword_errors) == len(EXPIRY_KEYWORDS):
        return {
            "status": "FAILED",
            "step": STEP_EXPIRIES,
            "error": {
                "error_code": "EXPIRIES_FETCH_ERROR",
                "message": "All expiry keyword fetches failed.",
                "details": keyword_errors,
            },
            "context": {"underlying_key": clean_key},
        }

    expiries_sorted = sorted(expiry_map.values(), key=lambda x: x["expiry"])
    for e in expiries_sorted:
        e["matched_keywords"] = sorted(e["matched_keywords"])

    return {
        "status": "SUCCESS",
        "underlying_key": clean_key,
        "underlying_symbol": symbol,
        "total_expiries": len(expiries_sorted),
        "default_expiry": expiries_sorted[0]["expiry"] if expiries_sorted else None,
        "expiries": expiries_sorted,
        "keyword_errors": keyword_errors or None,
    }


# ---------------------------------------------------------------------------
# Error parsing
# ---------------------------------------------------------------------------
def _parse_api_exception(e: ApiException) -> Dict[str, Any]:
    http_status = getattr(e, "status", None)
    reason = getattr(e, "reason", str(e))
    raw_body = getattr(e, "body", None)

    parsed: Dict[str, Any] = {}
    if raw_body:
        try:
            decoded = json.loads(raw_body) if isinstance(raw_body, str) else raw_body
            if isinstance(decoded, dict):
                errors = decoded.get("errors") or []
                if errors:
                    first = errors[0]
                    parsed = {
                        "error_code": first.get("error_code") or first.get("errorCode"),
                        "message": first.get("message"),
                    }
        except Exception:
            pass

    return {
        "error_code": parsed.get("error_code") or "UNKNOWN",
        "message": parsed.get("message") or reason or "Unknown API error",
        "http_status": http_status,
        "raw": str(e),
    }