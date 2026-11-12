# app/services/live_feed_ws_client_service.py
"""Shared local WebSocket consumer for the isolation_v1 market-data gateway."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import Any, Optional
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import websockets
from websockets.exceptions import ConnectionClosed

from ..config import Settings
from .live_feed_state_store import LiveFeedStateStore

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")


def _now_ist() -> datetime:
    return datetime.now(IST)


class LiveFeedWsClientService:
    """Maintain one socket for all active isolation legs plus the NIFTY index.

    The upstream /ws/market endpoint supports a comma-separated initial
    instrument_keys list and dynamic subscribe/unsubscribe messages. This
    service uses one shared session, so NIFTY is never duplicated for every
    isolation and option subscriptions are reference-counted implicitly.
    """

    def __init__(self, config: Settings, state_store: LiveFeedStateStore) -> None:
        self._config = config
        self._state_store = state_store
        self._isolation_legs: dict[str, dict[str, str]] = {}
        self._lock = asyncio.Lock()
        self._sync_event = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self._ws = None
        self._stopped = False
        self._ws_state = "idle"
        self._last_error: Optional[str] = None
        self._last_connected_at: Optional[str] = None
        self._last_frame_at: Optional[str] = None
        self._last_event: Optional[str] = None
        self._message_counts: dict[str, int] = {}
        self._last_message_at: dict[str, str] = {}
        self._reconnect_attempts = 0

    def _window_bounds(self) -> tuple[int, int]:
        start = self._config.live_feed_start_hour * 60 + self._config.live_feed_start_minute
        end = self._config.live_feed_end_hour * 60 + self._config.live_feed_end_minute
        return start, end

    def _inside_window(self, now: datetime | None = None) -> bool:
        local = (now or _now_ist()).astimezone(IST)
        minute = local.hour * 60 + local.minute
        start, end = self._window_bounds()
        return start <= minute < end

    def _seconds_until_boundary(self, now: datetime | None = None) -> float:
        local = (now or _now_ist()).astimezone(IST)
        if self._inside_window(local):
            target = local.replace(
                hour=self._config.live_feed_end_hour,
                minute=self._config.live_feed_end_minute,
                second=0,
                microsecond=0,
            )
        else:
            target = local.replace(
                hour=self._config.live_feed_start_hour,
                minute=self._config.live_feed_start_minute,
                second=0,
                microsecond=0,
            )
            if target <= local:
                target += timedelta(days=1)
        return max(0.1, (target - local).total_seconds())

    def _desired_context(self) -> dict[str, dict[str, set[str]]]:
        context: dict[str, dict[str, set[str]]] = {}

        def add(key: str, isolation_id: str, role: str) -> None:
            item = context.setdefault(key, {"isolation_ids": set(), "roles": set()})
            item["isolation_ids"].add(isolation_id)
            item["roles"].add(role)

        for isolation_id, legs in self._isolation_legs.items():
            for role, key in legs.items():
                if key:
                    add(key, isolation_id, role)
            add(self._config.nifty_instrument_key, isolation_id, "underlying")
        return context

    async def _desired_context_snapshot(self) -> dict[str, dict[str, set[str]]]:
        async with self._lock:
            return self._desired_context()

    async def register_isolation(self, isolation_id: str, legs: dict[str, str]) -> None:
        if not self._config.live_feed_enabled:
            logger.info("Live feed disabled; not registering isolation=%s", isolation_id)
            return
        normalized = {str(role): str(key) for role, key in legs.items() if key}
        async with self._lock:
            previous = self._isolation_legs.get(isolation_id)
            self._isolation_legs[isolation_id] = normalized
        self._sync_event.set()
        logger.info(
            "Live feed isolation registered id=%s legs=%s changed=%s",
            isolation_id, normalized, previous != normalized,
        )
        await self.ensure_running()

    async def unregister_isolation(self, isolation_id: str) -> None:
        async with self._lock:
            removed = self._isolation_legs.pop(isolation_id, None)
        if removed:
            self._sync_event.set()
            logger.info("Live feed isolation unregistered id=%s", isolation_id)

    async def ensure_running(self) -> None:
        if not self._config.live_feed_enabled or self._stopped:
            return
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="shared-live-market-feed")
            logger.info("Shared live-feed supervisor started")

    async def stop(self) -> None:
        self._stopped = True
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._ws = None
        self._ws_state = "idle"

    def status(self) -> dict[str, Any]:
        context = self._desired_context()
        start, end = self._window_bounds()
        return {
            "enabled": self._config.live_feed_enabled,
            "ws_state": self._ws_state,
            "websocket_url": self._config.live_feed_ws_url_effective,
            "mode": self._config.live_feed_mode,
            "session_window": {
                "start": f"{start // 60:02d}:{start % 60:02d}",
                "end_exclusive": f"{end // 60:02d}:{end % 60:02d}",
                "timezone": "Asia/Kolkata",
                "inside_window": self._inside_window(),
            },
            "active_isolations": len(self._isolation_legs),
            "desired_instrument_keys": sorted(context),
            "last_connected_at": self._last_connected_at,
            "last_frame_at": self._last_frame_at,
            "last_event": self._last_event,
            "message_counts": dict(self._message_counts),
            "last_message_at": dict(self._last_message_at),
            "reconnect_attempts": self._reconnect_attempts,
            "last_error": self._last_error,
        }

    def _build_url(self, keys: list[str]) -> str:
        query = urlencode(
            {"instrument_keys": ",".join(keys), "mode": self._config.live_feed_mode},
            safe="|,",
        )
        return f"{self._config.live_feed_ws_url_effective}/ws/market?{query}"

    async def _wait_for_signal(self, timeout: float | None = None) -> None:
        self._sync_event.clear()
        if timeout is None:
            await self._sync_event.wait()
            return
        try:
            await asyncio.wait_for(self._sync_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass

    async def _run(self) -> None:
        backoff = 1
        try:
            while not self._stopped:
                desired = await self._desired_context_snapshot()
                if not desired:
                    self._ws_state = "idle"
                    self._sync_event.clear()
                    # Recheck after clearing: a registration racing with this
                    # transition cannot be lost and leave the supervisor asleep.
                    if not await self._desired_context_snapshot():
                        await self._sync_event.wait()
                    continue
                if not self._inside_window():
                    self._ws_state = "outside_window"
                    delay = self._seconds_until_boundary()
                    logger.info(
                        "Live feed paused outside allowed IST window; "
                        "isolations=%d next_window_in=%.0fs",
                        len(self._isolation_legs), delay,
                    )
                    await self._wait_for_signal(delay)
                    continue
                try:
                    await self._connect_and_consume(sorted(desired))
                    backoff = 1
                    self._reconnect_attempts = 0
                except asyncio.CancelledError:
                    raise
                except ConnectionClosed as exc:
                    self._last_error = f"closed code={exc.code} reason={exc.reason}"
                    logger.warning("Live-feed socket closed: %s", self._last_error)
                except Exception as exc:
                    self._last_error = f"{type(exc).__name__}: {exc}"
                    logger.exception("Live-feed socket failed")
                finally:
                    self._ws = None
                if self._stopped:
                    break
                if not self._inside_window():
                    continue
                self._ws_state = "reconnecting"
                self._reconnect_attempts += 1
                await self._wait_for_signal(backoff)
                backoff = min(
                    backoff * 2,
                    max(1, self._config.live_feed_reconnect_max_backoff_seconds),
                )
        except asyncio.CancelledError:
            logger.info("Shared live-feed supervisor stopped")
            raise
        finally:
            self._ws = None
            if not self._stopped:
                self._ws_state = "idle"

    async def _connect_and_consume(self, initial_keys: list[str]) -> None:
        url = self._build_url(initial_keys)
        logger.info(
            "Connecting local live feed URL=%s mode=%s instruments=%s",
            url, self._config.live_feed_mode, initial_keys,
        )
        self._ws_state = "connecting"
        async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
            self._ws = ws
            self._ws_state = "connected"
            self._last_connected_at = _now_ist().isoformat()
            self._last_error = None
            active_keys = set(initial_keys)
            logger.info("Local live feed connected instruments=%d", len(active_keys))

            receiver = asyncio.create_task(self._receive_loop(ws))
            synchronizer = asyncio.create_task(self._synchronize_subscriptions(ws, active_keys))
            boundary = asyncio.create_task(asyncio.sleep(self._seconds_until_boundary()))
            try:
                done, pending = await asyncio.wait(
                    {receiver, synchronizer, boundary},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                if boundary in done:
                    self._ws_state = "outside_window"
                    logger.info("Live-feed session window ended; closing local socket")
                    await ws.close(code=1000, reason="live-feed window ended")
                else:
                    for task in done:
                        if task is not boundary:
                            await task
            finally:
                for task in (receiver, synchronizer, boundary):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(receiver, synchronizer, boundary, return_exceptions=True)
                self._ws = None

    async def _synchronize_subscriptions(self, ws, active_keys: set[str]) -> None:
        while True:
            # Clear before reading desired subscriptions. A concurrent
            # registration after clear leaves the event set for the next wait.
            self._sync_event.clear()
            desired_context = await self._desired_context_snapshot()
            desired_keys = set(desired_context)
            additions = sorted(desired_keys - active_keys)
            removals = sorted(active_keys - desired_keys)
            if additions:
                await ws.send(json.dumps({
                    "action": "subscribe", "instrument_keys": additions
                }))
                active_keys.update(additions)
                logger.info("Live feed dynamic subscribe instruments=%s", additions)
            if removals:
                await ws.send(json.dumps({
                    "action": "unsubscribe", "instrument_keys": removals
                }))
                active_keys.difference_update(removals)
                logger.info("Live feed dynamic unsubscribe instruments=%s", removals)
            if not desired_keys:
                return
            await self._sync_event.wait()

    async def _receive_loop(self, ws) -> None:
        async for raw in ws:
            self._last_frame_at = _now_ist().isoformat()
            try:
                payload = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                logger.debug("Ignoring non-JSON frame from local market feed")
                continue
            if not isinstance(payload, dict):
                continue
            event = payload.get("type")
            self._last_event = event
            if event == "subscribed":
                logger.info(
                    "Local market feed subscription confirmed mode=%s instruments=%s",
                    payload.get("mode"), payload.get("instrument_keys"),
                )
                continue
            if event == "error":
                self._last_error = str(payload.get("message") or "market-feed server error")
                logger.warning("Local market feed returned error: %s", self._last_error)
                continue
            if event != "market_data":
                logger.debug("Unknown local market-feed event=%s", event)
                continue
            key, data = payload.get("instrument_key"), payload.get("data")
            if not isinstance(key, str) or not isinstance(data, dict):
                continue
            context = await self._desired_context_snapshot()
            association = context.get(key)
            if association is None:
                continue
            await asyncio.to_thread(
                self._state_store.save_message,
                key,
                data,
                str(payload.get("mode") or self._config.live_feed_mode),
                sorted(association["isolation_ids"]),
                sorted(association["roles"]),
            )
            self._message_counts[key] = self._message_counts.get(key, 0) + 1
            self._last_message_at[key] = _now_ist().isoformat()
