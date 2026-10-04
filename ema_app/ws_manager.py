"""
WebSocket broadcast hub for live EMA crosses.

Each connected client can hold multiple filters. A filter matches a cross
when every populated field matches — omitted fields act as wildcards.

Supported filter fields:
    instrument_key  → exact key match
    underlying      → "NIFTY" / "SENSEX" (case-insensitive)
    strike          → exact numeric strike
    option_type     → "CE" / "PE"
    expiry          → exact expiry string

A client can also use `subscribe_all` to receive every cross.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Optional, Set

from fastapi import WebSocket

from core.logger import get_logger

logger = get_logger(__name__)


def _normalise_filter(raw: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if not isinstance(raw, dict):
        return out

    ik = raw.get("instrument_key")
    if ik:
        out["instrument_key"] = str(ik)

    u = raw.get("underlying")
    if u:
        out["underlying"] = str(u).upper()

    s = raw.get("strike")
    if s not in (None, ""):
        try:
            out["strike"] = float(s)
        except (TypeError, ValueError):
            pass

    ot = raw.get("option_type")
    if ot:
        ot = str(ot).upper()
        if ot in ("CE", "PE"):
            out["option_type"] = ot

    ex = raw.get("expiry")
    if ex:
        out["expiry"] = str(ex)

    return out


def _matches(cross: Dict[str, Any], filt: Dict[str, Any]) -> bool:
    if not filt:
        return True

    if "instrument_key" in filt:
        if cross.get("instrument_key") != filt["instrument_key"]:
            return False

    if "underlying" in filt:
        if str(cross.get("underlying") or "").upper() != filt["underlying"]:
            return False

    if "strike" in filt:
        try:
            if float(cross.get("strike") or 0) != float(filt["strike"]):
                return False
        except (TypeError, ValueError):
            return False

    if "option_type" in filt:
        if str(cross.get("option_type") or "").upper() != filt["option_type"]:
            return False

    if "expiry" in filt:
        if str(cross.get("expiry") or "") != filt["expiry"]:
            return False

    return True


class EmaWsClient:
    def __init__(self, ws: WebSocket) -> None:
        self.ws = ws
        self.filters: List[Dict[str, Any]] = []
        self.subscribe_all: bool = False

    def add_filter(self, raw: Dict[str, Any]) -> None:
        f = _normalise_filter(raw)
        if f and f not in self.filters:
            self.filters.append(f)

    def remove_filter(self, raw: Dict[str, Any]) -> None:
        f = _normalise_filter(raw)
        self.filters = [x for x in self.filters if x != f]

    def clear_filters(self) -> None:
        self.filters = []
        self.subscribe_all = False

    def matches(self, cross: Dict[str, Any]) -> bool:
        if self.subscribe_all:
            return True
        if not self.filters:
            return True   # default: receive everything
        return any(_matches(cross, f) for f in self.filters)


class EmaWsManager:
    def __init__(self) -> None:
        self._clients: Set[EmaWsClient] = set()
        self._lock = asyncio.Lock()

    async def register(self, ws: WebSocket) -> EmaWsClient:
        client = EmaWsClient(ws)
        async with self._lock:
            self._clients.add(client)
        return client

    async def unregister(self, client: EmaWsClient) -> None:
        async with self._lock:
            self._clients.discard(client)

    def client_count(self) -> int:
        return len(self._clients)

    async def broadcast(self, cross: Dict[str, Any]) -> int:
        """
        Broadcast a cross to all clients whose filter matches.
        Returns the number of clients reached.
        """
        payload = json.dumps({"type": "live_ema_cross", **cross}, default=str)

        async with self._lock:
            targets = list(self._clients)

        dead: List[EmaWsClient] = []
        sent = 0
        for client in targets:
            if not client.matches(cross):
                continue
            try:
                await client.ws.send_text(payload)
                sent += 1
            except Exception:
                dead.append(client)

        for client in dead:
            await self.unregister(client)

        return sent

    def broadcast_threadsafe(self, cross: Dict[str, Any], loop: asyncio.AbstractEventLoop) -> None:
        """
        Callable from the SDK's background threads. Schedules the broadcast
        on the FastAPI event loop.
        """
        try:
            asyncio.run_coroutine_threadsafe(self.broadcast(cross), loop)
        except Exception as exc:
            logger.warning("EMA ws broadcast failed: %s", exc)


ema_ws_manager = EmaWsManager()