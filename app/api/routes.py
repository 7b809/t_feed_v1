from datetime import datetime, timezone
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request

from app.core.config import settings
from app.core.logger import get_logger
from app.services.ema_cross_store import ema_cross_store
from app.services.historical_candle_store import historical_candle_store
from app.services.live_ema_cross_store import live_ema_cross_store
from app.services.live_ltp_ema_store import live_ltp_ema_store
from app.services.subscription_store import subscription_store
from app.services.telegram_service import telegram_service

logger = get_logger(__name__)

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

# ============================================================
# TELEGRAM REFRESH NOTIFICATIONS
# ------------------------------------------------------------
# Every project-level refresh operation (scheduled daily refresh
# is handled in lifespan.py; the manual ones are here) emits a
# start message and a completion message via Telegram.
#
# Both helpers are fire-and-forget: they schedule the send and
# return immediately, so a slow Telegram API never delays the
# HTTP response. Any failure to send is logged and swallowed.
# ============================================================

def _notify_refresh_start(
    label: str,
    source: str,
    started_at: datetime,
    extra_lines: list[str] | None = None,
) -> None:
    """Fire-and-forget Telegram notification for a refresh starting."""
    try:
        lines = [
            f"<b>Source:</b> {source}",
            f"<b>Started At:</b> {started_at.isoformat()}",
        ]
        if extra_lines:
            lines.extend(extra_lines)

        telegram_service.notify_event(
            f"{label} Started",
            lines,
            level="STARTUP",
        )
    except Exception:
        logger.exception("Telegram %s start notification failed", label)

def _notify_refresh_end(
    label: str,
    started_at: datetime,
    outcomes: dict,
) -> None:
    """Fire-and-forget Telegram notification for a refresh finishing."""
    try:
        completed_at = datetime.now(timezone.utc)
        elapsed = (completed_at - started_at).total_seconds()

        any_failed = any(
            isinstance(v, str) and v.startswith("failed")
            for v in outcomes.values()
        )

        lines = [
            f"<b>Elapsed:</b> {elapsed:.1f}s",
            f"<b>Completed At:</b> {completed_at.isoformat()}",
        ]
        for key, value in outcomes.items():
            label_k = key.replace("_", " ").title()
            lines.append(f"<b>{label_k}:</b> {value}")

        title = (
            f"{label} Completed"
            if not any_failed
            else f"{label} Completed with Errors"
        )
        telegram_service.notify_event(
            title,
            lines,
            level="SUCCESS" if not any_failed else "WARNING",
        )
    except Exception:
        logger.exception("Telegram %s completion notification failed", label)

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

# ---------------------------------------------------------------------------
# Hard refresh — jobs 1-4 sequentially
# ---------------------------------------------------------------------------
@router.post("/hard-refresh")
async def hard_refresh(request: Request):
    """Run jobs 1–4 sequentially. Job 5 is a background task and keeps running."""
    started_at = datetime.now(timezone.utc)

    _notify_refresh_start(
        label="Hard Refresh",
        source="POST /api/hard-refresh",
        started_at=started_at,
    )

    outcomes: dict[str, str] = {}

    try:
        # ---- Job 1: subscriptions -------------------------------------
        try:
            subscription_result = await subscription_store.refresh(
                reason="api-hard-refresh"
            )
            outcomes["subscriptions"] = "ok"
        except Exception as exc:
            outcomes["subscriptions"] = f"failed: {exc}"
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
        outcomes["instruments"] = str(len(instruments))

        # ---- Job 2: historical ----------------------------------------
        try:
            historical_result = await historical_candle_store.ensure_recent_candles(
                instruments, reason="api-hard-refresh"
            )
            outcomes["historical"] = (
                f"fetched={historical_result.get('fetched', 0)} "
                f"skipped={historical_result.get('skipped', 0)} "
                f"failed={historical_result.get('failed', 0)}"
            )
        except Exception as exc:
            historical_result = {"error": str(exc)}
            outcomes["historical"] = f"failed: {exc}"

        # ---- Job 3: intraday ------------------------------------------
        try:
            intraday_result = await historical_candle_store.ensure_intraday_candles(
                instruments, reason="api-hard-refresh"
            )
            if intraday_result.get("skipped"):
                outcomes["intraday"] = "skipped (outside market hours)"
            else:
                outcomes["intraday"] = (
                    f"updated={intraday_result.get('updated', 0)} "
                    f"empty={intraday_result.get('empty', 0)} "
                    f"failed={intraday_result.get('failed', 0)}"
                )
        except Exception as exc:
            intraday_result = {"error": str(exc)}
            outcomes["intraday"] = f"failed: {exc}"

        # ---- Job 4: EMA crosses ---------------------------------------
        try:
            ema_result = await ema_cross_store.compute_all(
                instruments, reason="api-hard-refresh"
            )
            outcomes["ema_crosses"] = (
                f"computed={ema_result.get('computed', 0)} "
                f"empty={ema_result.get('empty', 0)} "
                f"failed={ema_result.get('failed', 0)} "
                f"total_crosses={ema_result.get('total_crosses', 0)}"
            )
        except Exception as exc:
            ema_result = {"error": str(exc)}
            outcomes["ema_crosses"] = f"failed: {exc}"

        return {
            "subscriptions": subscription_result,
            "historical": historical_result,
            "intraday": intraday_result,
            "ema_crosses": ema_result,
            "live_ema": {"status": _live_ema_status_line()},
        }
    finally:
        _notify_refresh_end(
            label="Hard Refresh",
            started_at=started_at,
            outcomes=outcomes,
        )

def _live_ema_status_line() -> str:
    if settings.use_live_ltp_feed:
        return "running" if live_ltp_ema_store.running else "stopped"
    return "running" if live_ema_cross_store.running else "stopped"

# ---------------------------------------------------------------------------
# Individual manual refreshes
# ---------------------------------------------------------------------------
@router.post("/historical-refresh")
async def historical_refresh():
    started_at = datetime.now(timezone.utc)
    _notify_refresh_start(
        label="Historical Refresh",
        source="POST /api/historical-refresh",
        started_at=started_at,
    )

    outcomes: dict[str, str] = {}
    try:
        instruments = subscription_store.snapshot().get("active", [])
        outcomes["instruments"] = str(len(instruments))

        try:
            result = await historical_candle_store.ensure_recent_candles(
                instruments, reason="api-historical-refresh"
            )
            outcomes["historical"] = (
                f"fetched={result.get('fetched', 0)} "
                f"skipped={result.get('skipped', 0)} "
                f"failed={result.get('failed', 0)}"
            )
            return result
        except Exception as exc:
            outcomes["historical"] = f"failed: {exc}"
            raise HTTPException(
                status_code=502,
                detail=f"Historical refresh failed: {exc}",
            ) from exc
    finally:
        _notify_refresh_end(
            label="Historical Refresh",
            started_at=started_at,
            outcomes=outcomes,
        )

@router.post("/intraday-refresh")
async def intraday_refresh():
    started_at = datetime.now(timezone.utc)
    _notify_refresh_start(
        label="Intraday Refresh",
        source="POST /api/intraday-refresh",
        started_at=started_at,
    )

    outcomes: dict[str, str] = {}
    try:
        instruments = subscription_store.snapshot().get("active", [])
        outcomes["instruments"] = str(len(instruments))

        try:
            result = await historical_candle_store.ensure_intraday_candles(
                instruments, reason="api-intraday-refresh"
            )
            if result.get("skipped"):
                outcomes["intraday"] = "skipped (outside market hours)"
            else:
                outcomes["intraday"] = (
                    f"updated={result.get('updated', 0)} "
                    f"empty={result.get('empty', 0)} "
                    f"failed={result.get('failed', 0)}"
                )
            return result
        except Exception as exc:
            outcomes["intraday"] = f"failed: {exc}"
            raise HTTPException(
                status_code=502,
                detail=f"Intraday refresh failed: {exc}",
            ) from exc
    finally:
        _notify_refresh_end(
            label="Intraday Refresh",
            started_at=started_at,
            outcomes=outcomes,
        )

@router.post("/ema-refresh")
async def ema_refresh():
    started_at = datetime.now(timezone.utc)
    _notify_refresh_start(
        label="EMA Refresh",
        source="POST /api/ema-refresh",
        started_at=started_at,
    )

    outcomes: dict[str, str] = {}
    try:
        instruments = subscription_store.snapshot().get("active", [])
        outcomes["instruments"] = str(len(instruments))

        try:
            result = await ema_cross_store.compute_all(
                instruments, reason="api-ema-refresh"
            )
            outcomes["ema_crosses"] = (
                f"computed={result.get('computed', 0)} "
                f"empty={result.get('empty', 0)} "
                f"failed={result.get('failed', 0)} "
                f"total_crosses={result.get('total_crosses', 0)}"
            )
            return result
        except Exception as exc:
            outcomes["ema_crosses"] = f"failed: {exc}"
            raise HTTPException(
                status_code=502,
                detail=f"EMA refresh failed: {exc}",
            ) from exc
    finally:
        _notify_refresh_end(
            label="EMA Refresh",
            started_at=started_at,
            outcomes=outcomes,
        )

# ---------------------------------------------------------------------------
# Live EMA cross job (job 5) — status and manual controls
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

    started_at = datetime.now(timezone.utc)
    _notify_refresh_start(
        label="Live EMA Start",
        source="POST /api/live-ema/start",
        started_at=started_at,
        extra_lines=[
            f"<b>Backend:</b> "
            f"{'ltp feed' if settings.use_live_ltp_feed else 'rest polling'}",
        ],
    )

    outcomes: dict[str, str] = {}
    try:
        if settings.use_live_ltp_feed:
            result = await live_ltp_ema_store.start(
                lambda: subscription_store.snapshot().get("active", [])
            )
        else:
            result = await live_ema_cross_store.start(
                lambda: subscription_store.snapshot().get("active", [])
            )

        if isinstance(result, dict) and result.get("started") is False:
            outcomes["start"] = f"skipped: {result.get('reason', 'unknown')}"
        else:
            outcomes["start"] = "started"
        return result
    except Exception as exc:
        outcomes["start"] = f"failed: {exc}"
        raise
    finally:
        _notify_refresh_end(
            label="Live EMA Start",
            started_at=started_at,
            outcomes=outcomes,
        )

@router.post("/live-ema/stop")
async def live_ema_stop():
    started_at = datetime.now(timezone.utc)
    _notify_refresh_start(
        label="Live EMA Stop",
        source="POST /api/live-ema/stop",
        started_at=started_at,
    )

    outcomes: dict[str, str] = {}
    try:
        if settings.use_live_ltp_feed:
            result = await live_ltp_ema_store.stop()
        else:
            result = await live_ema_cross_store.stop()

        if isinstance(result, dict) and result.get("stopped") is False:
            outcomes["stop"] = f"skipped: {result.get('reason', 'unknown')}"
        else:
            outcomes["stop"] = "stopped"
        return result
    except Exception as exc:
        outcomes["stop"] = f"failed: {exc}"
        raise
    finally:
        _notify_refresh_end(
            label="Live EMA Stop",
            started_at=started_at,
            outcomes=outcomes,
        )

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