# app/services/sr_ws_client_service.py
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Any, Dict, Optional
from urllib.parse import quote
from zoneinfo import ZoneInfo

import websockets
from websockets.exceptions import ConnectionClosed

from ..config import Settings
from .sr_state_store import SrStateStore

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")

PERMANENT_CLOSE_CODES = {4400, 4404}


def _now_ist_iso() -> str:
    return datetime.now(IST).isoformat()


def _get_close_code(exc: ConnectionClosed) -> Optional[int]:
    rcvd = getattr(exc, "rcvd", None)
    if rcvd is not None:
        code = getattr(rcvd, "code", None)
        if code is not None:
            return code
    return getattr(exc, "code", None)


def _get_close_reason(exc: ConnectionClosed) -> str:
    rcvd = getattr(exc, "rcvd", None)
    if rcvd is not None:
        reason = getattr(rcvd, "reason", None)
        if reason:
            return reason
    return getattr(exc, "reason", "") or ""


class SrWsClientService:
    """One WebSocket connection per (instrument_key, leg_role)."""

    def __init__(
        self,
        config: Settings,
        state_store: SrStateStore,
        isolation_id: str,
        instrument_key: str,
        leg_role: str,
    ) -> None:
        self._config = config
        self._state_store = state_store
        self._isolation_id = isolation_id
        self._instrument_key = instrument_key
        self._leg_role = leg_role

        self._task: Optional[asyncio.Task] = None
        self._ws = None
        self._stopped = False

        self._ws_state: str = "idle"
        self._last_frame_at: Optional[str] = None
        self._last_frame_event: Optional[str] = None
        self._reconnect_attempts: int = 0
        self._last_error: Optional[str] = None
        self._configs: list[str] = []
        self._heartbeat_counter: int = 0

    # ---------------------------------------------------------------- API
    @property
    def instrument_key(self) -> str:
        return self._instrument_key

    @property
    def leg_role(self) -> str:
        return self._leg_role

    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self.is_running():
            return
        self._stopped = False
        self._task = asyncio.create_task(
            self._run(), name=f"sr_ws:{self._instrument_key}"
        )

    async def stop(self) -> None:
        self._stopped = True
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._ws_state = "idle"
        self._ws = None

    def status(self) -> Dict[str, Any]:
        return {
            "instrument_key": self._instrument_key,
            "leg_role": self._leg_role,
            "isolation_id": self._isolation_id,
            "ws_state": self._ws_state,
            "last_frame_at": self._last_frame_at,
            "last_frame_event": self._last_frame_event,
            "configs": list(self._configs),
            "reconnect_attempts": self._reconnect_attempts,
            "last_error": self._last_error,
        }

    # -------------------------------------------------------- verbose logs
    def _log_frame(self, msg: str, *args: Any) -> None:
        """Log a per-frame event at INFO when verbose, DEBUG otherwise.

        ``stacklevel=2`` makes ``%(filename)s:%(lineno)d`` point at the
        caller inside ``_handle_raw_frame`` — not at this helper.
        """
        level = logging.INFO if self._config.verbose else logging.DEBUG
        logger.log(level, msg, *args, stacklevel=2)

    # ------------------------------------------------------------ internals
    def _build_url(self) -> str:
        encoded = quote(self._instrument_key, safe="")
        base = self._config.sr_ws_base_url_effective
        cfg = self._config.sr_ws_default_config
        return f"{base}/ws/sr/{encoded}?config={cfg}"

    async def _run(self) -> None:
        backoff = 1
        max_backoff = self._config.sr_ws_reconnect_max_backoff_seconds

        while not self._stopped:
            try:
                await self._connect_and_consume()
                backoff = 1
                self._reconnect_attempts = 0
            except asyncio.CancelledError:
                raise
            except ConnectionClosed as exc:
                code = _get_close_code(exc)
                reason = _get_close_reason(exc)
                if code in PERMANENT_CLOSE_CODES:
                    self._ws_state = "failed"
                    self._last_error = f"permanent close code={code} reason={reason}"
                    logger.error(
                        "permanent close for %s code=%s reason=%s",
                        self._instrument_key,
                        code,
                        reason,
                    )
                    return
                self._reconnect_attempts += 1
                self._last_error = f"ConnectionClosed code={code} reason={reason}"
                logger.warning(
                    "ws closed for %s code=%s retry_in=%ss attempt=%s",
                    self._instrument_key,
                    code,
                    backoff,
                    self._reconnect_attempts,
                )
            except Exception as exc:
                self._reconnect_attempts += 1
                self._last_error = f"{type(exc).__name__}: {exc}"
                logger.exception(
                    "ws error for %s: %s", self._instrument_key, exc
                )

            if self._stopped:
                return
            self._ws_state = "reconnecting"
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)

    async def _connect_and_consume(self) -> None:
        url = self._build_url()
        logger.info(
            "ws connect leg=%s key=%s url=%s",
            self._leg_role,
            self._instrument_key,
            url,
        )
        async with websockets.connect(
            url, ping_interval=20, ping_timeout=20
        ) as ws:
            self._ws = ws
            self._ws_state = "connected"
            self._last_error = None
            keepalive = asyncio.create_task(self._keepalive(ws))
            try:
                async for raw in ws:
                    await self._handle_raw_frame(raw)
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

    async def _handle_raw_frame(self, raw) -> None:
        self._last_frame_at = _now_ist_iso()
        try:
            if isinstance(raw, (bytes, bytearray)):
                raw = raw.decode("utf-8")
            payload = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            logger.debug("non-JSON frame for %s", self._instrument_key)
            return
        if not isinstance(payload, dict):
            logger.debug("non-dict JSON frame for %s", self._instrument_key)
            return

        event = payload.get("event")
        self._last_frame_event = event

        if self._config.require_today_only_scope:
            scope = payload.get("data_scope")
            if scope != "today_only":
                logger.error(
                    "frame rejected: data_scope=%s for %s event=%s",
                    scope,
                    self._instrument_key,
                    event,
                )
                return

        if event == "connection_ack":
            self._configs = list(payload.get("configs") or [])
            self._log_frame(
                "connection_ack leg=%s key=%s configs=%s scope=%s",
                self._leg_role,
                self._instrument_key,
                self._configs,
                payload.get("data_scope"),
            )
        elif event == "snapshot":
            await asyncio.to_thread(
                self._state_store.upsert_sr_state,
                payload,
                self._instrument_key,
                self._leg_role,
                self._isolation_id,
                "snapshot",
            )
            self._log_frame(
                "snapshot leg=%s key=%s config=%s levels=%s",
                self._leg_role,
                self._instrument_key,
                payload.get("config"),
                payload.get("sr_level_count"),
            )
        elif event == "sr_update":
            await asyncio.to_thread(
                self._state_store.upsert_sr_state,
                payload,
                self._instrument_key,
                self._leg_role,
                self._isolation_id,
                "sr_update",
            )
            self._log_frame(
                "sr_update leg=%s key=%s config=%s levels=%s",
                self._leg_role,
                self._instrument_key,
                payload.get("config"),
                payload.get("sr_level_count"),
            )
        elif event == "heartbeat":
            await asyncio.to_thread(
                self._state_store.update_heartbeat,
                payload,
                self._instrument_key,
                self._leg_role,
            )
            self._heartbeat_counter += 1
            if self._heartbeat_counter % 100 == 0:
                logger.info(
                    "heartbeat #%s leg=%s key=%s config=%s",
                    self._heartbeat_counter,
                    self._leg_role,
                    self._instrument_key,
                    payload.get("config"),
                )
            else:
                logger.debug(
                    "heartbeat leg=%s key=%s config=%s",
                    self._leg_role,
                    self._instrument_key,
                    payload.get("config"),
                )
        else:
            logger.debug(
                "unknown event %s for %s", event, self._instrument_key
            )