import asyncio
import logging
from datetime import datetime
from typing import Optional

from app.config import settings

from app.database import (
    refresh_token_if_changed,
)

from app.services.daily_refresh_service import (
    refresh_market_data,
)

from app.services.live_market_feed_service import (
    market_feed_service,
)

logger = logging.getLogger("uvicorn")


class MarketScheduler:
    """
    Daily & Dynamic Trading Scheduler

    Scheduled Events:
        - Daily Refresh: 08:50
        - WebSocket Connect: 09:10
        - WebSocket Disconnect: 15:30

    Dynamic Conditions (when ENABLE_DYNAMIC_REFRESH is True):
        - Token Change Detection: Triggers refresh if database token changes
        - Interval Auto-Refresh: Triggers refresh every X minutes during trading hours
        - NIFTY Spot Movement: Triggers refresh when spot price moves >= NIFTY_POINTS_THRESHOLD

    Runs Monday-Friday only for dynamic/scheduled execution.
    """

    def __init__(self):
        self.running = False

        # Fixed scheduled task trackers
        self.last_refresh_date: Optional[str] = None
        self.last_connect_date: Optional[str] = None
        self.last_disconnect_date: Optional[str] = None

        # Dynamic refresh state trackers
        self.last_refresh_timestamp: Optional[datetime] = None
        self.last_refreshed_spot_price: Optional[float] = None

    @staticmethod
    def is_trading_day() -> bool:
        """
        Monday = 0
        Sunday = 6
        """
        return datetime.now().weekday() < 5

    def _is_market_hours(self, current_time_str: str) -> bool:
        """
        Checks if current time is within market open and close window.
        """
        return (
            settings.MARKET_OPEN_TIME <= current_time_str <= settings.MARKET_CLOSE_TIME
        )

    def _get_current_spot_price(self) -> Optional[float]:
        """
        Retrieves the latest spot price from market feed service.
        """
        try:
            if hasattr(market_feed_service, "get_latest_spot_price"):
                return market_feed_service.get_latest_spot_price()
            elif hasattr(market_feed_service, "latest_spot_price"):
                return getattr(market_feed_service, "latest_spot_price", None)
        except Exception as ex:
            logger.debug(f"Could not retrieve spot price from market feed: {ex}")
        return None

    def execute_market_refresh(self, reason: str = "Scheduled"):
        """
        Wrapper to execute market data refresh and update internal baseline trackers.
        """
        logger.info(f"Triggering Market Refresh [Reason: {reason}]...")
        refresh_market_data()

        # Update dynamic state trackers
        self.last_refresh_timestamp = datetime.now()
        spot_price = self._get_current_spot_price()
        if spot_price:
            self.last_refreshed_spot_price = spot_price
            logger.info(f"Updated dynamic refresh baseline spot price: {spot_price}")

    async def run_scheduler(self):

        logger.info("Market Scheduler Started")

        self.running = True

        while self.running:

            try:

                now = datetime.now()

                # --------------------------------------------------
                # Keep token synchronized with MongoDB & detect changes
                # --------------------------------------------------

                token_changed = False
                try:
                    token_changed = refresh_token_if_changed()
                except Exception as ex:
                    logger.error(f"Token sync failed: {ex}")

                # If token changed and token-driven refresh is enabled, refresh immediately
                if token_changed and settings.REFRESH_ON_TOKEN_CHANGE:
                    try:
                        self.execute_market_refresh(reason="Database Token Updated")
                    except Exception as ex:
                        logger.exception(f"Token-change Market Refresh Failed: {ex}")

                # --------------------------------------------------
                # Skip weekends
                # --------------------------------------------------

                if not self.is_trading_day():
                    await asyncio.sleep(60)
                    continue

                current_date = now.strftime("%Y-%m-%d")
                current_time = now.strftime("%H:%M")

                # ==================================================
                # SCHEDULED DAILY REFRESH
                # ==================================================

                if (
                    current_time >= settings.DAILY_REFRESH_TIME
                    and self.last_refresh_date != current_date
                ):

                    logger.info("Starting Scheduled Daily Market Refresh...")

                    try:

                        self.execute_market_refresh(reason="Daily Morning Schedule")
                        self.last_refresh_date = current_date

                        logger.info("Daily Market Refresh Completed.")

                    except Exception as ex:

                        logger.exception(f"Daily Refresh Failed: {ex}")

                # ==================================================
                # WEBSOCKET START
                # ==================================================

                if (
                    current_time >= settings.WEBSOCKET_CONNECT_TIME
                    and self.last_connect_date != current_date
                ):

                    logger.info("Starting WebSocket Connection...")

                    try:

                        # Ensure newest token is used
                        refresh_token_if_changed()

                        await market_feed_service.start()

                        self.last_connect_date = current_date

                        logger.info("WebSocket Connected Successfully.")

                    except Exception as ex:

                        logger.exception(f"WebSocket Startup Failed: {ex}")

                # ==================================================
                # WEBSOCKET STOP
                # ==================================================

                if (
                    current_time >= settings.MARKET_CLOSE_TIME
                    and self.last_disconnect_date != current_date
                ):

                    logger.info("Stopping Market WebSocket...")

                    try:

                        await market_feed_service.stop()

                        self.last_disconnect_date = current_date

                        logger.info("WebSocket Stopped Successfully.")

                    except Exception as ex:

                        logger.exception(f"WebSocket Shutdown Failed: {ex}")

                # ==================================================
                # DYNAMIC & CONDITIONAL REFRESH CHECKS
                # ==================================================

                if settings.ENABLE_DYNAMIC_REFRESH and self._is_market_hours(
                    current_time
                ):

                    # 1. Interval-based dynamic auto-refresh check
                    if (
                        settings.AUTO_REFRESH_INTERVAL_MINUTES > 0
                        and self.last_refresh_timestamp is not None
                    ):
                        elapsed_minutes = (
                            now - self.last_refresh_timestamp
                        ).total_seconds() / 60.0

                        if elapsed_minutes >= settings.AUTO_REFRESH_INTERVAL_MINUTES:
                            try:
                                self.execute_market_refresh(
                                    reason=f"Interval ({settings.AUTO_REFRESH_INTERVAL_MINUTES} mins elapsed)"
                                )
                            except Exception as ex:
                                logger.exception(
                                    f"Interval Dynamic Refresh Failed: {ex}"
                                )

                    # 2. NIFTY Point Movement Threshold Check
                    if (
                        settings.NIFTY_POINTS_THRESHOLD > 0
                        and self.last_refreshed_spot_price is not None
                    ):
                        current_spot = self._get_current_spot_price()

                        if current_spot:
                            price_diff = abs(
                                current_spot - self.last_refreshed_spot_price
                            )

                            if price_diff >= settings.NIFTY_POINTS_THRESHOLD:
                                try:
                                    self.execute_market_refresh(
                                        reason=f"NIFTY moved {price_diff:.2f} pts (Threshold: {settings.NIFTY_POINTS_THRESHOLD})"
                                    )
                                except Exception as ex:
                                    logger.exception(
                                        f"Spot Movement Dynamic Refresh Failed: {ex}"
                                    )

                # Use configurable loop interval
                sleep_interval = getattr(settings, "DYNAMIC_CHECK_INTERVAL_SECONDS", 15)
                await asyncio.sleep(sleep_interval)

            except Exception as ex:

                logger.exception(f"Scheduler Loop Error: {ex}")

                await asyncio.sleep(30)

    async def start(self):
        """
        Start scheduler task.
        """

        asyncio.create_task(self.run_scheduler())

        logger.info("Market Scheduler Task Created")

    async def stop(self):
        """
        Stop scheduler gracefully.
        """

        self.running = False

        logger.info("Market Scheduler Stopped")


market_scheduler = MarketScheduler()
