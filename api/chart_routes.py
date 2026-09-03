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

router = APIRouter(
    tags=["Chart"],
)

templates = Jinja2Templates(directory="templates")


@router.get("/charts")
async def list_chart_instruments(
    request: Request,
):
    try:
        instruments = get_chart_instruments()

        return templates.TemplateResponse(
            request=request,
            name="instrument_list.html",
            context={
                "request": request,
                "instruments": instruments,
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


@router.get("/chart/{instrument_key:path}")
async def view_chart(
    request: Request,
    instrument_key: str,
):
    try:
        instrument = get_chart_instrument(instrument_key)

        instruments = get_chart_instruments()
        logger.info(
            "Chart page instruments count: %s",
            len(instruments)
        )

        result = get_chart_data(instrument_key)

        return templates.TemplateResponse(
            request=request,
            name="chart.html",
            context={
                "request": request,
                "instrument_key": instrument_key,
                "instrument_name": (
                    instrument.get("trading_symbol")
                    if instrument
                    else instrument_key
                ),
                "instrument": instrument,
                "instruments": instruments,
                "candles": result.get("candles", []),
                "total_candles": result.get("total_candles", 0),
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


@router.get("/chart/api/{instrument_key:path}")
async def get_chart_json(
    instrument_key: str,
):
    try:
        instrument = get_chart_instrument(instrument_key)

        result = get_chart_data(instrument_key)

        return JSONResponse(
            content={
                "instrument": instrument,
                **result,
            }
        )

    except Exception as exc:
        logger.exception(
            "Chart api failed for %s: %s",
            instrument_key,
            exc,
        )

        raise HTTPException(
            status_code=500,
            detail=str(exc),
        )
