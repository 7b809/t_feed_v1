import asyncio
import logging
import time as _time
from datetime import datetime, time

from .config import (
    IST,
    SESSION_START_HOUR,
    SESSION_START_MINUTE,
    SESSION_END_HOUR,
    SESSION_END_MINUTE,
    SCHEDULER_TICK_SECONDS,
    SUBSCRIPTIONS_REFRESH_SECONDS,
)
from .socket_manager import SocketManager

logger = logging.getLogger(__name__)


def in_session(now: datetime | None = None) -> bool:
    now = now or datetime.now(IST)
    if now.weekday() >= 5:
        return False
    current = now.timetz().replace(tzinfo=None)
    start = time(SESSION_START_HOUR, SESSION_START_MINUTE)
    end = time(SESSION_END_HOUR, SESSION_END_MINUTE)
    return start <= current < end


async def session_scheduler(manager: SocketManager) -> None:
    """
    Background loop that:

      * refreshes subscriptions on a fixed interval (throttled), and
        starts any newly added workers when the market is open
      * opens all workers when the market window begins
      * closes all workers when the market window ends

    Manual start/stop via the API is allowed at any time, but the
    scheduler will enforce the market gate again on its next tick.
    """
    was_active = False
    last_refresh = 0.0

    # Initial refresh so the dashboard can render cards immediately.
    try:
        await manager.refresh_subscriptions(auto_start=in_session())
        last_refresh = _time.monotonic()
    except Exception:
        logger.exception("Initial subscription refresh failed")

    while True:
        active = in_session()
        try:
            # Throttled refresh
            if _time.monotonic() - last_refresh >= SUBSCRIPTIONS_REFRESH_SECONDS:
                await manager.refresh_subscriptions(auto_start=active)
                last_refresh = _time.monotonic()

            if active and not was_active:
                logger.info("IST market session started; connecting workers")
                await manager.start_all()
            elif not active and was_active:
                logger.info("IST market session ended; stopping workers")
                await manager.stop_all()

            was_active = active
        except Exception:
            logger.exception("Scheduler iteration failed")

        await asyncio.sleep(SCHEDULER_TICK_SECONDS)