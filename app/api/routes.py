from fastapi import APIRouter, HTTPException

from app.services.ema_cross_store import ema_cross_store
from app.services.historical_candle_store import historical_candle_store
from app.services.live_ema_cross_store import live_ema_cross_store
from app.services.live_ltp_ema_store import live_ltp_ema_store
from app.services.subscription_store import subscription_store

router = APIRouter(prefix="/api", tags=["subscriptions"])

@router.get("/instruments")
async def list_instruments():
    """Return the current ordered, de-duplicated in-memory list."""
    return subscription_store.snapshot()

@router.post("/hard-refresh")
async def hard_refresh():
    """Run jobs 1–4 sequentially. Job 5 is a background task and keeps running."""
    try:
        subscription_result = await subscription_store.refresh(
            reason="api-hard-refresh"
        )
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Unable to refresh subscription data: {exc}",
        ) from exc

    instruments = subscription_store.snapshot().get("active", [])

    try:
        historical_result = await historical_candle_store.ensure_recent_candles(
            instruments, reason="api-hard-refresh"
        )
    except Exception as exc:
        historical_result = {"error": str(exc)}

    try:
        intraday_result = await historical_candle_store.ensure_intraday_candles(
            instruments, reason="api-hard-refresh"
        )
    except Exception as exc:
        intraday_result = {"error": str(exc)}

    try:
        ema_result = await ema_cross_store.compute_all(
            instruments, reason="api-hard-refresh"
        )
    except Exception as exc:
        ema_result = {"error": str(exc)}

    return {
        "subscriptions": subscription_result,
        "historical": historical_result,
        "intraday": intraday_result,
        "ema_crosses": ema_result,
        "live_ema": {"status": _live_ema_status_line()},
    }

def _live_ema_status_line() -> str:
    from app.core.config import settings

    if settings.use_live_ltp_feed:
        return "running" if live_ltp_ema_store.running else "stopped"
    return "running" if live_ema_cross_store.running else "stopped"

@router.post("/historical-refresh")
async def historical_refresh():
    instruments = subscription_store.snapshot().get("active", [])
    try:
        return await historical_candle_store.ensure_recent_candles(
            instruments, reason="api-historical-refresh"
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Historical refresh failed: {exc}") from exc

@router.post("/intraday-refresh")
async def intraday_refresh():
    instruments = subscription_store.snapshot().get("active", [])
    try:
        return await historical_candle_store.ensure_intraday_candles(
            instruments, reason="api-intraday-refresh"
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Intraday refresh failed: {exc}") from exc

@router.post("/ema-refresh")
async def ema_refresh():
    instruments = subscription_store.snapshot().get("active", [])
    try:
        return await ema_cross_store.compute_all(
            instruments, reason="api-ema-refresh"
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"EMA refresh failed: {exc}") from exc

# ---------------------------------------------------------------------------
# Live EMA cross job (job 5)
# ---------------------------------------------------------------------------
@router.get("/live-ema/status")
async def live_ema_status():
    from app.core.config import settings

    if settings.use_live_ltp_feed:
        payload = live_ltp_ema_store.status()
    else:
        payload = {
            "backend": "rest",
            "running": live_ema_cross_store.running,
            "started_at": live_ema_cross_store.started_at,
            "stopped_at": live_ema_cross_store.stopped_at,
            "total_ticks": live_ema_cross_store.total_ticks,
            "last_tick_started_at": live_ema_cross_store.last_tick_started_at,
            "last_tick_completed_at": live_ema_cross_store.last_tick_completed_at,
            "last_tick_instruments": live_ema_cross_store.last_tick_instruments,
            "last_tick_new_crosses": live_ema_cross_store.last_tick_new_crosses,
            "last_tick_error": live_ema_cross_store.last_tick_error,
            "total_crosses_detected": live_ema_cross_store.total_crosses_detected,
        }

    payload["enabled"] = settings.live_ema_enabled
    payload["use_live_ltp_feed"] = settings.use_live_ltp_feed
    payload["config"] = {
        "tick_offset_seconds": settings.live_ema_tick_offset_seconds,
        "interval_seconds": settings.live_ema_interval_seconds,
        "market_timezone": settings.market_timezone,
        "market_open_time": settings.market_open_time,
        "market_close_time": settings.market_close_time,
        "feed_url": settings.live_feed_url,
    }
    return payload

@router.get("/live-ema/feed-status")
async def live_ema_feed_status():
    """Per-instrument upstream feed status + latest LTP.

    Payload (LTP backend)::

        {
          "backend": "ltp",
          "running": true,
          "feed_url": "wss://...",
          "interval_seconds": 60,
          "total_instruments": 156,
          "connected_instruments": 152,
          "total_ticks_received": 123456,
          "last_tick_at": "...",
          "instruments": {
            "NSE_INDEX|Nifty 50": {
              "status": "connected",
              "last_ltp": 22680.95,
              "last_tick_at": "...",
              "total_ticks": 8123,
              "connected_at": "...",
              "detail": null
            },
            ...
          }
        }

    The bundled web UI polls this endpoint (see ``app/static/app.js``)
    and renders a "LIVE LTP" badge next to each instrument whose
    ``status`` is ``"connected"``.
    """
    from app.core.config import settings

    if settings.use_live_ltp_feed:
        return live_ltp_ema_store.feed_status_snapshot()

    # REST backend: there is no per-instrument feed socket, so return a
    # minimal global status block. Clients key off ``backend == "rest"``
    # and skip the per-instrument badges.
    return {
        "backend": "rest",
        "running": live_ema_cross_store.running,
        "started_at": live_ema_cross_store.started_at,
        "stopped_at": live_ema_cross_store.stopped_at,
        "interval_seconds": settings.live_ema_interval_seconds,
        "feed_url": None,
        "total_instruments": 0,
        "connected_instruments": 0,
        "total_ticks_received": live_ema_cross_store.total_ticks,
        "total_candles_closed": 0,
        "total_crosses_detected": live_ema_cross_store.total_crosses_detected,
        "last_tick_at": live_ema_cross_store.last_tick_completed_at,
        "last_candle_at": None,
        "last_error": live_ema_cross_store.last_tick_error,
        "last_error_at": None,
        "instruments": {},
    }

@router.post("/live-ema/start")
async def live_ema_start():
    from app.core.config import settings

    if not settings.live_ema_enabled:
        raise HTTPException(
            status_code=409,
            detail="Live EMA cross job is disabled via LIVE_EMA_ENABLED",
        )
    if settings.use_live_ltp_feed:
        return await live_ltp_ema_store.start(
            lambda: subscription_store.snapshot().get("active", [])
        )
    return await live_ema_cross_store.start(
        lambda: subscription_store.snapshot().get("active", [])
    )

@router.post("/live-ema/stop")
async def live_ema_stop():
    from app.core.config import settings

    if settings.use_live_ltp_feed:
        return await live_ltp_ema_store.stop()
    return await live_ema_cross_store.stop()

@router.post("/live-ema/tick")
async def live_ema_tick():
    """Run a single tick on demand.

    For the LTP backend this just returns the current live state — ticks
    arrive from the upstream feed, not from a manual trigger.
    """
    from app.core.config import settings

    if not settings.live_ema_enabled:
        raise HTTPException(
            status_code=409,
            detail="Live EMA cross job is disabled via LIVE_EMA_ENABLED",
        )

    if settings.use_live_ltp_feed:
        return {
            "ok": True,
            "backend": "ltp",
            "note": (
                "LTP backend consumes upstream ticks; use /api/live-ema/status "
                "or a WS subscription to observe state."
            ),
            "status": live_ltp_ema_store.status(),
        }

    instruments = subscription_store.snapshot().get("active", [])
    await live_ema_cross_store._tick(instruments)
    return {
        "ok": True,
        "backend": "rest",
        "test_mode": settings.ema_test_mode,
        "instruments_checked": live_ema_cross_store.last_tick_instruments,
        "new_crosses": live_ema_cross_store.last_tick_new_crosses,
        "last_tick_completed_at": live_ema_cross_store.last_tick_completed_at,
    }

@router.get("/health")
async def health():
    from app.core.config import settings

    snapshot = subscription_store.snapshot()
    live_block = (
        live_ltp_ema_store.status()
        if settings.use_live_ltp_feed
        else {
            "backend": "rest",
            "enabled": settings.live_ema_enabled,
            "running": live_ema_cross_store.running,
            "tick_offset_seconds": settings.live_ema_tick_offset_seconds,
            "market_timezone": settings.market_timezone,
            "market_open_time": settings.market_open_time,
            "market_close_time": settings.market_close_time,
            "total_ticks": live_ema_cross_store.total_ticks,
            "last_tick_started_at": live_ema_cross_store.last_tick_started_at,
            "last_tick_completed_at": live_ema_cross_store.last_tick_completed_at,
            "last_tick_instruments": live_ema_cross_store.last_tick_instruments,
            "last_tick_new_crosses": live_ema_cross_store.last_tick_new_crosses,
            "last_tick_error": live_ema_cross_store.last_tick_error,
            "total_crosses_detected": live_ema_cross_store.total_crosses_detected,
        }
    )

    return {
        "status": "ok" if subscription_store.last_error is None else "degraded",
        "loaded_instruments": len(snapshot["active"]),
        "last_refreshed_at": subscription_store.last_refreshed_at,
        "last_error": subscription_store.last_error,
        "historical": {
            "last_run_at": historical_candle_store.last_run_at,
            "last_error": historical_candle_store.last_error,
            "fetched": historical_candle_store.fetched_count,
            "skipped": historical_candle_store.skipped_count,
            "failed": historical_candle_store.failed_count,
        },
        "intraday": {
            "market_hours_now": historical_candle_store.is_market_hours(),
            "last_run_at": historical_candle_store.intraday_last_run_at,
            "last_error": historical_candle_store.intraday_last_error,
            "updated": historical_candle_store.intraday_updated_count,
            "empty": historical_candle_store.intraday_empty_count,
            "failed": historical_candle_store.intraday_failed_count,
        },
        "ema_crosses": {
            "last_run_at": ema_cross_store.last_run_at,
            "last_error": ema_cross_store.last_error,
            "computed": ema_cross_store.computed_count,
            "empty": ema_cross_store.empty_count,
            "failed": ema_cross_store.failed_count,
            "total_crosses": ema_cross_store.total_crosses,
        },
        "live_ema": live_block,
    }