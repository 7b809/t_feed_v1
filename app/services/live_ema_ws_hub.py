"""Broadcast hub for our own live-EMA WebSocket endpoints.

``LiveLtpEmaStore`` publishes derived events (ticks, completed candles,
EMA crosses) to this hub, which fans them out to every connected client
subscribed to the same ``instrument_key``.
"""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from typing import Any

from fastapi import WebSocket

from app.core.logger import get_logger

logger = get_logger(__name__)

class LiveEmaWsHub:
    def __init__(self) -> None:
        self._subscribers: dict[str, set[WebSocket]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def subscribe(self, instrument_key: str, ws: WebSocket) -> None:
        async with self._lock:
            self._subscribers[instrument_key].add(ws)

    async def unsubscribe(self, instrument_key: str, ws: WebSocket) -> None:
        async with self._lock:
            bucket = self._subscribers.get(instrument_key)
            if not bucket:
                return
            bucket.discard(ws)
            if not bucket:
                self._subscribers.pop(instrument_key, None)

    async def broadcast(self, instrument_key: str, message: dict[str, Any]) -> None:
        async with self._lock:
            targets = list(self._subscribers.get(instrument_key, ()))
        if not targets:
            return

        text = json.dumps(message)
        dead: list[WebSocket] = []
        for ws in targets:
            try:
                await ws.send_text(text)
            except Exception:
                dead.append(ws)

        if dead:
            async with self._lock:
                bucket = self._subscribers.get(instrument_key)
                if bucket:
                    for ws in dead:
                        bucket.discard(ws)
                    if not bucket:
                        self._subscribers.pop(instrument_key, None)

    def subscription_counts(self) -> dict[str, int]:
        return {k: len(v) for k, v in self._subscribers.items() if v}

live_ema_ws_hub = LiveEmaWsHub()