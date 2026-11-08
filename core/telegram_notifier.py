# core/telegram_notifier.py
"""Telegram notification helper.

All outbound Telegram traffic goes through ``send_telegram_message``.

A single master switch, ``TELEGRAM_ENABLED`` (env var), controls whether
messages are actually sent:

  * ``TELEGRAM_ENABLED=true``  -> messages are sent (requires bot token
                                  and chat id to be configured).
  * ``TELEGRAM_ENABLED=false`` -> ``send_telegram_message`` returns
                                  ``False`` immediately, does not perform
                                  any HTTP request, and logs at INFO
                                  level on first call so the operator
                                  knows notifications are off.

This means you can keep the whole notification pipeline wired through
the project while running locally / in tests / on CI without spamming
a real chat. Every existing call site (``main.py``, ``scheduler.py``,
``jobs/daily_refresh.py``) keeps working unchanged.

Failure modes (missing token, missing chat id, HTTP error, network
error, JSON parse error) are **non-fatal**: every code path returns a
boolean and logs. No exception is ever raised to the caller.
"""

from __future__ import annotations

import threading

import requests

from core.config import (
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    TELEGRAM_ENABLED,
)
from core.logger import get_logger

logger = get_logger(__file__)

# One-shot flag so we only print "telegram disabled" once per process.
_disabled_notice_logged = threading.Lock()


def is_enabled() -> bool:
    """Return ``True`` when Telegram is enabled *and* credentials are set.

    Callers can use this to skip building expensive payloads (e.g. the
    daily summary) when notifications are turned off.
    """
    return bool(TELEGRAM_ENABLED and TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)


def _log_disabled_once(reason: str) -> None:
    """Log 'telegram disabled' exactly once per process."""
    global _disabled_notice_logged
    if _disabled_notice_logged.acquire(blocking=False):
        try:
            logger.info(
                "Telegram notifications are DISABLED (%s). "
                "Set TELEGRAM_ENABLED=true, TELEGRAM_BOT_TOKEN and "
                "TELEGRAM_CHAT_ID to enable.",
                reason,
            )
        finally:
            # Keep the lock held forever — simplest way to guarantee a
            # single log line. The lock is not used for mutual exclusion
            # of any other work.
            pass


def send_telegram_message(text: str) -> bool:
    """Send a text message via the Telegram Bot API.

    Returns ``True`` on success, ``False`` on any of:

      * ``TELEGRAM_ENABLED`` is not ``true``           (silent skip)
      * bot token or chat id missing                   (logged once)
      * HTTP error, timeout, network failure           (logged per call)

    Never raises.
    """
    if not TELEGRAM_ENABLED:
        _log_disabled_once("TELEGRAM_ENABLED=false")
        return False

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning(
            "Telegram enabled but token/chat id missing; skipping message."
        )
        return False

    if not text:
        logger.warning("Telegram message text is empty; skipping.")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        logger.info("Telegram message sent successfully.")
        return True
    except requests.HTTPError:
        # Upstox/Telegram 4xx/5xx — body may include the reason.
        logger.exception(
            "Telegram HTTP error: status=%s body=%s",
            getattr(resp, "status_code", "?"),
            getattr(resp, "text", "")[:300],
        )
        return False
    except Exception:
        logger.exception("Failed to send Telegram message")
        return False


def build_daily_refresh_summary(tasks: dict) -> str:
    """Build a single summary message for the daily refresh.

    One Telegram message per daily refresh, containing every task's
    pass/fail status. Sample output::

        *Daily Refresh Summary*
        ✅ Token fetched
        ✅ Token valid
        ✅ Feed connected
        ❌ Pivot levels updated today
        ❌ Pivot levels loaded
    """
    lines = ["*Daily Refresh Summary*"]
    for name, status in tasks.items():
        emoji = "✅" if status else "❌"
        lines.append(f"{emoji} {name}")
    return "\n".join(lines)