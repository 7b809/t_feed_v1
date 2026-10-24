# File: services/config_validation.py
"""Startup configuration validation.

Catches the class of misconfiguration that does not raise an error
but silently breaks a pipeline stage. Called once at startup from
``main.app_lifespan`` before anything else runs.

The validator returns a list of *errors* (fatal) and *warnings*
(non-fatal). The caller logs both and Telegram-alerts the errors.

Examples of silent bugs this catches
------------------------------------
    DAILY_REFRESH_DAYS=mon,funday
        -> APScheduler silently accepts it -> the job never fires.

    READINESS_DIGEST_HOUR=08, READINESS_PREFLIGHT_HOUR=07
        -> the preflight probe runs BEFORE the digest, defeating the
           whole "digest first, then re-check" design.
"""

from __future__ import annotations

from core import config
from core.logger import get_logger

logger = get_logger(__file__)

# --------------------------------------------------------------------------- #
# Valid day-of-week tokens accepted by APScheduler cron                       #
# --------------------------------------------------------------------------- #
_VALID_DAYS = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}


def _parse_days(value: str) -> list[str]:
    return [
        d.strip().lower()
        for d in str(value or "").split(",")
        if d.strip()
    ]


def validate_config() -> tuple[list[str], list[str]]:
    """Return ``(errors, warnings)`` for the current configuration.

    ``errors`` are fatal and should be surfaced on Telegram + logged
    at ERROR level. ``warnings`` are advisory and should be logged at
    WARNING level.
    """
    errors: list[str] = []
    warnings: list[str] = []

    # ------------------------------------------------------------------ #
    # 1. Daily refresh schedule                                          #
    # ------------------------------------------------------------------ #
    if not (0 <= config.DAILY_REFRESH_HOUR <= 23):
        errors.append(
            f"DAILY_REFRESH_HOUR={config.DAILY_REFRESH_HOUR} invalid (0-23)"
        )
    if not (0 <= config.DAILY_REFRESH_MINUTE <= 59):
        errors.append(
            f"DAILY_REFRESH_MINUTE={config.DAILY_REFRESH_MINUTE} invalid (0-59)"
        )
    for day in _parse_days(config.DAILY_REFRESH_DAYS):
        if day not in _VALID_DAYS:
            errors.append(f"DAILY_REFRESH_DAYS contains invalid day: {day!r}")

    # ------------------------------------------------------------------ #
    # 2. Readiness digest / preflight windows                            #
    # ------------------------------------------------------------------ #
    for name, hour, minute in (
        ("READINESS_DIGEST", config.READINESS_DIGEST_HOUR,
         config.READINESS_DIGEST_MINUTE),
        ("READINESS_PREFLIGHT", config.READINESS_PREFLIGHT_HOUR,
         config.READINESS_PREFLIGHT_MINUTE),
    ):
        if not (0 <= hour <= 23):
            errors.append(f"{name}_HOUR={hour} invalid (0-23)")
        if not (0 <= minute <= 59):
            errors.append(f"{name}_MINUTE={minute} invalid (0-59)")

    digest_min = (
        config.READINESS_DIGEST_HOUR * 60 + config.READINESS_DIGEST_MINUTE
    )
    preflight_min = (
        config.READINESS_PREFLIGHT_HOUR * 60
        + config.READINESS_PREFLIGHT_MINUTE
    )
    if digest_min >= preflight_min:
        errors.append(
            "READINESS_DIGEST time must be BEFORE READINESS_PREFLIGHT time"
        )

    # ------------------------------------------------------------------ #
    # 3. Telegram credentials sanity                                     #
    # ------------------------------------------------------------------ #
    if config.TELEGRAM_ENABLED:
        if not config.TELEGRAM_BOT_TOKEN:
            errors.append(
                "TELEGRAM_ENABLED=true but TELEGRAM_BOT_TOKEN is empty"
            )
        if not config.TELEGRAM_CHAT_ID:
            errors.append(
                "TELEGRAM_ENABLED=true but TELEGRAM_CHAT_ID is empty"
            )

    # ------------------------------------------------------------------ #
    # 4. Mongo sanity                                                    #
    # ------------------------------------------------------------------ #
    if not config.MONGO_URI:
        errors.append("MONGO_URL is empty")
    if not config.MONGO_DB:
        errors.append("MONGO_DB is empty")

    # ------------------------------------------------------------------ #
    # 5. Port sanity                                                     #
    # ------------------------------------------------------------------ #
    if not (1 <= config.PORT <= 65535):
        errors.append(f"PORT={config.PORT} is not a valid TCP port")

    return errors, warnings