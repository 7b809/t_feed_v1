"""SR and Supertrend WebSocket capture for both isolated option legs.

Uses the same upstream contracts as orderflow_v1:
  /ws/sr/{URL-encoded instrument key}?config=all
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
        self.enabled_sr = os.getenv("ISOLATED_SR_ENABLED", "true").lower() == "true"
        self.enabled_st = os.getenv("ISOLATED_SUPERTREND_ENABLED", "true").lower() == "true"
        self.base_url = os.getenv("EMA_WS_BASE_URL", "wss://ema.novag7.in").strip().rstrip("/")
        self.sr_config = os.getenv("EMA_SR_CONFIG", "all").strip() or "all"
        self.require_today_scope = os.getenv("REQUIRE_TODAY_ONLY_SCOPE", "true").lower() == "true"
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
            logger.warning("No instrument keys available for SR/Supertrend subscription")
            return
        self._ensure_loop()
        trade_date = str(record.get("trade_date_ist") or _now().date())
        isolation_id = f"{trade_date}__{record.get('instrument_key') or 'unknown'}"
        for role, key, symbol in valid:
            supertrend_shared = False
            if self.enabled_st:
                try:
                    from services.option_chain_history import seed_option_chain_history
                    destination = self._file_path(trade_date, role, key, "supertrend")
                    result = seed_option_chain_history(key, trade_date, role, "supertrend", destination)
                    if result.get("state") == "seeded":
                        logger.info("Seeded pre-isolation Supertrend history role=%s key=%s rows=%s", role, key, result.get("rows"))
                    # Reuse the already-running option-chain Supertrend socket
                    # and fan its frames into this isolation archive. Fall back
                    # to the isolation-owned socket if no shared task is active.
                    from services.option_chain_supertrend_service import register_option_chain_supertrend_archive
                    supertrend_shared = register_option_chain_supertrend_archive(
                        key, destination, isolation_id, role, symbol or key, record=record
                    )
                except Exception:
                    logger.exception("Could not seed/register option-chain Supertrend history role=%s key=%s", role, key)
            for indicator, enabled in (("sr", self.enabled_sr), ("supertrend", self.enabled_st)):
                if not enabled or (indicator == "supertrend" and supertrend_shared):
                    continue
                task_key = (isolation_id, role, indicator, key)
                if task_key in self._tasks and not self._tasks[task_key].done():
                    continue
                coro = self._run_feed(isolation_id, trade_date, role, key, symbol or key, indicator, record)
                future = asyncio.run_coroutine_threadsafe(coro, self._loop)
                self._tasks[task_key] = future
                self._status["|".join(task_key)] = {"state": "starting", "instrument_key": key}
        logger.info("SR/Supertrend capture scheduled isolation=%s legs=%d shared_supertrend=%s", isolation_id, len(valid), self.enabled_st)

        # Replay a signal already written to the option-chain Supertrend feed
        # for this same IST minute. Run outside the isolation caller so file
        # I/O, formatting, price lookup, and Telegram latency cannot delay it.
        if self.enabled_st and record.get("instrument_key"):
            threading.Thread(
                target=self._catch_up_current_minute_signal,
                args=(dict(record),),
                name=f"supertrend-catchup-{_safe(record.get('instrument_key'))}",
                daemon=True,
            ).start()

    @staticmethod
    def _catch_up_current_minute_signal(record):
        try:
            from services.supertrend_signal_notification_service import notify_current_minute_supertrend_signal
            notify_current_minute_supertrend_signal(record)
        except Exception:
            logger.exception(
                "Unexpected current-minute Supertrend catch-up failure; isolation continues | key=%s",
                record.get("instrument_key"),
            )

    def _file_path(self, trade_date, role, key, indicator):
        folder = self.root / indicator
        folder.mkdir(parents=True, exist_ok=True)
        return folder / f"{trade_date}_{role}_{_safe(key)}.jsonl"

    async def _run_feed(self, isolation_id, trade_date, role, key, symbol, indicator, isolation_record):
        encoded = quote(key, safe="")
        if indicator == "sr":
            url = f"{self.base_url}/ws/sr/{encoded}?config={self.sr_config}"
        else:
            url = f"{self.base_url}/ws/supertrend/{encoded}"
        task_key = "|".join((isolation_id, role, indicator, key))
        path = self._file_path(trade_date, role, key, indicator)
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
                        if self.require_today_scope and payload.get("data_scope") != "today_only":
                            logger.warning("%s frame rejected: data_scope=%s key=%s", indicator.upper(), payload.get("data_scope"), key)
                            continue
                        row = {
                            "received_at_ist": received_at,
                            "indicator": indicator,
                            "isolation_id": isolation_id,
                            "leg_role": role,
                            "instrument_key": key,
                            "trading_symbol": symbol,
                            "payload": payload,
                        }
                        if indicator == "supertrend":
                            try:
                                from services.option_chain_history import seed_option_chain_history
                                await asyncio.to_thread(seed_option_chain_history, key, trade_date, role, "supertrend", path)
                            except Exception:
                                logger.exception("Could not retry Supertrend history seed key=%s", key)
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
                        if indicator == "supertrend" and event == "supertrend_signal" and role == "isolated":
                            from services.supertrend_signal_notification_service import dispatch_isolated_supertrend_signal
                            dispatch_isolated_supertrend_signal(
                                isolation_record, payload, received_at, role,
                            )
                        if event == "supertrend_signal" or (indicator == "sr" and event == "sr_update"):
                            from services.isolation_telegram_service import notify_indicator_event
                            await asyncio.to_thread(notify_indicator_event, indicator, payload, symbol, role)
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
        return {"enabled": {"sr": self.enabled_sr, "supertrend": self.enabled_st},
                "base_url": self.base_url, "connections": dict(self._status)}

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
