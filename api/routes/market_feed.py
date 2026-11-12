# api/routes/market_feed.py
"""Downstream WebSocket gateway for Upstox MarketDataStreamerV3.

Connect:
  /ws/market?instrument_key=NSE_FO%7C45405
  /ws/market?instrument_keys=NSE_FO%7C45405,NSE_FO%7C45404
  /ws/market?instrument_key=NSE_FO%7C45405&mode=ltpc

A separate upstream connection is maintained for each requested mode because
Upstox selects the mode at streamer construction time. Clients using the same
mode share one upstream stream and reference-count instrument subscriptions.
"""

from __future__ import annotations

import asyncio
import json
import threading
import uuid
from urllib.parse import unquote

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from core.config import VALID_FEED_MODES
from core.logger import get_logger
from core.market_feed_service import MarketFeedService
from core.token_service import fetch_access_token
from jobs.daily_refresh import get_feed_service

logger = get_logger(__file__)
router = APIRouter(tags=["market-feed"])


def _parse_keys(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        for key in unquote(value).split(","):
            key = key.strip()
            if key and key not in result:
                result.append(key)
    return result


class WebSocketFeedManager:
    """Share upstream feeds per mode and isolate downstream client queues."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._feeds: dict[str, MarketFeedService] = {}
        self._listeners: dict[str, object] = {}
        self._baseline: dict[str, set[str]] = {}
        self._refcounts: dict[str, dict[str, int]] = {}
        self._clients: dict[str, dict] = {}
        self._thread_lock = threading.RLock()

    def _get_or_create_feed(self, mode: str) -> MarketFeedService:
        feed = self._feeds.get(mode)
        if feed and feed.get_health().get("running"):
            return feed

        # Reuse the application's primary feed if its mode matches.
        primary = get_feed_service()
        if primary and primary.get_health().get("running") and primary.mode == mode:
            feed = primary
        else:
            feed = MarketFeedService(fetch_access_token(), instrument_keys=[], mode=mode)
            feed.connect()

        self._feeds[mode] = feed
        self._baseline[mode] = set(feed.instrument_keys)
        self._refcounts.setdefault(mode, {})

        def listener(payload: dict, feed_mode: str = mode) -> None:
            self._dispatch(feed_mode, payload)

        self._listeners[mode] = listener
        feed.add_message_listener(listener)
        return feed

    def _dispatch(self, mode: str, feeds: dict) -> None:
        # SDK callback runs on a worker thread; never call WebSocket APIs here.
        with self._thread_lock:
            clients = list(self._clients.values())

        for instrument_key, payload in feeds.items():
            message = {
                "type": "market_data",
                "mode": mode,
                "instrument_key": instrument_key,
                "data": payload,
            }
            for client in clients:
                if client["mode"] != mode or instrument_key not in client["keys"]:
                    continue
                loop, queue = client["loop"], client["queue"]
                if loop.is_closed():
                    continue

                def enqueue(q=queue, item=message):
                    # Latest market state is more useful than a growing,
                    # increasingly stale queue when a consumer is slow.
                    if q.full():
                        try:
                            q.get_nowait()
                        except asyncio.QueueEmpty:
                            pass
                    try:
                        q.put_nowait(item)
                    except asyncio.QueueFull:
                        pass

                try:
                    loop.call_soon_threadsafe(enqueue)
                except RuntimeError:
                    pass

    async def add_client(self, client_id: str, mode: str, keys: list[str],
                         loop: asyncio.AbstractEventLoop,
                         queue: asyncio.Queue) -> None:
        async with self._lock:
            feed = await asyncio.to_thread(self._get_or_create_feed, mode)
            connected = await asyncio.to_thread(feed.wait_until_connected, 10.0)
            if not connected:
                raise RuntimeError(f"Upstream {mode} feed did not connect within 10 seconds")
            counts = self._refcounts[mode]
            to_add = [key for key in keys if counts.get(key, 0) == 0]
            if to_add:
                await asyncio.to_thread(feed.subscribe, to_add)
                missing = [key for key in to_add if key not in feed.instrument_keys]
                if missing:
                    raise RuntimeError(f"Upstream subscription failed: {missing}")

            with self._thread_lock:
                self._clients[client_id] = {
                    "mode": mode, "keys": set(keys), "loop": loop, "queue": queue
                }
            for key in keys:
                counts[key] = counts.get(key, 0) + 1

    async def change_client_keys(self, client_id: str, action: str,
                                 keys: list[str]) -> list[str]:
        async with self._lock:
            client = self._clients.get(client_id)
            if client is None:
                raise RuntimeError("WebSocket client is not registered")
            mode = client["mode"]
            feed = self._feeds[mode]
            counts = self._refcounts[mode]
            current = client["keys"]

            if action == "subscribe":
                changed = [key for key in keys if key not in current]
                to_add = [key for key in changed if counts.get(key, 0) == 0]
                if to_add:
                    await asyncio.to_thread(feed.subscribe, to_add)
                    missing = [key for key in to_add if key not in feed.instrument_keys]
                    if missing:
                        raise RuntimeError(f"Upstream subscription failed: {missing}")
                for key in changed:
                    current.add(key)
                    counts[key] = counts.get(key, 0) + 1
                return changed

            changed = [key for key in keys if key in current]
            for key in changed:
                current.remove(key)
                counts[key] = max(0, counts.get(key, 1) - 1)
                if counts[key] == 0:
                    counts.pop(key, None)
                    if key not in self._baseline.get(mode, set()):
                        await asyncio.to_thread(feed.unsubscribe, [key])
            return changed

    async def remove_client(self, client_id: str) -> None:
        async with self._lock:
            with self._thread_lock:
                client = self._clients.pop(client_id, None)
            if client is None:
                return
            mode = client["mode"]
            feed = self._feeds.get(mode)
            counts = self._refcounts.get(mode, {})
            for key in list(client["keys"]):
                counts[key] = max(0, counts.get(key, 1) - 1)
                if counts[key] == 0:
                    counts.pop(key, None)
                    if feed and key not in self._baseline.get(mode, set()):
                        await asyncio.to_thread(feed.unsubscribe, [key])

    def status(self) -> dict:
        return {
            mode: {
                "running": feed.get_health().get("running", False),
                "subscribed_count": len(feed.instrument_keys),
                "instrument_keys": list(feed.instrument_keys),
                "clients": sum(1 for c in self._clients.values() if c["mode"] == mode),
                "health": feed.get_health(),
            }
            for mode, feed in self._feeds.items()
        }

    def shutdown(self) -> None:
        primary = get_feed_service()
        for mode, feed in list(self._feeds.items()):
            listener = self._listeners.get(mode)
            if listener:
                feed.remove_message_listener(listener)
            if feed is not primary:
                feed.disconnect()
        self._feeds.clear()
        self._listeners.clear()
        self._baseline.clear()
        self._refcounts.clear()
        with self._thread_lock:
            self._clients.clear()


feed_manager = WebSocketFeedManager()


@router.get("/api/v1/feed/status")
def market_feed_status():
    """Show mode-specific upstream feeds and currently subscribed instruments."""
    return {"feeds": feed_manager.status()}


@router.websocket("/ws/market")
async def market_feed_websocket(websocket: WebSocket):
    """Stream one or many instruments; accept live subscribe/unsubscribe commands."""
    query = websocket.query_params
    keys = _parse_keys(query.getlist("instrument_key") + query.getlist("instrument_keys"))
    mode = (query.get("mode") or "full").strip().lower()
    if not keys:
        await websocket.close(code=1008, reason="Provide instrument_key or instrument_keys")
        return
    if mode not in VALID_FEED_MODES:
        await websocket.close(code=1008, reason=f"Invalid mode. Valid modes: {', '.join(VALID_FEED_MODES)}")
        return

    await websocket.accept()
    client_id = uuid.uuid4().hex
    queue: asyncio.Queue = asyncio.Queue(maxsize=500)
    sender: asyncio.Task | None = None

    async def send_data() -> None:
        while True:
            await websocket.send_json(await queue.get())

    try:
        await feed_manager.add_client(
            client_id, mode, keys, asyncio.get_running_loop(), queue
        )
        await websocket.send_json({
            "type": "subscribed",
            "mode": mode,
            "instrument_keys": keys,
            "message": "Live market data stream ready",
        })
        sender = asyncio.create_task(send_data())

        while True:
            raw = await websocket.receive_text()
            try:
                command = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_json({"type": "error", "message": "Invalid JSON"})
                continue

            action = str(command.get("action", "")).lower()
            raw_keys = command.get("instrument_keys", [])
            if isinstance(raw_keys, str):
                raw_keys = [raw_keys]
            if not isinstance(raw_keys, list):
                raw_keys = []
            command_keys = _parse_keys([str(key) for key in raw_keys])
            if not command_keys and command.get("instrument_key"):
                command_keys = _parse_keys([str(command["instrument_key"])])

            if action not in ("subscribe", "unsubscribe") or not command_keys:
                await websocket.send_json({
                    "type": "error",
                    "message": "Use action=subscribe|unsubscribe with instrument_key(s)",
                })
                continue
            try:
                changed = await feed_manager.change_client_keys(client_id, action, command_keys)
                await websocket.send_json({
                    "type": "subscribed" if action == "subscribe" else "unsubscribed",
                    "mode": mode,
                    "instrument_keys": changed,
                })
            except Exception as exc:
                await websocket.send_json({"type": "error", "message": str(exc)})

    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.exception("Market-feed WebSocket error")
        try:
            await websocket.send_json({"type": "error", "message": str(exc)})
        except Exception:
            pass
    finally:
        if sender:
            sender.cancel()
        await feed_manager.remove_client(client_id)


def shutdown_websocket_feeds() -> None:
    """Remove WebSocket listeners and stop manager-owned upstream streams."""
    feed_manager.shutdown()
