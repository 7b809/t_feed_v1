"""General-purpose chart WebSocket that streams refreshed candle data."""

import asyncio
import logging

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from core import config
from services.chart.chart_service import convert_to_chart_data, fetch_intraday_candles

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Chart market data"])


@router.websocket("/ws/chart")
async def chart_candles_socket(
    websocket: WebSocket,
    instrument_key: str = Query(..., min_length=1),
):
    key = instrument_key.strip()
    await websocket.accept()
    await websocket.send_json({"type": "connected", "instrument_key": key})
    try:
        while True:
            try:
                candles = await asyncio.to_thread(
                    fetch_intraday_candles,
                    key,
                    interval="minutes",
                    unit="1",
                )
                chart_candles = convert_to_chart_data(candles)
                if chart_candles:
                    await websocket.send_json({
                        "type": "candle",
                        "instrument_key": key,
                        "candle": chart_candles[-1],
                    })
                else:
                    await websocket.send_json({"type": "status", "status": "no_intraday_data"})
            except Exception as exc:
                logger.warning("Chart candle refresh failed for %s (%s).", key, type(exc).__name__)
                await websocket.send_json({"type": "status", "status": "temporarily_unavailable"})
            await asyncio.sleep(config.CHART_LIVE_POLL_SECONDS)
    except WebSocketDisconnect:
        logger.info("Chart WebSocket disconnected for %s.", key)
    except Exception as exc:
        logger.warning("Chart WebSocket closed for %s (%s).", key, type(exc).__name__)
