"""
upstox_app/router.py
HTTP API for the Upstox market + portfolio streamers.

Subscribe / unsubscribe / change-mode are IDEMPOTENT:
    - repeating the same call is a no-op (skipped list is returned)
    - changing mode on an existing key issues a change_mode under the hood

Endpoints never return the access token.
"""
from fastapi import APIRouter, HTTPException

from core.logger import get_logger
from upstox_app.market.market_streamer import market_streamer
from upstox_app.portfolio.portfolio_streamer import portfolio_streamer
from upstox_app.api.schemas import (
    ActionResponse,
    ChangeModeRequest,
    ConnectionStateResponse,
    MessagesResponse,
    ReconnectRequest,
    StreamerListStatus,
    StreamerStatus,
    SubscribeRequest,
    UnsubscribeRequest,
    WSStatusResponse,
)
from upstox_app.streamer.streamer_manager import start_all, status_all, stop_all
from upstox_app.streamer.ws_manager import fanout_event, ws_manager

logger = get_logger(__name__)

router = APIRouter(prefix="/upstox", tags=["upstox"])

# ── helpers ──────────────────────────────────────────────────────
def _jsonable(message) -> dict:
    try:
        if hasattr(message, "to_dict"):
            return message.to_dict()
        if isinstance(message, dict):
            return message
        return {"raw": str(message)}
    except Exception:  # noqa: BLE001
        return {"raw": str(message)}

# ── global status / lifecycle ────────────────────────────────────
@router.get("/streamers", response_model=StreamerListStatus)
def list_streamers() -> StreamerListStatus:
    logger.info("GET /upstox/streamers")
    return StreamerListStatus(**status_all())

@router.post("/streamers/start", response_model=ActionResponse)
def start_streamers() -> ActionResponse:
    logger.info("POST /upstox/streamers/start")
    result = start_all()
    ok = all(result.values())
    return ActionResponse(ok=ok, message=f"start={result}")

@router.post("/streamers/stop", response_model=ActionResponse)
def stop_streamers() -> ActionResponse:
    logger.info("POST /upstox/streamers/stop")
    result = stop_all()
    ok = all(result.values())
    return ActionResponse(ok=ok, message=f"stop={result}")

# ── market streamer ──────────────────────────────────────────────
@router.get("/market/status", response_model=StreamerStatus)
def market_status() -> StreamerStatus:
    logger.info("GET /upstox/market/status")
    return StreamerStatus(**market_streamer.status())

@router.post("/market/connect", response_model=ConnectionStateResponse)
def market_connect() -> ConnectionStateResponse:
    logger.info("POST /upstox/market/connect")
    ok = market_streamer.connect()
    if not ok:
        raise HTTPException(status_code=502, detail="Failed to connect market streamer")
    return ConnectionStateResponse(connected=True, name="market")

@router.post("/market/disconnect", response_model=ConnectionStateResponse)
def market_disconnect() -> ConnectionStateResponse:
    logger.info("POST /upstox/market/disconnect")
    ok = market_streamer.disconnect()
    if not ok:
        raise HTTPException(status_code=502, detail="Failed to disconnect market streamer")
    return ConnectionStateResponse(connected=False, name="market")

@router.post("/market/reconnect", response_model=ConnectionStateResponse)
def market_reconnect(payload: ReconnectRequest) -> ConnectionStateResponse:
    logger.info(
        "POST /upstox/market/reconnect | auto=%s | interval=%s | retries=%s",
        payload.auto_reconnect_enabled, payload.interval_seconds, payload.retry_count,
    )
    from upstox_app.common.config import upstox_config
    upstox_config.AUTO_RECONNECT_ENABLED = payload.auto_reconnect_enabled
    if payload.interval_seconds is not None:
        upstox_config.AUTO_RECONNECT_INTERVAL_SEC = payload.interval_seconds
    if payload.retry_count is not None:
        upstox_config.AUTO_RECONNECT_RETRY_COUNT = payload.retry_count

    ok = market_streamer.reconnect_with_latest_token()
    if not ok:
        raise HTTPException(status_code=502, detail="Failed to reconnect market streamer")
    return ConnectionStateResponse(connected=True, name="market")

@router.post("/market/subscribe", response_model=ActionResponse)
def market_subscribe(payload: SubscribeRequest) -> ActionResponse:
    logger.info("POST /upstox/market/subscribe | mode=%s | keys=%d", payload.mode, len(payload.instrument_keys))
    try:
        applied, skipped = market_streamer.subscribe(payload.instrument_keys, payload.mode)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if applied:
        fanout_event("upstream_subscribe", {"keys": applied, "mode": payload.mode, "by": "http"})

    return ActionResponse(
        ok=True,
        message=f"applied={len(applied)} skipped={len(skipped)}",
        requested=payload.instrument_keys,
        applied=applied,
        skipped=skipped,
        status=StreamerStatus(**market_streamer.status()),
    )

@router.post("/market/unsubscribe", response_model=ActionResponse)
def market_unsubscribe(payload: UnsubscribeRequest) -> ActionResponse:
    logger.info("POST /upstox/market/unsubscribe | keys=%d", len(payload.instrument_keys))
    try:
        applied, skipped = market_streamer.unsubscribe(payload.instrument_keys)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if applied:
        fanout_event("upstream_unsubscribe", {"keys": applied, "by": "http"})

    return ActionResponse(
        ok=True,
        message=f"applied={len(applied)} skipped={len(skipped)}",
        requested=payload.instrument_keys,
        applied=applied,
        skipped=skipped,
        status=StreamerStatus(**market_streamer.status()),
    )

@router.post("/market/change-mode", response_model=ActionResponse)
def market_change_mode(payload: ChangeModeRequest) -> ActionResponse:
    logger.info("POST /upstox/market/change-mode | mode=%s | keys=%d", payload.mode, len(payload.instrument_keys))
    try:
        applied, skipped = market_streamer.change_mode(payload.instrument_keys, payload.mode)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if applied:
        fanout_event("upstream_change_mode", {"keys": applied, "mode": payload.mode, "by": "http"})

    return ActionResponse(
        ok=True,
        message=f"applied={len(applied)} skipped={len(skipped)}",
        requested=payload.instrument_keys,
        applied=applied,
        skipped=skipped,
        status=StreamerStatus(**market_streamer.status()),
    )

@router.get("/market/messages", response_model=MessagesResponse)
def market_messages(limit: int = 100, clear: bool = False) -> MessagesResponse:
    logger.info("GET /upstox/market/messages | limit=%d | clear=%s", limit, clear)
    raw = market_streamer.get_messages(limit=limit, clear=clear)
    return MessagesResponse(streamer="market", returned=len(raw), messages=[_jsonable(m) for m in raw])

@router.post("/market/messages/clear", response_model=ActionResponse)
def market_messages_clear() -> ActionResponse:
    logger.info("POST /upstox/market/messages/clear")
    removed = market_streamer.clear_messages()
    return ActionResponse(ok=True, message=f"cleared {removed} messages")

# ── downstream WebSocket status ──────────────────────────────────
@router.get("/ws/status", response_model=WSStatusResponse)
def ws_status() -> WSStatusResponse:
    """Snapshot of the downstream WebSocket server state."""
    logger.info("GET /upstox/ws/status")
    upstream_keys = market_streamer.status().get("subscribed_keys", [])
    return WSStatusResponse(clients=ws_manager.client_count(), upstream_subscribed_keys=upstream_keys)

# ── portfolio streamer ───────────────────────────────────────────
@router.get("/portfolio/status", response_model=StreamerStatus)
def portfolio_status() -> StreamerStatus:
    logger.info("GET /upstox/portfolio/status")
    return StreamerStatus(**portfolio_streamer.status())

@router.post("/portfolio/connect", response_model=ConnectionStateResponse)
def portfolio_connect() -> ConnectionStateResponse:
    logger.info("POST /upstox/portfolio/connect")
    ok = portfolio_streamer.connect()
    if not ok:
        raise HTTPException(status_code=502, detail="Failed to connect portfolio streamer")
    return ConnectionStateResponse(connected=True, name="portfolio")

@router.post("/portfolio/disconnect", response_model=ConnectionStateResponse)
def portfolio_disconnect() -> ConnectionStateResponse:
    logger.info("POST /upstox/portfolio/disconnect")
    ok = portfolio_streamer.disconnect()
    if not ok:
        raise HTTPException(status_code=502, detail="Failed to disconnect portfolio streamer")
    return ConnectionStateResponse(connected=False, name="portfolio")

@router.post("/portfolio/reconnect", response_model=ConnectionStateResponse)
def portfolio_reconnect() -> ConnectionStateResponse:
    logger.info("POST /upstox/portfolio/reconnect")
    ok = portfolio_streamer.reconnect_with_latest_token()
    if not ok:
        raise HTTPException(status_code=502, detail="Failed to reconnect portfolio streamer")
    return ConnectionStateResponse(connected=True, name="portfolio")

@router.get("/portfolio/messages", response_model=MessagesResponse)
def portfolio_messages(limit: int = 100, clear: bool = False) -> MessagesResponse:
    logger.info("GET /upstox/portfolio/messages | limit=%d | clear=%s", limit, clear)
    raw = portfolio_streamer.get_messages(limit=limit, clear=clear)
    return MessagesResponse(streamer="portfolio", returned=len(raw), messages=[_jsonable(m) for m in raw])

@router.post("/portfolio/messages/clear", response_model=ActionResponse)
def portfolio_messages_clear() -> ActionResponse:
    logger.info("POST /upstox/portfolio/messages/clear")
    removed = portfolio_streamer.clear_messages()
    return ActionResponse(ok=True, message=f"cleared {removed} messages")