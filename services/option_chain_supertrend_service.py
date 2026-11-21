"""Per-contract Supertrend WebSocket capture for the live option chain."""
from __future__ import annotations
import asyncio, json, os, threading
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
        boundary = (now + timedelta(days=7 - now.weekday())).replace(hour=9, minute=15, second=0, microsecond=0)
    elif now.time().replace(tzinfo=None) < dt_time(9, 15):
        boundary = now.replace(hour=9, minute=15, second=0, microsecond=0)
    else:
        boundary = (now + timedelta(days=1)).replace(hour=9, minute=15, second=0, microsecond=0)
        while boundary.weekday() >= 5:
            boundary += timedelta(days=1)
    return max(1.0, (boundary - now).total_seconds())

class OptionChainSupertrendCapture:
    def __init__(self):
        self.enabled = os.getenv("OPTION_CHAIN_SUPERTREND_ENABLED", "true").strip().lower() == "true"
        self.base_url = os.getenv("EMA_WS_BASE_URL", "wss://ema.novag7.in").strip().rstrip("/")
        self.require_today_scope = os.getenv("REQUIRE_TODAY_ONLY_SCOPE", "true").strip().lower() == "true"
        self.max_connections = max(1, int(os.getenv("OPTION_CHAIN_SUPERTREND_MAX_CONNECTIONS", "120")))
        self._loop = None
        self._thread = None
        self._tasks = {}
        self._status = {}
        self._archive_sinks = {}
        self._lock = threading.RLock()
        self._semaphore = None

    def _ensure_loop(self):
        with self._lock:
            if self._loop and self._loop.is_running():
                return
            ready = threading.Event()
            def runner():
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                self._loop = loop
                self._semaphore = asyncio.Semaphore(self.max_connections)
                ready.set()
                loop.run_forever()
                pending = asyncio.all_tasks(loop)
                for task in pending: task.cancel()
                if pending: loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                loop.close()
            self._thread = threading.Thread(target=runner, name="option-chain-supertrend-loop", daemon=True)
            self._thread.start()
        ready.wait(timeout=5)

    @staticmethod
    def _folder(meta):
        day = str(meta.get("trade_date_ist") or _now().date().isoformat()).replace("-", "")
        index_folder = str(meta.get("index_folder") or "unknown")
        strike = f"{float(meta['strike_price']):g}"
        side = str(meta.get("instrument_type") or "").lower()
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in str(meta["instrument_key"]))[:120]
        return Path(os.getenv("OPTION_CHAIN_DATA_DIR", "data/option_chain")) / index_folder / day / f"{strike}_{side}_{safe}"

    def start(self, contracts):
        if not self.enabled: return self.status()
        valid = [row for row in contracts if row.get("instrument_key")]
        if not valid: return self.status()
        self._ensure_loop()
        for meta in valid:
            day = str(meta.get("trade_date_ist") or _now().date().isoformat())
            key = str(meta["instrument_key"])
            task_key = (day, key)
            with self._lock:
                current = self._tasks.get(task_key)
                if current is not None and not current.done(): continue
            row = dict(meta); row["trade_date_ist"] = day
            future = asyncio.run_coroutine_threadsafe(self._run_feed(row), self._loop)
            with self._lock:
                self._tasks[task_key] = future
                self._status[key] = {"state": "starting", "trade_date_ist": day}
        logger.info("Option-chain Supertrend capture scheduled for %d contracts", len(valid))
        return self.status()

    def register_archive_sink(self, instrument_key, path, isolation_id, role, symbol):
        """Fan an existing option-chain Supertrend stream into an isolation file."""
        day = _now().date().isoformat()
        task = self._tasks.get((day, str(instrument_key)))
        if task is None or task.done():
            return False
        sink = {
            "path": Path(path),
            "isolation_id": isolation_id,
            "role": role,
            "symbol": symbol,
        }
        with self._lock:
            self._archive_sinks.setdefault(str(instrument_key), {})[str(path)] = sink
        logger.info("Registered isolation Supertrend archive sink role=%s key=%s path=%s", role, instrument_key, path)
        return True

    async def _run_feed(self, meta):
        key = str(meta["instrument_key"]); symbol = str(meta.get("trading_symbol") or key)
        day = str(meta["trade_date_ist"])
        path = self._folder(meta) / "supertrend_feed.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        url = f"{self.base_url}/ws/supertrend/{quote(key, safe='')}"
        backoff = 1
        while True:
            if not _inside_session():
                with self._lock: self._status[key] = {"state": "outside_session", "file": str(path)}
                await asyncio.sleep(_until_session()); continue
            try:
                async with self._semaphore:
                    logger.info("OPTION-CHAIN SUPERTREND connecting | %s", symbol)
                    async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
                        with self._lock: self._status[key] = {"state": "connected", "file": str(path), "connected_at": _now().isoformat()}
                        logger.info("OPTION-CHAIN SUPERTREND connected | %s", symbol)
                        backoff = 1
                        async for raw in ws:
                            received = _now().isoformat()
                            try:
                                if isinstance(raw, (bytes, bytearray)): raw = raw.decode("utf-8")
                                payload = json.loads(raw)
                            except (UnicodeDecodeError, json.JSONDecodeError):
                                logger.warning("Option-chain Supertrend non-JSON frame: %s", key); continue
                            if not isinstance(payload, dict): continue
                            if self.require_today_scope and payload.get("data_scope") != "today_only":
                                logger.warning("Option-chain Supertrend rejected frame scope=%s key=%s", payload.get("data_scope"), key); continue
                            event = {"received_at_ist": received, "trade_date_ist": day,
                                "index_instrument_key": meta.get("index_instrument_key"), "index_canonical": meta.get("index_canonical"),
                                "expiry": meta.get("expiry"), "strike_price": meta.get("strike_price"),
                                "option_type": meta.get("instrument_type"), "instrument_key": key,
                                "trading_symbol": symbol, "payload": payload}
                            await asyncio.to_thread(self._append, path, event)
                            with self._lock:
                                sinks = list(self._archive_sinks.get(key, {}).values())
                                self._status[key] = {"state": "connected", "file": str(path), "last_event": payload.get("event"), "last_frame_at": received}
                            for sink in sinks:
                                archive_row = {
                                    "received_at_ist": received,
                                    "indicator": "supertrend",
                                    "isolation_id": sink["isolation_id"],
                                    "leg_role": sink["role"],
                                    "instrument_key": key,
                                    "trading_symbol": sink["symbol"],
                                    "payload": payload,
                                }
                                await asyncio.to_thread(self._append, sink["path"], archive_row)
            except asyncio.CancelledError:
                with self._lock: self._status[key] = {"state": "stopped", "file": str(path)}
                raise
            except Exception as exc:
                with self._lock: self._status[key] = {"state": "reconnecting", "file": str(path), "error": str(exc)}
                logger.warning("Option-chain Supertrend error | %s | %s | retry=%ss", symbol, exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, int(os.getenv("ISOLATED_WS_MAX_BACKOFF_SECONDS", "30")))

    @staticmethod
    def _append(path, row):
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")

    def stop(self):
        if not self._loop or not self._loop.is_running(): return
        async def cancel_all():
            for future in list(self._tasks.values()): future.cancel()
            await asyncio.sleep(0.1); self._tasks.clear()
        future = asyncio.run_coroutine_threadsafe(cancel_all(), self._loop)
        try: future.result(timeout=5)
        except Exception: logger.exception("Error stopping option-chain Supertrend feeds")
        with self._lock:
            self._archive_sinks.clear()
        self._loop.call_soon_threadsafe(self._loop.stop)

    def status(self):
        with self._lock: states = dict(self._status)
        counts = {}
        for item in states.values():
            state = item.get("state", "unknown"); counts[state] = counts.get(state, 0) + 1
        return {"enabled": self.enabled, "base_url": self.base_url, "max_connections": self.max_connections,
            "tracked_instruments": len(states), "states": counts, "connections": states}

_manager = OptionChainSupertrendCapture()
def start_option_chain_supertrend_capture(contracts): return _manager.start(contracts)
def register_option_chain_supertrend_archive(instrument_key, path, isolation_id, role, symbol):
    return _manager.register_archive_sink(instrument_key, path, isolation_id, role, symbol)
def get_option_chain_supertrend_status(): return _manager.status()
def stop_option_chain_supertrend_capture(): _manager.stop()
