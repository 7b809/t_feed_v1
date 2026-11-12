# app/services/supertrend_ws_client_service.py
"""Downstream WebSocket client for the upstream Supertrend indicator feed."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Any, Optional
from urllib.parse import quote
from zoneinfo import ZoneInfo

import websockets
from websockets.exceptions import ConnectionClosed

from ..config import Settings
from .supertrend_state_store import SupertrendStateStore

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")
PERMANENT_CLOSE_CODES = {4400, 4404}


def _now_ist_iso() -> str:
    return datetime.now(IST).isoformat()


def _close_code(exc: ConnectionClosed) -> Optional[int]:
    received = getattr(exc, "rcvd", None)
    return getattr(received, "code", None) if received is not None else getattr(exc, "code", None)


def _close_reason(exc: ConnectionClosed) -> str:
    received = getattr(exc, "rcvd", None)
    return (getattr(received, "reason", "") or "") if received is not None else (getattr(exc, "reason", "") or "")


class SupertrendWsClientService:
    """One Supertrend WebSocket connection per (instrument_key, leg_role)."""

    def __init__(self, config: Settings, state_store: SupertrendStateStore,
                 isolation_id: str, instrument_key: str, leg_role: str) -> None:
        self._config, self._state_store = config, state_store
        self._isolation_id, self._instrument_key, self._leg_role = isolation_id, instrument_key, leg_role
        self._task: Optional[asyncio.Task] = None
        self._stopped = False
        self._ws = None
        self._ws_state = "idle"
        self._last_frame_at: Optional[str] = None
        self._last_frame_event: Optional[str] = None
        self._reconnect_attempts = 0
        self._last_error: Optional[str] = None
        self._heartbeat_counter = 0

    @property
    def instrument_key(self) -> str:
        return self._instrument_key

    @property
    def leg_role(self) -> str:
        return self._leg_role

    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def status(self) -> dict[str, Any]:
        return {
            "instrument_key": self._instrument_key, "leg_role": self._leg_role,
            "isolation_id": self._isolation_id, "ws_state": self._ws_state,
            "last_frame_at": self._last_frame_at, "last_frame_event": self._last_frame_event,
            "reconnect_attempts": self._reconnect_attempts, "last_error": self._last_error,
        }

    async def start(self) -> None:
        if self.is_running():
            return
        self._stopped = False
        self._task = asyncio.create_task(self._run(), name=f"supertrend_ws:{self._instrument_key}")

    async def stop(self) -> None:
        self._stopped = True
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._ws_state, self._ws = "idle", None

    def _build_url(self) -> str:
        base = self._config.supertrend_ws_base_url_effective
        return f"{base}/ws/supertrend/{quote(self._instrument_key, safe='')}"

    async def _run(self) -> None:
        backoff = 1
        maximum = self._config.sr_ws_reconnect_max_backoff_seconds
        while not self._stopped:
            try:
                await self._connect_and_consume()
                backoff = 1
                self._reconnect_attempts = 0
            except asyncio.CancelledError:
                raise
            except ConnectionClosed as exc:
                code, reason = _close_code(exc), _close_reason(exc)
                if code in PERMANENT_CLOSE_CODES:
                    self._ws_state = "failed"
                    self._last_error = f"permanent close code={code} reason={reason}"
                    logger.error("Supertrend permanent close key=%s code=%s reason=%s",
                                 self._instrument_key, code, reason)
                    return
                self._reconnect_attempts += 1
                self._last_error = f"ConnectionClosed code={code} reason={reason}"
                logger.warning("Supertrend WS closed key=%s retry_in=%ss attempt=%s",
                               self._instrument_key, backoff, self._reconnect_attempts)
            except Exception as exc:
                self._reconnect_attempts += 1
                self._last_error = f"{type(exc).__name__}: {exc}"
                logger.exception("Supertrend WS error key=%s: %s", self._instrument_key, exc)
            if self._stopped:
                return
            self._ws_state = "reconnecting"
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, maximum)

    async def _connect_and_consume(self) -> None:
        url = self._build_url()
        logger.info("Supertrend WS connect leg=%s key=%s url=%s",
                    self._leg_role, self._instrument_key, url)
        async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
            self._ws, self._ws_state, self._last_error = ws, "connected", None
            keepalive = asyncio.create_task(self._keepalive(ws))
            try:
                async for raw in ws:
                    await self._handle_frame(raw)
            finally:
                keepalive.cancel()
                try:
                    await keepalive
                except (asyncio.CancelledError, Exception):
                    pass
                self._ws = None

    async def _keepalive(self, ws) -> None:
        try:
            while True:
                await asyncio.sleep(self._config.sr_ws_keepalive_seconds)
                try:
                    await ws.send("ping")
                except Exception:
                    return
        except asyncio.CancelledError:
            return

    async def _handle_frame(self, raw: Any) -> None:
        self._last_frame_at = _now_ist_iso()
        try:
            if isinstance(raw, (bytes, bytearray)):
                raw = raw.decode("utf-8")
            payload = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            logger.debug("Non-JSON Supertrend frame key=%s", self._instrument_key)
            return
        if not isinstance(payload, dict):
            return
        event = payload.get("event")
        self._last_frame_event = event
        if self._config.require_today_only_scope and payload.get("data_scope") != "today_only":
            logger.error("Rejecting Supertrend frame key=%s event=%s scope=%s",
                         self._instrument_key, event, payload.get("data_scope"))
            return

        if event == "connection_ack":
            logger.info("Supertrend connection_ack leg=%s key=%s scope=%s date=%s",
                        self._leg_role, self._instrument_key,
                        payload.get("data_scope"), payload.get("today_ist_date"))
        elif event == "snapshot":
            await asyncio.to_thread(self._state_store.save_snapshot, payload,
                                    self._instrument_key, self._leg_role, self._isolation_id)
            logger.info("Supertrend snapshot received leg=%s key=%s signals=%s trend=%s",
                        self._leg_role, self._instrument_key,
                        payload.get("signal_count"), payload.get("trend"))
        elif event == "supertrend_signal":
            await asyncio.to_thread(self._state_store.save_signal, payload,
                                    self._instrument_key, self._leg_role, self._isolation_id)
            logger.info("Supertrend confirmed signal leg=%s key=%s signal=%s time=%s price=%s",
                        self._leg_role, self._instrument_key, payload.get("signal"),
                        payload.get("confirmed_time"), payload.get("price"))
        elif event == "heartbeat":
            await asyncio.to_thread(self._state_store.update_heartbeat, payload,
                                    self._instrument_key, self._leg_role, self._isolation_id)
            self._heartbeat_counter += 1
            if self._heartbeat_counter % 100 == 0:
                logger.info("Supertrend heartbeat #%s leg=%s key=%s trend=%s",
                            self._heartbeat_counter, self._leg_role,
                            self._instrument_key, payload.get("trend"))
            else:
                logger.debug("Supertrend heartbeat leg=%s key=%s", self._leg_role, self._instrument_key)
        else:
            logger.debug("Unknown Supertrend event=%s key=%s", event, self._instrument_key)
