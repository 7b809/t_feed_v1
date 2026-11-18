"""Periodic Upstox token validation scheduler.

Runs ``get_token_status()`` every ``TOKEN_CHECK_INTERVAL_MINUTES`` minutes
(default 30) to detect expired or revoked tokens before they break order
placement or instrument search. The check itself is performed by
``core.token_service`` — this module only schedules it, emits one log line
per check, and reports state transitions via Telegram.

No profile data (user_id, user_name, broker, exchanges) is ever logged or
notified. The only outcome recorded is whether the token is ``present`` /
``valid``, and (when invalid) the ``error`` category plus the Upstox error
code. This mirrors the PII-free contract of ``get_token_status()``.

Notification policy — deliberately low-noise:
    * valid     -> invalid  : WARNING alert (trading is now broken)
    * invalid   -> valid    : SUCCESS alert (token was refreshed)
    * invalid at startup    : WARNING alert
    * valid at startup      : no alert (startup notification already fired)
    * no change             : log only, no Telegram

The first check of the process fires ~10 s after startup so the service has
finished its own startup notification before the token heartbeat begins.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from core.config import (
    TIMEZONE,
    TOKEN_CHECK_ENABLED,
    TOKEN_CHECK_INTERVAL_MINUTES,
)
from core.logger import get_logger
from core.telegram_notifier import notify_event
from core.token_service import get_token_status

logger = get_logger(__file__)

_scheduler: BackgroundScheduler | None = None

# Last observed state. "unknown" means no check has run yet in this process;
# it never triggers a Telegram alert on its own — it only primes the
# transition detector so the first real check reports a change if needed.
_last_state: str = "unknown"

# Delay before the very first check, so the service's own startup Telegram
# notification ("Sandbox service started") is not immediately followed by a
# second message in the same second.
_FIRST_CHECK_DELAY_SECONDS = 10


def _format_status_for_log(status: dict) -> str:
    """Compact, log-safe description of a ``get_token_status()`` result.

    Only includes fields that are already PII-free: present/valid/error and
    the Upstox error code. Never includes the token value itself.
    """
    if status.get("valid"):
        return "valid"
    parts = ["invalid"]
    if status.get("error"):
        parts.append(f"error={status['error']}")
    if status.get("upstream_status"):
        parts.append(f"upstream_status={status['upstream_status']}")
    if status.get("upstream_error_code"):
        parts.append(f"upstream_error_code={status['upstream_error_code']}")
    return " ".join(parts)


def _transition_alert(
    previous: str, current: str, status: dict
) -> None:
    """Fire a Telegram alert when the token state changes.

    ``previous`` is "unknown" on the first check of the process.
    """
    error_line = f"Error: {status.get('error', 'unknown')}"
    code_line = (
        f"Upstox error code: {status['upstream_error_code']}"
        if status.get("upstream_error_code")
        else None
    )

    if current == "valid" and previous in {"unknown", "valid"}:
        return  # normal state, nothing to announce

    if current == "invalid":
        if previous == "unknown":
            title = "Upstox token invalid at startup"
        else:
            title = "Upstox token became invalid"
        details = [
            error_line,
            *([code_line] if code_line else []),
            "Order placement and instrument search will fail until the token "
            "is refreshed in app_tokens/upstox_access_token.",
        ]
        notify_event(title, details, "WARNING")
        return

    # current == "valid", previous == "invalid"
    notify_event(
        "Upstox token recovered",
        ["Token validation succeeded after a previous failure."],
        "SUCCESS",
    )


def _run_token_check() -> None:
    """Perform one validation cycle and emit log + transition alert.

    ``get_token_status()`` never raises; any exception here would indicate a
    bug in this module, so it is logged defensively and the scheduler keeps
    running.
    """
    global _last_state

    try:
        status = get_token_status()
    except Exception:
        logger.exception("Upstox token check raised an unexpected exception")
        return

    valid = bool(status.get("valid"))
    current_state = "valid" if valid else "invalid"

    # Log every check. INFO on success (heartbeat), WARNING on failure.
    # The structured fields make this grep-able in the logs:
    #   grep "Upstox token check" logs/token_check_scheduler.log
    if valid:
        logger.info(
            "Upstox token check: valid (present=%s)",
            status.get("present", False),
        )
    else:
        logger.warning(
            "Upstox token check: %s",
            _format_status_for_log(status),
        )

    try:
        _transition_alert(_last_state, current_state, status)
    except Exception:
        # Notification failure must never stop the check itself.
        logger.exception("Could not emit token-state transition alert")

    _last_state = current_state


def start_token_check_scheduler() -> BackgroundScheduler | None:
    """Start the periodic token-check job. Never makes startup fragile."""
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        return _scheduler
    if not TOKEN_CHECK_ENABLED:
        logger.info("Periodic Upstox token check disabled by configuration")
        return None
    try:
        tz = ZoneInfo(TIMEZONE)
        scheduler = BackgroundScheduler(timezone=tz)
        scheduler.add_job(
            _run_token_check,
            IntervalTrigger(minutes=TOKEN_CHECK_INTERVAL_MINUTES),
            id="periodic_upstox_token_check",
            name="Periodic Upstox Token Check",
            # Run the first check shortly after startup instead of waiting
            # the full interval — the operator should learn about an invalid
            # token within seconds, not 30 minutes from now.
            next_run_time=datetime.now(tz=tz)
            + timedelta(seconds=_FIRST_CHECK_DELAY_SECONDS),
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=300,
        )
        scheduler.start()
        _scheduler = scheduler
        logger.info(
            "Periodic Upstox token check scheduled every %d min "
            "(first run in %ds)",
            TOKEN_CHECK_INTERVAL_MINUTES,
            _FIRST_CHECK_DELAY_SECONDS,
        )
        return _scheduler
    except Exception:
        logger.exception(
            "Upstox token check scheduler could not initialise; "
            "application will continue"
        )
        return None


def stop_token_check_scheduler() -> None:
    """Stop the scheduler during FastAPI shutdown."""
    global _scheduler
    if _scheduler is not None:
        try:
            _scheduler.shutdown(wait=False)
        except Exception:
            logger.exception("Could not stop token check scheduler cleanly")
        _scheduler = None


def get_token_check_scheduler() -> BackgroundScheduler | None:
    """Expose the scheduler for tests or introspection endpoints."""
    return _scheduler