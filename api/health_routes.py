from datetime import datetime, timezone

from fastapi import APIRouter, status

from services.option_service import options_cache
from services.token_service import token_service
from services.upstox_websocket import upstox_streamer
from services.opening_range_service import (
    get_opening_range_status,
    get_or_ema_strategy_status,
)
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

    websocket_running = bool(getattr(upstox_streamer, "is_running", False))

    opening_range_status = {}

    try:
        opening_range_status = get_opening_range_status()
    except Exception as ex:
        opening_range_status = {
            "status": "error",
            "error": f"{type(ex).__name__}: {ex}",
        }

    or_ema_strategy_status = {}

    try:
        or_ema_strategy_status = get_or_ema_strategy_status()
    except Exception as ex:
        or_ema_strategy_status = {
            "status": "error",
            "error": f"{type(ex).__name__}: {ex}",
        }

    opening_range_ready = opening_range_status.get("status") in [
        "success",
        "partial_success",
    ]

    strategy_enabled = bool(or_ema_strategy_status.get("enabled", False))
    strategy_eligible_count = int(or_ema_strategy_status.get("eligible_count") or 0)

    strategy_ready = strategy_enabled and strategy_eligible_count > 0

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
            "opening_range": (
                "ready"
                if opening_range_ready
                else opening_range_status.get("status", "not_ready")
            ),
            "or_ema_strategy": (
                "ready"
                if strategy_ready
                else (
                    "enabled_waiting_for_opening_range"
                    if strategy_enabled
                    else "disabled"
                )
            ),
        },
        "metrics": {
            "subscribed_instruments": cached_keys_count,
            "connected_ws_clients": connected_clients,
            "opening_range": {
                "status": opening_range_status.get("status"),
                "date": opening_range_status.get("date"),
                "last_run_at": opening_range_status.get("last_run_at"),
                "total_instruments": opening_range_status.get("total_instruments"),
                "success_count": opening_range_status.get("success_count"),
                "failed_count": opening_range_status.get("failed_count"),
                "empty_count": opening_range_status.get("empty_count"),
                "insufficient_data_count": opening_range_status.get(
                    "insufficient_data_count"
                ),
                "touch_events_count": opening_range_status.get("touch_events_count"),
                "latest_main_index_ltp": opening_range_status.get(
                    "latest_main_index_ltp"
                ),
            },
            "or_ema_strategy": {
                "enabled": or_ema_strategy_status.get("enabled"),
                "selection_mode": or_ema_strategy_status.get("selection_mode"),
                "lock_after_first_selection": or_ema_strategy_status.get(
                    "lock_after_first_selection"
                ),
                "confirm_selected_only": or_ema_strategy_status.get(
                    "confirm_selected_only"
                ),
                "store_non_selected_touches": or_ema_strategy_status.get(
                    "store_non_selected_touches"
                ),
                "or_average": or_ema_strategy_status.get("or_average"),
                "strike_from": or_ema_strategy_status.get("strike_from"),
                "strike_to": or_ema_strategy_status.get("strike_to"),
                "eligible_count": or_ema_strategy_status.get("eligible_count"),
                "touched_count": or_ema_strategy_status.get("touched_count"),
                "selected_touch_key": or_ema_strategy_status.get("selected_touch_key"),
                "selected_instrument_key": or_ema_strategy_status.get(
                    "selected_instrument_key"
                ),
                "selected_level": or_ema_strategy_status.get("selected_level"),
                "selected_reason": or_ema_strategy_status.get("selected_reason"),
                "selected_at": or_ema_strategy_status.get("selected_at"),
                "alerts_sent_count": or_ema_strategy_status.get("alerts_sent_count"),
                "latest_ticks_count": or_ema_strategy_status.get("latest_ticks_count"),
            },
        },
        "strategy_flow": {
            "description": (
                "OR + EMA strategy selects the first eligible touched instrument. "
                "If multiple eligible instruments touch at the same timestamp or candle, "
                "the strike nearest to current NIFTY spot is selected. Only the selected "
                "touched instrument can trigger Telegram alert after EMA confirmation."
            ),
            "selection_rule": "first_touch_same_time_nearest_to_nifty",
            "non_selected_touches": "stored_for_debug_only",
            "telegram_alert_condition": (
                "selected instrument must touch configured OR level first, "
                "then same selected instrument must later produce EMA crossover"
            ),
        },
    }
