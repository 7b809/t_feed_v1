from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request

from app.core.config import settings
from app.services.ema_cross_store import ema_cross_store
from app.services.historical_candle_store import historical_candle_store
from app.services.live_ema_cross_store import live_ema_cross_store
from app.services.live_ltp_ema_store import live_ltp_ema_store
from app.services.subscription_store import subscription_store

router = APIRouter(prefix="/api", tags=["subscriptions"])

# ============================================================
# LIVE-EMA CONNECT URL HELPERS
# ------------------------------------------------------------
# Every row returned by /api/instruments (and inside the
# subscriptions block of /api/hard-refresh) gets a per-instrument
# ``connect_url`` pointing at our own live-EMA WebSocket:
#
#     wss://<host>/ws/live-ema/by-instrument/<url-encoded key>
#
# Base URL resolution order:
#   1. settings.public_ws_base_url           (if configured)
#   2. X-Forwarded-Proto / X-Forwarded-Host  (reverse-proxy aware)
#   3. request.url scheme + Host header
# ============================================================

WS_LIVE_EMA_PATH = "/ws/live-ema/by-instrument"


def _resolve_ws_base_url(request: Request) -> str:
    configured = getattr(settings, "public_ws_base_url", None)
    if configured:
        return str(configured).rstrip("/")

    forwarded_proto = request.headers.get("x-forwarded-proto")
    scheme = (
        forwarded_proto.split(",")[0].strip()
        if forwarded_proto
        else request.url.scheme
    )
    ws_scheme = "wss" if scheme in ("https", "wss") else "ws"

    forwarded_host = request.headers.get("x-forwarded-host")
    host = (
        forwarded_host.split(",")[0].strip()
        if forwarded_host
        else request.headers.get("host")
    ) or request.url.netloc

    return f"{ws_scheme}://{host}"


def _build_live_ema_connect_url(base_url: str, instrument_key: str) -> str:
    return f"{base_url}{WS_LIVE_EMA_PATH}/{quote(instrument_key, safe='')}"


def _enrich_snapshot_with_connect_urls(
    snapshot: dict,
    request: Request,
) -> dict:
    """Return a copy of ``snapshot`` with a per-row ``connect_url``."""
    base_url = _resolve_ws_base_url(request)
    active = snapshot.get("active", []) or []

    enriched_active = []
    for item in active:
        if not isinstance(item, dict):
            enriched_active.append(item)
            continue

        ikey = item.get("instrument_key")
        if isinstance(ikey, str) and ikey.strip():
            connect_url = _build_live_ema_connect_url(base_url, ikey)
        else:
            connect_url = None

        enriched_active.append({**item, "connect_url": connect_url})

    return {
        **snapshot,
        "ws_base_url": base_url,
        "active": enriched_active,
    }


# ---------------------------------------------------------------------------
# List instruments
# ---------------------------------------------------------------------------
@router.get("/instruments")
async def list_instruments(request: Request):
    """Return the current ordered, de-duplicated in-memory list.

    Each active instrument carries a ``connect_url`` field pointing at
    our own live-EMA WebSocket for that instrument.
    """
    return _enrich_snapshot_with_connect_urls(
        subscription_store.snapshot(),
        request,
    )


@router.post("/hard-refresh")
async def hard_refresh(request: Request):
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

    # Enrich the subscriptions block so callers see connect_url here too.
    try:
        subscription_result = _enrich_snapshot_with_connect_urls(
            subscription_result,
            request,
        )
    except Exception:
        # Never fail the hard refresh just because URL building broke.
        pass

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
    if settings.use_live_ltp_feed:
        return live_ltp_ema_store.feed_status_snapshot()

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
    if settings.use_live_ltp_feed:
        return await live_ltp_ema_store.stop()
    return await live_ema_cross_store.stop()


@router.post("/live-ema/tick")
async def live_ema_tick():
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