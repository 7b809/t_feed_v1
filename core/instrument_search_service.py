# core/instrument_search_service.py
"""Upstox Instruments Search API client (GET /v2/instruments/search).

Exposes three call paths:

  * ``search_nifty_option``                — legacy ATM lookup (first match)
  * ``search_nifty_option_at_strike``      — explicit strike (first match)
  * ``search_nifty_option_at_strike_all``  — explicit strike (ALL matches)
  * ``search_raw``                         — pass-through, full response body

The last two were added specifically so the ``/api/v1/search/*`` FastAPI
routes can surface instruments to an operator for direct inspection,
rather than only returning the single first match the isolation
pipeline needs.
"""

from __future__ import annotations

import requests

from core.config import (
    UPSTOX_SEARCH_URL,
    NIFTY_SEARCH_EXCHANGE,
    NIFTY_SEARCH_SEGMENT,
    NIFTY_SEARCH_EXPIRY,
    NIFTY_SEARCH_USE_ATM_OFFSET,
    NIFTY_STRIKE_STEP,
)
from core.logger import get_logger

logger = get_logger(__file__)


class InstrumentSearchError(RuntimeError):
    """Raised when the search API returns no usable result."""


def compute_atm_strike(nifty_ltp: float, step: int = NIFTY_STRIKE_STEP) -> int:
    """Round NIFTY spot to the nearest strike-step multiple."""
    if nifty_ltp <= 0:
        raise ValueError(f"Invalid NIFTY LTP: {nifty_ltp}")
    return int(round(nifty_ltp / step) * step)


class InstrumentSearchService:
    def __init__(self, access_token: str):
        if not access_token:
            raise ValueError("access_token is required")
        self.access_token = access_token

    # ------------------------------------------------------------------ #
    # Legacy / isolation path                                            #
    # ------------------------------------------------------------------ #
    def search_nifty_option(self, nifty_ltp: float, option_type: str) -> dict:
        """Legacy entry — implicit ATM strike via atm_offset=0."""
        option_type = option_type.upper()
        _validate_type(option_type)
        params = self._build_atm_params(option_type)
        return self._request_and_take_first(params)

    def search_nifty_option_at_strike(
        self,
        nifty_ltp: float,
        option_type: str,
        strike: int | None = None,
    ) -> dict:
        """Search for the option at the given explicit strike (first match)."""
        option_type = option_type.upper()
        _validate_type(option_type)
        if strike is None:
            strike = compute_atm_strike(nifty_ltp)

        params = {
            "query": f"NIFTY {option_type} {strike}",
            "exchanges": NIFTY_SEARCH_EXCHANGE,
            "segments": NIFTY_SEARCH_SEGMENT,
            "instrument_types": option_type,
            "expiry": NIFTY_SEARCH_EXPIRY,
            "page_number": 1,
            "records": 30,
        }
        logger.info(
            "Search (explicit strike): strike=%d, type=%s, nifty_ltp=%.2f",
            strike, option_type, nifty_ltp,
        )

        try:
            data = self._do_request(params)
        except InstrumentSearchError:
            data = []

        filtered = [
            d for d in data
            if _float_eq(d.get("strike_price"), strike)
            and (d.get("instrument_type") or "").upper() == option_type
        ]
        if filtered:
            return self._log_and_return(filtered[0], "explicit_query")

        # Fallback 1: broader query without strike
        fallback = {
            "query": "NIFTY",
            "exchanges": NIFTY_SEARCH_EXCHANGE,
            "segments": NIFTY_SEARCH_SEGMENT,
            "instrument_types": option_type,
            "expiry": NIFTY_SEARCH_EXPIRY,
            "page_number": 1,
            "records": 30,
        }
        try:
            data = self._do_request(fallback)
        except InstrumentSearchError:
            data = []

        filtered = [
            d for d in data
            if _float_eq(d.get("strike_price"), strike)
            and (d.get("instrument_type") or "").upper() == option_type
        ]
        if filtered:
            return self._log_and_return(
                filtered[0], "broad_query+strike_filter"
            )

        # Fallback 2: atm_offset=0 (server-side ATM)
        try:
            return self._request_and_take_first(
                self._build_atm_params(option_type),
                source="atm_offset_fallback",
            )
        except InstrumentSearchError:
            raise InstrumentSearchError(
                f"No instrument found for strike={strike}, "
                f"option_type={option_type}"
            )

    # ------------------------------------------------------------------ #
    # NEW — for /api/v1/search/* routes                                  #
    # ------------------------------------------------------------------ #
    def search_nifty_option_at_strike_all(
        self,
        option_type: str,
        strike: int,
        expiry: str | None = None,
        exchanges: str | None = None,
        segments: str | None = None,
        max_records: int = 30,
    ) -> list[dict]:
        """Return ALL matching instruments at the given strike.

        Companion to ``search_nifty_option_at_strike`` (which returns only
        the first match). Used by ``GET /api/v1/search/instrument/all`` so
        operators can inspect every contract at a strike — e.g. to confirm
        only one expiry is live, or to see the full list before picking.

        Returns ``[]`` on empty response (never raises on empty).
        """
        option_type = option_type.upper()
        _validate_type(option_type)

        params = {
            "query": f"NIFTY {option_type} {strike}",
            "exchanges": exchanges or NIFTY_SEARCH_EXCHANGE,
            "segments": segments or NIFTY_SEARCH_SEGMENT,
            "instrument_types": option_type,
            "expiry": expiry or NIFTY_SEARCH_EXPIRY,
            "page_number": 1,
            "records": min(max_records, 30),
        }
        logger.info(
            "Search (all matches): strike=%d, type=%s, params=%s",
            strike, option_type, params,
        )

        try:
            data = self._do_request(params)
        except InstrumentSearchError:
            return []

        filtered = [
            d for d in data
            if _float_eq(d.get("strike_price"), strike)
            and (d.get("instrument_type") or "").upper() == option_type
        ]

        # If the explicit query returned nothing, try the broader query
        if not filtered:
            fallback = {
                "query": "NIFTY",
                "exchanges": exchanges or NIFTY_SEARCH_EXCHANGE,
                "segments": segments or NIFTY_SEARCH_SEGMENT,
                "instrument_types": option_type,
                "expiry": expiry or NIFTY_SEARCH_EXPIRY,
                "page_number": 1,
                "records": min(max_records, 30),
            }
            try:
                data = self._do_request(fallback)
            except InstrumentSearchError:
                data = []
            filtered = [
                d for d in data
                if _float_eq(d.get("strike_price"), strike)
                and (d.get("instrument_type") or "").upper() == option_type
            ]

        logger.info(
            "Search (all matches) returned %d instrument(s) for strike=%d %s",
            len(filtered), strike, option_type,
        )
        return filtered

    def search_raw(self, params: dict) -> dict:
        """Pass-through to /v2/instruments/search; returns the RAW body.

        Unlike ``_do_request`` (which returns only the ``data`` list),
        this returns the entire response — including ``status`` and
        ``meta_data``. Used by ``GET /api/v1/search/instrument/raw`` for
        inspection and debugging against the live Upstox endpoint.
        """
        try:
            resp = requests.get(
                UPSTOX_SEARCH_URL,
                headers=self._headers(),
                params=params,
                timeout=10,
            )
            resp.raise_for_status()
            return resp.json()
        except requests.HTTPError as exc:
            raise InstrumentSearchError(
                f"HTTP {exc.response.status_code if exc.response else '?'}: "
                f"{getattr(exc.response, 'text', '')[:300]}"
            ) from exc
        except Exception as exc:
            raise InstrumentSearchError(f"Search failed: {exc}") from exc

    # ------------------------------------------------------------------ #
    # Internals                                                          #
    # ------------------------------------------------------------------ #
    def _build_atm_params(self, option_type: str) -> dict:
        return {
            "query": "NIFTY",
            "exchanges": NIFTY_SEARCH_EXCHANGE,
            "segments": NIFTY_SEARCH_SEGMENT,
            "instrument_types": option_type,
            "expiry": NIFTY_SEARCH_EXPIRY,
            "atm_offset": 0,
            "page_number": 1,
            "records": 30,
        }

    def _request_and_take_first(
        self, params: dict, source: str = "atm_offset"
    ) -> dict:
        data = self._do_request(params)
        if not data:
            raise InstrumentSearchError(
                f"No instruments returned for params={params}"
            )
        return self._log_and_return(data[0], source)

    def _do_request(self, params: dict) -> list[dict]:
        try:
            resp = requests.get(
                UPSTOX_SEARCH_URL,
                headers=self._headers(),
                params=params,
                timeout=10,
            )
            resp.raise_for_status()
            body = resp.json()
        except requests.HTTPError as exc:
            raise InstrumentSearchError(
                f"HTTP {exc.response.status_code if exc.response else '?'}: "
                f"{getattr(exc.response, 'text', '')[:300]}"
            ) from exc
        except Exception as exc:
            raise InstrumentSearchError(f"Search failed: {exc}") from exc
        return body.get("data") or []

    def _log_and_return(self, first: dict, source: str) -> dict:
        logger.info(
            "Isolation candidate (%s): %s | strike=%s | expiry=%s | key=%s",
            source,
            first.get("trading_symbol"),
            first.get("strike_price"),
            first.get("expiry"),
            first.get("instrument_key"),
        )
        return first

    def _headers(self) -> dict:
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.access_token}",
        }


def _validate_type(option_type: str) -> None:
    if option_type not in ("CE", "PE"):
        raise ValueError(f"option_type must be CE or PE, got {option_type}")


def _float_eq(a, b) -> bool:
    try:
        return abs(float(a) - float(b)) < 1e-6
    except (TypeError, ValueError):
        return False