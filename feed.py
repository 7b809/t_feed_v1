import asyncio
import json
import os
from datetime import datetime
import websockets

# CONFIGURATION
TEST_MODE = True

LOCAL_WS = "ws://127.0.0.1:8000/ws/feed"
PROD_WS = "wss://t-feed.up.railway.app/ws/feed"
WS_URL = LOCAL_WS if TEST_MODE else PROD_WS

LOG_DIR = "logs"

# Ensure output directory exists
os.makedirs(LOG_DIR, exist_ok=True)


def get_timestamp():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def write_log(filepath, text):
    """Appends structured text log messages to the specified log file."""
    with open(filepath, "a", encoding="utf-8") as f:
        f.write(text + "\n")


async def listen_and_log(interval: int, filename: str, strike: int = 23900, option_type: str = "CE"):
    """
    Connects to the WebSocket feed for a specific interval and appends responses to a log file.
    Clears the log file upon establishing a new subscription.
    """
    filepath = os.path.join(LOG_DIR, filename)

    # Clear log file on startup
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(f"=== Starting Feed Logger for Interval {interval}m at {get_timestamp()} ===\n\n")

    payload = {
        "strike": strike,
        "type": option_type,
        "interval": interval
    }

    print(f"[{get_timestamp()}] [Interval {interval}m] Connecting to {WS_URL}...")

    try:
        async with websockets.connect(WS_URL) as ws:
            print(f"[{get_timestamp()}] [Interval {interval}m] Connected. Sending subscription...")
            write_log(filepath, f"[{get_timestamp()}] Connected to {WS_URL}")

            # Send subscription payload
            await ws.send(json.dumps(payload))
            write_log(filepath, f"[{get_timestamp()}] Sent Subscription:\n{json.dumps(payload, indent=4)}\n")

            message_count = 0

            # Listen continuously for messages
            async for message in ws:
                message_count += 1
                timestamp_str = f"[{get_timestamp()}]"

                try:
                    data = json.loads(message)

                    # If subscription confirmation received, clear startup headers and begin fresh
                    if data.get("status") == "subscribed":
                        with open(filepath, "w", encoding="utf-8") as f:
                            f.write(f"=== NEW SUBSCRIPTION ACTIVATED ({interval}m) AT {get_timestamp()} ===\n\n")
                        message_count = 1

                    formatted_data = json.dumps(data, indent=4)
                    log_entry = (
                        f"----------------------------------------------------\n"
                        f"{timestamp_str} Message #{message_count}\n"
                        f"{formatted_data}\n"
                    )
                except json.JSONDecodeError:
                    log_entry = (
                        f"----------------------------------------------------\n"
                        f"{timestamp_str} Message #{message_count}\n"
                        f"{message}\n"
                    )

                write_log(filepath, log_entry)
                print(f"{timestamp_str} [Interval {interval}m] Logged message #{message_count}")

    except Exception as e:
        error_msg = f"[{get_timestamp()}] [Interval {interval}m] Connection Error: {e}"
        print(error_msg)
        write_log(filepath, f"\n{error_msg}\n")


async def main():
    # Run both 1-minute and 5-minute tasks concurrently
    await asyncio.gather(
        listen_and_log(interval=1, filename="feed_1min.log"),
        listen_and_log(interval=5, filename="feed_5min.log")
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nLogging stopped by user.")