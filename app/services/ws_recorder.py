"""Self-connect WebSocket recorder for offline replay / analysis.

When ``settings.ws_recorder_enabled`` is True, this service acts as a
WebSocket **client** of our own server (the same process — uvicorn can
serve and originate WS connections concurrently on one event loop).

It records two streams:

1. The Nifty 50 index (``settings.ws_recorder_nifty_key``), started
   immediately.
2. A randomly chosen instrument whose ``strike_price`` is **nearest to
   the Nifty 50 LTP** captured from the first Nifty tick.

Every frame received is appended as one JSON line to::

    <ws_recorder_dir>/<safe_instrument_key>/<YYYYMMDD_HHMMSS>.jsonl

Each line has the shape::

    {"recorded_at": "<iso>", "instrument_key": "...", "role": "...",
     "message": "<raw frame text>"}

A fresh file is created for every session and every reconnect. Sessions
never overwrite each other.

This service is a best-effort observer: it never blocks startup, never
raises out of its public methods, and reconnects with a fixed backoff.
"""

from __future__ import annotations

import asyncio
import json
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

import websockets
from websockets.exceptions import ConnectionClosed

from app.core.config import settings
from app.core.logger import get_logger

logger = get_logger(__name__)


def _safe_filename(instrument_key: str) -> str:
    """Turn ``NSE_INDEX|Nifty 50`` into ``NSE_INDEX_Nifty_50``."""
    return (
        str(instrument_key)
        .replace("|", "_")
        .replace(" ", "_")
        .replace("/", "_")
        .replace("\\", "_")
    )


class WsRecorderService:
    """Records our own live-EMA WebSocket streams to disk."""

    def __init__(self) -> None:
        self.running = False
        self.started_at: str | None = None
        self.stopped_at: str | None = None

        self._tasks: dict[str, asyncio.Task] = {}
        self._stop_event: asyncio.Event | None = None

        self._nifty_key: str = ""
        self._nifty_ltp: float | None = None
        self._nearest_key: str | None = None

        self._output_dir: Path | None = None
        self._instruments_by_key: dict[str, dict[str, Any]] = {}

        self._records: dict[str, dict[str, Any]] = {}

        self.total_messages = 0
        self.total_bytes = 0
        self.last_error: str | None = None
        self.last_error_at: str | None = None

    # ------------------------------------------------------------------ #
    # Lifecycle                                                          #
    # ------------------------------------------------------------------ #
    async def start(
        self,
        instruments_provider: Callable[[], list[dict[str, Any]]],
    ) -> dict[str, Any]:
        if self.running:
            return {"started": False, "reason": "already-running"}

        # Prepare output dir.
        output_dir = settings.ws_recorder_dir
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            logger.exception(
                "WS recorder: unable to create output dir %s", output_dir
            )
            return {"started": False, "reason": "output-dir-failed"}
        self._output_dir = output_dir

        # Snapshot instruments for the "nearest strike" lookup.
        try:
            instruments = [
                i for i in instruments_provider() if isinstance(i, dict)
            ]
        except Exception:
            instruments = []
        self._instruments_by_key = {
            str(i.get("instrument_key")): i
            for i in instruments
            if isinstance(i.get("instrument_key"), str)
        }

        # Reset state.
        self._stop_event = asyncio.Event()
        self._nifty_ltp = None
        self._nearest_key = None
        self._records.clear()
        self.total_messages = 0
        self.total_bytes = 0

        nifty_key = settings.ws_recorder_nifty_key
        self._nifty_key = nifty_key

        self._records[nifty_key] = {
            "instrument_key": nifty_key,
            "role": "nifty-index",
            "file": None,
            "messages": 0,
            "bytes": 0,
            "status": "pending",
            "started_at": None,
            "last_message_at": None,
            "error": None,
        }

        # Spawn the Nifty consumer. Do NOT await the actual connection
        # here — the WS server side of uvicorn is not accepting yet
        # (we are still inside lifespan startup), so awaiting would
        # deadlock. The consumer runs as an independent task.
        self._tasks[nifty_key] = asyncio.create_task(
            self._consume(nifty_key, role="nifty-index"),
            name=f"ws-recorder:{_safe_filename(nifty_key)}",
        )

        self.running = True
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.stopped_at = None

        logger.info(
            "WS recorder started; nifty=%s output_dir=%s base_url=%s",
            nifty_key,
            output_dir,
            self._base_url(),
        )
        return {
            "started": True,
            "nifty_key": nifty_key,
            "output_dir": str(output_dir),
        }

    async def stop(self) -> dict[str, Any]:
        if not self.running:
            return {"stopped": False, "reason": "not-running"}

        if self._stop_event is not None:
            self._stop_event.set()

        tasks = list(self._tasks.values())
        self._tasks.clear()

        for task in tasks:
            task.cancel()

        for task in tasks:
            try:
                await asyncio.wait_for(task, timeout=3)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                pass

        self.running = False
        self.stopped_at = datetime.now(timezone.utc).isoformat()

        logger.info(
            "WS recorder stopped; total_messages=%d total_bytes=%d",
            self.total_messages,
            self.total_bytes,
        )
        return {"stopped": True}

    # ------------------------------------------------------------------ #
    # Base URL                                                           #
    # ------------------------------------------------------------------ #
    def _base_url(self) -> str:
        url = (settings.ws_recorder_base_url or "").rstrip("/")
        if url:
            return url

        base = (settings.public_ws_base_url or "").rstrip("/")
        if base:
            return base

        # Same-process default; matches run.bat / start.sh.
        return "ws://127.0.0.1:8002"

    # ------------------------------------------------------------------ #
    # WS client consumer                                                 #
    # ------------------------------------------------------------------ #
    async def _consume(self, instrument_key: str, role: str) -> None:
        rec = self._records.setdefault(
            instrument_key,
            {
                "instrument_key": instrument_key,
                "role": role,
                "file": None,
                "messages": 0,
                "bytes": 0,
                "status": "pending",
                "started_at": None,
                "last_message_at": None,
                "error": None,
            },
        )

        url = (
            f"{self._base_url()}/ws/live-ema/by-instrument/"
            f"{quote(instrument_key, safe='')}"
        )

        assert self._stop_event is not None

        while not self._stop_event.is_set():
            try:
                rec["status"] = "connecting"
                rec["started_at"] = datetime.now(timezone.utc).isoformat()

                async with websockets.connect(
                    url,
                    ping_interval=20,
                    ping_timeout=20,
                    max_size=2 ** 20,
                    close_timeout=5,
                ) as ws:
                    rec["status"] = "connected"
                    file_path = self._new_session_file(instrument_key)
                    rec["file"] = str(file_path)

                    logger.info(
                        "WS recorder connected; instrument=%s role=%s file=%s",
                        instrument_key,
                        role,
                        file_path,
                    )

                    # Record file is line-buffered; each frame is flushed
                    # immediately so a crash never loses more than one line.
                    with file_path.open("a", encoding="utf-8") as fh:
                        while not self._stop_event.is_set():
                            try:
                                raw = await asyncio.wait_for(
                                    ws.recv(), timeout=60
                                )
                            except asyncio.TimeoutError:
                                continue
                            except ConnectionClosed:
                                rec["status"] = "closed"
                                break

                            self._write_frame(
                                fh, instrument_key, role, raw, rec
                            )

                            if role == "nifty-index":
                                self._maybe_capture_nifty_ltp(raw)

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                rec["status"] = "error"
                rec["error"] = f"{type(exc).__name__}: {exc}"
                self.last_error = rec["error"]
                self.last_error_at = datetime.now(timezone.utc).isoformat()
                logger.warning(
                    "WS recorder connection error; instrument=%s error=%s",
                    instrument_key,
                    exc,
                )

            if self._stop_event.is_set():
                break

            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=max(1, settings.ws_recorder_reconnect_seconds),
                )
            except asyncio.TimeoutError:
                pass

        rec["status"] = "stopped"

    # ------------------------------------------------------------------ #
    # Frame handling                                                     #
    # ------------------------------------------------------------------ #
    def _write_frame(
        self,
        fh,
        instrument_key: str,
        role: str,
        raw: Any,
        rec: dict[str, Any],
    ) -> None:
        try:
            if isinstance(raw, (bytes, bytearray)):
                raw = raw.decode("utf-8", errors="replace")

            record = {
                "recorded_at": datetime.now(timezone.utc).isoformat(),
                "instrument_key": instrument_key,
                "role": role,
                "message": raw,
            }
            line = json.dumps(record, ensure_ascii=False)
            fh.write(line + "\n")
            fh.flush()

            size = len(line) + 1
            rec["messages"] += 1
            rec["bytes"] += size
            rec["last_message_at"] = record["recorded_at"]

            self.total_messages += 1
            self.total_bytes += size
        except Exception:
            logger.exception(
                "WS recorder write failed; instrument=%s", instrument_key
            )

    def _maybe_capture_nifty_ltp(self, raw: Any) -> None:
        """Extract the Nifty 50 LTP from the first tick, then pick nearest."""
        if self._nifty_ltp is not None:
            return
        try:
            if isinstance(raw, (bytes, bytearray)):
                raw = raw.decode("utf-8", errors="replace")
            payload = json.loads(raw)
        except Exception:
            return
        if not isinstance(payload, dict):
            return
        if payload.get("type") != "tick":
            return

        data = payload.get("data") or {}
        ltp = data.get("ltp")
        try:
            ltp_f = float(ltp)
        except (TypeError, ValueError):
            return

        self._nifty_ltp = ltp_f
        logger.info(
            "WS recorder captured nifty ltp=%.4f; picking nearest instrument",
            ltp_f,
        )

        # Fire-and-forget. The consumer task continues recording.
        asyncio.create_task(self._pick_nearest_and_connect(ltp_f))

    async def _pick_nearest_and_connect(self, nifty_ltp: float) -> None:
        if self._nearest_key is not None:
            return

        # Build the candidate list: any instrument with a valid strike,
        # excluding the Nifty index itself.
        candidates: list[tuple[float, str, dict[str, Any]]] = []
        for key, item in self._instruments_by_key.items():
            if key == self._nifty_key:
                continue
            strike = item.get("strike_price")
            try:
                strike_f = float(strike)
            except (TypeError, ValueError):
                continue
            candidates.append((abs(strike_f - nifty_ltp), key, item))

        if not candidates:
            logger.warning(
                "WS recorder: no candidate instruments found near "
                "nifty ltp=%.4f; second stream not started",
                nifty_ltp,
            )
            return

        # Nearest distance first, then random tie-break between equally
        # close strikes.
        min_distance = min(c[0] for c in candidates)
        ties = [c for c in candidates if c[0] == min_distance]
        _, key, item = random.choice(ties)

        self._nearest_key = key

        self._records[key] = {
            "instrument_key": key,
            "role": "nearest-instrument",
            "file": None,
            "messages": 0,
            "bytes": 0,
            "status": "pending",
            "started_at": None,
            "last_message_at": None,
            "error": None,
            "trading_symbol": item.get("trading_symbol"),
            "strike_price": item.get("strike_price"),
            "instrument_type": item.get("instrument_type"),
            "expiry": item.get("expiry"),
            "nifty_ltp_at_pick": nifty_ltp,
            "distance": min_distance,
        }

        self._tasks[key] = asyncio.create_task(
            self._consume(key, role="nearest-instrument"),
            name=f"ws-recorder:{_safe_filename(key)}",
        )

        logger.info(
            "WS recorder picked nearest instrument; key=%s "
            "symbol=%s strike=%s type=%s distance=%.4f",
            key,
            item.get("trading_symbol"),
            item.get("strike_price"),
            item.get("instrument_type"),
            min_distance,
        )

    # ------------------------------------------------------------------ #
    # File layout                                                        #
    # ------------------------------------------------------------------ #
    def _new_session_file(self, instrument_key: str) -> Path:
        assert self._output_dir is not None
        safe = _safe_filename(instrument_key)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        sub = self._output_dir / safe
        sub.mkdir(parents=True, exist_ok=True)
        return sub / f"{ts}.jsonl"

    # ------------------------------------------------------------------ #
    # Introspection                                                      #
    # ------------------------------------------------------------------ #
    def status(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "started_at": self.started_at,
            "stopped_at": self.stopped_at,
            "base_url": self._base_url(),
            "output_dir": str(self._output_dir) if self._output_dir else None,
            "nifty_key": self._nifty_key,
            "nifty_ltp": self._nifty_ltp,
            "nearest_key": self._nearest_key,
            "total_messages": self.total_messages,
            "total_bytes": self.total_bytes,
            "last_error": self.last_error,
            "last_error_at": self.last_error_at,
            "records": {k: dict(v) for k, v in self._records.items()},
        }


ws_recorder_service = WsRecorderService()