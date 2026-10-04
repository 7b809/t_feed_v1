"""HTTP routes for the Instrument Search API."""

from __future__ import annotations

import inspect
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Query
from fastapi.responses import JSONResponse

from core.logger import get_logger
from upstox_app.instruments_search import service
from upstox_app.instruments_search.schemas import InstrumentSearchQuery

logger = get_logger(__name__)

router = APIRouter(prefix="/upstox/instruments", tags=["instruments-search"])


# ---------------------------------------------------------------------------
# Core search endpoint (GET)
# ---------------------------------------------------------------------------
@router.get(
    "/search",
    summary="Search Upstox instruments",
    description=(
        "Free-text search across exchanges and segments with filters for "
        "instrument type, expiry, and ATM offset. Requires a valid access token. "
        "Results are paginated (max 30 records per page)."
    ),
)
async def search_instruments(
    query: str = Query(
        ...,
        min_length=1,
        max_length=50,
        description="Free text: symbol, name, ISIN, or strike (e.g. RELIANCE, NIFTY 24000 CE).",
    ),
    exchanges: Optional[str] = Query(
        None, description="Comma-separated: ALL, NSE, BSE, MCX. Default: ALL."
    ),
    segments: Optional[str] = Query(
        None, description="Comma-separated: ALL, EQ, FO, CURR, COMM, INDEX, OPT, FUT."
    ),
    instrument_types: Optional[str] = Query(
        None, description="Comma-separated: CE, PE, FUT, EQ, etc."
    ),
    expiry: Optional[str] = Query(
        None, description="Keyword (current_week, next_month, ...) or yyyy-MM-dd."
    ),
    atm_offset: Optional[int] = Query(
        None, description="Distance from ATM strike. 0 = ATM, positive = above, negative = below."
    ),
    page_number: int = Query(1, ge=1, description="Page number (starts at 1)."),
    records: int = Query(10, ge=1, le=30, description="Records per page (1-30)."),
) -> Any:
    """Search instruments with full filter support."""
    result = await service.search_instruments(
        query=query,
        exchanges=exchanges,
        segments=segments,
        instrument_types=instrument_types,
        expiry=expiry,
        atm_offset=atm_offset,
        page_number=page_number,
        records=records,
    )

    if not result.get("success"):
        return JSONResponse(
            status_code=result.get("status_code", 502),
            content={
                "success": False,
                "error": result.get("error"),
                "upstream": result.get("upstream"),
            },
        )

    return result.get("data")


# ---------------------------------------------------------------------------
# POST variant
# ---------------------------------------------------------------------------
@router.post(
    "/search",
    summary="Search Upstox instruments (POST body)",
    description="Same as GET /search but accepts the query object in the JSON body.",
)
async def search_instruments_post(payload: InstrumentSearchQuery = Body(...)) -> Any:
    """Search instruments using a JSON request body."""
    result = await service.search_instruments(
        query=payload.query,
        exchanges=payload.exchanges,
        segments=payload.segments,
        instrument_types=payload.instrument_types,
        expiry=payload.expiry,
        atm_offset=payload.atm_offset,
        page_number=payload.page_number,
        records=payload.records,
    )

    if not result.get("success"):
        return JSONResponse(
            status_code=result.get("status_code", 502),
            content={
                "success": False,
                "error": result.get("error"),
                "upstream": result.get("upstream"),
            },
        )

    return result.get("data")


# ---------------------------------------------------------------------------
# Resolve a single instrument key
# ---------------------------------------------------------------------------
@router.get(
    "/resolve",
    summary="Resolve an instrument_key to its full metadata",
    description=(
        "Convenience endpoint: pass a full instrument_key like "
        "`NSE_FO|157318` or `NSE_EQ|INE002A01018` and get the matching "
        "instrument record(s)."
    ),
)
async def resolve_instrument_key(
    instrument_key: str = Query(..., description="Full instrument key, e.g. NSE_FO|157318."),
) -> Any:
    result = await service.get_instrument_by_key(instrument_key)

    if not result.get("success"):
        return JSONResponse(
            status_code=result.get("status_code", 502),
            content={
                "success": False,
                "error": result.get("error"),
                "upstream": result.get("upstream"),
            },
        )

    return result.get("data")


# ---------------------------------------------------------------------------
# ATM option chain helper
# ---------------------------------------------------------------------------
@router.get(
    "/option-chain/atm",
    summary="Fetch ATM (or near-ATM) option contracts for an underlying",
    description=(
        "Convenience endpoint: pass an underlying symbol (e.g. NIFTY, RELIANCE) "
        "and get the ATM or nearby CE/PE contracts. Defaults to current week."
    ),
)
async def option_chain_atm(
    underlying: str = Query(..., description="Underlying symbol, e.g. NIFTY or RELIANCE."),
    expiry: str = Query("current_week", description="Expiry keyword or yyyy-MM-dd."),
    atm_offset: int = Query(0, description="0 = ATM, positive = above, negative = below."),
    exchange: str = Query("NSE", description="Exchange: NSE, BSE, or MCX."),
    option_type: str = Query("CE,PE", description="Comma-separated: CE, PE."),
) -> Any:
    result = await service.get_option_chain_atm(
        underlying=underlying,
        expiry=expiry,
        atm_offset=atm_offset,
        exchange=exchange,
        option_type=option_type,
    )

    if not result.get("success"):
        return JSONResponse(
            status_code=result.get("status_code", 502),
            content={
                "success": False,
                "error": result.get("error"),
                "upstream": result.get("upstream"),
            },
        )

    return result.get("data")


# ---------------------------------------------------------------------------
# Subscribe an instrument to the upstream market streamer
# ---------------------------------------------------------------------------
async def _maybe_await(value: Any) -> Any:
    """Await a value if it's awaitable, otherwise return as-is."""
    if inspect.isawaitable(value):
        return await value
    return value


async def _subscribe_via_streamer(
    instrument_key: str, mode: str = "full"
) -> Optional[Dict[str, Any]]:
    """
    Try every plausible streamer entry point to add a subscription.

    Returns a dict describing the successful path, or None if nothing worked.
    """
    # ---- 1) streamer_manager singleton -----------------------------------
    try:
        from upstox_app.streamer.streamer_manager import streamer_manager  # type: ignore
    except Exception:
        streamer_manager = None

    if streamer_manager is not None:
        for attr in ("subscribe", "subscribe_instruments", "add_subscription"):
            fn = getattr(streamer_manager, attr, None)
            if not callable(fn):
                continue
            for args in (([instrument_key],), ([instrument_key], mode)):
                try:
                    result = await _maybe_await(fn(*args))
                    return {
                        "source": f"streamer_manager.{attr}",
                        "result": result,
                    }
                except TypeError:
                    # Signature mismatch — try the next argument shape.
                    continue
                except Exception as exc:
                    logger.debug("streamer_manager.%s failed: %s", attr, exc)
                    break

    # ---- 2) market_streamer singleton -----------------------------------
    try:
        from upstox_app.market.market_streamer import market_streamer  # type: ignore
    except Exception:
        market_streamer = None

    if market_streamer is not None:
        for attr in ("subscribe", "subscribe_instruments", "add_subscription"):
            fn = getattr(market_streamer, attr, None)
            if not callable(fn):
                continue
            for args in (([instrument_key],), ([instrument_key], mode)):
                try:
                    result = await _maybe_await(fn(*args))
                    return {
                        "source": f"market_streamer.{attr}",
                        "result": result,
                    }
                except TypeError:
                    continue
                except Exception as exc:
                    logger.debug("market_streamer.%s failed: %s", attr, exc)
                    break

    # ---- 3) HTTP loopback to the existing REST endpoint ------------------
    try:
        import httpx  # type: ignore

        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000", timeout=10.0
        ) as client:
            r = await client.post(
                "/upstox/market/subscribe",
                json={"instrument_keys": [instrument_key], "mode": mode},
            )
            if r.status_code < 400:
                try:
                    payload = r.json()
                except Exception:
                    payload = {"raw": r.text}
                return {
                    "source": "http:/upstox/market/subscribe",
                    "result": payload,
                }
    except Exception as exc:
        logger.debug("HTTP subscribe failed: %s", exc)

    return None


@router.post(
    "/subscribe",
    summary="Subscribe an instrument to the market streamer",
    description=(
        "Adds the instrument to the upstream market streamer so live ticks "
        "start flowing to `/all-feeds` and any `/ws/market` clients. Safe to "
        "call multiple times for the same key — subscriptions are idempotent."
    ),
)
async def subscribe_instrument(payload: Dict[str, Any] = Body(...)) -> Any:
    instrument_key = (payload or {}).get("instrument_key")
    mode = (payload or {}).get("mode", "full")

    if not instrument_key:
        return JSONResponse(
            status_code=400,
            content={"success": False, "error": "instrument_key is required."},
        )

    result = await _subscribe_via_streamer(instrument_key, mode=mode)

    if not result:
        return JSONResponse(
            status_code=502,
            content={
                "success": False,
                "error": "No streamer subscribe path was available.",
                "instrument_key": instrument_key,
            },
        )

    return {
        "success": True,
        "instrument_key": instrument_key,
        "mode": mode,
        "subscribed": [instrument_key],
        "source": result.get("source"),
        "message": "Subscription accepted.",
    }


# ---------------------------------------------------------------------------
# Health / config introspection
# ---------------------------------------------------------------------------
@router.get(
    "/search/status",
    summary="Instrument Search service status",
    description="Shows whether a token is available and what filters are supported.",
)
async def search_status() -> Dict[str, Any]:
    token_available = bool(service._get_access_token())
    return {
        "success": True,
        "token_available": token_available,
        "upstream_url": service.UPSTOX_SEARCH_URL,
        "valid_exchanges": sorted(service.VALID_EXCHANGES),
        "valid_segments": sorted(service.VALID_SEGMENTS),
        "valid_expiry_keywords": sorted(service.VALID_EXPIRY_KEYWORDS),
        "max_records_per_page": 30,
        "max_query_length": 50,
    }