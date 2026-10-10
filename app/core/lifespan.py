import json
from contextlib import asynccontextmanager

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

    logger.info("Application startup completed")

    yield

    # ---- Shutdown -----------------------------------------------------

    logger.info("Application shutdown initiated")

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
