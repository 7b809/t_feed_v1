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
    Daily Trading Scheduler

    Daily Refresh:
        08:50

    WebSocket Connect:
        09:10

    WebSocket Disconnect:
        15:30

    Token Change Detection:
        Every scheduler cycle

    Runs Monday-Friday only.
    """

    def __init__(self):
        self.running = False

        self.last_refresh_date: Optional[str] = None
        self.last_connect_date: Optional[str] = None
        self.last_disconnect_date: Optional[str] = None

    @staticmethod
    def is_trading_day() -> bool:
        """
        Monday = 0
        Sunday = 6
        """

        return datetime.now().weekday() < 5

    async def run_scheduler(self):

        logger.info("Market Scheduler Started")

        self.running = True

        while self.running:

            try:

                now = datetime.now()

                # --------------------------------------------------
                # Keep token synchronized with MongoDB
                # --------------------------------------------------

                try:
                    refresh_token_if_changed()
                except Exception as ex:
                    logger.error(f"Token sync failed: {ex}")

                # --------------------------------------------------
                # Skip weekends
                # --------------------------------------------------

                if not self.is_trading_day():
                    await asyncio.sleep(60)
                    continue

                current_date = now.strftime("%Y-%m-%d")

                current_time = now.strftime("%H:%M")

                # ==================================================
                # DAILY REFRESH
                # ==================================================

                if (
                    current_time >= settings.DAILY_REFRESH_TIME
                    and self.last_refresh_date != current_date
                ):

                    logger.info("Starting Daily Market Refresh...")

                    try:

                        refresh_market_data()

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

                await asyncio.sleep(20)

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
