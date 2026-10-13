import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, time as dt_time, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import FastAPI

from app.core.config import settings
from app.core.logger import get_logger
from app.services.ema_cross_store import ema_cross_store
from app.services.historical_candle_store import historical_candle_store
from app.services.live_ema_cross_store import live_ema_cross_store
from app.services.live_ltp_ema_store import live_ltp_ema_store
from app.services.subscription_store import subscription_store
from app.services.telegram_service import telegram_service

logger = get_logger(__name__)

# ----------------------------------------------------------------------
# Logging configuration
# ----------------------------------------------------------------------

# Maximum number of instruments to display in a log preview.
# Set to 0 to disable instrument previews completely.
LOG_INSTRUMENT_PREVIEW_LIMIT = 3

# Maximum number of characters to print for a JSON object.
# Set to 0 to disable this character limit.
LOG_JSON_MAX_CHARS = 3000

def json_dumps(data, max_chars=LOG_JSON_MAX_CHARS):
    """
    Convert an object to readable JSON with a maximum output length.

    default=str handles datetime and other non-JSON-native values.
    The character limit prevents large objects from flooding logs.
    """

    try:
        formatted = json.dumps(
            data,
            indent=2,
            ensure_ascii=False,
            default=str,
        )

        if max_chars and len(formatted) > max_chars:
            remaining = len(formatted) - max_chars

            return (
                formatted[:max_chars]
                + "\n... [TRUNCATED: "
                + str(remaining)
                + " additional characters omitted]"
            )

        return formatted

    except Exception as exc:
        return json.dumps(
            {
                "serialization_error": str(exc),
                "value": repr(data),
            },
            indent=2,
            ensure_ascii=False,
        )

def log_instrument_summary(instruments, label="Instruments"):
    """
    Log instrument counts and a small preview instead of the full list.
    """

    instruments = instruments or []

    preview_limit = max(
        0,
        LOG_INSTRUMENT_PREVIEW_LIMIT,
    )

    instrument_keys = [
        item.get("instrument_key")
        for item in instruments
        if isinstance(item, dict) and item.get("instrument_key")
    ]

    summary = {
        "total_instruments": len(instruments),
        "instruments_with_keys": len(instrument_keys),
        "preview_limit": preview_limit,
        "preview": [
            {
                "instrument_key": item.get("instrument_key"),
                "trading_symbol": item.get("trading_symbol"),
                "underlying_key": item.get("underlying_key"),
                "strike_price": item.get("strike_price"),
                "instrument_type": item.get("instrument_type"),
                "expiry": item.get("expiry"),
            }
            for item in instruments[:preview_limit]
            if isinstance(item, dict)
        ],
        "additional_instruments_omitted": max(
            0,
            len(instruments) - preview_limit,
        ),
    }

    logger.info(
        "%s summary:\n%s",
        label,
        json_dumps(summary),
    )

def log_job_summary(label, summary):
    """
    Log a job summary as JSON without dumping unrelated application data.
    """

    logger.info(
        "%s:\n%s",
        label,
        json_dumps(summary),
    )

# ----------------------------------------------------------------------
# Startup phase tracking (consumed by StartupGateMiddleware in main.py)
# ----------------------------------------------------------------------

def _set_phase(app: FastAPI, phase: str) -> None:
    """Record the current startup phase on ``app.state`` for the gate."""
    try:
        app.state.startup_phase = phase
    except Exception:
        # Never let bookkeeping crash startup.
        pass
    logger.debug("Startup phase -> %s", phase)

def _mark_startup_complete(app: FastAPI) -> None:
    try:
        app.state.startup_phase = "ready"
        app.state.startup_complete = True
        app.state.startup_completed_at = datetime.now(timezone.utc).isoformat()
    except Exception:
        pass

# ----------------------------------------------------------------------
# Telegram helpers
# ----------------------------------------------------------------------

def _format_uptime(total_seconds: int) -> str:
    """Format seconds as ``2h 15m 32s`` (or ``45s`` for short runs)."""
    total_seconds = max(0, int(total_seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m {seconds}s"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"

def _telegram_startup_details(
    app_started_at: datetime,
    instruments_count: int,
    live_started: bool,
) -> dict:
    """Build the key/value payload for the Telegram 'started' message."""
    return {
        "project": "Ordered Instrument Jobs",
        "version": "1.5.0",
        "instruments": instruments_count,
        "live_ema": (
            "enabled" if settings.live_ema_enabled and live_started
            else ("disabled" if not settings.live_ema_enabled else "failed to start")
        ),
        "live_ema_backend": (
            "ltp feed" if settings.use_live_ltp_feed else "rest polling"
        ),
        "interval_seconds": settings.live_ema_interval_seconds,
        "market_window": (
            f"{settings.market_open_time}-{settings.market_close_time} "
            f"{settings.market_timezone}"
        ),
        "daily_refresh": (
            f"{settings.daily_refresh_time} {settings.market_timezone}"
            if settings.daily_refresh_enabled
            else "disabled"
        ),
        "started_at": app_started_at.isoformat(),
    }

def _telegram_shutdown_details(
    app_started_at: datetime,
    instruments_count: int,
    live_state: dict,
    reason: str,
) -> dict:
    """Build the key/value payload for the Telegram 'stopped' message."""
    uptime_seconds = int(
        (datetime.now(timezone.utc) - app_started_at).total_seconds()
    )
    return {
        "project": "Ordered Instrument Jobs",
        "uptime": _format_uptime(uptime_seconds),
        "instruments": instruments_count,
        "live_ticks": live_state.get(
            "total_ticks_received",
            live_state.get("total_ticks"),
        ),
        "live_crosses": live_state.get("total_crosses_detected"),
        "stopped_at": datetime.now(timezone.utc).isoformat(),
        "reason": reason,
    }

# ----------------------------------------------------------------------
# Daily maintenance refresh (jobs 1 -> 4) on market days
# ----------------------------------------------------------------------

def _parse_daily_refresh_time(value: str) -> dt_time:
    """Parse ``HH:MM`` (24h). Falls back to 08:40 on any parse error."""
    try:
        hh, mm = value.strip().split(":")
        return dt_time(int(hh), int(mm))
    except Exception:
        logger.warning(
            "Invalid DAILY_REFRESH_TIME=%r; falling back to 08:40", value
        )
        return dt_time(8, 40)

def _next_market_day_run(now: datetime, target: dt_time) -> datetime:
    """Return the next Mon-Fri occurrence of ``target`` after ``now``.

    ``now`` must be timezone-aware in the market timezone. Weekends
    (Sat=5, Sun=6) are skipped.
    """
    candidate = now.replace(
        hour=target.hour,
        minute=target.minute,
        second=0,
        microsecond=0,
    )
    if candidate <= now:
        candidate = candidate + timedelta(days=1)

    # Skip weekends.
    while candidate.weekday() >= 5:
        candidate = candidate + timedelta(days=1)

    return candidate

async def _run_daily_refresh_once() -> None:
    """Run jobs 1 -> 4 once and log a compact summary."""

    logger.info("Daily maintenance refresh starting")

    # Job 1 — subscriptions
    try:
        await subscription_store.refresh(reason="daily-refresh")
    except Exception:
        logger.exception("Daily refresh: subscription refresh failed")

    instruments = subscription_store.snapshot().get("active", []) or []

    # Job 2 — historical candles
    try:
        summary = await historical_candle_store.ensure_recent_candles(
            instruments, reason="daily-refresh"
        )
        log_job_summary("Daily refresh: historical job summary", summary)
    except Exception:
        logger.exception("Daily refresh: historical candle job failed")

    # Job 3 — intraday candles (skips itself outside market hours)
    try:
        summary = await historical_candle_store.ensure_intraday_candles(
            instruments, reason="daily-refresh"
        )
        log_job_summary("Daily refresh: intraday job summary", summary)
    except Exception:
        logger.exception("Daily refresh: intraday candle job failed")

    # Job 4 — batch EMA crosses
    try:
        summary = await ema_cross_store.compute_all(
            instruments, reason="daily-refresh"
        )
        log_job_summary("Daily refresh: EMA cross job summary", summary)
    except Exception:
        logger.exception("Daily refresh: EMA cross job failed")

    logger.info("Daily maintenance refresh completed")

async def _daily_refresh_loop() -> None:
    """Fire the maintenance refresh once per market day at the configured time.

    Weekends are always skipped. The loop is cancellable via
    ``asyncio.CancelledError`` (raised from ``lifespan`` on shutdown).
    """

    target_time = _parse_daily_refresh_time(settings.daily_refresh_time)

    try:
        tz = ZoneInfo(settings.market_timezone)
    except Exception:
        logger.exception(
            "Invalid MARKET_TIMEZONE=%s for daily refresh; falling back to UTC",
            settings.market_timezone,
        )
        tz = ZoneInfo("UTC")

    logger.info(
        "Daily refresh scheduler armed; time=%s tz=%s market_days=Mon-Fri",
        target_time.strftime("%H:%M"),
        settings.market_timezone,
    )

    try:
        while True:
            now = datetime.now(tz)
            next_run = _next_market_day_run(now, target_time)
            wait_seconds = max(1.0, (next_run - now).total_seconds())

            logger.info(
                "Daily refresh next run scheduled at %s (in %.0f seconds)",
                next_run.isoformat(),
                wait_seconds,
            )

            try:
                await asyncio.sleep(wait_seconds)
            except asyncio.CancelledError:
                logger.info("Daily refresh loop cancelled")
                raise

            # Guard: if for any reason we woke early, keep sleeping.
            now = datetime.now(tz)
            if now.weekday() >= 5:
                # Weekend rolled in — skip silently.
                continue

            try:
                await _run_daily_refresh_once()
            except Exception:
                logger.exception("Daily maintenance refresh crashed")
    except asyncio.CancelledError:
        raise

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application startup and shutdown."""

    # ------------------------------------------------------------------
    # Startup bookkeeping (consumed by StartupGateMiddleware)
    # ------------------------------------------------------------------
    app_started_at = datetime.now(timezone.utc)
    try:
        app.state.startup_complete = False
        app.state.startup_phase = "initializing"
        app.state.startup_started_at = app_started_at.isoformat()
        app.state.startup_completed_at = None
    except Exception:
        pass

    logger.info("Application startup initiated")

    # Track these for the Telegram startup message.
    startup_instruments: list = []
    live_started = False

    # ---- Job 1: Subscription synchronization --------------------------

    _set_phase(app, "subscriptions")

    try:
        await subscription_store.refresh(
            reason="application-startup"
        )

    except Exception:
        logger.exception(
            "Application started without fresh subscription data"
        )

    # Obtain the current subscription snapshot.
    try:
        subscription_snapshot = subscription_store.snapshot()

        instruments = subscription_snapshot.get(
            "active",
            [],
        ) or []

        # Do not print the complete snapshot.
        log_instrument_summary(
            instruments,
            label="Active subscription instruments",
        )

    except Exception:
        logger.exception(
            "Failed to retrieve active subscriptions"
        )

        instruments = []

    startup_instruments = instruments

    # ---- Job 2: Recent historical candles -----------------------------

    _set_phase(app, "historical")

    try:
        summary = await historical_candle_store.ensure_recent_candles(
            instruments,
            reason="application-startup",
        )

        log_job_summary(
            "Startup historical candle job summary",
            summary,
        )

    except Exception:
        logger.exception(
            "Historical candle job did not complete at startup"
        )

    # ---- Job 3: Today's intraday candles -----------------------------

    _set_phase(app, "intraday")

    try:
        summary = await historical_candle_store.ensure_intraday_candles(
            instruments,
            reason="application-startup",
        )

        log_job_summary(
            "Startup intraday candle job summary",
            summary,
        )

    except Exception:
        logger.exception(
            "Intraday candle job did not complete at startup"
        )

    # ---- Job 4: Batch EMA crosses ------------------------------------

    _set_phase(app, "ema_crosses")

    try:
        summary = await ema_cross_store.compute_all(
            instruments,
            reason="application-startup",
        )

        log_job_summary(
            "Startup EMA cross job summary",
            summary,
        )

    except Exception:
        logger.exception(
            "EMA cross job did not complete at startup"
        )

    # ---- Job 5: Live EMA crosses -------------------------------------

    _set_phase(app, "live_ema")

    if settings.live_ema_enabled:

        if settings.use_live_ltp_feed:
            try:
                logger.info(
                    "Starting live LTP EMA job"
                )

                await live_ltp_ema_store.start(
                    lambda: subscription_store.snapshot().get(
                        "active",
                        [],
                    )
                )

                live_started = True

                logger.info(
                    "Live LTP EMA job started successfully"
                )

            except Exception:
                logger.exception(
                    "Live LTP EMA job failed to start"
                )

        else:
            try:
                logger.info(
                    "Starting live REST EMA cross job"
                )

                await live_ema_cross_store.start(
                    lambda: subscription_store.snapshot().get(
                        "active",
                        [],
                    )
                )

                live_started = True

                logger.info(
                    "Live EMA cross job started successfully"
                )

            except Exception:
                logger.exception(
                    "Live EMA cross job failed to start"
                )

    else:
        logger.info(
            "Live EMA job disabled via LIVE_EMA_ENABLED"
        )

    # ---- Daily maintenance refresh (jobs 1 -> 4) ---------------------
    # Runs once per market day (Mon-Fri) at DAILY_REFRESH_TIME in
    # MARKET_TIMEZONE. Job 5 keeps running in the background.
    daily_refresh_task: asyncio.Task | None = None

    _set_phase(app, "daily_refresh")

    if settings.daily_refresh_enabled:
        try:
            daily_refresh_task = asyncio.create_task(
                _daily_refresh_loop(),
                name="daily-refresh",
            )
            logger.info(
                "Daily refresh job started; time=%s tz=%s",
                settings.daily_refresh_time,
                settings.market_timezone,
            )
        except Exception:
            logger.exception("Failed to start the daily refresh job")
    else:
        logger.info(
            "Daily refresh job disabled via DAILY_REFRESH_ENABLED"
        )

    # ------------------------------------------------------------------
    # Mark startup complete — requests are now allowed through the gate.
    # This must run BEFORE the Telegram send (or before any await that
    # could hang) so the app becomes reachable even if Telegram is slow.
    # ------------------------------------------------------------------
    _mark_startup_complete(app)

    logger.info("Application startup completed")

    # ---- Telegram: application started (fire-and-forget) -------------
    try:
        telegram_service.notify_startup(
            _telegram_startup_details(
                app_started_at=app_started_at,
                instruments_count=len(startup_instruments),
                live_started=live_started,
            )
        )
    except Exception:
        logger.exception("Telegram startup notification failed")

    # ------------------------------ yield -----------------------------
    yield

    # ---- Shutdown -----------------------------------------------------

    logger.info("Application shutdown initiated")

    # ---- Telegram: application stopping ------------------------------
    # Fire-and-forget with a short cap so a slow Telegram API never
    # delays the process exit.
    try:
        live_state: dict = {}
        try:
            if settings.use_live_ltp_feed:
                live_state = live_ltp_ema_store.status() or {}
            else:
                live_state = {
                    "total_ticks": live_ema_cross_store.total_ticks,
                    "total_crosses_detected": (
                        live_ema_cross_store.total_crosses_detected
                    ),
                }
        except Exception:
            live_state = {}

        try:
            instruments_count = len(
                subscription_store.snapshot().get("active", [])
            )
        except Exception:
            instruments_count = 0

        shutdown_task = asyncio.create_task(
            telegram_service.send_shutdown(
                _telegram_shutdown_details(
                    app_started_at=app_started_at,
                    instruments_count=instruments_count,
                    live_state=live_state,
                    reason="application shutdown",
                ),
                timeout=3,
            )
        )
        try:
            await asyncio.wait_for(shutdown_task, timeout=3.5)
        except asyncio.TimeoutError:
            shutdown_task.cancel()
        except Exception:
            logger.exception("Telegram shutdown notification failed")
    except Exception:
        logger.exception("Telegram shutdown notification failed")

    # Stop the daily refresh task first so it does not fire mid-shutdown.
    if daily_refresh_task is not None and not daily_refresh_task.done():
        daily_refresh_task.cancel()
        try:
            await daily_refresh_task
        except (asyncio.CancelledError, Exception):
            pass
        logger.info("Daily refresh job stopped successfully")

    if settings.live_ema_enabled:

        try:
            await live_ltp_ema_store.stop()

            logger.info(
                "Live LTP EMA job stopped successfully"
            )

        except Exception:
            logger.exception(
                "Live LTP EMA job failed to stop cleanly"
            )

        try:
            await live_ema_cross_store.stop()

            logger.info(
                "Live EMA cross job stopped successfully"
            )

        except Exception:
            logger.exception(
                "Live EMA cross job failed to stop cleanly"
            )

    logger.info("Application shutdown completed")