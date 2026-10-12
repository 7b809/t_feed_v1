import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates

from services.chart.chart_service import get_chart_data

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Charts"])
templates = Jinja2Templates(directory="templates")


def _instrument(instrument_key: str) -> dict:
    name = instrument_key.rsplit("|", 1)[-1].replace("_", " ")
    return {"instrument_key": instrument_key, "trading_symbol": name}


def _chart_payload(instrument_key: str) -> dict:
    try:
        result = get_chart_data(instrument_key)
    except Exception as exc:
        logger.warning("Chart history unavailable for %s (%s).", instrument_key, type(exc).__name__)
        result = None
    if not isinstance(result, dict):
        result = {}
    candles = result.get("candles")
    if not isinstance(candles, list):
        candles = []
    return {"candles": candles, "total_candles": len(candles)}


@router.get("/charts")
def chart_index(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="instrument_list.html",
        context={"request": request, "page_title": "Charts"},
    )


@router.get("/chart/{instrument_key:path}")
def view_chart(request: Request, instrument_key: str):
    instrument = _instrument(instrument_key)
    payload = _chart_payload(instrument_key)
    return templates.TemplateResponse(
        request=request,
        name="chart.html",
        context={
            "request": request,
            "page_title": f"Chart - {instrument['trading_symbol']}",
            "instrument_key": instrument_key,
            "instrument_name": instrument["trading_symbol"],
            "instrument": instrument,
            **payload,
        },
    )


@router.get("/chart/api/{instrument_key:path}")
def get_chart_json(instrument_key: str):
    return JSONResponse(content={"instrument": _instrument(instrument_key), **_chart_payload(instrument_key)})
