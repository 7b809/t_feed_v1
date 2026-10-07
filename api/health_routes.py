from datetime import datetime, timezone

from fastapi import APIRouter, status

from services.option_service import options_cache
from services.token_service import token_service
from services.novag7_websocket import novag7_feed_manager
from ws_feed.broadcaster import broadcaster

router = APIRouter()


@router.get("/health", status_code=status.HTTP_200_OK)
async def get_health_status():
    """Returns the application health and system status in JSON format."""

    has_token = bool(token_service.get_access_token())
    cached_keys_count = len(options_cache.get("subscribed_keys", []))

    connected_clients = (
        broadcaster.get_active_connections_count()
        if hasattr(broadcaster, "get_active_connections_count")
        else 0
    )

    feed_status = novag7_feed_manager.get_status()
    websocket_running = bool(feed_status.get("is_running"))

    is_healthy = has_token and cached_keys_count > 0 and websocket_running

    return {
        "status": "healthy" if is_healthy else "degraded",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "services": {
            "token_service": "active" if has_token else "missing_token",
            "options_cache": (
                f"loaded ({cached_keys_count} keys)"
                if cached_keys_count > 0
                else "empty"
            ),
            "websocket_feed": "active" if websocket_running else "inactive",
            "live_feed_provider": "novag7",
        },
        "metrics": {
            "subscribed_instruments": cached_keys_count,
            "connected_ws_clients": connected_clients,
            "connected_feed_instruments": feed_status.get("connected_count", 0),
        },
    }
