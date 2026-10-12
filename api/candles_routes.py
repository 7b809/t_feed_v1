"""General historical and intraday candle endpoints for chart clients."""

import logging
from datetime import date

from fastapi import APIRouter, HTTPException, Query

from services.chart import chart_service as charts

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Candles"])


def _key(instrument_key: str) -> str:
    value = str(instrument_key or "").strip()
    if not value:
        raise HTTPException(status_code=400, detail="instrument_key is required")
    return value


@router.get("/candles")
def get_candles(
    instrument_key: str = Query(...),
    days: int = Query(7, ge=1, le=365),
    interval: str = Query("minutes"),
    unit: str = Query("1"),
):
    key = _key(instrument_key)
    try:
        result = charts.get_chart_data(key, days=days, interval=interval, unit=unit)
        return result
    except Exception as exc:
        logger.exception("Candle retrieval failed for %s (%s).", key, type(exc).__name__)
        raise HTTPException(status_code=502, detail="Market history provider request failed") from exc


@router.get("/candles/historical")
def get_historical(
    instrument_key: str = Query(...),
    days: int = Query(7, ge=1, le=365),
    interval: str = Query("minutes"),
    unit: str = Query("1"),
    end_date: date | None = Query(None),
):
    key = _key(instrument_key)
    try:
        raw = charts.fetch_historical_candles(key, days=days, interval=interval, unit=unit, end_date=end_date)
        candles = charts.convert_to_chart_data(charts.merge_candles(raw, []))
        return {"instrument_key": key, "candles": candles, "total_candles": len(candles)}
    except Exception as exc:
        logger.exception("Historical candle retrieval failed for %s (%s).", key, type(exc).__name__)
        raise HTTPException(status_code=502, detail="Market history provider request failed") from exc


@router.get("/candles/intraday")
def get_intraday(
    instrument_key: str = Query(...),
    interval: str = Query("minutes"),
    unit: str = Query("1"),
):
    key = _key(instrument_key)
    try:
        candles = charts.convert_to_chart_data(charts.fetch_intraday_candles(key, interval=interval, unit=unit))
        return {"instrument_key": key, "candles": candles, "total_candles": len(candles)}
    except Exception as exc:
        logger.exception("Intraday candle retrieval failed for %s (%s).", key, type(exc).__name__)
        raise HTTPException(status_code=502, detail="Market history provider request failed") from exc


def _older(instrument_key: str, before_date: date, days: int):
    key = _key(instrument_key)
    try:
        return charts.get_older_chart_data(key, before_date=before_date, days=days)
    except Exception as exc:
        logger.exception("Older candle retrieval failed for %s (%s).", key, type(exc).__name__)
        raise HTTPException(status_code=502, detail="Market history provider request failed") from exc


@router.get("/candles/older")
def get_older(
    instrument_key: str = Query(...),
    before_date: date = Query(...),
    days: int = Query(30, ge=1, le=365),
):
    return _older(instrument_key, before_date, days)


@router.get("/api/older-candles")
def get_older_chart_candles(
    instrument_key: str = Query(...),
    before_date: date = Query(...),
    days: int = Query(30, ge=1, le=365),
):
    return _older(instrument_key, before_date, days)
