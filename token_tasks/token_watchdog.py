"""
token_tasks/token_watchdog.py

Periodic token health watchdog.

Runs an asyncio task that, every WATCHDOG_INTERVAL_SEC:
  1. Calls check_token_health() in a worker thread.
  2. If the token is valid and was previously invalid: send a recovery
     message and clear internal state.
  3. If the token is invalid and was previously valid: send the initial
     "token expired" alert.
  4. If the token is still invalid: send a reminder every
     WATCHDOG_NOTIFY_COOLDOWN_SEC, never faster.

The watchdog only observes; it never touches MongoDB or the profile
endpoint directly. All notifications go through `telegram_manager`, so
disabling Telegram turns this into a silent monitor.
"""
import asyncio
import time
from typing import Any, Dict, Optional

from core.logger import get_logger
from telegram_app.manager import telegram_manager
from token_tasks.config import token_config
from token_tasks.health_check import check_token_health

logger = get_logger(__name__)


def _short(exc: BaseException) -> str:
    msg = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
    if len(msg) > 160:
        msg = msg[:160] + "…"
    return f"{type(exc).__name__}: {msg}" if msg else type(exc).__name__


class TokenWatchdog:
    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        self._stop_event: Optional[asyncio.Event] = None

        # Watchdog state
        self._was_valid: bool = True
        self._invalid_since: Optional[float] = None
        self._last_notify_at: Optional[float] = None
        self._reminder_count: int = 0

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    # ---- lifecycle -----------------------------------------------------
    def start(self, initial_valid: bool = True) -> None:
        """Start the watchdog loop. `initial_valid` seeds the state so the
        first invalid observation is treated correctly (avoids a duplicate
        alert when the lifespan has already notified)."""
        if not token_config.WATCHDOG_ENABLED:
            logger.info("Token watchdog disabled")
            return
        if self.running:
            logger.info("Token watchdog already running")
            return

        self._was_valid = bool(initial_valid)
        self._invalid_since = None
        self._last_notify_at = None
        self._reminder_count = 0

        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._run_loop())
        logger.info(
            "Token watchdog started | interval=%ds | cooldown=%ds | initial_valid=%s",
            token_config.WATCHDOG_INTERVAL_SEC,
            token_config.WATCHDOG_NOTIFY_COOLDOWN_SEC,
            initial_valid,
        )

    async def stop(self) -> None:
        if self._task is None:
            return
        if self._stop_event:
            self._stop_event.set()
        try:
            await asyncio.wait_for(self._task, timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning("Token watchdog did not stop in time; cancelling")
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._task = None
        self._stop_event = None
        logger.info("Token watchdog stopped")

    # ---- loop ----------------------------------------------------------
    async def _run_loop(self) -> None:
        interval = max(30, int(token_config.WATCHDOG_INTERVAL_SEC))

        # Optional immediate check on startup
        if token_config.WATCHDOG_RUN_ON_START:
            await self._check_once()

        while self._stop_event and not self._stop_event.is_set():
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
                return  # stop requested
            except asyncio.TimeoutError:
                pass
            await self._check_once()

    async def _check_once(self) -> None:
        try:
            health = await asyncio.to_thread(check_token_health)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "watchdog::_check_once failed | step=health_check | reason=%s",
                _short(exc),
            )
            return
        try:
            self._evaluate(health)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "watchdog::_evaluate failed | step=classify | reason=%s",
                _short(exc),
            )

    # ---- state machine -------------------------------------------------
    def _evaluate(self, health: Dict[str, Any]) -> None:
        valid = bool(health.get("valid"))
        now = time.time()
        cooldown = max(60, int(token_config.WATCHDOG_NOTIFY_COOLDOWN_SEC))

        if valid:
            if not self._was_valid:
                self._notify_recovered()
            self._was_valid = True
            self._invalid_since = None
            self._last_notify_at = None
            self._reminder_count = 0
            return

        # Invalid token path
        first_time = self._was_valid or self._invalid_since is None
        if first_time:
            self._invalid_since = now
            self._last_notify_at = now
            self._reminder_count = 0
            self._notify_expired(health, reminder=False)
        else:
            elapsed = now - (self._last_notify_at or 0.0)
            if elapsed >= cooldown:
                self._last_notify_at = now
                self._reminder_count += 1
                self._notify_expired(health, reminder=True)
            else:
                logger.debug(
                    "watchdog | token still invalid | next reminder in %.0fs",
                    cooldown - elapsed,
                )
        self._was_valid = False
        telegram_manager.mark_token_healthy(False)

    # ---- notifications -------------------------------------------------
    def _notify_expired(self, health: Dict[str, Any], reminder: bool) -> None:
        reason = health.get("error") or health.get("validation_status") or "unknown"
        invalid_since_iso = None
        if self._invalid_since:
            from datetime import datetime
            invalid_since_iso = (
                datetime.fromtimestamp(self._invalid_since)
                .astimezone()
                .strftime("%Y-%m-%d %H:%M:%S")
            )
        try:
            telegram_manager.notify_token_expired(
                reason=str(reason),
                invalid_since=invalid_since_iso,
                reminder_number=self._reminder_count,
                is_reminder=reminder,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "watchdog::_notify_expired failed | step=telegram | reason=%s",
                _short(exc),
            )

    def _notify_recovered(self) -> None:
        try:
            telegram_manager.notify_token_recovered()
            telegram_manager.mark_token_healthy(True)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "watchdog::_notify_recovered failed | step=telegram | reason=%s",
                _short(exc),
            )


token_watchdog = TokenWatchdog()