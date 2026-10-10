import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import websockets


BASE_WS_URL = "wss://feed.novag7.in/ws/market"
INSTRUMENT_KEY = "NSE_FO|44612"

LOG_DIR = Path("logs")
LOG_DIR.mkdir(parents=True, exist_ok=True)

LOG_FILE = LOG_DIR / "nifty_50_feed.log"


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)

logger = logging.getLogger("nifty-feed")


def build_ws_url() -> str:
    encoded_key = quote(INSTRUMENT_KEY, safe="")
    return f"{BASE_WS_URL}?instrument_key={encoded_key}"


async def connect_and_receive():
    ws_url = build_ws_url()

    logger.info("Starting WebSocket feed")
    logger.info("Instrument: %s", INSTRUMENT_KEY)
    logger.info("WebSocket URL: %s", ws_url)

    while True:
        try:
            logger.info("Connecting to WebSocket...")

            async with websockets.connect(
                ws_url,
                ping_interval=20,
                ping_timeout=20,
                close_timeout=10,
                max_size=None,
            ) as websocket:

                logger.info("WebSocket connected")
                logger.info("Receiving feed...")

                while True:
                    message = await websocket.recv()

                    timestamp = datetime.now().astimezone().isoformat()

                    # Try to parse JSON for readable logging
                    try:
                        data = json.loads(message)

                        log_record = {
                            "received_at": timestamp,
                            "instrument_key": INSTRUMENT_KEY,
                            "data": data,
                        }

                        logger.info(
                            "FEED | %s",
                            json.dumps(
                                log_record,
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                        )

                    except json.JSONDecodeError:
                        logger.info(
                            "FEED | received_at=%s | data=%s",
                            timestamp,
                            message,
                        )

        except asyncio.CancelledError:
            logger.info("WebSocket task cancelled")
            raise

        except Exception as exc:
            logger.exception(
                "WebSocket connection/feed error: %s",
                exc,
            )

            logger.info("Reconnecting in 5 seconds...")
            await asyncio.sleep(5)


async def main():
    logger.info("=" * 80)
    logger.info("NIFTY 50 SOCKET LOGGER STARTED")
    logger.info("=" * 80)

    await connect_and_receive()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("NIFTY 50 SOCKET LOGGER STOPPED")