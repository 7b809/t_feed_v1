# jobs/daily_refresh.py
"""Daily refresh job: token, feed, pivot levels, Telegram summary."""

from core.token_service import fetch_access_token, validate_access_token
from core.market_feed_service import MarketFeedService
from core.pivot_levels_loader import (
    is_pivot_levels_updated_today,
    load_pivot_levels_to_local,
)
from core.telegram_notifier import (
    send_telegram_message,
    build_daily_refresh_summary,
)
from core.logger import get_logger

logger = get_logger(__file__)

_feed_service = None   # module‑level so the disconnect job can access it


def run_daily_refresh():
    """Execute all daily refresh tasks and send **one** Telegram summary."""
    global _feed_service
    tasks = {}

    # 1. Fetch token
    try:
        token = fetch_access_token()
        tasks["Token fetched"] = True
    except Exception:
        logger.exception("Token fetch failed.")
        tasks["Token fetched"] = False
        send_telegram_message(build_daily_refresh_summary(tasks))
        return

    # 2. Validate token
    valid = validate_access_token(token)
    tasks["Token valid"] = valid
    if not valid:
        send_telegram_message(build_daily_refresh_summary(tasks))
        return

    # 3. Connect to feed
    try:
        _feed_service = MarketFeedService(token)
        _feed_service.connect()
        tasks["Feed connected"] = True
    except Exception:
        logger.exception("Feed connection failed.")
        tasks["Feed connected"] = False

    # 4. Check pivot levels
    try:
        updated = is_pivot_levels_updated_today()
        tasks["Pivot levels updated today"] = updated
        if updated:
            load_pivot_levels_to_local()
            tasks["Pivot levels loaded"] = True
        else:
            tasks["Pivot levels loaded"] = False
    except Exception:
        logger.exception("Pivot levels check failed.")
        tasks["Pivot levels updated today"] = False
        tasks["Pivot levels loaded"] = False

    # 5. Send single summary message
    summary = build_daily_refresh_summary(tasks)
    send_telegram_message(summary)
    logger.info("Daily refresh completed: %s", tasks)


def disconnect_feed():
    """Disconnect the market feed (called at 15:45)."""
    global _feed_service
    if _feed_service:
        _feed_service.disconnect()
        logger.info("Feed disconnected at end of day.")