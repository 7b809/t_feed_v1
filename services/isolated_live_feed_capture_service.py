"""Best-effort raw Upstox feed capture for the isolated pair and main indexes.

This is intentionally separate from isolation decision-making. Any subscription,
listener, queue or disk error is logged and swallowed at this boundary.
"""
from __future__ import annotations

import json
import os
import queue
import threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from core.config import INSTRUMENT_KEYS, INDEX_METADATA
from core.logger import get_logger
from jobs.daily_refresh import get_feed_service

logger = get_logger(__file__)
IST = ZoneInfo("Asia/Kolkata")


def _safe(value: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in str(value))[:100]


def _positive_int_env(name: str, default: int) -> int:
    try:
        return max(1000, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        logger.warning("Invalid %s; using safe default=%d", name, default)
        return default


class IsolatedLiveFeedCapture:
    def __init__(self):
        self.root = Path(os.getenv("ISOLATED_DATA_DIR", "data/isolated")) / "live_feed"
        self.queue = queue.Queue(maxsize=_positive_int_env("ISOLATED_LIVE_FEED_QUEUE_SIZE", 20000))
        self.lock = threading.RLock()
        self.feed = None
        self.listener = None
        self.writer = None
        self.stop_event = threading.Event()
        self.metadata: dict[str, dict] = {}
        self.subscribed: set[str] = set()
        self.dropped = 0
        self.state = {"enabled": True, "state": "not_started"}

    def start(self, record: dict, feed=None) -> dict:
        if os.getenv("ISOLATED_LIVE_FEED_ENABLED", "true").strip().lower() != "true":
            return {"enabled": False, "state": "disabled"}
        feed = feed or get_feed_service()
        if feed is None or not feed.get_health().get("running"):
            self.state = {"enabled": True, "state": "waiting_for_primary_feed"}
            logger.warning("Isolated live-feed capture deferred: primary market feed is not running")
            return dict(self.state)

        trade_date = str(record.get("trade_date_ist") or datetime.now(IST).date().isoformat())
        candidates = [
            ("isolated", record.get("instrument_key"), record.get("trading_symbol")),
            ("opposite", (record.get("opposite_leg") or {}).get("instrument_key"),
             (record.get("opposite_leg") or {}).get("trading_symbol")),
        ]
        for key in INSTRUMENT_KEYS:
            meta = INDEX_METADATA.get(key, {})
            candidates.append((f"main_index_{meta.get('canonical', 'unknown').lower()}",
                               key, meta.get("trading_symbol", key)))

        desired = {}
        for role, key, symbol in candidates:
            if not key:
                continue
            desired[key] = {
                "role": role,
                "instrument_key": key,
                "trading_symbol": symbol or key,
                "trade_date_ist": trade_date,
            }

        with self.lock:
            # Preserve metadata from previous active isolation roles on same day.
            self.metadata.update(desired)
            if self.feed is not feed:
                if self.feed is not None and self.listener is not None:
                    try:
                        self.feed.remove_message_listener(self.listener)
                    except Exception:
                        logger.exception("Could not detach old isolated live-feed listener")
                self.feed = feed
                self.listener = self._on_message
                try:
                    feed.add_message_listener(self.listener)
                except Exception:
                    logger.exception("Could not register isolated live-feed listener")
                    self.state = {"enabled": True, "state": "listener_registration_failed"}
                    return dict(self.state)

            self._ensure_writer()

        # Subscribe only keys not already present in the shared feed. This
        # service does not own the base index subscriptions or existing legs.
        try:
            existing = set(feed.instrument_keys)
            new_keys = [key for key in desired if key not in existing]
            added = set()
            for offset in range(0, len(new_keys), 100):
                added.update(feed.subscribe(new_keys[offset:offset + 100]))
            self.subscribed.update(added)
            missing = [key for key in desired if key not in set(feed.instrument_keys)]
            self.state = {
                "enabled": True,
                "state": "running" if not missing else "partially_subscribed",
                "trade_date_ist": trade_date,
                "requested_instruments": len(desired),
                "subscribed_instruments": len(desired) - len(missing),
                "missing_instrument_keys": missing,
                "file_directory": str(self.root),
            }
            logger.info(
                "Isolated live-feed capture %s: instruments=%d missing=%d",
                self.state["state"], len(desired), len(missing),
            )
        except Exception:
            logger.exception("Isolated live-feed subscription failed; isolation remains active")
            self.state = {"enabled": True, "state": "subscription_failed"}
        return dict(self.state)

    def _ensure_writer(self):
        if self.writer and self.writer.is_alive():
            return
        self.stop_event.clear()
        self.writer = threading.Thread(
            target=self._writer_loop, name="isolated-live-feed-writer", daemon=True
        )
        self.writer.start()

    def _on_message(self, feeds: dict):
        received = datetime.now(IST).isoformat()
        with self.lock:
            for key, payload in feeds.items():
                meta = self.metadata.get(key)
                if not meta:
                    continue
                row = {
                    "received_at_ist": received,
                    "trade_date_ist": meta["trade_date_ist"],
                    "role": meta["role"],
                    "instrument_key": key,
                    "trading_symbol": meta["trading_symbol"],
                    "payload": payload,
                }
                try:
                    self.queue.put_nowait((meta, row))
                except queue.Full:
                    self.dropped += 1
                    if self.dropped == 1 or self.dropped % 1000 == 0:
                        logger.error("Isolated live-feed writer queue full; dropped=%d", self.dropped)

    def _writer_loop(self):
        while not self.stop_event.is_set() or not self.queue.empty():
            try:
                meta, row = self.queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                day = str(meta["trade_date_ist"]).replace("-", "")
                folder = self.root / day
                folder.mkdir(parents=True, exist_ok=True)
                filename = f"{meta['role']}_{_safe(meta['instrument_key'])}.jsonl"
                with (folder / filename).open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
            except Exception:
                logger.exception("Could not write isolated live-feed record")
            finally:
                self.queue.task_done()

    def status(self):
        result = dict(self.state)
        result["queued_rows"] = self.queue.qsize()
        result["dropped_rows"] = self.dropped
        return result

    def stop(self):
        with self.lock:
            if self.feed is not None and self.listener is not None:
                try:
                    self.feed.remove_message_listener(self.listener)
                except Exception:
                    logger.exception("Could not detach isolated live-feed listener")
            self.listener = None
            self.feed = None
        self.stop_event.set()
        if self.writer and self.writer.is_alive():
            self.writer.join(timeout=5)
        self.writer = None


_capture = IsolatedLiveFeedCapture()


def start_isolated_live_feed(record: dict, feed=None) -> dict:
    try:
        return _capture.start(record, feed)
    except Exception:
        logger.exception("Isolated live-feed capture failed; continuing isolation workflow")
        return {"enabled": True, "state": "error"}


def get_isolated_live_feed_status() -> dict:
    try:
        return _capture.status()
    except Exception:
        logger.exception("Could not read isolated live-feed capture status")
        return {"enabled": True, "state": "status_error"}


def stop_isolated_live_feed() -> None:
    try:
        _capture.stop()
    except Exception:
        logger.exception("Isolated live-feed capture shutdown failed")
