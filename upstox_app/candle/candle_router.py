from fastapi import APIRouter, HTTPException

from upstox_app.candle.candle_service import candle_service
from upstox_app.common.config import UpstoxAppConfig

router = APIRouter(prefix="/upstox/candles", tags=["candles"])


@router.get("/status")
def candles_status():
    return {
        "enabled": UpstoxAppConfig.CANDLES_ENABLED,
        "load_on_startup": UpstoxAppConfig.CANDLES_LOAD_ON_STARTUP,
        "days": UpstoxAppConfig.CANDLES_DAYS,
        "unit": UpstoxAppConfig.CANDLES_UNIT,
        "interval": UpstoxAppConfig.CANDLES_INTERVAL,
        "include_intraday": UpstoxAppConfig.CANDLES_INCLUDE_INTRADAY,
        "fetch_delay_sec": UpstoxAppConfig.CANDLES_FETCH_DELAY_SEC,
    }


@router.post("/ensure")
def ensure_all():
    return candle_service.ensure_all_enabled()


@router.post("/ensure/{index_name}")
def ensure_one(index_name: str):
    name = index_name.upper()
    if name not in {"NIFTY", "SENSEX"} and name not in __import__("core.config", fromlist=["CoreConfig"]).CoreConfig.MAIN_INDEXES:
        raise HTTPException(status_code=404, detail=f"unknown index: {index_name}")
    return candle_service.ensure_index(name)