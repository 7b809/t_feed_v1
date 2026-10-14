import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote
import shutil
import websockets


# ==================================================================
# CONFIGURATION
# ==================================================================

# Runtime duration (spec: 15 minutes)
RUN_DURATION_MINUTES = 15

# ---------- Endpoint selection ------------------------------------
# "deployed"  -> wss://feed.novag7.in/ws/market
# "localhost" -> ws://localhost:8000/ws/market
ENDPOINT = "localhost"  # "deployed" or "localhost"

BASE_WS_URLS = {
    "deployed":  "wss://feed.novag7.in/ws/market",
    "localhost": "ws://localhost:8000/ws/market",
}

# ---------- Instruments -------------------------------------------
INSTRUMENT_KEY_LIST = [
    "NSE_FO|47142",
    "NSE_INDEX|Nifty 50",
    "BSE_INDEX|SENSEX",
]

# ---------- Interval modes (top-level object) ---------------------
# Keys   : friendly mode names used for filenames + record tagging.
# Values : interval in minutes, or None for the raw live ticker feed.
#
# Only "default" (raw ticker) is enabled below.  To also capture
# candle feeds, uncomment the "1min" / "3min" / "5min" lines.
INTERVAL_MODES = {
    "default": None,
    "1min":    1,
    "3min":    3,
    "5min":    5,
}

# ---------- Output layout -----------------------------------------
#   LOG_DIR  / _controller.log     <- project / controller log
#   FEED_DIR / <inst>__<mode>.json <- one JSON file per (instrument, mode)
LOG_DIR  = Path("logs")
FEED_DIR = Path("feeds")

for _d in (LOG_DIR, FEED_DIR):
    if _d.exists() and _d.is_dir():
        shutil.rmtree(_d)
    _d.mkdir(parents=True, exist_ok=True)

# Asia/Kolkata is UTC+5:30 with no DST.
IST = timezone(timedelta(hours=5, minutes=30))


# ==================================================================
# HELPERS
# ==================================================================

def sanitize(name: str) -> str:
    """
    Turn an instrument key / mode name into a filesystem-safe
    filename fragment.
        NSE_INDEX|Nifty 50   -> NSE_INDEX_Nifty_50
        default              -> default
        1min                 -> 1min
    """
    return (
        name.replace("|", "_")
            .replace(" ", "_")
            .replace("/", "_")
            .replace(":", "_")
    )


def build_ws_url(base_url: str, instrument_key: str, interval) -> str:
    encoded_key = quote(instrument_key, safe="")
    if interval is None:
        return f"{base_url}?instrument_key={encoded_key}"
    return f"{base_url}?instrument_key={encoded_key}&interval={interval}"


def feed_filename(instrument_key: str, mode: str) -> str:
    return f"{sanitize(instrument_key)}__{mode}.json"


def make_file_only_logger(name: str, log_file: Path) -> logging.Logger:
    """Logger that writes ONLY to a file (no console output)."""
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if logger.handlers:
        return logger

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(message)s"
    )
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    return logger


def make_console_logger(name: str) -> logging.Logger:
    """Logger that writes ONLY to the console (no file output)."""
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if logger.handlers:
        return logger

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(message)s"
    )
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    return logger


# ==================================================================
# FEED JSON WRITER  (pretty-printed JSON lines, one file per feed)
# ==================================================================

class FeedJsonWriter:
    """
    Append-only writer for a single feed file.

    Each record is written with indent=4 formatting.
    """
    def __init__(self, path: Path):
        self.path = path
        self._fh = None

    def _ensure_open(self):
        if self._fh is None:
            self._fh = self.path.open("a", encoding="utf-8")

    def write(self, record: dict) -> None:
        self._ensure_open()
        # Modified here to use indent=4 for clean formatting
        self._fh.write(
            json.dumps(record, ensure_ascii=False, indent=4)
        )
        self._fh.write("\n")
        self._fh.flush()

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            finally:
                self._fh = None


# ==================================================================
# CONNECTION WORKER
# ==================================================================

async def run_feed(
    base_url: str,
    instrument_key: str,
    mode: str,
    interval,
    stop_event: asyncio.Event,
    console: logging.Logger,
    controller_logger: logging.Logger,
) -> None:
    """
    One connection worker: keeps a single WebSocket alive, appends
    every feed message as an indented JSON block to
        feeds/<instrument>__<mode>.json
    and reconnects on failure until stop_event is set.
    """
    feed_file = FEED_DIR / feed_filename(instrument_key, mode)
    writer = FeedJsonWriter(feed_file)

    ws_url = build_ws_url(base_url, instrument_key, interval)

    console.info(
        "Feed started | instrument=%s | mode=%s", instrument_key, mode
    )
    controller_logger.info(
        "WORKER START | instrument=%s | mode=%s | interval=%s",
        instrument_key, mode, interval,
    )
    controller_logger.info("WORKER URL   | %s", ws_url)
    controller_logger.info("WORKER FILE  | %s", feed_file)

    try:
        while not stop_event.is_set():
            try:
                controller_logger.info(
                    "Connecting | instrument=%s | mode=%s",
                    instrument_key, mode,
                )

                async with websockets.connect(
                    ws_url,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=10,
                    max_size=None,
                ) as websocket:

                    controller_logger.info(
                        "Connected  | instrument=%s | mode=%s",
                        instrument_key, mode,
                    )

                    while not stop_event.is_set():
                        recv_task = asyncio.create_task(websocket.recv())
                        stop_task = asyncio.create_task(stop_event.wait())

                        done, pending = await asyncio.wait(
                            {recv_task, stop_task},
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        for t in pending:
                            t.cancel()

                        if stop_task in done and stop_event.is_set():
                            recv_task.cancel()
                            controller_logger.info(
                                "Stop signal received, closing | "
                                "instrument=%s | mode=%s",
                                instrument_key, mode,
                            )
                            return

                        message = recv_task.result()
                        received_at = datetime.now(IST).isoformat()

                        try:
                            data = json.loads(message)

                            # Candle feeds arrive as a JSON array
                            # [ts, o, h, l, c, vol, oi]
                            if isinstance(data, list):
                                record = {
                                    "received_at":    received_at,
                                    "instrument_key": instrument_key,
                                    "mode":           mode,
                                    "interval":       interval if interval is not None else "raw",
                                    "candle":         data,
                                }
                            else:
                                record = {
                                    "received_at":    received_at,
                                    "instrument_key": instrument_key,
                                    "mode":           mode,
                                    "interval":       interval if interval is not None else "raw",
                                    "data":           data,
                                }

                            writer.write(record)

                        except json.JSONDecodeError:
                            writer.write({
                                "received_at":    received_at,
                                "instrument_key": instrument_key,
                                "mode":           mode,
                                "raw":            message,
                            })

            except asyncio.CancelledError:
                controller_logger.info(
                    "Worker cancelled | instrument=%s | mode=%s",
                    instrument_key, mode,
                )
                raise

            except Exception as exc:
                controller_logger.exception(
                    "Connection/feed error | instrument=%s | mode=%s | %s",
                    instrument_key, mode, exc,
                )

                if stop_event.is_set():
                    return

                controller_logger.info(
                    "Reconnecting in 5s | instrument=%s | mode=%s",
                    instrument_key, mode,
                )
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=5)
                    return
                except asyncio.TimeoutError:
                    pass

    finally:
        writer.close()
        controller_logger.info(
            "Worker stopped | instrument=%s | mode=%s",
            instrument_key, mode,
        )


# ==================================================================
# MAIN
# ==================================================================

async def main():
    base_url = BASE_WS_URLS[ENDPOINT]

    console = make_console_logger("feed.controller")
    controller_logger = make_file_only_logger(
        "feed.controller.file",
        LOG_DIR / "_controller.log",
    )

    deadline = datetime.now(IST) + timedelta(minutes=RUN_DURATION_MINUTES)
    total_sockets = len(INSTRUMENT_KEY_LIST) * len(INTERVAL_MODES)

    # -------- Startup banner (console + controller log) -----------
    banner = [
        "=" * 70,
        "MULTI-INTERVAL SOCKET LOGGER STARTED",
        f"Endpoint       : {ENDPOINT}",
        f"Base URL       : {base_url}",
        f"Instruments    : {INSTRUMENT_KEY_LIST}",
        f"Interval modes : {list(INTERVAL_MODES.keys())}",
        f"Mode -> minute : {INTERVAL_MODES}",
        f"Total sockets  : {total_sockets}",
        f"Log directory  : {LOG_DIR.resolve()}",
        f"Feed directory : {FEED_DIR.resolve()}",
        f"Will close at  : {deadline.strftime('%Y-%m-%d %H:%M:%S')} IST",
        "=" * 70,
    ]
    for line in banner:
        console.info(line)
        controller_logger.info(line)

    stop_event = asyncio.Event()

    tasks = []
    for instrument_key in INSTRUMENT_KEY_LIST:
        for mode, interval in INTERVAL_MODES.items():
            tasks.append(asyncio.create_task(
                run_feed(
                    base_url=base_url,
                    instrument_key=instrument_key,
                    mode=mode,
                    interval=interval,
                    stop_event=stop_event,
                    console=console,
                    controller_logger=controller_logger,
                )
            ))

    try:
        await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True),
            timeout=RUN_DURATION_MINUTES * 60,
        )

    except asyncio.TimeoutError:
        msg = (
            f"Run duration reached ({RUN_DURATION_MINUTES} minutes). "
            f"Closing all feeds..."
        )
        console.info(msg)
        controller_logger.info(msg)

    except asyncio.CancelledError:
        msg = "Main task cancelled. Closing all feeds..."
        console.info(msg)
        controller_logger.info(msg)
        raise

    finally:
        stop_event.set()

        for task in tasks:
            task.cancel()

        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception) and not isinstance(
                result, asyncio.CancelledError
            ):
                controller_logger.error(
                    "Worker ended with error: %r", result
                )

        console.info("=" * 70)
        console.info("ALL FEEDS CLOSED")
        console.info(
            "Closed at     : %s IST",
            datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"),
        )
        console.info("=" * 70)

        controller_logger.info("=" * 70)
        controller_logger.info("MULTI-INTERVAL SOCKET LOGGER STOPPED")
        controller_logger.info(
            "Closed at (IST): %s", datetime.now(IST).isoformat()
        )
        controller_logger.info("=" * 70)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nInterrupted by user. Exiting.")
