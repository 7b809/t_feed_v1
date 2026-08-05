import asyncio
from datetime import datetime, timezone

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query

from core.logger import get_logger
from services.option_service import options_cache
from ws_feed.broadcaster import broadcaster

logger = get_logger(__file__)

router = APIRouter()


@router.websocket("/ws")
@router.websocket("/all-feeds")
async def websocket_all_feeds(websocket: WebSocket):
    """WebSocket endpoint returning live market feeds for all loaded contracts."""

    logger.info(f"Incoming websocket request for /all-feeds from {websocket.client}")

    try:
        await websocket.accept()
        logger.info("WebSocket accepted for /all-feeds")

        if hasattr(broadcaster, "connect_all_feeds"):
            await broadcaster.connect_all_feeds(websocket)
        else:
            await broadcaster.connect(websocket)

        await websocket.send_json(
            {
                "type": "connected",
                "endpoint": "/all-feeds",
                "message": "Connected to all feeds websocket. Waiting for live market ticks.",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "subscribed_instruments": len(options_cache.get("subscribed_keys", [])),
            }
        )

        while True:
            try:
                client_message = await asyncio.wait_for(
                    websocket.receive_text(),
                    timeout=30,
                )

                if client_message:
                    await websocket.send_json(
                        {
                            "type": "client_message_received",
                            "message": client_message,
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        }
                    )

            except asyncio.TimeoutError:
                await websocket.send_json(
                    {
                        "type": "ping",
                        "endpoint": "/all-feeds",
                        "message": "WebSocket alive. Waiting for live ticks.",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                )

    except WebSocketDisconnect:
        logger.info("Client disconnected from /all-feeds websocket")

    except Exception as ex:
        logger.error(f"/all-feeds websocket error: {type(ex).__name__}: {ex}")

    finally:
        if hasattr(broadcaster, "disconnect_all_feeds"):
            broadcaster.disconnect_all_feeds(websocket)
        else:
            broadcaster.disconnect(websocket)


@router.websocket("/option")
async def websocket_option(
    websocket: WebSocket,
    strike: float = Query(..., description="Strike Price, e.g., 24500"),
    striketype: str = Query(..., description="Option type: ce or pe"),
):
    """WebSocket endpoint filtering live feeds by strike price and CE/PE."""

    itype = striketype.upper()

    logger.info(
        f"Incoming websocket request for /option from {websocket.client}. "
        f"strike={strike}, striketype={itype}"
    )

    if itype not in ["CE", "PE"]:
        try:
            await websocket.accept()
            await websocket.send_json(
                {
                    "type": "error",
                    "message": "Invalid striketype. Value must be 'ce' or 'pe'.",
                    "received": striketype,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            )
            await websocket.close(code=1008, reason="striketype must be 'ce' or 'pe'")
        except Exception as ex:
            logger.error(
                f"Error while closing invalid /option websocket: "
                f"{type(ex).__name__}: {ex}"
            )
        return

    try:
        await websocket.accept()
        logger.info(f"WebSocket accepted for /option {strike}_{itype}")

        if hasattr(broadcaster, "connect_option"):
            await broadcaster.connect_option(
                websocket,
                strike_price=strike,
                instrument_type=itype,
            )
        else:
            await broadcaster.connect(websocket)

        logger.info(f"Option client registered for {strike}_{itype}")

        await websocket.send_json(
            {
                "type": "connected",
                "endpoint": "/option",
                "strike": strike,
                "striketype": itype,
                "message": "Connected to option websocket. Waiting for matching option ticks.",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        )

        while True:
            try:
                client_message = await asyncio.wait_for(
                    websocket.receive_text(),
                    timeout=30,
                )

                if client_message:
                    await websocket.send_json(
                        {
                            "type": "client_message_received",
                            "message": client_message,
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        }
                    )

            except asyncio.TimeoutError:
                await websocket.send_json(
                    {
                        "type": "ping",
                        "endpoint": "/option",
                        "strike": strike,
                        "striketype": itype,
                        "message": "WebSocket alive. Waiting for matching option ticks.",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                )

    except WebSocketDisconnect:
        logger.info(f"Client disconnected from /option websocket: {strike}_{itype}")

    except Exception as ex:
        logger.error(
            f"/option websocket error for {strike}_{itype}: "
            f"{type(ex).__name__}: {ex}"
        )

    finally:
        if hasattr(broadcaster, "disconnect_option"):
            broadcaster.disconnect_option(websocket, strike, itype)
        else:
            broadcaster.disconnect(websocket)


@router.websocket("/ws/ema-crossover")
async def websocket_ema_crossover(websocket: WebSocket):
    """Dedicated WebSocket endpoint streaming real-time EMA crossover events."""

    logger.info(
        f"Incoming websocket request for /ws/ema-crossover from {websocket.client}"
    )

    try:
        await websocket.accept()
        logger.info("WebSocket accepted for /ws/ema-crossover")

        if hasattr(broadcaster, "connect_ema_crossover"):
            await broadcaster.connect_ema_crossover(websocket)
        else:
            await broadcaster.connect(websocket)

        await websocket.send_json(
            {
                "type": "connected",
                "endpoint": "/ws/ema-crossover",
                "message": "Connected to EMA Crossover feed. Waiting for crossover signals.",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        )

        while True:
            try:
                client_message = await asyncio.wait_for(
                    websocket.receive_text(),
                    timeout=30,
                )

                if client_message:
                    await websocket.send_json(
                        {
                            "type": "client_message_received",
                            "message": client_message,
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        }
                    )

            except asyncio.TimeoutError:
                await websocket.send_json(
                    {
                        "type": "ping",
                        "endpoint": "/ws/ema-crossover",
                        "message": "WebSocket alive. Waiting for EMA crossover signals.",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                )

    except WebSocketDisconnect:
        logger.info("Client disconnected from /ws/ema-crossover websocket")

    except Exception as ex:
        logger.error(f"/ws/ema-crossover websocket error: {type(ex).__name__}: {ex}")

    finally:
        if hasattr(broadcaster, "disconnect_ema_crossover"):
            broadcaster.disconnect_ema_crossover(websocket)
        else:
            broadcaster.disconnect(websocket)
