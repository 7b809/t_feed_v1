import asyncio
from datetime import datetime, timezone

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query

from core.logger import get_logger
from services.option_service import options_cache
from ws_feed.broadcaster import broadcaster

logger = get_logger(__file__)

router = APIRouter()


# ============================================================
# Helper: Resolve Instrument Key
# ============================================================


def resolve_instrument_key(
    instrument_key: str | None = None,
    strike: float | None = None,
    striketype: str | None = None,
) -> str | None:
    """
    Resolves instrument key.

    Supports:
    1. Direct instrument_key
       Example:
           NSE_INDEX|Nifty 50
           NSE_FO|41012

    2. strike + striketype
       Example:
           strike=24500, striketype=ce
    """

    if instrument_key:
        return instrument_key

    if strike is None or not striketype:
        return None

    option_type = str(striketype).upper()

    if option_type not in ["CE", "PE"]:
        return None

    cache_data = options_cache.get("data", [])

    for item in cache_data:
        try:
            item_strike = item.get("strike_price")
            item_type = str(item.get("instrument_type", "")).upper()

            if float(item_strike) == float(strike) and item_type == option_type:
                return item.get("instrument_key")

        except Exception:
            continue

    return None


def resolve_ema_instrument_key(
    instrument_key: str | None = None,
    strike: float | None = None,
    striketype: str | None = None,
) -> str | None:
    """Resolves EMA crossover instrument key."""

    return resolve_instrument_key(
        instrument_key=instrument_key,
        strike=strike,
        striketype=striketype,
    )


def resolve_opening_range_instrument_key(
    instrument_key: str | None = None,
    strike: float | None = None,
    striketype: str | None = None,
) -> str | None:
    """Resolves Opening Range instrument key."""

    return resolve_instrument_key(
        instrument_key=instrument_key,
        strike=strike,
        striketype=striketype,
    )


def resolve_or_ema_strategy_instrument_key(
    instrument_key: str | None = None,
    strike: float | None = None,
    striketype: str | None = None,
) -> str | None:
    """Resolves OR + EMA strategy instrument key."""

    return resolve_instrument_key(
        instrument_key=instrument_key,
        strike=strike,
        striketype=striketype,
    )


# ============================================================
# All Feeds WebSocket
# ============================================================


@router.websocket("/ws")
@router.websocket("/all-feeds")
async def websocket_all_feeds(websocket: WebSocket):
    """
    WebSocket endpoint returning live market feeds for all loaded contracts.

    This can also receive:
    - live ticks
    - live EMA crossover events
    - Opening Range touch events
    - optional OR + EMA strategy events

    EMA crossover events may include opening_range context.
    """

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
                "message": (
                    "Connected to all feeds websocket. Waiting for live ticks, "
                    "EMA crossover events, Opening Range events, and optional "
                    "OR + EMA strategy events."
                ),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "subscribed_instruments": len(options_cache.get("subscribed_keys", [])),
                "ema_opening_range_enrichment": True,
                "or_ema_strategy_events": "optional",
                "or_ema_strategy_selection": "first_touch_same_time_nearest_to_nifty",
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
                            "endpoint": "/all-feeds",
                            "message": client_message,
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        }
                    )

            except asyncio.TimeoutError:
                await websocket.send_json(
                    {
                        "type": "ping",
                        "endpoint": "/all-feeds",
                        "message": (
                            "WebSocket alive. Waiting for live ticks, EMA crossover "
                            "events, Opening Range events, and optional strategy events."
                        ),
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


# ============================================================
# Option WebSocket
# ============================================================


@router.websocket("/option")
async def websocket_option(
    websocket: WebSocket,
    strike: float = Query(..., description="Strike Price, e.g., 24500"),
    striketype: str = Query(..., description="Option type: ce or pe"),
):
    """
    WebSocket endpoint filtering live feeds by strike price and CE/PE.

    This endpoint can receive:
    - matching live option ticks
    - matching EMA crossover events
    - matching Opening Range events
    - optional matching OR + EMA strategy events
    """

    itype = str(striketype).upper()

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
                    "endpoint": "/option",
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
                "message": (
                    "Connected to option websocket. Waiting for matching option ticks, "
                    "EMA crossover events, Opening Range events, and optional "
                    "OR + EMA strategy events."
                ),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "ema_opening_range_enrichment": True,
                "or_ema_strategy_events": "optional",
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
                            "endpoint": "/option",
                            "strike": strike,
                            "striketype": itype,
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
                        "message": (
                            "WebSocket alive. Waiting for matching option ticks, "
                            "EMA crossover events, Opening Range events, and optional "
                            "strategy events."
                        ),
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


# ============================================================
# Global EMA Crossover WebSocket
# ============================================================


@router.websocket("/ws/ema-crossover")
async def websocket_ema_crossover(websocket: WebSocket):
    """
    Dedicated WebSocket endpoint streaming real-time EMA crossover events.

    New flow:
    - Receives EMA crossover events for all initialized instruments.
    - Events are not filtered by legacy selected Opening Range instrument.
    - Each event may include opening_range levels when available.
    - Telegram EMA alerts are disabled.
    - OR + EMA strategy alerting is handled separately.
    """

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
                "message": (
                    "Connected to EMA Crossover feed. Waiting for EMA crossover "
                    "signals enriched with Opening Range levels when available."
                ),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "scope": "all_instruments",
                "ema_opening_range_enrichment": True,
                "selected_or_filtering": "disabled",
                "telegram_ema_alerts": "disabled",
                "or_ema_strategy_alerts": "handled_separately",
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
                            "endpoint": "/ws/ema-crossover",
                            "message": client_message,
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        }
                    )

            except asyncio.TimeoutError:
                await websocket.send_json(
                    {
                        "type": "ping",
                        "endpoint": "/ws/ema-crossover",
                        "message": (
                            "WebSocket alive. Waiting for EMA crossover signals "
                            "with Opening Range context."
                        ),
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


# ============================================================
# Instrument-Specific EMA Crossover WebSocket
# ============================================================


@router.websocket("/ws/ema-crossover/instrument")
async def websocket_ema_crossover_instrument(
    websocket: WebSocket,
    instrument_key: str = Query(
        default=None,
        description="Instrument key, e.g. NSE_INDEX|Nifty 50 or NSE_FO|41012",
    ),
    strike: float = Query(
        default=None,
        description="Option strike price, e.g. 24500. Optional if instrument_key is provided.",
    ),
    striketype: str = Query(
        default=None,
        description="Option type: ce or pe. Optional if instrument_key is provided.",
    ),
):
    """
    Dedicated WebSocket endpoint for one instrument's live EMA crossover events.
    """

    resolved_instrument_key = resolve_ema_instrument_key(
        instrument_key=instrument_key,
        strike=strike,
        striketype=striketype,
    )

    logger.info(
        f"Incoming websocket request for /ws/ema-crossover/instrument from "
        f"{websocket.client}. "
        f"instrument_key={instrument_key}, "
        f"strike={strike}, "
        f"striketype={striketype}, "
        f"resolved_instrument_key={resolved_instrument_key}"
    )

    if not resolved_instrument_key:
        try:
            await websocket.accept()

            await websocket.send_json(
                {
                    "type": "error",
                    "endpoint": "/ws/ema-crossover/instrument",
                    "message": (
                        "Unable to resolve instrument. Provide either instrument_key "
                        "or valid strike + striketype."
                    ),
                    "input": {
                        "instrument_key": instrument_key,
                        "strike": strike,
                        "striketype": striketype,
                    },
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            )

            await websocket.close(
                code=1008,
                reason="Unable to resolve EMA crossover instrument",
            )

        except Exception as ex:
            logger.error(
                f"Error while closing unresolved EMA instrument websocket: "
                f"{type(ex).__name__}: {ex}"
            )

        return

    try:
        await websocket.accept()

        logger.info(
            f"WebSocket accepted for /ws/ema-crossover/instrument. "
            f"resolved_instrument_key={resolved_instrument_key}"
        )

        if hasattr(broadcaster, "connect_ema_instrument"):
            await broadcaster.connect_ema_instrument(
                websocket=websocket,
                instrument_key=resolved_instrument_key,
            )
        else:
            await broadcaster.connect(websocket)

        await websocket.send_json(
            {
                "type": "connected",
                "endpoint": "/ws/ema-crossover/instrument",
                "message": (
                    "Connected to instrument-specific EMA crossover feed. "
                    "Waiting for EMA crossover signals enriched with Opening Range "
                    "levels when available."
                ),
                "instrument_key": resolved_instrument_key,
                "input": {
                    "instrument_key": instrument_key,
                    "strike": strike,
                    "striketype": striketype,
                },
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "scope": "single_instrument",
                "ema_opening_range_enrichment": True,
                "selected_or_filtering": "disabled",
                "telegram_ema_alerts": "disabled",
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
                            "endpoint": "/ws/ema-crossover/instrument",
                            "instrument_key": resolved_instrument_key,
                            "message": client_message,
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        }
                    )

            except asyncio.TimeoutError:
                await websocket.send_json(
                    {
                        "type": "ping",
                        "endpoint": "/ws/ema-crossover/instrument",
                        "instrument_key": resolved_instrument_key,
                        "message": (
                            "WebSocket alive. Waiting for instrument-specific "
                            "EMA crossover signals with Opening Range context."
                        ),
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                )

    except WebSocketDisconnect:
        logger.info(
            f"Client disconnected from /ws/ema-crossover/instrument websocket. "
            f"instrument_key={resolved_instrument_key}"
        )

    except Exception as ex:
        logger.error(
            f"/ws/ema-crossover/instrument websocket error for "
            f"{resolved_instrument_key}: {type(ex).__name__}: {ex}"
        )

    finally:
        if hasattr(broadcaster, "disconnect_ema_instrument"):
            broadcaster.disconnect_ema_instrument(
                websocket=websocket,
                instrument_key=resolved_instrument_key,
            )
        else:
            broadcaster.disconnect(websocket)


# ============================================================
# Global Opening Range WebSocket
# ============================================================


@router.websocket("/ws/opening-range")
async def websocket_opening_range(websocket: WebSocket):
    """
    Dedicated WebSocket endpoint streaming Opening Range events.

    New flow:
    - Receives Opening Range events for all instruments.
    - Touch events do not use legacy selected OR lock.
    - OR + EMA strategy selected touch is handled separately.
    """

    logger.info(
        f"Incoming websocket request for /ws/opening-range from {websocket.client}"
    )

    try:
        await websocket.accept()
        logger.info("WebSocket accepted for /ws/opening-range")

        if hasattr(broadcaster, "connect_opening_range"):
            await broadcaster.connect_opening_range(websocket)
        else:
            await broadcaster.connect(websocket)

        await websocket.send_json(
            {
                "type": "connected",
                "endpoint": "/ws/opening-range",
                "message": (
                    "Connected to Opening Range feed. Waiting for Opening Range "
                    "events for all instruments."
                ),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "scope": "all_instruments",
                "selected_or_filtering": "disabled",
                "or_ema_strategy_selection": "handled_separately",
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
                            "endpoint": "/ws/opening-range",
                            "message": client_message,
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        }
                    )

            except asyncio.TimeoutError:
                await websocket.send_json(
                    {
                        "type": "ping",
                        "endpoint": "/ws/opening-range",
                        "message": "WebSocket alive. Waiting for Opening Range events.",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                )

    except WebSocketDisconnect:
        logger.info("Client disconnected from /ws/opening-range websocket")

    except Exception as ex:
        logger.error(f"/ws/opening-range websocket error: {type(ex).__name__}: {ex}")

    finally:
        if hasattr(broadcaster, "disconnect_opening_range"):
            broadcaster.disconnect_opening_range(websocket)
        else:
            broadcaster.disconnect(websocket)


# ============================================================
# Instrument-Specific Opening Range WebSocket
# ============================================================


@router.websocket("/ws/opening-range/instrument")
async def websocket_opening_range_instrument(
    websocket: WebSocket,
    instrument_key: str = Query(
        default=None,
        description="Instrument key, e.g. NSE_INDEX|Nifty 50 or NSE_FO|41012",
    ),
    strike: float = Query(
        default=None,
        description="Option strike price, e.g. 24500. Optional if instrument_key is provided.",
    ),
    striketype: str = Query(
        default=None,
        description="Option type: ce or pe. Optional if instrument_key is provided.",
    ),
):
    """
    Dedicated WebSocket endpoint for one instrument's Opening Range events.
    """

    resolved_instrument_key = resolve_opening_range_instrument_key(
        instrument_key=instrument_key,
        strike=strike,
        striketype=striketype,
    )

    logger.info(
        f"Incoming websocket request for /ws/opening-range/instrument from "
        f"{websocket.client}. "
        f"instrument_key={instrument_key}, "
        f"strike={strike}, "
        f"striketype={striketype}, "
        f"resolved_instrument_key={resolved_instrument_key}"
    )

    if not resolved_instrument_key:
        try:
            await websocket.accept()

            await websocket.send_json(
                {
                    "type": "error",
                    "endpoint": "/ws/opening-range/instrument",
                    "message": (
                        "Unable to resolve instrument. Provide either instrument_key "
                        "or valid strike + striketype."
                    ),
                    "input": {
                        "instrument_key": instrument_key,
                        "strike": strike,
                        "striketype": striketype,
                    },
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            )

            await websocket.close(
                code=1008,
                reason="Unable to resolve Opening Range instrument",
            )

        except Exception as ex:
            logger.error(
                f"Error while closing unresolved Opening Range instrument websocket: "
                f"{type(ex).__name__}: {ex}"
            )

        return

    try:
        await websocket.accept()

        logger.info(
            f"WebSocket accepted for /ws/opening-range/instrument. "
            f"resolved_instrument_key={resolved_instrument_key}"
        )

        if hasattr(broadcaster, "connect_opening_range_instrument"):
            await broadcaster.connect_opening_range_instrument(
                websocket=websocket,
                instrument_key=resolved_instrument_key,
            )
        else:
            await broadcaster.connect(websocket)

        await websocket.send_json(
            {
                "type": "connected",
                "endpoint": "/ws/opening-range/instrument",
                "message": (
                    "Connected to instrument-specific Opening Range feed. "
                    "Waiting for Opening Range events."
                ),
                "instrument_key": resolved_instrument_key,
                "input": {
                    "instrument_key": instrument_key,
                    "strike": strike,
                    "striketype": striketype,
                },
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "scope": "single_instrument",
                "selected_or_filtering": "disabled",
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
                            "endpoint": "/ws/opening-range/instrument",
                            "instrument_key": resolved_instrument_key,
                            "message": client_message,
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        }
                    )

            except asyncio.TimeoutError:
                await websocket.send_json(
                    {
                        "type": "ping",
                        "endpoint": "/ws/opening-range/instrument",
                        "instrument_key": resolved_instrument_key,
                        "message": (
                            "WebSocket alive. Waiting for instrument-specific "
                            "Opening Range events."
                        ),
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                )

    except WebSocketDisconnect:
        logger.info(
            f"Client disconnected from /ws/opening-range/instrument websocket. "
            f"instrument_key={resolved_instrument_key}"
        )

    except Exception as ex:
        logger.error(
            f"/ws/opening-range/instrument websocket error for "
            f"{resolved_instrument_key}: {type(ex).__name__}: {ex}"
        )

    finally:
        if hasattr(broadcaster, "disconnect_opening_range_instrument"):
            broadcaster.disconnect_opening_range_instrument(
                websocket=websocket,
                instrument_key=resolved_instrument_key,
            )
        else:
            broadcaster.disconnect(websocket)


# ============================================================
# Global OR + EMA Strategy WebSocket
# ============================================================


@router.websocket("/ws/or-ema-strategy")
async def websocket_or_ema_strategy(websocket: WebSocket):
    """
    Dedicated WebSocket endpoint streaming OR + EMA strategy events.

    Strategy selection rule:
    - First eligible touch wins.
    - If multiple eligible instruments touch at the same timestamp/candle,
      nearest strike to current NIFTY spot wins.
    - Only selected touched instrument can trigger strategy confirmation.
    - Non-selected touches are retained for debug only.
    """

    logger.info(
        f"Incoming websocket request for /ws/or-ema-strategy from {websocket.client}"
    )

    try:
        await websocket.accept()
        logger.info("WebSocket accepted for /ws/or-ema-strategy")

        if hasattr(broadcaster, "connect_or_ema_strategy"):
            await broadcaster.connect_or_ema_strategy(websocket)
        else:
            await broadcaster.connect(websocket)

        await websocket.send_json(
            {
                "type": "connected",
                "endpoint": "/ws/or-ema-strategy",
                "message": (
                    "Connected to OR + EMA Strategy feed. Waiting for strategy "
                    "selection and alert events."
                ),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "scope": "all_instruments",
                "selection_mode": "first_touch_same_time_nearest_to_nifty",
                "telegram_alerts": "enabled_when_configured",
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
                            "endpoint": "/ws/or-ema-strategy",
                            "message": client_message,
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        }
                    )

            except asyncio.TimeoutError:
                await websocket.send_json(
                    {
                        "type": "ping",
                        "endpoint": "/ws/or-ema-strategy",
                        "message": (
                            "WebSocket alive. Waiting for OR + EMA strategy events."
                        ),
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                )

    except WebSocketDisconnect:
        logger.info("Client disconnected from /ws/or-ema-strategy websocket")

    except Exception as ex:
        logger.error(f"/ws/or-ema-strategy websocket error: {type(ex).__name__}: {ex}")

    finally:
        if hasattr(broadcaster, "disconnect_or_ema_strategy"):
            broadcaster.disconnect_or_ema_strategy(websocket)
        else:
            broadcaster.disconnect(websocket)


# ============================================================
# Instrument-Specific OR + EMA Strategy WebSocket
# ============================================================


@router.websocket("/ws/or-ema-strategy/instrument")
async def websocket_or_ema_strategy_instrument(
    websocket: WebSocket,
    instrument_key: str = Query(
        default=None,
        description="Instrument key, e.g. NSE_FO|41012 or NSE_INDEX|Nifty 50",
    ),
    strike: float = Query(
        default=None,
        description="Option strike price, e.g. 24500. Optional if instrument_key is provided.",
    ),
    striketype: str = Query(
        default=None,
        description="Option type: ce or pe. Optional if instrument_key is provided.",
    ),
):
    """
    Dedicated WebSocket endpoint for one instrument's OR + EMA strategy events.

    Supports:
        /ws/or-ema-strategy/instrument?instrument_key=NSE_FO%7C41012
        /ws/or-ema-strategy/instrument?strike=24500&striketype=ce
        /ws/or-ema-strategy/instrument?strike=24500&striketype=pe
    """

    resolved_instrument_key = resolve_or_ema_strategy_instrument_key(
        instrument_key=instrument_key,
        strike=strike,
        striketype=striketype,
    )

    logger.info(
        f"Incoming websocket request for /ws/or-ema-strategy/instrument from "
        f"{websocket.client}. "
        f"instrument_key={instrument_key}, "
        f"strike={strike}, "
        f"striketype={striketype}, "
        f"resolved_instrument_key={resolved_instrument_key}"
    )

    if not resolved_instrument_key:
        try:
            await websocket.accept()

            await websocket.send_json(
                {
                    "type": "error",
                    "endpoint": "/ws/or-ema-strategy/instrument",
                    "message": (
                        "Unable to resolve instrument. Provide either instrument_key "
                        "or valid strike + striketype."
                    ),
                    "input": {
                        "instrument_key": instrument_key,
                        "strike": strike,
                        "striketype": striketype,
                    },
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            )

            await websocket.close(
                code=1008,
                reason="Unable to resolve OR EMA strategy instrument",
            )

        except Exception as ex:
            logger.error(
                f"Error while closing unresolved OR EMA strategy instrument websocket: "
                f"{type(ex).__name__}: {ex}"
            )

        return

    try:
        await websocket.accept()

        logger.info(
            f"WebSocket accepted for /ws/or-ema-strategy/instrument. "
            f"resolved_instrument_key={resolved_instrument_key}"
        )

        if hasattr(broadcaster, "connect_or_ema_strategy_instrument"):
            await broadcaster.connect_or_ema_strategy_instrument(
                websocket=websocket,
                instrument_key=resolved_instrument_key,
            )
        else:
            await broadcaster.connect(websocket)

        await websocket.send_json(
            {
                "type": "connected",
                "endpoint": "/ws/or-ema-strategy/instrument",
                "message": (
                    "Connected to instrument-specific OR + EMA Strategy feed. "
                    "Waiting for strategy events."
                ),
                "instrument_key": resolved_instrument_key,
                "input": {
                    "instrument_key": instrument_key,
                    "strike": strike,
                    "striketype": striketype,
                },
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "scope": "single_instrument",
                "selection_mode": "first_touch_same_time_nearest_to_nifty",
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
                            "endpoint": "/ws/or-ema-strategy/instrument",
                            "instrument_key": resolved_instrument_key,
                            "message": client_message,
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        }
                    )

            except asyncio.TimeoutError:
                await websocket.send_json(
                    {
                        "type": "ping",
                        "endpoint": "/ws/or-ema-strategy/instrument",
                        "instrument_key": resolved_instrument_key,
                        "message": (
                            "WebSocket alive. Waiting for instrument-specific "
                            "OR + EMA strategy events."
                        ),
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                )

    except WebSocketDisconnect:
        logger.info(
            f"Client disconnected from /ws/or-ema-strategy/instrument websocket. "
            f"instrument_key={resolved_instrument_key}"
        )

    except Exception as ex:
        logger.error(
            f"/ws/or-ema-strategy/instrument websocket error for "
            f"{resolved_instrument_key}: {type(ex).__name__}: {ex}"
        )

    finally:
        if hasattr(broadcaster, "disconnect_or_ema_strategy_instrument"):
            broadcaster.disconnect_or_ema_strategy_instrument(
                websocket=websocket,
                instrument_key=resolved_instrument_key,
            )
        else:
            broadcaster.disconnect(websocket)
