"""
Instrument Search service.

Wraps Upstox `GET /v2/instruments/search` — no SDK dependency, uses httpx
directly so it works even if the Upstox SDK version in the project doesn't
expose this endpoint yet.

Auth: pulls the access token from `token_service` on every call, so token
refreshes are picked up automatically without restarting the app.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import httpx

from core.logger import get_logger

logger = get_logger(__name__)

UPSTOX_SEARCH_URL = "https://api.upstox.com/v2/instruments/search"

# Sensible default timeout — the search endpoint is lightweight but Upstox
# can be slow under load.
DEFAULT_TIMEOUT_SEC = 30.0

# Allowed filter values — kept in one place for validation messages.
VALID_EXCHANGES = {"ALL", "NSE", "BSE", "MCX"}
VALID_SEGMENTS = {"ALL", "EQ", "FO", "CURR", "COMM", "INDEX", "OPT", "FUT"}
VALID_EXPIRY_KEYWORDS = {
    "current_week", "this_week", "near_week", "weekly", "next_week", "far_week",
    "current_month", "this_month", "near_month", "monthly", "next_month", "far_month",
}


def _get_access_token() -> Optional[str]:
    """Pull the current Upstox access token from the token service."""
    try:
        from token_tasks.service import token_service  # type: ignore

        return token_service.get_access_token()
    except Exception as exc:
        logger.warning("Unable to resolve access token: %s", exc)
        return None


def _build_params(
    query: str,
    exchanges: Optional[str] = None,
    segments: Optional[str] = None,
    instrument_types: Optional[str] = None,
    expiry: Optional[str] = None,
    atm_offset: Optional[int] = None,
    page_number: int = 1,
    records: int = 10,
) -> Dict[str, Any]:
    """Build the query-parameter dict, omitting any None values."""
    params: Dict[str, Any] = {
        "query": query,
        "page_number": page_number,
        "records": records,
    }
    if exchanges:
        params["exchanges"] = exchanges
    if segments:
        params["segments"] = segments
    if instrument_types:
        params["instrument_types"] = instrument_types
    if expiry:
        params["expiry"] = expiry
    if atm_offset is not None:
        params["atm_offset"] = atm_offset
    return params


async def search_instruments(
    query: str,
    exchanges: Optional[str] = None,
    segments: Optional[str] = None,
    instrument_types: Optional[str] = None,
    expiry: Optional[str] = None,
    atm_offset: Optional[int] = None,
    page_number: int = 1,
    records: int = 10,
) -> Dict[str, Any]:
    """
    Search Upstox instruments.

    Returns a dict with the upstream JSON on success, or a structured error
    payload on failure. Never raises — the caller always gets a dict.
    """
    token = _get_access_token()
    if not token:
        return {
            "success": False,
            "status_code": 401,
            "error": "Access token not available. Save a token first via /token/save or Telegram /save_token.",
        }

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
    }

    params = _build_params(
        query=query,
        exchanges=exchanges,
        segments=segments,
        instrument_types=instrument_types,
        expiry=expiry,
        atm_offset=atm_offset,
        page_number=page_number,
        records=records,
    )

    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SEC) as client:
            response = await client.get(UPSTOX_SEARCH_URL, headers=headers, params=params)

        try:
            payload = response.json()
        except Exception:
            payload = {"raw_text": response.text}

        if response.status_code >= 400:
            logger.warning(
                "Instrument search failed | status=%s | query=%s | body=%s",
                response.status_code,
                query,
                payload,
            )
            return {
                "success": False,
                "status_code": response.status_code,
                "error": payload.get("errors") or payload.get("message") or "Upstox returned an error.",
                "upstream": payload,
            }

        return {
            "success": True,
            "status_code": response.status_code,
            "data": payload,
        }

    except httpx.TimeoutException:
        logger.warning("Instrument search timed out | query=%s", query)
        return {
            "success": False,
            "status_code": 504,
            "error": f"Upstox instrument search timed out after {DEFAULT_TIMEOUT_SEC}s.",
        }
    except Exception as exc:
        logger.exception("Instrument search failed | query=%s", query)
        return {
            "success": False,
            "status_code": 502,
            "error": f"Upstox instrument search request failed: {exc}",
        }


async def get_instrument_by_key(instrument_key: str) -> Dict[str, Any]:
    """
    Convenience: resolve a single instrument_key to its full metadata.

    Uses the segment portion of the key as a hint so the search returns the
    exact contract. Falls back to a bare query if the key is malformed.
    """
    if "|" not in instrument_key:
        return await search_instruments(query=instrument_key, records=30)

    exchange_hint, _ = instrument_key.split("|", 1)
    exchange = exchange_hint.split("_", 1)[0] if "_" in exchange_hint else exchange_hint
    segment = exchange_hint.split("_", 1)[1] if "_" in exchange_hint else None

    return await search_instruments(
        query=instrument_key,
        exchanges=exchange,
        segments=segment,
        records=30,
    )


async def get_option_chain_atm(
    underlying: str,
    expiry: str = "current_week",
    atm_offset: int = 0,
    exchange: str = "NSE",
    option_type: str = "CE,PE",
) -> Dict[str, Any]:
    """
    Convenience: fetch the ATM (or near-ATM) option contracts for an underlying.
    """
    return await search_instruments(
        query=underlying,
        exchanges=exchange,
        segments="FO",
        instrument_types=option_type,
        expiry=expiry,
        atm_offset=atm_offset,
        page_number=1,
        records=30,
    )