import asyncio
import json
import logging
from datetime import datetime
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed

from .config import (
    BASE_DIR,
    FILES_DIR,
    IST,
    RECONNECT_DELAY_SECONDS,
    MAX_DASHBOARD_MESSAGES,
    SUBSCRIPTIONS,
    sanitize_instrument_key,
)
from .subscriptions import fetch_subscriptions, find_matches

logger = logging.getLogger(__name__)


class SocketManager:
    def __init__(self):
        self.tasks: dict[str, asyncio.Task] = {}
        self.connections: dict[str, Any] = {}
        self.states: dict[str, dict] = {}          # worker_key -> live state
        self.worker_configs: dict[str, dict] = {}  # worker_key -> static config
        self.recent_messages: list[dict] = []
        self.clients: set[asyncio.Queue] = set()

        self.snapshots: dict[str, dict] = {}       # feed -> last raw API response
        self.last_refresh_at: str | None = None
        self.last_refresh_summary: dict | None = None

        FILES_DIR.mkdir(parents=True, exist_ok=True)
        for cfg in SUBSCRIPTIONS.values():
            cfg["dir"].mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ #
    # Worker state helpers
    # ------------------------------------------------------------------ #
    def _make_state(self, worker_cfg: dict) -> dict:
        return {
            "key": worker_cfg["key"],
            "feed": worker_cfg["feed"],
            "instrument_key": worker_cfg["instrument_key"],
            "label": worker_cfg["label"],
            "url": worker_cfg["url"],
            "file": str(worker_cfg["file"].relative_to(BASE_DIR)),
            "status": "idle",
            "connected_at": None,
            "last_message_at": None,
            "messages_received": 0,
            "last_message": None,   # parsed JSON (or raw text if unparseable)
            "last_raw": None,       # exact websocket text, verbatim
            "last_error": None,
        }

    # ------------------------------------------------------------------ #
    # Snapshot
    # ------------------------------------------------------------------ #
    def snapshot(self) -> dict:
        connected = sum(
            1 for s in self.states.values() if s["status"] == "connected"
        )
        return {
            "time": datetime.now(IST).isoformat(),
            "session": "running" if connected else "not_connected",
            "worker_count": len(self.states),
            "connected_count": connected,
            "sockets": self.states,
            "recent_messages": self.recent_messages[-MAX_DASHBOARD_MESSAGES:],
            "last_refresh_at": self.last_refresh_at,
            "last_refresh_summary": self.last_refresh_summary,
        }

    # ------------------------------------------------------------------ #
    # Pub/sub
    # ------------------------------------------------------------------ #
    async def broadcast(self, event: dict) -> None:
        dead = []
        for queue in list(self.clients):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                dead.append(queue)
        for queue in dead:
            self.clients.discard(queue)

    async def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self.clients.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self.clients.discard(queue)

    # ------------------------------------------------------------------ #
    # Subscription refresh / worker reconciliation
    # ------------------------------------------------------------------ #
    async def refresh_subscriptions(self, auto_start: bool = False) -> dict:
        """
        Fetch subscriptions for every feed, match against wanted specs,
        and reconcile the live worker set.

        `auto_start=True`  — new workers connect immediately (used inside
                             the market session).
        `auto_start=False` — new workers are registered in "idle" state.
        """
        summary: dict = {
            "started_at": datetime.now(IST).isoformat(),
            "feeds": {},
            "added": [],
            "removed": [],
            "updated": [],
            "errors": {},
        }

        for feed, cfg in SUBSCRIPTIONS.items():
            try:
                data = await fetch_subscriptions(cfg["api_url"])
            except Exception as exc:
                logger.error("Subscription fetch failed for %s: %s", feed, exc)
                summary["errors"][feed] = f"{type(exc).__name__}: {exc}"
                continue

            self.snapshots[feed] = data
            items = data.get("active") or []
            matched = find_matches(items, cfg.get("wanted") or [])

            # Build the desired worker set for this feed.
            desired: dict[str, dict] = {}
            for item in matched:
                ikey = item.get("instrument_key")
                connect_url = item.get("connect_url")
                if not ikey or not connect_url:
                    continue
                slug = sanitize_instrument_key(ikey)
                wkey = f"{feed}__{slug}"
                desired[wkey] = {
                    "key": wkey,
                    "feed": feed,
                    "instrument_key": ikey,
                    "label": (
                        f"{cfg['label']} · "
                        f"{item.get('trading_symbol') or ikey}"
                    ),
                    "url": connect_url,
                    "file": cfg["dir"] / f"{slug}.jsonl",
                    "meta": item,
                }

            existing = {
                k for k, c in self.worker_configs.items() if c["feed"] == feed
            }
            added = set(desired) - existing
            removed = existing - set(desired)
            common = existing & set(desired)

            # ---- Added ----
            for k in added:
                self.worker_configs[k] = desired[k]
                self.states[k] = self._make_state(desired[k])
                desired[k]["file"].parent.mkdir(parents=True, exist_ok=True)
                await self.broadcast(
                    {"type": "state", "data": self.states[k]}
                )
                if auto_start:
                    await self.start(k)
                summary["added"].append(k)
                logger.info("Worker added: %s", k)

            # ---- Removed ----
            for k in removed:
                await self.stop(k)
                self.worker_configs.pop(k, None)
                self.states.pop(k, None)
                await self.broadcast(
                    {"type": "state_removed", "data": {"key": k}}
                )
                summary["removed"].append(k)
                logger.info("Worker removed: %s", k)

            # ---- Common: refresh url / label / meta ----
            for k in common:
                new_cfg = desired[k]
                old_cfg = self.worker_configs[k]
                url_changed = old_cfg["url"] != new_cfg["url"]

                old_cfg["url"] = new_cfg["url"]
                old_cfg["label"] = new_cfg["label"]
                old_cfg["meta"] = new_cfg["meta"]
                self.states[k]["url"] = new_cfg["url"]
                self.states[k]["label"] = new_cfg["label"]

                if url_changed:
                    # Reconnect with the new URL if the worker was live.
                    task = self.tasks.get(k)
                    if task and not task.done():
                        await self.stop(k)
                        await self.start(k)
                    summary["updated"].append(k)
                    logger.info("Worker URL changed, reconnecting: %s", k)
                else:
                    await self.broadcast(
                        {"type": "state", "data": self.states[k]}
                    )

            summary["feeds"][feed] = {
                "fetched": len(items),
                "matched": len(desired),
                "wanted": len(cfg.get("wanted") or []),
            }

        summary["finished_at"] = datetime.now(IST).isoformat()
        self.last_refresh_at = summary["finished_at"]
        self.last_refresh_summary = summary
        await self.broadcast({"type": "refresh", "data": summary})
        return summary

    # ------------------------------------------------------------------ #
    # Control
    # ------------------------------------------------------------------ #
    async def start_all(self) -> None:
        for key in list(self.worker_configs):
            await self.start(key)

    async def stop_all(self) -> None:
        for key in list(self.worker_configs):
            await self.stop(key)

    async def start_feed(self, feed: str) -> None:
        for key, cfg in self.worker_configs.items():
            if cfg["feed"] == feed:
                await self.start(key)

    async def stop_feed(self, feed: str) -> None:
        for key, cfg in self.worker_configs.items():
            if cfg["feed"] == feed:
                await self.stop(key)

    async def start(self, key: str) -> None:
        if key not in self.worker_configs:
            raise KeyError(key)
        task = self.tasks.get(key)
        if task and not task.done():
            return
        self.states[key]["status"] = "connecting"
        self.states[key]["last_error"] = None
        self.tasks[key] = asyncio.create_task(
            self._connection_loop(key), name=f"socket-{key}"
        )
        await self.broadcast({"type": "state", "data": self.states[key]})

    async def stop(self, key: str) -> None:
        if key not in self.worker_configs:
            return
        task = self.tasks.get(key)
        ws = self.connections.get(key)
        if ws:
            try:
                await ws.close()
            except Exception:
                pass
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self.tasks.pop(key, None)
        self.connections.pop(key, None)
        if key in self.states:
            self.states[key]["status"] = "stopped"
            await self.broadcast({"type": "state", "data": self.states[key]})

    # ------------------------------------------------------------------ #
    # Connection loop
    # ------------------------------------------------------------------ #
    async def _connection_loop(self, key: str) -> None:
        while True:
            cfg = self.worker_configs.get(key)
            if cfg is None:
                return  # worker was removed while sleeping

            try:
                self.states[key]["status"] = "connecting"
                self.states[key]["url"] = cfg["url"]
                await self.broadcast(
                    {"type": "state", "data": self.states[key]}
                )

                async with websockets.connect(
                    cfg["url"],
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=5,
                    open_timeout=15,
                    max_size=8 * 1024 * 1024,
                ) as ws:
                    self.connections[key] = ws
                    self.states[key]["status"] = "connected"
                    self.states[key]["connected_at"] = (
                        datetime.now(IST).isoformat()
                    )
                    self.states[key]["last_error"] = None
                    await self.broadcast(
                        {"type": "state", "data": self.states[key]}
                    )

                    async for raw in ws:
                        received_at = datetime.now(IST).isoformat()

                        # Preserve the exact websocket text verbatim.
                        raw_text = raw if isinstance(raw, str) else str(raw)

                        # Best-effort JSON parse for downstream consumers.
                        try:
                            parsed = json.loads(raw_text)
                        except (json.JSONDecodeError, TypeError):
                            parsed = None

                        record = {
                            "socket": key,
                            "feed": cfg["feed"],
                            "instrument_key": cfg["instrument_key"],
                            "label": cfg["label"],
                            "received_at": received_at,
                            "raw": raw_text,
                            "payload": parsed,
                        }
                        self._append_record(cfg["file"], record)

                        self.states[key]["messages_received"] += 1
                        self.states[key]["last_message_at"] = received_at
                        self.states[key]["last_message"] = parsed
                        self.states[key]["last_raw"] = raw_text

                        self.recent_messages.append(record)
                        if len(self.recent_messages) > MAX_DASHBOARD_MESSAGES:
                            self.recent_messages = self.recent_messages[
                                -MAX_DASHBOARD_MESSAGES:
                            ]

                        await self.broadcast(
                            {"type": "message", "data": record}
                        )
                        await self.broadcast(
                            {"type": "state", "data": self.states[key]}
                        )

            except asyncio.CancelledError:
                raise
            except (ConnectionClosed, OSError, TimeoutError) as exc:
                if key in self.states:
                    self.states[key]["last_error"] = str(exc)
                logger.warning("%s websocket disconnected/error: %s", key, exc)
            except Exception as exc:
                if key in self.states:
                    self.states[key]["last_error"] = (
                        f"{type(exc).__name__}: {exc}"
                    )
                logger.exception("%s websocket error", key)
            finally:
                self.connections.pop(key, None)
                if key in self.states and self.states[key]["status"] != "stopped":
                    self.states[key]["status"] = "disconnected"
                    await self.broadcast(
                        {"type": "state", "data": self.states[key]}
                    )

            await asyncio.sleep(RECONNECT_DELAY_SECONDS)

    # ------------------------------------------------------------------ #
    # Disk append
    # ------------------------------------------------------------------ #
    @staticmethod
    def _append_record(path, record) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(
                json.dumps(record, ensure_ascii=False, default=str) + "\n"
            )