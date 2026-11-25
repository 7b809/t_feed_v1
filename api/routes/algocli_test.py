"""Authenticated test endpoint for AlgoCLI prompt generation and GTT placement."""
from __future__ import annotations

import hmac

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from core.config import (
    ALGOCLI_TEST_API_ENABLED,
    ALGOCLI_TEST_API_KEY,
    ALGOCLI_TEST_LIVE_WRITES_ENABLED,
)
from core.logger import get_logger
from services.algocli_test_service import AlgoCLITestService

logger = get_logger(__file__)
router = APIRouter(prefix="/api/algocli-test", tags=["algocli-test"])


class PlaceGTTTestRequest(BaseModel):
    # Accepted for compatibility with the caller payload, but intentionally
    # excluded from serialization, prompt construction, logs and disk.
    api_token: str | None = Field(default=None, exclude=True)
    instrument_key: str = Field(min_length=1, max_length=128)
    trading_symbol: str = Field(min_length=1, max_length=128)
    entry: float | None = None
    target: float = Field(gt=0)
    stoploss: float = Field(gt=0)
    lots: int = Field(default=1, ge=1, le=100)
    confirm_live_order: bool = False
    operation_id: str | None = Field(default=None, max_length=64)


@router.post("/place-gtt")
def test_place_gtt(
    body: PlaceGTTTestRequest,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    """Generate and archive a place_gtt prompt; execute only behind two gates."""
    if not ALGOCLI_TEST_API_ENABLED:
        raise HTTPException(status_code=404, detail="AlgoCLI test API is disabled.")
    if not ALGOCLI_TEST_API_KEY:
        raise HTTPException(status_code=503, detail="ALGOCLI_TEST_API_KEY is not configured.")
    if not x_api_key or not hmac.compare_digest(x_api_key, ALGOCLI_TEST_API_KEY):
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key.")
    if body.entry is not None:
        raise HTTPException(
            status_code=422,
            detail="Only entry=null (immediate market entry) is supported by this test endpoint.",
        )
    try:
        return AlgoCLITestService().place_gtt(
            instrument_key=body.instrument_key,
            trading_symbol=body.trading_symbol,
            entry=body.entry,
            target=body.target,
            stoploss=body.stoploss,
            lots=body.lots,
            confirm_live_order=body.confirm_live_order,
            live_writes_enabled=ALGOCLI_TEST_LIVE_WRITES_ENABLED,
            operation_id=body.operation_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("AlgoCLI test API failed")
        raise HTTPException(status_code=500, detail="AlgoCLI test request could not be processed.") from exc


@router.get("/get-positions")
def test_get_positions(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    """Run the read-only get_positions prompt to verify live AlgoAI health."""
    if not ALGOCLI_TEST_API_ENABLED:
        raise HTTPException(status_code=404, detail="AlgoCLI test API is disabled.")
    if not ALGOCLI_TEST_API_KEY:
        raise HTTPException(status_code=503, detail="ALGOCLI_TEST_API_KEY is not configured.")
    if not x_api_key or not hmac.compare_digest(x_api_key, ALGOCLI_TEST_API_KEY):
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key.")

    try:
        result = AlgoCLITestService().get_positions_health()
        if result.get("status") != "HEALTHY":
            return JSONResponse(status_code=502, content=result)
        return result
    except Exception as exc:
        logger.exception("AlgoAI get-positions health check failed")
        raise HTTPException(
            status_code=500,
            detail="AlgoAI get-positions health check could not be processed.",
        ) from exc
