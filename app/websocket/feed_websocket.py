import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.upstox_services.fetch_options import (
    options_cache,
    NIFTY_INDEX_FEED,
)
from app.websocket.websocket_manager import websocket_manager

logger = logging.getLogger("uvicorn")

router = APIRouter()


SUPPORTED_INTERVALS = [0, 1, 3, 5]


def get_nifty_index_key():
    """
    Returns Nifty index instrument key.
    """

    return NIFTY_INDEX_FEED.get(
        "instrument_key",
        "NSE_INDEX|Nifty 50",
    )


def is_nifty_index(
    instrument_key,
):
    """
    Checks whether given instrument key is Nifty index.
    """

    return instrument_key == get_nifty_index_key()


def find_instrument_by_strike(
    strike_price,
    instrument_type,
):
    """
    Resolve strike + type into option contract.

    Example:
        23400 + CE
        ->
        NSE_FO|63935
    """

    data = options_cache.get("data", [])

    instrument_type = instrument_type.upper().strip()

    for contract in data:

        if (
            contract.get("strike_price") is not None
            and float(contract.get("strike_price")) == float(strike_price)
            and contract.get("instrument_type") == instrument_type
        ):
            return contract

    return None


def feed_exists(
    instrument_key,
):
    """
    Checks whether feed exists in available instruments.

    Supports:
    - Nifty index
    - Filtered option instruments
    """

    if is_nifty_index(instrument_key):
        return True

    data = options_cache.get("data", [])

    for item in data:
        if item.get("instrument_key") == instrument_key:
            return True

    return False


def get_feed_metadata(
    instrument_key,
):
    """
    Returns feed metadata for subscription acknowledgement.
    """

    if is_nifty_index(instrument_key):
        return {
            **NIFTY_INDEX_FEED,
            "supported_intervals": SUPPORTED_INTERVALS,
        }

    data = options_cache.get("data", [])

    for item in data:
        if item.get("instrument_key") == instrument_key:
            return {
                **item,
                "supported_intervals": SUPPORTED_INTERVALS,
            }

    return None


@router.websocket("/ws/feed")
async def websocket_feed(
    websocket: WebSocket,
):
    """
    Generic Feed WebSocket.

    Client connects:

        ws://host/ws/feed

    Then sends subscription request.

    Nifty index live tick:

        {
            "instrument_key": "NSE_INDEX|Nifty 50",
            "interval": 0
        }

    Nifty index candle:

        {
            "instrument_key": "NSE_INDEX|Nifty 50",
            "interval": 1
        }

    Option live tick:

        {
            "instrument_key": "NSE_FO|63935",
            "interval": 0
        }

    Option candle:

        {
            "instrument_key": "NSE_FO|63935",
            "interval": 1
        }

    Option by strike:

        {
            "strike": 23400,
            "type": "CE",
            "interval": 1
        }

    Supported intervals for both Nifty and options:

        0 = live full tick
        1 = 1 minute candle
        3 = 3 minute candle
        5 = 5 minute candle
    """

    await websocket_manager.connect(websocket)

    active_subscriptions = []

    try:

        await websocket.send_json(
            {
                "status": "connected",
                "message": "Send subscription request.",
                "supported_intervals": SUPPORTED_INTERVALS,
                "examples": [
                    {
                        "instrument_key": "NSE_INDEX|Nifty 50",
                        "interval": 0,
                    },
                    {
                        "instrument_key": "NSE_INDEX|Nifty 50",
                        "interval": 1,
                    },
                    {
                        "instrument_key": "NSE_FO|63935",
                        "interval": 0,
                    },
                    {
                        "instrument_key": "NSE_FO|63935",
                        "interval": 1,
                    },
                    {
                        "strike": 23400,
                        "type": "CE",
                        "interval": 0,
                    },
                    {
                        "strike": 23400,
                        "type": "PE",
                        "interval": 1,
                    },
                ],
            }
        )

        while True:

            raw_message = await websocket.receive_text()

            try:
                request = json.loads(raw_message)

            except Exception:

                await websocket.send_json(
                    {
                        "status": "error",
                        "message": "Invalid JSON payload.",
                    }
                )

                continue

            # --------------------------------------------------
            # Parse interval
            # --------------------------------------------------

            try:
                interval = int(
                    request.get(
                        "interval",
                        0,
                    )
                )

            except Exception:

                await websocket.send_json(
                    {
                        "status": "error",
                        "message": "interval must be numeric. Allowed values: 0, 1, 3, 5.",
                    }
                )

                continue

            if interval not in SUPPORTED_INTERVALS:

                await websocket.send_json(
                    {
                        "status": "error",
                        "message": "Interval must be one of 0, 1, 3, 5.",
                    }
                )

                continue

            instrument_key = request.get("instrument_key")

            # --------------------------------------------------
            # Resolve strike + option type
            # --------------------------------------------------

            if not instrument_key:

                strike = request.get("strike")
                option_type = request.get("type")

                if strike is not None and option_type:

                    contract = find_instrument_by_strike(
                        strike_price=strike,
                        instrument_type=option_type,
                    )

                    if not contract:

                        await websocket.send_json(
                            {
                                "status": "error",
                                "message": (
                                    f"Contract not found for strike={strike}, "
                                    f"type={option_type}."
                                ),
                            }
                        )

                        continue

                    instrument_key = contract.get("instrument_key")

            if not instrument_key:

                await websocket.send_json(
                    {
                        "status": "error",
                        "message": "Provide either instrument_key or strike + type.",
                    }
                )

                continue

            # --------------------------------------------------
            # Validate feed exists
            # --------------------------------------------------

            if not feed_exists(instrument_key):

                await websocket.send_json(
                    {
                        "status": "error",
                        "message": f"Feed not found: {instrument_key}",
                    }
                )

                continue

            feed_metadata = get_feed_metadata(instrument_key)

            # --------------------------------------------------
            # Subscribe
            # --------------------------------------------------

            subscription_key = (
                instrument_key,
                interval,
            )

            websocket_manager.subscribe(
                websocket=websocket,
                instrument_key=instrument_key,
                interval=interval,
            )

            if subscription_key not in active_subscriptions:
                active_subscriptions.append(subscription_key)

            await websocket.send_json(
                {
                    "status": "subscribed",
                    "instrument_key": instrument_key,
                    "interval": interval,
                    "feed_metadata": feed_metadata,
                }
            )

            logger.info(f"WS client subscribed {instrument_key} interval={interval}")

    except WebSocketDisconnect:

        logger.info("WebSocket client disconnected.")

    except Exception as ex:

        logger.exception(f"Feed websocket error: {ex}")

    finally:

        for instrument_key, interval in active_subscriptions:

            websocket_manager.unsubscribe(
                websocket=websocket,
                instrument_key=instrument_key,
                interval=interval,
            )

        await websocket_manager.disconnect(websocket)
