# services/config_validation.py
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

    DUMMY_ORDER_ALLOW_LTP_ENTRY_FALLBACK=true,
    DUMMY_ORDER_STRICT_DETERMINISTIC_REPLAY=true
        -> two flags whose intents are mutually exclusive. The
           deterministic replay path (the default) would silently
           ignore the LTP fallback — the operator thinks they've
           re-enabled the old behaviour but they haven't. Rejected
           as an ERROR so the ambiguity is surfaced at startup.

    DUMMY_ORDER_WAIT_FOR_SR_BACKFILL=false
        -> reopens the 14:16-on-2026-10-29 class of bug where a
           mid-day restart triggers an order before the SR backfill
           completes. Allowed (some operators want the speed) but
           loudly warned.

    DUMMY_ORDER_DISABLE_RUNTIME_PATH_SWITCH=false
        -> re-enables runtime Path A/B flipping, so replay is no
           longer a pure function of the isolation record. Allowed
           for forensics but warned.
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

    # ------------------------------------------------------------------ #
    # 6. Dummy-order deterministic-replay guards (2026-11-04)            #
    # ------------------------------------------------------------------ #
    # These flags were added to make the post-isolation execution
    # pipeline auditable. The two ERROR/WARNING conditions below guard
    # against operators unknowingly reverting to non-deterministic
    # behaviour, which is precisely the class of bug this validator
    # exists to catch.
    # ------------------------------------------------------------------ #

    # 6a. Mutually-exclusive intent: LTP fallback + strict determinism.
    #     STRICT wins at runtime, so allowing this combination would
    #     silently discard the operator's apparent opt-in to the LTP
    #     fallback. Fail loudly instead.
    if (
        config.DUMMY_ORDER_ALLOW_LTP_ENTRY_FALLBACK
        and config.DUMMY_ORDER_STRICT_DETERMINISTIC_REPLAY
    ):
        errors.append(
            "DUMMY_ORDER_ALLOW_LTP_ENTRY_FALLBACK=true conflicts with "
            "DUMMY_ORDER_STRICT_DETERMINISTIC_REPLAY=true — the two flags "
            "are mutually exclusive in intent (strict determinism wins at "
            "runtime, silently discarding the LTP fallback). Set "
            "DUMMY_ORDER_STRICT_DETERMINISTIC_REPLAY=false if you really "
            "want the LTP fallback, or set "
            "DUMMY_ORDER_ALLOW_LTP_ENTRY_FALLBACK=false."
        )

    # 6b. SR-backfill wait disabled: reopens the 2026-10-29 14:16 bug
    #     where a mid-day restart could fire an order before the SR
    #     backfill had completed. Warn (not fail) because some ops
    #     deliberately trade latency for speed.
    if not config.DUMMY_ORDER_WAIT_FOR_SR_BACKFILL:
        warnings.append(
            "DUMMY_ORDER_WAIT_FOR_SR_BACKFILL=false — the service will no "
            "longer block until the SR backfill completes before firing "
            "the first post-isolation trigger. This reopens the "
            "14:16-on-2026-10-29 class of bug (order placed against a "
            "partially-backfilled SR state on mid-day restart)."
        )

    # 6c. Runtime Path A/B switch re-enabled: replay is no longer a pure
    #     function of the isolation record. Warn, not fail — forensics
    #     legitimately wants this sometimes.
    if not config.DUMMY_ORDER_DISABLE_RUNTIME_PATH_SWITCH:
        warnings.append(
            "DUMMY_ORDER_DISABLE_RUNTIME_PATH_SWITCH=false — runtime "
            "Path A/B flipping is still possible. Replay is no longer a "
            "pure function of the isolation record's trigger_index."
        )

    # 6d. DUMMY_ORDER_DRIFT_GUARD intentionally has NO validator here.
    #     Setting it to false is a legitimate (if unusual) operator
    #     choice for catch-up recomputation; it is not a
    #     misconfiguration in the sense this validator is designed to
    #     catch. The drift guard's warnings are emitted at the call
    #     site inside dummy_order_service when it actually overwrites
    #     a stored entry.

    return errors, warnings