"""
upstox_app/market_quote/quote_router.py

HTTP endpoints for the quote service.

  GET  /upstox/quotes/status
  GET  /upstox/quotes/sample?index=NIFTY&count=10
  POST /upstox/quotes/batch      {"instrument_keys": [...], "interval": "1m"}
  POST /upstox/quotes/all

Default interval is 1m unless overridden in the request or via
QUOTES_INTERVAL in .env.
"""
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from core.logger import get_logger
from upstox_app.common.config import upstox_config
from upstox_app.market_quote.quote_service import quote_service

logger = get_logger(__name__)

router = APIRouter(prefix="/upstox/quotes", tags=["quotes"])


def _short(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {str(exc)[:150]}"


class QuoteBatchRequest(BaseModel):
    instrument_keys: List[str] = Field(..., min_length=1)
    interval: Optional[str] = None


@router.get("/status")
def status() -> Dict[str, Any]:
    return {
        "enabled": bool(getattr(upstox_config, "QUOTES_ENABLED", True)),
        "batch_size": int(getattr(upstox_config, "QUOTES_BATCH_SIZE", 10)),
        "interval": str(getattr(upstox_config, "QUOTES_INTERVAL", "1m")),
        "request_delay_sec": float(
            getattr(upstox_config, "QUOTES_REQUEST_DELAY_SEC", 0.2)
        ),
    }


@router.get("/sample")
def sample(
    index: str = Query("NIFTY"),
    count: int = Query(10, ge=1, le=500),
    interval: Optional[str] = Query(None),
) -> Dict[str, Any]:
    try:
        return quote_service.fetch_random_sample(
            index, count=count, interval=interval
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "quote_router::sample failed | step=service | reason=%s", _short(exc)
        )
        raise HTTPException(status_code=500, detail="sample failed")


@router.post("/batch")
def batch(payload: QuoteBatchRequest) -> Dict[str, Any]:
    try:
        return quote_service.fetch_ohlc_batch(
            payload.instrument_keys, interval=payload.interval
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "quote_router::batch failed | step=service | reason=%s", _short(exc)
        )
        raise HTTPException(status_code=500, detail="batch failed")


@router.post("/all")
def all_enabled() -> Dict[str, Any]:
    try:
        return quote_service.fetch_all_enabled()
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "quote_router::all_enabled failed | step=service | reason=%s", _short(exc)
        )
        raise HTTPException(status_code=500, detail="all_enabled failed")