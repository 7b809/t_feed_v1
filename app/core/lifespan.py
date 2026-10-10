import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, time as dt_time, timedelta
from zoneinfo import ZoneInfo

from fastapi import FastAPI

from app.core.config import settings
from app.core.logger import get_logger
from app.services.ema_cross_store import ema_cross_store
from app.services.historical_candle_store import historical_candle_store
from app.services.live_ema_cross_store import live_ema_cross_store
from app.services.live_ltp_ema_store import live_ltp_ema_store
from app.services.subscription_store import subscription_store

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

    logger.info("Application startup initiated")

    # ---- Job 1: Subscription synchronization --------------------------

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

    # ---- Job 2: Recent historical candles -----------------------------

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

    logger.info("Application startup completed")

    yield

    # ---- Shutdown -----------------------------------------------------

    logger.info("Application shutdown initiated")

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