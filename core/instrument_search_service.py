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

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

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

_IST = ZoneInfo("Asia/Kolkata")


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
        """Search for the option at the given explicit strike (first match).

        Resolution order (first success wins):

          1. Explicit query ``NIFTY <TYPE> <STRIKE>`` for each expiry
             candidate (configured ``NIFTY_SEARCH_EXPIRY`` → next Thursday
             → the Thursday after → ``current_month``).
          2. Broad query ``NIFTY`` filtered client-side by strike + type,
             again across every expiry candidate.
          3. Server-side ATM lookup (``atm_offset=0``) across every expiry
             candidate.

        The configured keyword ``current_week`` is tried first, but on an
        expiry day, or if the provider has already rolled the weekly
        contract, an explicit query for that keyword can return nothing.
        The fallback chain keeps isolation creation alive in that window
        instead of dropping the touch entirely.
        """
        option_type = option_type.upper()
        _validate_type(option_type)
        if strike is None:
            strike = compute_atm_strike(nifty_ltp)

        expiries = self._expiry_candidates()

        base = {
            "exchanges": NIFTY_SEARCH_EXCHANGE,
            "segments": NIFTY_SEARCH_SEGMENT,
            "instrument_types": option_type,
            "page_number": 1,
            "records": 30,
        }

        logger.info(
            "Search (explicit strike): strike=%d, type=%s, nifty_ltp=%.2f, "
            "expiry_candidates=%s",
            strike, option_type, nifty_ltp, expiries,
        )

        # ---- Pass 1 & 2: explicit-then-broad query across expiries ------
        for expiry_value in expiries:
            for query in (f"NIFTY {option_type} {strike}", "NIFTY"):
                params = {**base, "query": query, "expiry": expiry_value}
                try:
                    data = self._do_request(params)
                except InstrumentSearchError as exc:
                    logger.warning(
                        "Search attempt failed (expiry=%s query=%r): %s",
                        expiry_value, query, exc,
                    )
                    continue

                filtered = [
                    d for d in data
                    if _float_eq(d.get("strike_price"), strike)
                    and (d.get("instrument_type") or "").upper() == option_type
                ]
                if filtered:
                    source = (
                        "explicit_query"
                        if query.startswith("NIFTY ")
                        else "broad_query+strike_filter"
                    )
                    return self._log_and_return(
                        filtered[0],
                        f"{source}(expiry={expiry_value})",
                    )

        # ---- Pass 3: server-side ATM lookup across expiries -------------
        for expiry_value in expiries:
            params = {
                **base,
                "query": "NIFTY",
                "expiry": expiry_value,
                "atm_offset": 0,
            }
            try:
                return self._request_and_take_first(
                    params,
                    source=f"atm_offset_fallback(expiry={expiry_value})",
                )
            except InstrumentSearchError as exc:
                logger.warning(
                    "ATM fallback failed (expiry=%s): %s",
                    expiry_value, exc,
                )

        raise InstrumentSearchError(
            f"No instrument found for strike={strike}, "
            f"option_type={option_type}. "
            f"Tried expiries={expiries}. "
            "Check the 'Upstox search status=' log lines above for the "
            "provider's actual error."
        )

    def _expiry_candidates(self) -> list[str]:
        """Ordered list of expiry values to try in ``search_nifty_option_at_strike``.

        First entry is whatever is configured (``NIFTY_SEARCH_EXPIRY``,
        default ``current_week``). Remaining entries are concrete
        ``YYYY-MM-DD`` dates plus ``current_month`` — so if the configured
        keyword stops resolving (e.g. on expiry day, or after a weekly
        roll) the search still has a chance to find the live contract.

        NIFTY weekly options expire on Thursday. Two Thursdays are
        included to cover the roll window around expiry day.
        """
        candidates: list[str] = [NIFTY_SEARCH_EXPIRY]

        today = datetime.now(_IST).date()
        # 3 == Thursday in Python's weekday() (Mon=0)
        days_until_thu = (3 - today.weekday()) % 7
        if days_until_thu == 0:
            # Today is Thursday — the *current* weekly may already have
            # expired by the time this runs. Start with next week.
            days_until_thu = 7
        next_thu = today + timedelta(days=days_until_thu)
        candidates.append(next_thu.isoformat())
        candidates.append((next_thu + timedelta(days=7)).isoformat())
        candidates.append("current_month")

        # Deduplicate while preserving order.
        return list(dict.fromkeys(c for c in candidates if c))

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

        Note: unlike the first-match path, this does **not** iterate across
        expiry candidates — the operator (or caller) is expected to have
        passed an explicit ``expiry`` if they care. If ``expiry`` is
        omitted, ``NIFTY_SEARCH_EXPIRY`` is used as-is.

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
                UPSTOX_SEARCH_URL, headers=self._headers(), params=params, timeout=10,
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

        # Surface API-level errors that come back as 200.
        status = (body.get("status") or "").lower()
        if status and status != "success":
            errors = body.get("errors") or []
            error_summary = "; ".join(
                f"{e.get('errorCode','?')}: {e.get('message','?')}" for e in errors
            ) or str(body)[:300]
            logger.error(
                "Upstox search status=%s for params=%s: %s",
                status, params, error_summary,
            )
            raise InstrumentSearchError(
                f"Upstox API status={status}: {error_summary}"
            )

        data = body.get("data")
        if data is None:
            logger.warning(
                "Upstox search: no 'data' key. params=%s body=%s",
                params, str(body)[:500],
            )
            raise InstrumentSearchError(f"No 'data' in response: {str(body)[:200]}")

        if not isinstance(data, list):
            logger.warning(
                "Upstox search: 'data' is %s not list. params=%s body=%s",
                type(data).__name__, params, str(body)[:500],
            )
            return []

        return data


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