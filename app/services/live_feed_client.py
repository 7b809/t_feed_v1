"""Upstream LTP WebSocket client for a single instrument.

Connects to ``{LIVE_FEED_URL}?instrument_key={url-encoded key}`` and parses
JSON payloads of the shape::

    {
      "received_at": "2026-10-07T10:21:47.492625+05:30",
      "instrument_key": "NSE_INDEX|Nifty 50",
      "data": {
        "instrument_key": "NSE_INDEX|Nifty 50",
        "feed": {"ltpc": {"ltp": 22680.95, "ltt": "1791348706000", "cp": 22776.1}}
      }
    }

Only ``ltp`` / ``ltt`` / ``cp`` are extracted; everything else is ignored.

Reconnection
------------
On any error or normal close the client waits ``LIVE_FEED_RECONNECT_SECONDS``
and reconnects, forever, until ``stop()`` is called. Every status transition
is reported via the ``on_status`` callback.

Market hours
------------
The upstream feed decides whether to send data based on market hours and
trading days. This client does not replicate that logic — it simply sits
connected and forwards whatever arrives.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable
from urllib.parse import quote

import websockets
from websockets.exceptions import ConnectionClosed

from app.core.config import settings
from app.core.logger import get_logger

logger = get_logger(__name__)

TickCallback = Callable[[str, dict[str, Any]], Awaitable[None]]
StatusCallback = Callable[[str, str, str | None], Awaitable[None]]

class LiveFeedClient:
    def __init__(
        self,
        instrument_key: str,
        on_tick: TickCallback,
        on_status: StatusCallback,
    ) -> None:
        self.instrument_key = instrument_key
        self._on_tick = on_tick
        self._on_status = on_status
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()

    # ------------------------------------------------------------------ #
    # Lifecycle                                                          #
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(
            self._run(), name=f"live-feed:{self.instrument_key}"
        )

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except asyncio.TimeoutError:
                self._task.cancel()
                try:
                    await self._task
                except (asyncio.CancelledError, Exception):
                    pass
        self._task = None

    # ------------------------------------------------------------------ #
    # Connection loop                                                    #
    # ------------------------------------------------------------------ #
    def _url(self) -> str:
        encoded = quote(self.instrument_key, safe="")
        return f"{settings.live_feed_url}?instrument_key={encoded}"

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self._connect_and_consume()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self._on_status(
                    self.instrument_key, "error", f"{type(exc).__name__}: {exc}"
                )
                logger.warning(
                    "Live feed client error; instrument=%s error=%s",
                    self.instrument_key,
                    exc,
                )

            if self._stop_event.is_set():
                break

            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=settings.live_feed_reconnect_seconds,
                )
            except asyncio.TimeoutError:
                pass

        await self._on_status(self.instrument_key, "stopped", None)

    async def _connect_and_consume(self) -> None:
        url = self._url()
        await self._on_status(self.instrument_key, "connecting", url)

        async with websockets.connect(
            url,
            ping_interval=settings.live_feed_ping_interval_seconds,
            ping_timeout=settings.live_feed_ping_interval_seconds,
            max_size=2**20,
            close_timeout=5,
        ) as ws:
            await self._on_status(self.instrument_key, "connected", url)
            while not self._stop_event.is_set():
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=60)
                except asyncio.TimeoutError:
                    continue
                except ConnectionClosed:
                    await self._on_status(self.instrument_key, "closed", None)
                    return
                self._handle_message(raw)

    def _handle_message(self, raw: Any) -> None:
        if isinstance(raw, (bytes, bytearray)):
            try:
                raw = raw.decode("utf-8", errors="replace")
            except Exception:
                return

        try:
            payload = json.loads(raw)
        except Exception:
            return

        if not isinstance(payload, dict):
            return

        data = payload.get("data")
        if not isinstance(data, dict):
            return
        feed = data.get("feed")
        if not isinstance(feed, dict):
            return
        ltpc = feed.get("ltpc")
        if not isinstance(ltpc, dict):
            return
        ltp = ltpc.get("ltp")
        if ltp is None:
            return

        tick = {
            "ltp": ltp,
            "ltt": ltpc.get("ltt"),
            "cp": ltpc.get("cp"),
            "received_at": payload.get("received_at")
            or datetime.now(timezone.utc).isoformat(),
        }
        asyncio.create_task(self._on_tick(self.instrument_key, tick))