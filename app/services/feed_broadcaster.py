# app/services/feed_broadcaster.py
#
# Async fan-out for WebSocket clients.
# Channels:
#   index:<nifty|sensex>
#   options:<underlying>:<strike><type>       (e.g. options:nifty:21000CE)
#   instrument:<instrument_key>               (raw key, e.g. NSE_FO|54321)
#
# The dashboard at templates/index.html uses ``channel_counts()`` to show
# how many WebSocket clients are attached per channel.

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Any

from fastapi import WebSocket

logger = logging.getLogger(__name__)

class FeedBroadcaster:
    def __init__(self) -> None:
        self._subs: dict[str, set[WebSocket]] = defaultdict(set)
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ #
    async def register(self, channel: str, ws: WebSocket) -> None:
        async with self._lock:
            self._subs[channel].add(ws)
        logger.info("WS subscribed channel=%s total=%d", channel, len(self._subs[channel]))

    async def unregister(self, channel: str, ws: WebSocket) -> None:
        async with self._lock:
            self._subs[channel].discard(ws)
            if not self._subs[channel]:
                self._subs.pop(channel, None)

    async def count(self, channel: str) -> int:
        async with self._lock:
            return len(self._subs.get(channel, ()))

    # ------------------------------------------------------------------ #
    # Dashboard helper: snapshot of channel → live socket count.
    # Read-only and doesn't prune dead sockets (that happens on broadcast).
    # ------------------------------------------------------------------ #
    def channel_counts(self) -> dict[str, int]:
        return {
            channel: len(sockets)
            for channel, sockets in self._subs.items()
            if sockets
        }

    # ------------------------------------------------------------------ #
    async def broadcast(self, payload: dict[str, Any], channels: list[str]) -> None:
        targets: set[WebSocket] = set()
        async with self._lock:
            for channel in channels:
                targets.update(self._subs.get(channel, ()))
        if not targets:
            return
        dead: list[WebSocket] = []
        for ws in targets:
            try:
                await ws.send_json(payload)
            except Exception:  # noqa: BLE001
                dead.append(ws)
        if dead:
            async with self._lock:
                for channel in list(self._subs.keys()):
                    for ws in dead:
                        self._subs[channel].discard(ws)
                    if not self._subs[channel]:
                        self._subs.pop(channel, None)