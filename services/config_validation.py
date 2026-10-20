"""Startup configuration validation.

Catches the class of misconfiguration that does not raise an error
but silently breaks a pipeline stage. Called once at startup from
``main.app_lifespan`` before anything else runs.

The validator returns a list of *errors* (fatal) and *warnings*
(non-fatal). The caller logs both and Telegram-alerts the errors.

Examples of silent bugs this catches
------------------------------------
    STRATEGY_USEFUL_WINDOW_START_HOUR > ..._END_HOUR
        -> the market-hours gate always skips -> no strategy ever runs.

    DAILY_REFRESH_DAYS=mon,funday
        -> APScheduler silently accepts it -> the job never fires.

    OPENING_RANGE_HOUR=25
        -> cron never fires -> OR never runs -> Stage 1 skips.

    STRATEGY_ORDER_HOUR:MINUTE <= OPENING_RANGE_HOUR:MINUTE
        -> Stage 4 fires before OR even exists.

    FRONTEND_STRATEGY_1_ENABLED=true but selenium is missing
        -> browser launch always fails, but the rest of the pipeline
           "looks" green.
"""

from __future__ import annotations

from typing import Any

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
    # 2. OR / Stage 1 / Stage 4 timing                                   #
    # ------------------------------------------------------------------ #
    if not (0 <= config.OPENING_RANGE_HOUR <= 23):
        errors.append(
            f"OPENING_RANGE_HOUR={config.OPENING_RANGE_HOUR} invalid (0-23)"
        )
    if not (0 <= config.OPENING_RANGE_MINUTE <= 59):
        errors.append(
            f"OPENING_RANGE_MINUTE={config.OPENING_RANGE_MINUTE} invalid (0-59)"
        )
    if not (0 <= config.OPENING_RANGE_SECOND <= 59):
        errors.append(
            f"OPENING_RANGE_SECOND={config.OPENING_RANGE_SECOND} invalid (0-59)"
        )
    if not (0 <= config.STRATEGY_ORDER_HOUR <= 23):
        errors.append(
            f"STRATEGY_ORDER_HOUR={config.STRATEGY_ORDER_HOUR} invalid (0-23)"
        )
    if not (0 <= config.STRATEGY_ORDER_MINUTE <= 59):
        errors.append(
            f"STRATEGY_ORDER_MINUTE={config.STRATEGY_ORDER_MINUTE} invalid (0-59)"
        )

    or_minutes = config.OPENING_RANGE_HOUR * 60 + config.OPENING_RANGE_MINUTE
    s4_minutes = config.STRATEGY_ORDER_HOUR * 60 + config.STRATEGY_ORDER_MINUTE
    if s4_minutes <= or_minutes:
        errors.append(
            "STRATEGY_ORDER time must be AFTER OPENING_RANGE time "
            f"(OR={or_minutes} min, order={s4_minutes} min)"
        )

    # ------------------------------------------------------------------ #
    # 3. Useful-window gate                                              #
    # ------------------------------------------------------------------ #
    ws = (
        config.STRATEGY_USEFUL_WINDOW_START_HOUR * 60
        + config.STRATEGY_USEFUL_WINDOW_START_MINUTE
    )
    we = (
        config.STRATEGY_USEFUL_WINDOW_END_HOUR * 60
        + config.STRATEGY_USEFUL_WINDOW_END_MINUTE
    )
    if ws >= we:
        errors.append(
            "STRATEGY_USEFUL_WINDOW_START >= END — "
            "the market-hours gate will always skip"
        )

    # The useful window must contain the OR time, Stage 1 time, and
    # Stage 4 time — otherwise a refresh that runs inside the window
    # would still skip the very stages it is supposed to run.
    if not (ws <= or_minutes <= we):
        errors.append(
            "OPENING_RANGE time falls OUTSIDE "
            "STRATEGY_USEFUL_WINDOW_START..END"
        )
    if not (ws <= s4_minutes <= we):
        errors.append(
            "STRATEGY_ORDER time falls OUTSIDE "
            "STRATEGY_USEFUL_WINDOW_START..END"
        )

    # ------------------------------------------------------------------ #
    # 4. Readiness digest / preflight windows                            #
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
    if preflight_min >= or_minutes:
        warnings.append(
            "READINESS_PREFLIGHT time is at or after OPENING_RANGE time — "
            "the pre-flight check may race the OR cron"
        )

    # ------------------------------------------------------------------ #
    # 5. FE-S1 sanity                                                    #
    # ------------------------------------------------------------------ #
    if getattr(config, "FRONTEND_STRATEGY_1_ENABLED", False):
        try:
            from selenium import webdriver  # noqa: F401
        except ImportError:
            errors.append(
                "FRONTEND_STRATEGY_1_ENABLED=true but selenium is not installed"
            )

    # ------------------------------------------------------------------ #
    # 6. Telegram credentials sanity                                     #
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
    # 7. Mongo sanity                                                    #
    # ------------------------------------------------------------------ #
    if not config.MONGO_URI:
        errors.append("MONGO_URL is empty")
    if not config.MONGO_DB:
        errors.append("MONGO_DB is empty")

    # ------------------------------------------------------------------ #
    # 8. Port sanity                                                     #
    # ------------------------------------------------------------------ #
    if not (1 <= config.PORT <= 65535):
        errors.append(f"PORT={config.PORT} is not a valid TCP port")

    # ------------------------------------------------------------------ #
    # 9. Strategy 2 priority sanity                                      #
    # ------------------------------------------------------------------ #
    priority = [str(t).upper() for t in (config.STRATEGY_2_TICKER_PRIORITY or [])]
    if config.STRATEGY_2_ENABLED and not priority:
        warnings.append("STRATEGY_2_ENABLED=true but ticker priority list is empty")

    return errors, warnings