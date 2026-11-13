# services/external_sr_websocket_service.py
"""External EMA V2 S/R WebSocket consumer.

The project is a consumer only: pivot detection and S/R calculation live in
EMA V2. This service owns connection lifecycle, snapshots, freshness/readiness,
and a read-compatible levels API for the existing strategy/order services.

Market-hours gate
-----------------
Connections are only *initiated* Monday-Friday, 09:00-16:00 IST.
Existing connections are gracefully closed at 16:00 IST, including when the
upstream socket is silent. Set ``EXTERNAL_SR_TEST_EMA=true`` (or the legacy
``TEST_EMA=true``) only for an intentional off-hours test. Default is **false**.
The upstream host is supplied privately through ``EXTERNAL_SR_WS_URL``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

import websockets
from websockets.exceptions import ConnectionClosed

logger = logging.getLogger(__name__)


def _parse_hhmm(value: str, fallback: tuple[int, int]) -> tuple[int, int]:
    """Parse an HH:MM market-window setting without breaking startup."""
    try:
        hour_text, minute_text = str(value).strip().split(":", 1)
        hour, minute = int(hour_text), int(minute_text)
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour, minute
    except (TypeError, ValueError):
        pass
    logger.warning("Invalid external S/R market-window time %r; using %02d:%02d", value, *fallback)
    return fallback


class ExternalSRWebSocketService:
    CONFIGS = ("15/3", "15/15", "15/30")

    # Market window (IST), configurable but defaulting to 09:00-16:00.
    # The close boundary is exclusive: an active socket is closed at 16:00.
    MARKET_OPEN_HOUR, MARKET_OPEN_MIN = _parse_hhmm(
        os.getenv("EXTERNAL_SR_WINDOW_START", "09:00"), (9, 0)
    )
    MARKET_CLOSE_HOUR, MARKET_CLOSE_MIN = _parse_hhmm(
        os.getenv("EXTERNAL_SR_WINDOW_END", "16:00"), (16, 0)
    )

    # How often a quiet live connection re-checks the market window via
    # ``asyncio.wait_for`` around ``ws.recv()``. Lower = more responsive
    # close at 15:40, higher = fewer wake-ups on a silent feed.
    RECV_WINDOW_CHECK_SECONDS = 60.0

    def __init__(self) -> None:
        configured_base_url = os.getenv("EXTERNAL_SR_WS_URL", "").strip()
        self.endpoint_configured = bool(configured_base_url)
        self.base_url = (configured_base_url or "wss://external-sr.example.invalid").rstrip("/")
        if self.base_url.startswith("https://"):
            self.base_url = "wss://" + self.base_url[8:]
        elif self.base_url.startswith("http://"):
            self.base_url = "ws://" + self.base_url[7:]
        self.enabled = os.getenv("EXTERNAL_SR_WS_ENABLED", "true").lower() in {"1", "true", "yes", "on"}

        # TEST_EMA: bypass the market-hours gate. Prefer the namespaced env
        # var, fall back to bare TEST_EMA for convenience. Default = false.
        _test_ema_raw = (
            os.getenv("EXTERNAL_SR_TEST_EMA")
            or os.getenv("TEST_EMA")
            or "false"
        )
        self.test_ema = _test_ema_raw.strip().lower() in {"1", "true", "yes", "on"}

        self.stale_seconds = max(60, int(os.getenv("EXTERNAL_SR_STALE_SECONDS", "180")))
        try:
            from core import config
            configured_log_dir = getattr(config, "EXTERNAL_SR_LOG_DIR", "data/live_sr")
            project_root = Path(getattr(config, "PROJECT_ROOT", Path(__file__).resolve().parents[1]))
        except Exception:
            configured_log_dir = os.getenv("EXTERNAL_SR_LOG_DIR", "data/live_sr")
            project_root = Path(__file__).resolve().parents[1]
        self.log_dir = Path(configured_log_dir)
        if not self.log_dir.is_absolute():
            self.log_dir = project_root / self.log_dir
        self.log_dir = self.log_dir.resolve()
        self._market_tz = ZoneInfo(os.getenv("MARKET_TIMEZONE", "Asia/Kolkata"))
        self._log_lock = threading.Lock()
        self._lock = threading.RLock()
        # Last S/R shape written per instrument/config. Heartbeats and
        # repeated updates with unchanged pivot levels are not persisted.
        self._last_logged_sr_signature: dict[tuple[str, str], tuple[Any, ...]] = {}
        self._states: dict[tuple[str, str], dict[str, Any]] = {}
        self._wanted: set[tuple[str, str]] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._tasks: dict[tuple[str, str], asyncio.Task] = {}
        self._stop = threading.Event()

    # ------------------------------------------------------------------
    # Market-hours gate helpers
    # ------------------------------------------------------------------
    def _within_market_window(self, at: datetime | None = None) -> bool:
        """True iff connections are allowed at ``at`` (defaults to now IST).

        ``TEST_EMA`` short-circuits this and always returns True.
        """
        if self.test_ema:
            return True
        now = at or datetime.now(self._market_tz)
        if now.weekday() >= 5:  # Sat / Sun
            return False
        start = now.replace(
            hour=self.MARKET_OPEN_HOUR,
            minute=self.MARKET_OPEN_MIN,
            second=0, microsecond=0,
        )
        end = now.replace(
            hour=self.MARKET_CLOSE_HOUR,
            minute=self.MARKET_CLOSE_MIN,
            second=0, microsecond=0,
        )
        return start <= now < end

    def _seconds_until_next_window(self) -> float:
        """Seconds until the next configured weekday opening (0 if inside)."""
        now = datetime.now(self._market_tz)
        if self._within_market_window(now):
            return 0.0
        today_start = now.replace(
            hour=self.MARKET_OPEN_HOUR,
            minute=self.MARKET_OPEN_MIN,
            second=0, microsecond=0,
        )
        if now < today_start:
            candidate = today_start
        else:
            candidate = today_start + timedelta(days=1)
        while candidate.weekday() >= 5:
            candidate += timedelta(days=1)
        return max(1.0, (candidate - now).total_seconds())

    async def _wait_for_market_window(self) -> None:
        """Sleep until the market window opens; interruptible via cancellation."""
        while not self._stop.is_set() and not self._within_market_window():
            wait = self._seconds_until_next_window()
            # Cap at 1h so a system clock jump can't strand us for days.
            wait = max(1.0, min(wait, 3600.0))
            # Small jitter so ~700 tasks don't all handshake at 09:15:00.000.
            jitter = random.uniform(0.0, 3.0)
            await asyncio.sleep(wait + jitter)

    def _seconds_until_window_close(self, at: datetime | None = None) -> float:
        """Seconds until today's configured close boundary; never negative."""
        now = at or datetime.now(self._market_tz)
        end = now.replace(
            hour=self.MARKET_CLOSE_HOUR,
            minute=self.MARKET_CLOSE_MIN,
            second=0,
            microsecond=0,
        )
        return max(0.0, (end - now).total_seconds())

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        if not self.enabled:
            logger.warning("[external-sr] disabled by EXTERNAL_SR_WS_ENABLED")
            return
        if not self.endpoint_configured:
            logger.error("[external-sr] EXTERNAL_SR_WS_URL is not configured; refusing placeholder endpoint")
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
        if loop and not loop.is_closed():
            for cfg in accepted:
                loop.call_soon_threadsafe(
                    lambda selected_cfg=cfg: asyncio.create_task(
                        self._ensure_task(key, selected_cfg)
                    )
                )
        return {"status": "subscribing", "instrument_key": key, "configs": accepted}

    def subscribe_core_indexes(self) -> dict[str, Any]:
        """Subscribe all four monitored cash indexes to external EMA V2 S/R.

        This is deliberately independent of option isolation, so index
        snapshots and updates are always collected when the service is enabled.
        """
        keys = (
            "NSE_INDEX|NIFTY MID SELECT",
            "NSE_INDEX|Nifty Bank",
            "NSE_INDEX|Nifty 50",
            "BSE_INDEX|SENSEX",
        )
        results = {key: self.subscribe(key) for key in keys}
        logger.info(
            "[external-sr] core index subscriptions requested count=%d keys=%s",
            len(results), list(results),
        )
        return {"status": "subscribing", "count": len(results), "results": results}

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def _log_path_for(self, key: str, cfg: str) -> Path:
        safe_key = re.sub(r"[^A-Za-z0-9._-]+", "_", key).strip("._-") or "instrument"
        safe_cfg = re.sub(r"[^A-Za-z0-9._-]+", "-", cfg).strip("._-") or "config"
        return self.log_dir / f"{safe_key}__{safe_cfg}.jsonl"

    def _persist_event(self, key: str, cfg: str, message: dict[str, Any], received_at: float) -> bool:
        """Append one meaningful S/R state; return whether it reached disk."""
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            record = {
                "received_at": datetime.fromtimestamp(received_at, self._market_tz).isoformat(),
                "instrument_key": key,
                "config": cfg,
                "event": str(message.get("event") or ""),
                # Preserve the complete upstream payload without reshaping it.
                "payload": message,
            }
            line = json.dumps(record, ensure_ascii=False, separators=(",", ":"), default=str)
            path = self._log_path_for(key, cfg)
            with self._log_lock:
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
                    handle.flush()
            return True
        except Exception:
            # Logging is best-effort and must never interrupt live S/R consumption.
            logger.exception("[external-sr] failed to persist event key=%s config=%s", key, cfg)
            return False

    # ------------------------------------------------------------------
    # Consume loop (gated by market hours)
    # ------------------------------------------------------------------
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
            # ---- Market-hours gate: only *initiate* connections in-window ----
            if not self._within_market_window():
                logger.debug(
                    "[external-sr] outside market window; deferring key=%s cfg=%s next_open_in=%.1fs",
                    key, cfg, self._seconds_until_next_window(),
                )
                await self._wait_for_market_window()
                backoff = 1.0
                continue

            try:
                logger.info("[external-sr] connecting key=%s config=%s", key, cfg)
                async with websockets.connect(url, ping_interval=20, ping_timeout=20, close_timeout=5) as ws:
                    self._patch((key, cfg), {"connected": True, "status": "connected"})
                    backoff = 1.0
                    while not self._stop.is_set():
                        # ---- Graceful close if window ends mid-stream ----
                        if not self._within_market_window():
                            logger.info(
                                "[external-sr] market window ended; closing key=%s cfg=%s",
                                key, cfg,
                            )
                            break
                        # Wake at the exact close boundary even when the
                        # upstream is silent; do not wait for the full poll interval.
                        recv_timeout = min(
                            self.RECV_WINDOW_CHECK_SECONDS,
                            max(0.05, self._seconds_until_window_close()),
                        )
                        try:
                            raw = await asyncio.wait_for(
                                ws.recv(), timeout=recv_timeout,
                            )
                        except asyncio.TimeoutError:
                            # Silent feed → re-check window and go again.
                            continue
                        try:
                            # websockets may deliver either str or bytes depending
                            # on the negotiated frame and library version.
                            if isinstance(raw, bytes):
                                raw = raw.decode("utf-8")
                            message = json.loads(raw)
                        except (TypeError, UnicodeDecodeError, json.JSONDecodeError):
                            logger.warning("[external-sr] invalid JSON frame key=%s cfg=%s", key, cfg)
                            continue
                        if isinstance(message, dict):
                            self._handle_event(key, cfg, message)
                        else:
                            logger.warning("[external-sr] ignored non-object JSON frame key=%s cfg=%s", key, cfg)
            except asyncio.CancelledError:
                raise
            except ConnectionClosed as exc:
                logger.warning("[external-sr] closed key=%s cfg=%s code=%s", key, cfg, exc.code)
                if exc.code in {4400, 4404}:
                    self._patch((key, cfg), {"connected": False, "status": f"rejected:{exc.code}"})
                    return
            except Exception:
                logger.exception("[external-sr] connection failed key=%s cfg=%s", key, cfg)

            if self._stop.is_set():
                self._patch((key, cfg), {"connected": False, "status": "stopped"})
                return

            self._patch((key, cfg), {"connected": False, "status": "reconnecting"})

            # Don't burn backoff off-hours — just wait for the next window.
            if not self._within_market_window():
                backoff = 1.0
                continue

            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    def _patch(self, token: tuple[str, str], values: dict[str, Any]) -> None:
        with self._lock:
            state = self._states.setdefault(token, {
                "instrument_key": token[0], "config": token[1], "resistances": [],
                "supports": [], "levels": [], "ready": False,
            })
            state.update(values)

    @staticmethod
    def _sr_signature(resistances: list[dict[str, Any]], supports: list[dict[str, Any]]) -> tuple[Any, ...]:
        """Build a stable signature from pivot identity, ignoring rolling bar indexes."""
        signature: list[tuple[str, str, str, str]] = []
        for side, levels in (("resistance", resistances), ("support", supports)):
            for level in levels:
                raw_price = level.get("price")
                try:
                    price = format(float(raw_price), ".10g") if raw_price is not None else ""
                except (TypeError, ValueError, OverflowError):
                    price = str(raw_price or "").strip()
                pivot_time = (
                    level.get("pivot_time")
                    or level.get("confirmed_time")
                    or level.get("timestamp")
                    or level.get("time")
                    or ""
                )
                signature.append((
                    side,
                    str(level.get("level") or "").strip(),
                    price,
                    str(pivot_time).strip(),
                ))
        return tuple(sorted(signature))

    def _handle_event(self, key: str, cfg: str, message: dict[str, Any]) -> None:
        event = str(message.get("event") or "").strip().lower()
        if event not in {"snapshot", "sr_update", "heartbeat"}:
            logger.debug("[external-sr] ignored event=%s key=%s", event, key)
            return
        now = time.time()
        token = (key, cfg)
        callback_levels: list[dict[str, Any]] = []
        should_persist = False
        signature: tuple[Any, ...] | None = None
        with self._lock:
            state = self._states.setdefault(token, {
                "instrument_key": key, "config": cfg, "resistances": [],
                "supports": [], "levels": [], "ready": False,
            })
            if event in {"snapshot", "sr_update"}:
                raw_resistances = message.get("resistances")
                raw_supports = message.get("supports")
                resistances = [
                    dict(x) for x in (raw_resistances if isinstance(raw_resistances, list) else [])
                    if isinstance(x, dict)
                ]
                supports = [
                    dict(x) for x in (raw_supports if isinstance(raw_supports, list) else [])
                    if isinstance(x, dict)
                ]
                levels = resistances + supports

                signature = self._sr_signature(resistances, supports)
                previous_signature = self._last_logged_sr_signature.get(token)
                # Persist the first observed S/R state and every genuinely
                # changed state. An identical reconnect snapshot is not a new state.
                should_persist = signature != previous_signature

                state.update({
                    "resistances": resistances,
                    "supports": supports,
                    "levels": levels,
                    "ready": True,
                    "event": event,
                    "candle_count": message.get("candle_count"),
                    "cycle_number": message.get("cycle_number"),
                })
                if event == "sr_update":
                    callback_levels = levels

            # Heartbeats also refresh freshness and status, but are never written.
            state.update({
                "connected": True,
                "status": message.get("status") or "ok",
                "current_price": message.get("current_price", state.get("current_price")),
                "last_ts": message.get("last_ts", state.get("last_ts")),
                "last_event": event,
                "last_event_at": now,
                "cycle_time": message.get("cycle_time"),
                "nearest_resistance": message.get("nearest_resistance"),
                "nearest_support": message.get("nearest_support"),
                "meta": message.get("meta") or state.get("meta") or {},
            })

        # Persist outside the state lock. Mark the signature only after a
        # successful write so a later identical update can retry a failed write.
        if should_persist:
            saved = self._persist_event(key, cfg, message, now)
            if saved and signature is not None:
                with self._lock:
                    self._last_logged_sr_signature[token] = signature

        if callback_levels:
            try:
                from services.isolated_sr_tracker_service import isolated_sr_tracker_service
                if isolated_sr_tracker_service.is_tracking(key):
                    for level in callback_levels:
                        isolated_sr_tracker_service.on_sr_level_confirmed(key, level)
            except Exception:
                logger.exception("[external-sr] isolated tracker callback failed for %s", key)

    # ------------------------------------------------------------------
    # Read API
    # ------------------------------------------------------------------
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
        result["is_ready"] = bool(result.get("ready") and result.get("connected") and result.get("last_event_at") and time.time() - result["last_event_at"] <= self.stale_seconds)
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
                    "ready": state.get("ready"), "fresh": bool(state.get("connected") and age is not None and age <= self.stale_seconds),
                    "age_seconds": round(age, 1) if age is not None else None,
                    "last_event": state.get("last_event"), "status": state.get("status"),
                    "level_count": len(state.get("levels") or []),
                })
        return {
            "enabled": self.enabled,
            "test_ema": self.test_ema,
            "market_window_open": self._within_market_window(),
            "market_window": (
                f"{self.MARKET_OPEN_HOUR:02d}:{self.MARKET_OPEN_MIN:02d}-"
                f"{self.MARKET_CLOSE_HOUR:02d}:{self.MARKET_CLOSE_MIN:02d} IST (Mon-Fri); "
                "bypassed only when TEST_EMA=true"
            ),
            "endpoint_configured": self.endpoint_configured,
            "subscriptions": rows,
        }

    def unsubscribe(self, instrument_key: str, configs: tuple[str, ...] | list[str] | None = None) -> None:
        key = str(instrument_key or "").strip()
        selected = tuple(configs or self.CONFIGS)
        loop = self._loop
        with self._lock:
            tokens = [(key, cfg) for cfg in selected if (key, cfg) in self._wanted]
            for token in tokens:
                self._wanted.discard(token)
        if loop and not loop.is_closed():
            for token in tokens:
                loop.call_soon_threadsafe(
                    lambda selected_token=token: asyncio.create_task(
                        self._cancel_task(selected_token)
                    )
                )

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