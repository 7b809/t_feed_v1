import json
import logging
from datetime import datetime

import websocket

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
)

WS_URL = "wss://t-feed.up.railway.app/ws/feed"

SUBSCRIPTION = {
    "strike": 23900,
    "type": "CE",
    "interval": 0,  # 0 = Live Tick
}


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def on_open(ws):
    logging.info("=" * 80)
    logging.info("CONNECTED TO WEBSOCKET")
    logging.info("URL : %s", WS_URL)
    logging.info("SUB : %s", json.dumps(SUBSCRIPTION, indent=4))
    logging.info("=" * 80)

    ws.send(json.dumps(SUBSCRIPTION))
    logging.info("Subscription request sent.")


def on_message(ws, message):
    print("\n" + "=" * 80)
    print(f"[{now()}] MESSAGE RECEIVED")
    print("=" * 80)

    try:
        payload = json.loads(message)
        print(json.dumps(payload, indent=4))
    except Exception:
        print(message)


def on_error(ws, error):
    logging.error("WEBSOCKET ERROR")
    logging.exception(error)


def on_close(ws, close_status_code, close_msg):
    logging.info("=" * 80)
    logging.info("WEBSOCKET CLOSED")
    logging.info("Status Code : %s", close_status_code)
    logging.info("Message     : %s", close_msg)
    logging.info("=" * 80)


if __name__ == "__main__":
    websocket.enableTrace(False)

    ws = websocket.WebSocketApp(
        WS_URL,
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close,
    )

    logging.info("Connecting to %s", WS_URL)

    ws.run_forever(
        ping_interval=30,
        ping_timeout=10,
    )
