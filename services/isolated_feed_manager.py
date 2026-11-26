"""Multi-interval Supertrend WebSocket capture for isolated option legs.

Uses the same upstream contracts as orderflow_v1:
  /ws/supertrend/{URL-encoded instrument key}

Each raw JSON event is appended to a daily JSONL file. The feed clients run
on a dedicated asyncio loop so the synchronous isolation scanner is never
blocked by WebSocket lifetime or reconnect handling.
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
from datetime import datetime, time as dt_time, timedelta
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

import websockets

from core.logger import get_logger

logger = get_logger(__file__)
IST = ZoneInfo("Asia/Kolkata")


def _now():
    return datetime.now(IST)


def _inside_session(now=None):
    now = now or _now()
    return now.weekday() < 5 and dt_time(9, 15) <= now.time().replace(tzinfo=None) < dt_time(15, 30)


def _until_session(now=None):
    now = now or _now()
    if now.weekday() >= 5:
        days_ahead = 7 - now.weekday()
        boundary = (now + timedelta(days=days_ahead)).replace(hour=9, minute=15, second=0, microsecond=0)
    elif now.time().replace(tzinfo=None) < dt_time(9, 15):
        boundary = now.replace(hour=9, minute=15, second=0, microsecond=0)
    else:
        boundary = (now + timedelta(days=1)).replace(hour=9, minute=15, second=0, microsecond=0)
        while boundary.weekday() >= 5:
            boundary += timedelta(days=1)
    return max(1.0, (boundary - now).total_seconds())


def _safe(value):
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in str(value))[:100]


class IsolatedFeedManager:
    def __init__(self):
        self.enabled_st = os.getenv("ISOLATED_SUPERTREND_ENABLED", "true").lower() == "true"
        self.base_url = os.getenv("EMA_WS_BASE_URL", "wss://ema.novag7.in").strip().rstrip("/")
        self.require_today_scope = os.getenv("REQUIRE_TODAY_ONLY_SCOPE", "true").lower() == "true"
        from core.config import SUPERTREND_INTERVALS
        self.supertrend_intervals = list(SUPERTREND_INTERVALS)
        self.root = Path(os.getenv("ISOLATED_DATA_DIR", "data/isolated"))
        self._loop = None
        self._thread = None
        self._tasks = {}
        self._lock = threading.Lock()
        self._status = {}

    def _ensure_loop(self):
        with self._lock:
            if self._loop and self._loop.is_running():
                return
            ready = threading.Event()
            def runner():
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                self._loop = loop
                ready.set()
                loop.run_forever()
                pending = asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                loop.close()
            self._thread = threading.Thread(target=runner, name="isolated-feed-loop", daemon=True)
            self._thread.start()
        ready.wait(timeout=5)

    def start_isolation(self, record: dict):
        legs = [
            ("isolated", record.get("instrument_key"), record.get("trading_symbol")),
            ("opposite", (record.get("opposite_leg") or {}).get("instrument_key"),
             (record.get("opposite_leg") or {}).get("trading_symbol")),
        ]
        valid = [(role, key, symbol) for role, key, symbol in legs if key]
        if not valid:
            logger.warning("No instrument keys available for Supertrend subscription")
            return
        self._ensure_loop()
        trade_date = str(record.get("trade_date_ist") or _now().date())
        isolation_id = f"{trade_date}__{record.get('instrument_key') or 'unknown'}"
        for role, key, symbol in valid:
            if not self.enabled_st:
                continue
            for interval in self.supertrend_intervals:
                task_key = (isolation_id, role, "supertrend", key, interval)
                if task_key in self._tasks and not self._tasks[task_key].done():
                    continue
                coro = self._run_feed(isolation_id, trade_date, role, key, symbol or key, "supertrend", record, interval=interval)
                future = asyncio.run_coroutine_threadsafe(coro, self._loop)
                self._tasks[task_key] = future
                status_key = "|".join(str(part) for part in task_key)
                self._status[status_key] = {"state": "starting", "instrument_key": key, "interval": interval}
        logger.info("Supertrend capture scheduled isolation=%s legs=%d intervals=%s", isolation_id, len(valid), self.supertrend_intervals)


    def _file_path(self, trade_date, role, key, indicator, interval=None):
        folder = self.root / indicator
        folder.mkdir(parents=True, exist_ok=True)
        suffix = f"_{interval}" if indicator == "supertrend" and interval and interval != "1m" else ""
        return folder / f"{trade_date}_{role}_{_safe(key)}{suffix}.jsonl"

    async def _run_feed(self, isolation_id, trade_date, role, key, symbol, indicator, isolation_record, interval=None):
        encoded = quote(key, safe="")
        if interval == "1m":
            url = f"{self.base_url}/ws/supertrend/{encoded}"
        else:
            url = f"{self.base_url}/ws/supertrend/interval/{interval}/{encoded}"
        task_key = "|".join(str(part) for part in (isolation_id, role, indicator, key, interval) if part)
        path = self._file_path(trade_date, role, key, indicator, interval=interval)
        backoff = 1
        while True:
            if not _inside_session():
                self._status[task_key] = {"state": "outside_session", "file": str(path)}
                await asyncio.sleep(_until_session())
                continue
            try:
                logger.info("%s WS connecting role=%s symbol=%s url=%s", indicator.upper(), role, symbol, url)
                async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
                    connected_at = _now().isoformat()
                    self._status[task_key] = {"state": "connected", "file": str(path), "connected_at": connected_at}
                    logger.info(
                        "%s feed connected | role=%s | instrument=%s | at=%s",
                        indicator.upper(), role, symbol, connected_at,
                    )
                    backoff = 1
                    async for raw in ws:
                        received_at = _now().isoformat()
                        try:
                            if isinstance(raw, (bytes, bytearray)):
                                raw = raw.decode("utf-8")
                            payload = json.loads(raw)
                        except (UnicodeDecodeError, json.JSONDecodeError):
                            logger.warning("%s WS received non-JSON frame key=%s", indicator, key)
                            continue
                        if not isinstance(payload, dict):
                            continue
                        body = payload.get("data") if isinstance(payload.get("data"), dict) else payload
                        data_scope = body.get("data_scope") or payload.get("data_scope")
                        if self.require_today_scope and data_scope != "today_only":
                            logger.warning("%s frame rejected: data_scope=%s key=%s", indicator.upper(), data_scope, key)
                            continue
                        frame_interval = body.get("interval") or payload.get("interval")
                        if frame_interval and str(frame_interval).lower() != interval:
                            logger.warning("Supertrend interval mismatch expected=%s received=%s key=%s", interval, frame_interval, key)
                            continue
                        try:
                            from services.supertrend_snapshot_cache import update_supertrend_snapshot_cache
                            update_supertrend_snapshot_cache(key, payload, received_at, interval=interval)
                        except Exception:
                            logger.exception("Could not update Supertrend snapshot cache key=%s interval=%s", key, interval)

                        # Evaluate the pair after every accepted frame. The sandbox
                        # service reads both instruments' interval-specific caches,
                        # chooses the earliest confirmed signal once, and waits for
                        # a fresh live quote before saving a plan.
                        try:
                            from jobs.daily_refresh import get_feed_service
                            from services.sandbox_signal_service import (
                                process_supertrend_signal_update,
                            )

                            def _live_quote(instrument_key):
                                market_feed = get_feed_service()
                                if market_feed is None:
                                    return {"ltp": None, "stale": True, "reason": "market_feed_unavailable"}
                                health = market_feed.get_health()
                                instrument = (health.get("instruments") or {}).get(instrument_key) or {}
                                ltp = market_feed.get_ltp(instrument_key)
                                return {
                                    "ltp": ltp,
                                    "stale": bool(instrument.get("stale", True)),
                                    "age_seconds": instrument.get("age_seconds"),
                                    "received_at": instrument.get("last_recv"),
                                }

                            outcome = process_supertrend_signal_update(
                                isolation_record, interval, _live_quote
                            )
                            if outcome.get("state") not in {
                                "waiting_for_snapshot", "waiting_for_first_signal",
                                "already_processed",
                            }:
                                logger.info(
                                    "Sandbox signal evaluation | interval=%s | state=%s",
                                    interval, outcome.get("state"),
                                )
                        except Exception:
                            logger.exception(
                                "Sandbox Supertrend signal evaluation failed | key=%s interval=%s",
                                key, interval,
                            )
                        row = {
                            "received_at_ist": received_at,
                            "indicator": "supertrend",
                            "isolation_id": isolation_id,
                            "leg_role": role,
                            "instrument_key": key,
                            "trading_symbol": symbol,
                            "interval": interval,
                            "payload": payload,
                        }
                        await asyncio.to_thread(self._append_jsonl, path, row)
                        event = payload.get("event")
                        if event == "connection_ack":
                            logger.info(
                                "%s WS acknowledged | role=%s | instrument=%s",
                                indicator.upper(), role, symbol,
                            )
                        elif event == "snapshot":
                            logger.debug(
                                "%s WS initial snapshot received | role=%s | instrument=%s",
                                indicator.upper(), role, symbol,
                            )
                        self._status[task_key] = {
                            "state": "connected", "file": str(path), "last_event": event,
                            "last_frame_at": received_at,
                        }
            except asyncio.CancelledError:
                self._status[task_key] = {"state": "stopped", "file": str(path)}
                raise
            except Exception as exc:
                self._status[task_key] = {"state": "reconnecting", "file": str(path), "error": str(exc)}
                logger.warning("%s WS error role=%s key=%s: %s; retry=%ss",
                               indicator.upper(), role, key, exc, backoff)
                if backoff == 1:
                    logger.warning(
                        "%s feed interrupted | role=%s | instrument=%s | error=%s",
                        indicator.upper(), role, symbol, exc,
                    )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, int(os.getenv("ISOLATED_WS_MAX_BACKOFF_SECONDS", "30")))

    @staticmethod
    def _append_jsonl(path: Path, row: dict):
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")

    def status(self):
        return {"enabled": {"supertrend": self.enabled_st},
                "base_url": self.base_url, "supertrend_intervals": list(self.supertrend_intervals), "connections": dict(self._status)}

    def stop(self):
        if not self._loop or not self._loop.is_running():
            return
        async def cancel_all():
            # run_coroutine_threadsafe returns concurrent.futures.Future objects,
            # not asyncio Tasks; cancelling them propagates cancellation to the loop.
            for future in list(self._tasks.values()):
                future.cancel()
            await asyncio.sleep(0.1)
            self._tasks.clear()
        future = asyncio.run_coroutine_threadsafe(cancel_all(), self._loop)
        try:
            future.result(timeout=5)
        except Exception:
            logger.exception("Error stopping isolated SR/Supertrend feed clients")
        self._loop.call_soon_threadsafe(self._loop.stop)


_feed_manager = IsolatedFeedManager()


def start_isolated_feeds(record: dict) -> None:
    _feed_manager.start_isolation(record)


def get_isolated_feed_status() -> dict:
    return _feed_manager.status()


def stop_isolated_feeds() -> None:
    _feed_manager.stop()
