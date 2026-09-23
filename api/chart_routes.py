import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates

from services.chart.chart_service import get_chart_data
from services.option_service import (
    get_chart_instrument,
    get_chart_instruments,
)

logger = logging.getLogger(__name__)


# ============================================================
# ROUTER
# ============================================================

router = APIRouter(
    tags=["Chart"],
)


# ============================================================
# JINJA2
# ============================================================

templates = Jinja2Templates(directory="templates")


# ============================================================
# HELPERS
# ============================================================


def _safe_instrument(instrument_key: str) -> dict:
    """
    Return the instrument dict for a key, or an empty dict.

    Never returns None — templates rely on `.get(...)` being safe.
    """
    try:
        instrument = get_chart_instrument(instrument_key)
    except Exception as exc:
        logger.warning(
            "Instrument lookup failed for %s: %s",
            instrument_key,
            exc,
        )
        instrument = None

    if not isinstance(instrument, dict):
        if instrument is not None:
            logger.warning(
                "Instrument lookup for %s returned %s (expected dict). "
                "Falling back to {}.",
                instrument_key,
                type(instrument).__name__,
            )
        return {}
    return instrument


def _safe_chart_data(instrument_key: str) -> dict:
    """
    Call get_chart_data and guarantee a dict with 'candles'
    and 'total_candles' keys.

    On failure, returns an empty payload so the page can still
    render (the live WebSocket feed will populate the chart).
    """
    try:
        result = get_chart_data(instrument_key)
    except Exception as exc:
        logger.exception(
            "get_chart_data failed for %s: %s",
            instrument_key,
            exc,
        )
        result = None

    if not isinstance(result, dict):
        return {"candles": [], "total_candles": 0}

    candles = result.get("candles") or []
    if not isinstance(candles, list):
        candles = []

    total = result.get("total_candles")
    if not isinstance(total, int):
        try:
            total = int(total)
        except (TypeError, ValueError):
            total = len(candles)

    return {
        **result,
        "candles": candles,
        "total_candles": total,
    }


def _resolve_instrument_name(
    instrument: dict,
    instrument_key: str,
) -> str:
    """
    Human-readable name for the chart page header.
    """
    name = None
    if isinstance(instrument, dict):
        name = (
            instrument.get("trading_symbol")
            or instrument.get("name")
            or instrument.get("symbol")
        )
    if not name:
        name = instrument_key
    return name


# ============================================================
# CHART INSTRUMENTS
# ============================================================


@router.get(
    "/charts",
)
async def list_chart_instruments(
    request: Request,
):
    """
    Render the chart instrument selection page.

    instrument_list.html should extend base.html.
    """

    try:
        instruments = get_chart_instruments() or []

        logger.info(
            "Chart instruments loaded: %s",
            len(instruments),
        )

        return templates.TemplateResponse(
            request=request,
            name="instrument_list.html",
            context={
                "request": request,
                "instruments": instruments,
                "page_title": "Chart Instruments",
                "page_heading": "Chart Instruments",
                "page_subtitle": "Select an instrument to open its live chart",
            },
        )

    except Exception as exc:
        logger.exception(
            "Failed to load chart instruments page: %s",
            exc,
        )

        raise HTTPException(
            status_code=500,
            detail=str(exc),
        )


# ============================================================
# INDIVIDUAL CHART
# ============================================================


@router.get(
    "/chart/{instrument_key:path}",
)
async def view_chart(
    request: Request,
    instrument_key: str,
):
    """
    Render the individual chart page.

    chart.html should extend base.html.

    Notes
    -----
    * `instrument` is ALWAYS a dict (possibly empty) so that
      Jinja2 `.get(...)` calls never raise UndefinedError.
    * `candles` / `total_candles` are ALWAYS populated (possibly
      empty) so the chart template can render unconditionally.
    * If the historical data fetch fails, the page still renders
      and the live WebSocket feed will populate the chart.
    """

    try:
        instrument = _safe_instrument(instrument_key)

        if not instrument:
            logger.warning(
                "Chart page requested for unknown instrument: %s",
                instrument_key,
            )

        instruments = get_chart_instruments() or []

        logger.info(
            "Chart page instruments count: %s (requested=%s)",
            len(instruments),
            instrument_key,
        )

        chart_payload = _safe_chart_data(instrument_key)

        candles = chart_payload.get("candles", [])
        total_candles = chart_payload.get("total_candles", 0)

        if not candles:
            logger.warning(
                "Chart page rendering with EMPTY candles for %s. "
                "Historical fetch may have failed; live feed will "
                "populate the chart.",
                instrument_key,
            )

        instrument_name = _resolve_instrument_name(
            instrument,
            instrument_key,
        )

        return templates.TemplateResponse(
            request=request,
            name="chart.html",
            context={
                "request": request,
                "page_title": f"Chart - {instrument_name}",
                "page_heading": instrument_name,
                "page_subtitle": "Live market chart and candle data",
                "instrument_key": instrument_key,
                "instrument_name": instrument_name,
                # Guaranteed dict — never None
                "instrument": instrument,
                # Guaranteed list — never None
                "instruments": instruments,
                # Guaranteed list — never None
                "candles": candles,
                # Guaranteed int — never None
                "total_candles": total_candles,
            },
        )

    except Exception as exc:
        logger.exception(
            "Chart page load failed for %s: %s",
            instrument_key,
            exc,
        )

        raise HTTPException(
            status_code=500,
            detail=str(exc),
        )


# ============================================================
# CHART JSON API
# ============================================================


@router.get(
    "/chart/api/{instrument_key:path}",
)
async def get_chart_json(
    instrument_key: str,
):
    """
    Return chart data as JSON.

    This endpoint is used by JavaScript and therefore does
    NOT use Jinja2.

    The response shape is always consistent:

        {
            "instrument": {...} | {},
            "candles": [...],
            "total_candles": <int>
        }
    """

    try:
        instrument = _safe_instrument(instrument_key)
        chart_payload = _safe_chart_data(instrument_key)

        return JSONResponse(
            content={
                "instrument": instrument,
                **chart_payload,
            }
        )

    except Exception as exc:
        logger.exception(
            "Chart API failed for %s: %s",
            instrument_key,
            exc,
        )

        raise HTTPException(
            status_code=500,
            detail=str(exc),
        ) 