# upstox_app/apis/expired_instruments_api.py
"""FastAPI routes for Upstox *expired instrument* APIs.

Three endpoints, one per service method:

    GET  /upstox-app/expired/expiries
         ?instrument_key=NSE_INDEX|Nifty 50

    GET  /upstox-app/expired/contracts
         ?instrument_key=NSE_INDEX|Nifty 50
         &expiry_date=2025-04-30

    GET  /upstox-app/expired/candles
         ?expired_instrument_key=NSE_FO|54452|24-04-2025
         &interval=1minute
         &from_date=2025-04-24
         &to_date=2025-04-24

Every endpoint:

  * runs the underlying SDK call inside ``asyncio.to_thread`` so the
    event loop stays responsive during Upstox network calls;
  * returns HTTP 200 with a JSON body whose ``status`` field carries
    the outcome (``ok`` / ``no_token`` / ``invalid_input`` / ``error``);
  * never exposes the access token — see
    ``upstox_app.services.expired_instruments_service`` for the
    security contract.

Provider / token errors return HTTP 200 with ``status="error"`` (or
``status="no_token"``) so callers can distinguish "transport OK but
provider refused" from a genuine HTTP-level failure. A malformed
required query parameter returns HTTP 422 (FastAPI's default), which
is what callers expect for bad input.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Query

from upstox_app.services.expired_instruments_service import (
    expired_instruments_service,
)

router = APIRouter(
    prefix="/upstox-app/expired",
    tags=["expired-instruments"],
)


# --------------------------------------------------------------------------- #
# 1. Expiries list                                                            #
# --------------------------------------------------------------------------- #
@router.get("/expiries")
async def get_expiries(
    instrument_key: str = Query(
        ...,
        min_length=3,
        description=(
            "Upstox index instrument key, e.g. 'NSE_INDEX|Nifty 50' "
            "or 'BSE_INDEX|SENSEX'."
        ),
    ),
) -> dict[str, Any]:
    """Return the list of expired expiry dates for the given index."""
    return await asyncio.to_thread(
        expired_instruments_service.get_expiries,
        instrument_key,
    )


# --------------------------------------------------------------------------- #
# 2. Expired contracts for one expiry                                         #
# --------------------------------------------------------------------------- #
@router.get("/contracts")
async def get_expired_contracts(
    instrument_key: str = Query(
        ...,
        min_length=3,
        description="Upstox index instrument key.",
    ),
    expiry_date: str = Query(
        ...,
        min_length=8,
        max_length=10,
        description="Expiry date in YYYY-MM-DD form (e.g. '2025-04-30').",
    ),
) -> dict[str, Any]:
    """Return the list of expired option contracts for one expiry date."""
    return await asyncio.to_thread(
        expired_instruments_service.get_expired_option_contracts,
        instrument_key,
        expiry_date,
    )


# --------------------------------------------------------------------------- #
# 3. Historical candles for one expired instrument                            #
# --------------------------------------------------------------------------- #
@router.get("/candles")
async def get_expired_candles(
    expired_instrument_key: str = Query(
        ...,
        min_length=3,
        description=(
            "Expired instrument key from /contracts, including the "
            "expiry suffix — e.g. 'NSE_FO|54452|24-04-2025'."
        ),
    ),
    interval: str = Query(
        default="1minute",
        description=(
            "Upstox interval token: '1minute', '30minute', '1day', "
            "'1week', or '1month'."
        ),
    ),
    from_date: str = Query(
        ...,
        min_length=8,
        max_length=10,
        description="From date in YYYY-MM-DD form.",
    ),
    to_date: str = Query(
        ...,
        min_length=8,
        max_length=10,
        description="To date in YYYY-MM-DD form.",
    ),
) -> dict[str, Any]:
    """Return historical candles for one expired instrument."""
    return await asyncio.to_thread(
        expired_instruments_service.get_expired_historical_candles,
        expired_instrument_key,
        interval,
        from_date,
        to_date,
    )