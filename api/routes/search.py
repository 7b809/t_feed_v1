# api/routes/search.py
"""Instrument Search API routes.

Exposes the Upstox ``GET /v2/instruments/search`` endpoint through this
app so operators can verify instrument resolution from `curl`, `/docs`,
or any HTTP client — without shelling out to Upstox directly and having
to babysit the access token.

Routes
------
GET /api/v1/search/instrument                  First match at a given strike
GET /api/v1/search/instrument/all              All matches at a given strike
GET /api/v1/search/instrument/atm              ATM lookup derived from NIFTY LTP
GET /api/v1/search/instrument/raw              Raw Upstox response
GET /api/v1/search/instrument/dummy-isolation  Full isolation doc from first match

All routes fetch the access token from Mongo via ``fetch_access_token()``
on every request. There is no caching — each call hits Upstox. That's
intentional so you see the exact same behaviour the isolation service
would see at that moment.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query

from core.config import (
    NIFTY_SEARCH_EXCHANGE,
    NIFTY_SEARCH_SEGMENT,
    NIFTY_SEARCH_EXPIRY,
    NIFTY_STRIKE_STEP,
    TIMEZONE,
)
from core.instrument_search_service import (
    InstrumentSearchError,
    InstrumentSearchService,
    compute_atm_strike,
)
from core.logger import get_logger
from core.token_service import fetch_access_token
from services.dummy_isolation_builder import build_dummy_isolation_record

logger = get_logger(__file__)

router = APIRouter(prefix="/api/v1/search", tags=["search"])


# --------------------------------------------------------------------------- #
# Helper                                                                      #
# --------------------------------------------------------------------------- #
def _get_search_service() -> InstrumentSearchService:
    """Build a fresh search service using the current Mongo token."""
    try:
        token = fetch_access_token()
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Token fetch failed: {exc}",
        )
    return InstrumentSearchService(token)


# --------------------------------------------------------------------------- #
# GET /api/v1/search/instrument  — first match at a strike                    #
# --------------------------------------------------------------------------- #
@router.get("/instrument")
def search_instrument(
    strike: int = Query(..., description="Explicit strike (e.g. 25050)"),
    option_type: str = Query(..., description="CE or PE"),
    expiry: str = Query(
        NIFTY_SEARCH_EXPIRY,
        description=(
            "Expiry keyword (current_week, current_month, …) or "
            "yyyy-MM-dd."
        ),
    ),
    exchange: str = Query(
        NIFTY_SEARCH_EXCHANGE,
        description="Exchange filter (default NSE).",
    ),
    segment: str = Query(
        NIFTY_SEARCH_SEGMENT,
        description="Segment filter (default FO).",
    ),
):
    """Return the FIRST NIFTY option contract at ``strike`` for ``option_type``."""
    option_type = option_type.upper()
    if option_type not in ("CE", "PE"):
        raise HTTPException(
            status_code=400, detail="option_type must be CE or PE"
        )

    service = _get_search_service()
    try:
        instrument = service.search_nifty_option_at_strike(
            nifty_ltp=0.0,
            option_type=option_type,
            strike=strike,
        )
    except InstrumentSearchError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except Exception as exc:
        logger.exception("search_instrument failed")
        raise HTTPException(status_code=500, detail=str(exc))

    return {
        "strike": strike,
        "option_type": option_type,
        "expiry": expiry,
        "exchange": exchange,
        "segment": segment,
        "match_count": 1,
        "instrument": instrument,
    }


# --------------------------------------------------------------------------- #
# GET /api/v1/search/instrument/all  — all matches at a strike                #
# --------------------------------------------------------------------------- #
@router.get("/instrument/all")
def search_instrument_all(
    strike: int = Query(..., description="Explicit strike (e.g. 25050)"),
    option_type: str = Query(..., description="CE or PE"),
    expiry: str = Query(NIFTY_SEARCH_EXPIRY),
    exchange: str = Query(NIFTY_SEARCH_EXCHANGE),
    segment: str = Query(NIFTY_SEARCH_SEGMENT),
    max_records: int = Query(
        30, ge=1, le=30,
        description="Max records to request from Upstox (cap 30).",
    ),
):
    """Return ALL matching NIFTY option contracts at ``strike``."""
    option_type = option_type.upper()
    if option_type not in ("CE", "PE"):
        raise HTTPException(
            status_code=400, detail="option_type must be CE or PE"
        )

    service = _get_search_service()
    try:
        instruments = service.search_nifty_option_at_strike_all(
            option_type=option_type,
            strike=strike,
            expiry=expiry,
            exchanges=exchange,
            segments=segment,
            max_records=max_records,
        )
    except InstrumentSearchError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except Exception as exc:
        logger.exception("search_instrument_all failed")
        raise HTTPException(status_code=500, detail=str(exc))

    return {
        "strike": strike,
        "option_type": option_type,
        "expiry": expiry,
        "exchange": exchange,
        "segment": segment,
        "match_count": len(instruments),
        "instruments": instruments,
    }


# --------------------------------------------------------------------------- #
# GET /api/v1/search/instrument/atm  — ATM derived from NIFTY LTP             #
# --------------------------------------------------------------------------- #
@router.get("/instrument/atm")
def search_instrument_atm(
    nifty_ltp: float = Query(
        ..., description="NIFTY spot LTP (e.g. 25038.75)"
    ),
    option_type: str = Query(..., description="CE or PE"),
    atm_offset: int = Query(
        0,
        description=(
            "0 = ATM; +N = N strikes above ATM; -N = below. "
            f"Strike step = {NIFTY_STRIKE_STEP}."
        ),
    ),
    expiry: str = Query(NIFTY_SEARCH_EXPIRY),
    exchange: str = Query(NIFTY_SEARCH_EXCHANGE),
    segment: str = Query(NIFTY_SEARCH_SEGMENT),
):
    """Compute the ATM strike from NIFTY LTP and look it up."""
    option_type = option_type.upper()
    if option_type not in ("CE", "PE"):
        raise HTTPException(
            status_code=400, detail="option_type must be CE or PE"
        )

    try:
        base_strike = compute_atm_strike(nifty_ltp, NIFTY_STRIKE_STEP)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    final_strike = base_strike + (atm_offset * NIFTY_STRIKE_STEP)

    service = _get_search_service()
    try:
        instrument = service.search_nifty_option_at_strike(
            nifty_ltp=nifty_ltp,
            option_type=option_type,
            strike=final_strike,
        )
    except InstrumentSearchError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except Exception as exc:
        logger.exception("search_instrument_atm failed")
        raise HTTPException(status_code=500, detail=str(exc))

    return {
        "nifty_ltp": nifty_ltp,
        "computed_atm": base_strike,
        "atm_offset": atm_offset,
        "final_strike": final_strike,
        "option_type": option_type,
        "expiry": expiry,
        "exchange": exchange,
        "segment": segment,
        "instrument": instrument,
    }


# --------------------------------------------------------------------------- #
# GET /api/v1/search/instrument/raw  — full Upstox response                   #
# --------------------------------------------------------------------------- #
@router.get("/instrument/raw")
def search_instrument_raw(
    query: str = Query(
        ...,
        description='Free-text query (e.g. "NIFTY 25050 CE")',
    ),
    exchanges: str = Query(NIFTY_SEARCH_EXCHANGE),
    segments: str = Query(NIFTY_SEARCH_SEGMENT),
    instrument_types: str = Query(
        "", description='Comma-separated (e.g. "CE,PE") or empty.',
    ),
    expiry: str = Query(NIFTY_SEARCH_EXPIRY),
    atm_offset: int | None = Query(
        None,
        description="If provided, passes atm_offset to Upstox (0 = ATM).",
    ),
    page_number: int = Query(1, ge=1),
    records: int = Query(30, ge=1, le=30),
):
    """Pass-through to Upstox ``/v2/instruments/search`` — raw response."""
    params = {
        "query": query,
        "exchanges": exchanges,
        "segments": segments,
        "expiry": expiry,
        "page_number": page_number,
        "records": records,
    }
    if instrument_types:
        params["instrument_types"] = instrument_types
    if atm_offset is not None:
        params["atm_offset"] = atm_offset

    service = _get_search_service()
    try:
        body = service.search_raw(params)
    except InstrumentSearchError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except Exception as exc:
        logger.exception("search_instrument_raw failed")
        raise HTTPException(status_code=500, detail=str(exc))

    return body


# --------------------------------------------------------------------------- #
# GET /api/v1/search/instrument/dummy-isolation  — build isolation doc        #
# --------------------------------------------------------------------------- #
@router.get("/instrument/dummy-isolation")
def search_instrument_dummy_isolation(
    strike: int = Query(..., description="Explicit strike (e.g. 25050)"),
    option_type: str = Query(..., description="CE or PE"),
    expiry: str = Query(NIFTY_SEARCH_EXPIRY),
    exchange: str = Query(NIFTY_SEARCH_EXCHANGE),
    segment: str = Query(NIFTY_SEARCH_SEGMENT),
    nifty_ltp: float = Query(
        0.0,
        description=(
            "NIFTY spot LTP. Pass 0 to use ``strike`` as a proxy "
            "(diagnostic fields only)."
        ),
    ),
    option_ltp: float = Query(
        150.0,
        description=(
            "Isolated option LTP snapshot (dummy). Drives the mode "
            "decision — STANDARD for 100 ≤ price ≤ 250, "
            "EARLY_EXTREME_REVERSAL otherwise (if touch time < 10:30)."
        ),
    ),
    touch_time: str | None = Query(
        None,
        description=(
            "ISO timestamp for isolation touch time. Defaults to "
            "today 09:30:00 IST — inside the EARLY_EXTREME_REVERSAL "
            "cutoff (before 10:30). Pass an explicit time after 10:30 "
            "to pin STANDARD regardless of option_ltp."
        ),
    ),
    persist: bool = Query(
        False,
        description=(
            "When true, writes the dummy record to Mongo "
            "``project_info._id='instrument_isolation'`` AND to "
            "``isolation_events`` history. This overwrites any existing "
            "isolation for the current trade date — use for testing only."
        ),
    ),
):
    """Build a full isolation-shaped document from the first search match.

    Combines:

      1. ``search_nifty_option_at_strike_all`` to fetch matching instruments
      2. ``build_dummy_isolation_record`` to construct the isolation doc
      3. Optional ``IsolationRepository.upsert_current`` to persist

    The returned document matches the exact shape written by the real
    isolation pipeline (``services/isolation_service.py::_build_record``),
    so downstream consumers (Path A execution service, UI, etc.) can be
    tested end-to-end without waiting for a live touch.

    Example calls
    -------------
    Preview, ATM CE (STANDARD pinned because 150 is inside [100, 250])::

        GET /api/v1/search/instrument/dummy-isolation?strike=25050&option_type=CE&option_ltp=150

    Preview, EARLY_EXTREME_REVERSAL (price above ₹250, default 09:30 touch)::

        GET /api/v1/search/instrument/dummy-isolation?strike=25050&option_type=CE&option_ltp=320

    Persist a dummy STANDARD record so the execution service picks it up::

        GET /api/v1/search/instrument/dummy-isolation?strike=25050&option_type=PE&option_ltp=180&persist=true

    """
    option_type = option_type.upper()
    if option_type not in ("CE", "PE"):
        raise HTTPException(
            status_code=400, detail="option_type must be CE or PE"
        )

    # --- 1. Fetch matching instruments ----------------------------------
    service = _get_search_service()
    try:
        instruments = service.search_nifty_option_at_strike_all(
            option_type=option_type,
            strike=strike,
            expiry=expiry,
            exchanges=exchange,
            segments=segment,
            max_records=30,
        )
    except InstrumentSearchError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except Exception as exc:
        logger.exception("dummy-isolation: search failed")
        raise HTTPException(status_code=500, detail=str(exc))

    if not instruments:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No instruments found for strike={strike} "
                f"{option_type} (expiry={expiry})"
            ),
        )

    first = instruments[0]

    # --- 2. Resolve effective nifty_ltp ---------------------------------
    nifty_ltp_effective = (
        float(nifty_ltp) if nifty_ltp > 0 else float(strike)
    )
    nifty_ltp_source = (
        "dummy_param" if nifty_ltp > 0 else "strike_proxy"
    )

    # --- 3. Resolve touch_time ------------------------------------------
    if touch_time:
        touch_time_iso = touch_time
    else:
        now = datetime.now(ZoneInfo(TIMEZONE))
        touch_time_iso = now.replace(
            hour=9, minute=30, second=0, microsecond=0
        ).isoformat()

    # --- 4. Build the record --------------------------------------------
    try:
        record = build_dummy_isolation_record(
            instrument=first,
            strike=strike,
            bias=option_type,
            nifty_ltp=nifty_ltp_effective,
            option_ltp=float(option_ltp),
            touch_time_iso=touch_time_iso,
            nifty_ltp_source=nifty_ltp_source,
        )
    except Exception as exc:
        logger.exception("dummy-isolation: build failed")
        raise HTTPException(status_code=500, detail=str(exc))

    # --- 5. Optionally persist ------------------------------------------
    if persist:
        try:
            from repositories.isolation_repository import IsolationRepository

            repo = IsolationRepository()
            repo.upsert_current(record)
            repo.append_history(record)
            record["persisted"] = True
            logger.warning(
                "Dummy isolation PERSISTED to Mongo "
                "(overwrote today's isolation): %s | mode=%s | "
                "trade_date=%s",
                record["trading_symbol"],
                record["standard_execution_mode"],
                record["trade_date_ist"],
            )
        except Exception as exc:
            logger.exception("dummy-isolation: persist failed")
            record["persisted"] = False
            record["persist_error"] = str(exc)

    # --- 6. Serialise _id for JSON response ----------------------------
    out = dict(record)
    out["_id"] = str(out["_id"])
    return out