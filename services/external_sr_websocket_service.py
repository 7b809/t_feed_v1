"""External EMA V2 S/R WebSocket consumer.

The project is a consumer only: pivot detection and S/R calculation live in
EMA V2. This service owns connection lifecycle, snapshots, freshness/readiness,
and a read-compatible levels API for the existing strategy/order services.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from typing import Any
from urllib.parse import quote

import websockets
from websockets.exceptions import ConnectionClosed

logger = logging.getLogger(__name__)


class ExternalSRWebSocketService:
    CONFIGS = ("15/3", "15/15", "15/30")

    def __init__(self) -> None:
        self.base_url = os.getenv("EXTERNAL_SR_WS_URL", "wss://ema.novag7.in").strip().rstrip("/")
        if self.base_url.startswith("https://"):
            self.base_url = "wss://" + self.base_url[8:]
        elif self.base_url.startswith("http://"):
            self.base_url = "ws://" + self.base_url[7:]
        self.enabled = os.getenv("EXTERNAL_SR_WS_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
        self.stale_seconds = max(60, int(os.getenv("EXTERNAL_SR_STALE_SECONDS", "180")))
        self._lock = threading.RLock()
        self._states: dict[tuple[str, str], dict[str, Any]] = {}
        self._wanted: set[tuple[str, str]] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._tasks: dict[tuple[str, str], asyncio.Task] = {}
        self._stop = threading.Event()

    def start(self) -> None:
        if not self.enabled:
            logger.warning("[external-sr] disabled by EXTERNAL_SR_WS_ENABLED")
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._thread_main, name="external-sr-ws", daemon=True)
        self._thread.start()

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        with self._lock:
            wanted = list(self._wanted)
        for key, cfg in wanted:
            loop.create_task(self._ensure_task(key, cfg))
        try:
            loop.run_forever()
        finally:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.close()

    def subscribe(self, instrument_key: str, configs: tuple[str, ...] | list[str] | None = None) -> dict[str, Any]:
        key = str(instrument_key or "").strip()
        if not key:
            return {"status": "error", "reason": "missing_instrument_key"}
        if not self.enabled:
            return {"status": "disabled", "instrument_key": key}
        selected = tuple(configs or ("15/3", "15/15", "15/30"))
        accepted = []
        with self._lock:
            for cfg in selected:
                if cfg not in self.CONFIGS:
                    continue
                token = (key, cfg)
                self._wanted.add(token)
                self._states.setdefault(token, {
                    "instrument_key": key, "config": cfg, "event": None,
                    "resistances": [], "supports": [], "levels": [],
                    "ready": False, "connected": False, "last_event_at": None,
                    "last_ts": None, "current_price": None, "status": "connecting",
                })
                accepted.append(cfg)
        self.start()
        loop = self._loop
        if loop and loop.is_running():
            for cfg in accepted:
                asyncio.run_coroutine_threadsafe(self._ensure_task(key, cfg), loop)
        return {"status": "subscribing", "instrument_key": key, "configs": accepted}

    async def _ensure_task(self, key: str, cfg: str) -> None:
        token = (key, cfg)
        task = self._tasks.get(token)
        if task is None or task.done():
            self._tasks[token] = asyncio.create_task(self._consume(key, cfg), name=f"external-sr-{cfg}-{key}")

    async def _consume(self, key: str, cfg: str) -> None:
        path = "/ws/sr/" + quote(key, safe="")
        url = f"{self.base_url}{path}?config={quote(cfg, safe='/')}"
        backoff = 1.0
        while not self._stop.is_set():
            try:
                logger.info("[external-sr] connecting key=%s config=%s", key, cfg)
                async with websockets.connect(url, ping_interval=20, ping_timeout=20, close_timeout=5) as ws:
                    self._patch((key, cfg), {"connected": True, "status": "connected"})
                    backoff = 1.0
                    async for raw in ws:
                        try:
                            message = json.loads(raw)
                        except (TypeError, json.JSONDecodeError):
                            logger.warning("[external-sr] invalid JSON key=%s cfg=%s", key, cfg)
                            continue
                        if isinstance(message, dict):
                            self._handle_event(key, cfg, message)
            except asyncio.CancelledError:
                raise
            except ConnectionClosed as exc:
                logger.warning("[external-sr] closed key=%s cfg=%s code=%s", key, cfg, exc.code)
                if exc.code in {4400, 4404}:
                    self._patch((key, cfg), {"connected": False, "status": f"rejected:{exc.code}"})
                    return
            except Exception:
                logger.exception("[external-sr] connection failed key=%s cfg=%s", key, cfg)
            self._patch((key, cfg), {"connected": False, "status": "reconnecting"})
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    def _patch(self, token: tuple[str, str], values: dict[str, Any]) -> None:
        with self._lock:
            state = self._states.setdefault(token, {
                "instrument_key": token[0], "config": token[1], "resistances": [],
                "supports": [], "levels": [], "ready": False,
            })
            state.update(values)

    def _handle_event(self, key: str, cfg: str, message: dict[str, Any]) -> None:
        event = str(message.get("event") or "")
        if event not in {"snapshot", "sr_update", "heartbeat"}:
            logger.debug("[external-sr] ignored event=%s key=%s", event, key)
            return
        now = time.time()
        token = (key, cfg)
        callback_levels: list[dict[str, Any]] = []
        with self._lock:
            state = self._states.setdefault(token, {
                "instrument_key": key, "config": cfg, "resistances": [],
                "supports": [], "levels": [], "ready": False,
            })
            if event in {"snapshot", "sr_update"}:
                resistances = [dict(x) for x in (message.get("resistances") or []) if isinstance(x, dict)]
                supports = [dict(x) for x in (message.get("supports") or []) if isinstance(x, dict)]
                levels = resistances + supports
                state.update({
                    "resistances": resistances, "supports": supports, "levels": levels,
                    "ready": True, "event": event,
                    "candle_count": message.get("candle_count"),
                    "cycle_number": message.get("cycle_number"),
                })
                if event == "sr_update":
                    callback_levels = levels
            state.update({
                "connected": True, "status": message.get("status") or "ok",
                "current_price": message.get("current_price", state.get("current_price")),
                "last_ts": message.get("last_ts", state.get("last_ts")),
                "last_event": event, "last_event_at": now,
                "cycle_time": message.get("cycle_time"),
                "nearest_resistance": message.get("nearest_resistance"),
                "nearest_support": message.get("nearest_support"),
                "meta": message.get("meta") or state.get("meta") or {},
            })
        if callback_levels:
            try:
                from services.isolated_sr_tracker_service import isolated_sr_tracker_service
                if isolated_sr_tracker_service.is_tracking(key):
                    for level in callback_levels:
                        isolated_sr_tracker_service.on_sr_level_confirmed(key, level)
            except Exception:
                logger.exception("[external-sr] isolated tracker callback failed for %s", key)

    def get_levels(self, instrument_key: str, config: str = "15/3") -> dict[str, Any] | None:
        token = (str(instrument_key or "").strip(), config)
        with self._lock:
            state = self._states.get(token)
            if not state:
                return None
            result = dict(state)
            result["resistances"] = [dict(x) for x in state.get("resistances") or []]
            result["supports"] = [dict(x) for x in state.get("supports") or []]
            result["levels"] = [dict(x) for x in state.get("levels") or []]
        result["is_ready"] = bool(result.get("ready") and result.get("last_event_at") and time.time() - result["last_event_at"] <= self.stale_seconds)
        result["fresh"] = result["is_ready"]
        return result

    def get_all_configs(self, instrument_key: str) -> dict[str, dict[str, Any] | None]:
        return {cfg: self.get_levels(instrument_key, cfg) for cfg in self.CONFIGS}

    def is_ready(self, instrument_key: str, config: str = "15/3") -> bool:
        state = self.get_levels(instrument_key, config)
        return bool(state and state.get("is_ready"))

    def status(self) -> dict[str, Any]:
        with self._lock:
            rows = []
            for (key, cfg), state in self._states.items():
                age = time.time() - state["last_event_at"] if state.get("last_event_at") else None
                rows.append({
                    "instrument_key": key, "config": cfg, "connected": state.get("connected"),
                    "ready": state.get("ready"), "fresh": age is not None and age <= self.stale_seconds,
                    "age_seconds": round(age, 1) if age is not None else None,
                    "last_event": state.get("last_event"), "status": state.get("status"),
                    "level_count": len(state.get("levels") or []),
                })
        return {"enabled": self.enabled, "base_url": self.base_url, "subscriptions": rows}

    def unsubscribe(self, instrument_key: str, configs: tuple[str, ...] | list[str] | None = None) -> None:
        key = str(instrument_key or "").strip()
        selected = tuple(configs or self.CONFIGS)
        loop = self._loop
        with self._lock:
            tokens = [(key, cfg) for cfg in selected if (key, cfg) in self._wanted]
            for token in tokens:
                self._wanted.discard(token)
        if loop and loop.is_running():
            for token in tokens:
                asyncio.run_coroutine_threadsafe(self._cancel_task(token), loop)

    async def _cancel_task(self, token: tuple[str, str]) -> None:
        task = self._tasks.pop(token, None)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def stop(self) -> None:
        self._stop.set()
        loop = self._loop
        if loop and loop.is_running():
            async def cancel_all():
                tasks = list(self._tasks.values())
                self._tasks.clear()
                for task in tasks:
                    task.cancel()
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
                loop.stop()
            asyncio.run_coroutine_threadsafe(cancel_all(), loop)


external_sr_websocket_service = ExternalSRWebSocketService()
