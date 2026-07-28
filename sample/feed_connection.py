import json
import os
import logging
from app.config import settings
from app.database import connect_to_mongo, load_upstox_token, token_state
import upstox_client

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def append_tick_to_file(file_path: str, data: dict):
    """Appends a single tick JSON object as a new line in a file."""
    # Ensure directory exists
    os.makedirs(os.path.dirname(file_path), exist_ok=True)
    
    with open(file_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(data) + "\n")


def start_stream():
    # 1. Connect to Mongo & fetch the stored token into memory
    connect_to_mongo()
    load_upstox_token()

    access_token = token_state.access_token
    if not access_token:
        logger.error("Could not retrieve access_token from DB! Exiting.")
        return

    logger.info("Retrieved Upstox Access Token successfully.")

    # 2. Configure Upstox Client
    configuration = upstox_client.Configuration()
    configuration.access_token = access_token
    api_client = upstox_client.ApiClient(configuration)

    # 3. Instantiate MarketDataStreamerV3
    streamer = upstox_client.MarketDataStreamerV3(api_client)

    # Instrument to subscribe to
    target_instrument = "NSE_FO|63935"
    mode = settings.UPSTOX_MODE  # Uses "ltpc" or "full" from .env

    def on_open():
        logger.info(f"WebSocket Connected! Subscribing to {target_instrument} in '{mode}' mode...")
        streamer.subscribe([target_instrument], mode)

    def on_message(message):
        logger.info(f"Tick received: {message}")
        
        # Save received tick data to disk
        append_tick_to_file(settings.TICK_FILE, message)

    def on_error(error):
        logger.error(f"WebSocket Error: {error}")

    def on_close(close_status_code, close_msg):
        logger.warning(f"WebSocket Closed: {close_status_code} - {close_msg}")

    # Register handlers
    streamer.on("open", on_open)
    streamer.on("message", on_message)
    streamer.on("error", on_error)
    streamer.on("close", on_close)

    # Connect WebSocket
    logger.info("Connecting to Upstox WebSocket...")
    streamer.connect()


if __name__ == "__main__":
    start_stream()