"""Persist combined live isolation and dummy-order state to MongoDB.

One document is maintained per market-local trading date. Local JSON
files remain the primary runtime/recovery state; this service mirrors
their latest contents asynchronously and retries transient MongoDB errors.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from pymongo import MongoClient

from core import config
from core.logger import get_logger

logger = get_logger(__file__)


class LiveOrderSnapshotService:
    """Debounced daily snapshot writer with retry and shutdown flushing."""

    def __init__(self) -> None:
        self._client: MongoClient | None = None
        self._collection = None
        self._client_lock = threading.Lock()
        self._worker_lock = threading.Lock()
        self._persist_lock = threading.Lock()
        self._requested = threading.Event()
        self._stop_requested = threading.Event()
        self._worker: threading.Thread | None = None
        self._last_signature: str | None = None
        self._last_document_id: str | None = None
        self._last_error_log_at = 0.0

    @staticmethod
    def _market_timezone() -> ZoneInfo:
        try:
            return ZoneInfo(config.MARKET_TIMEZONE)
        except Exception:
            return ZoneInfo("Asia/Kolkata")

    @classmethod
    def _now_market(cls) -> datetime:
        return datetime.now(cls._market_timezone())

    @staticmethod
    def _retry_initial_seconds() -> float:
        return max(
            0.1,
            float(getattr(
                config, "LIVE_ORDER_SNAPSHOT_RETRY_INITIAL_SECONDS", 1.0
            )),
        )

    @staticmethod
    def _retry_max_seconds() -> float:
        return max(
            LiveOrderSnapshotService._retry_initial_seconds(),
            float(getattr(
                config, "LIVE_ORDER_SNAPSHOT_RETRY_MAX_SECONDS", 30.0
            )),
        )

    def _source_paths(self) -> tuple[Path, Path]:
        root = Path(config.LIVE_DATA_DIR)
        isolation_path = root / str(
            getattr(config, "LEVEL_ISOLATION_FILENAME",
                    "level_isolated_instrument.json")
        )
        orders_path = root / str(
            getattr(config, "DUMMY_ORDER_FILENAME", "dummy_orders.json")
        )
        return isolation_path, orders_path

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any] | None:
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.exception(
                "[live-order-snapshot] failed reading source file: %s", path
            )
            return None
        if not isinstance(payload, dict):
            logger.warning(
                "[live-order-snapshot] source root is not an object: %s", path
            )
            return None
        return payload

    def _get_collection(self):
        if self._collection is not None:
            return self._collection

        with self._client_lock:
            if self._collection is not None:
                return self._collection

            mongo_uri = str(getattr(config, "MONGO_URI", "") or "").strip()
            database_name = str(
                getattr(config, "LIVE_ORDER_SNAPSHOT_MONGO_DB", "")
                or getattr(config, "DAILY_BACKUP_MONGO_DB", "")
                or getattr(config, "MONGO_DB", "")
            ).strip()
            collection_name = str(
                getattr(config, "LIVE_ORDER_SNAPSHOT_COLLECTION",
                        "dummy_orders")
            ).strip()

            if not mongo_uri:
                raise RuntimeError("MONGO_URL/MONGO_URI is not configured")
            if not database_name:
                raise RuntimeError(
                    "LIVE_ORDER_SNAPSHOT_MONGO_DB / DAILY_BACKUP_MONGO_DB "
                    "is not configured"
                )
            if not collection_name:
                raise RuntimeError(
                    "LIVE_ORDER_SNAPSHOT_COLLECTION cannot be empty"
                )

            self._client = MongoClient(
                mongo_uri,
                serverSelectionTimeoutMS=5000,
                connectTimeoutMS=5000,
            )
            self._collection = self._client[database_name][collection_name]
            logger.info(
                "[live-order-snapshot] MongoDB target ready: %s.%s",
                database_name,
                collection_name,
            )
            return self._collection

    def request_snapshot(self) -> None:
        """Queue a snapshot request; safe to call from service save paths."""
        if not bool(getattr(config, "LIVE_ORDER_SNAPSHOT_ENABLED", True)):
            return

        with self._worker_lock:
            if self._stop_requested.is_set():
                logger.warning(
                    "[live-order-snapshot] request ignored during shutdown"
                )
                return
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(
                    target=self._worker_loop,
                    name="live-order-mongo-snapshot",
                    daemon=True,
                )
                self._worker.start()

        self._requested.set()

    def _log_write_failure(self, exc: Exception) -> None:
        now = time.monotonic()
        if now - self._last_error_log_at >= 60:
            logger.exception(
                "[live-order-snapshot] MongoDB write failed; retry pending: %s",
                exc,
            )
            self._last_error_log_at = now

    def _worker_loop(self) -> None:
        debounce = max(
            0.1,
            float(getattr(
                config, "LIVE_ORDER_SNAPSHOT_DEBOUNCE_SECONDS", 0.5
            )),
        )
        retry_delay = self._retry_initial_seconds()

        while not self._stop_requested.is_set():
            if not self._requested.wait(timeout=0.5):
                continue
            self._requested.clear()

            # Trailing-edge debounce merges rapid isolation/order writes.
            while self._requested.wait(timeout=debounce):
                self._requested.clear()

            while not self._stop_requested.is_set():
                try:
                    with self._persist_lock:
                        self._persist_latest()
                    retry_delay = self._retry_initial_seconds()
                    break
                except Exception as exc:
                    self._log_write_failure(exc)
                    # A new source update wakes the retry early. Otherwise
                    # retry with capped exponential backoff until successful.
                    self._requested.wait(timeout=retry_delay)
                    self._requested.clear()
                    retry_delay = min(
                        retry_delay * 2.0, self._retry_max_seconds()
                    )

        logger.info("[live-order-snapshot] background worker stopped")

    def _persist_latest(self) -> bool:
        isolation_path, orders_path = self._source_paths()
        isolated_data = self._read_json(isolation_path)
        orders_data = self._read_json(orders_path)

        # Do not create an empty daily record before either source has data.
        # If one source is temporarily absent/unreadable, do not erase its
        # existing MongoDB value.
        if isolated_data is None and orders_data is None:
            logger.debug(
                "[live-order-snapshot] no source files available; nothing to save"
            )
            return False

        now = self._now_market()
        date_id = now.date().isoformat()

        values: dict[str, Any] = {}
        if isolated_data is not None:
            values["isolated_instrument"] = isolated_data
        if orders_data is not None:
            values["orders_data"] = orders_data

        signature = json.dumps(
            values, sort_keys=True, separators=(",", ":"), default=str
        )
        if (
            date_id == self._last_document_id
            and signature == self._last_signature
        ):
            return True

        collection = self._get_collection()
        collection.update_one(
            {"_id": date_id},
            {
                "$set": {
                    **values,
                    "last_updated_time": now,
                },
                "$setOnInsert": {
                    "created_datetime": now,
                },
            },
            upsert=True,
        )

        self._last_document_id = date_id
        self._last_signature = signature
        logger.info(
            "[live-order-snapshot] saved date=%s fields=%s",
            date_id,
            ",".join(sorted(values)),
        )
        return True

    def flush(self, timeout_seconds: float | None = None) -> bool:
        """Synchronously persist the newest files, retrying until timeout.

        Call this after stopping feed/scheduler producers during application
        shutdown. Returns False if the final snapshot could not be confirmed.
        """
        if not bool(getattr(config, "LIVE_ORDER_SNAPSHOT_ENABLED", True)):
            logger.info("[live-order-snapshot] disabled; shutdown flush skipped")
            return True

        if timeout_seconds is None:
            timeout_seconds = float(getattr(
                config, "LIVE_ORDER_SNAPSHOT_SHUTDOWN_TIMEOUT_SECONDS", 20.0
            ))
        timeout_seconds = max(0.1, float(timeout_seconds))
        deadline = time.monotonic() + timeout_seconds
        retry_delay = self._retry_initial_seconds()

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.error(
                    "[live-order-snapshot] final flush timed out after %.1fs",
                    timeout_seconds,
                )
                return False

            acquired = self._persist_lock.acquire(timeout=remaining)
            if not acquired:
                logger.error(
                    "[live-order-snapshot] final flush could not acquire "
                    "persistence lock before timeout"
                )
                return False

            try:
                self._persist_latest()
                logger.info("[live-order-snapshot] final shutdown flush complete")
                return True
            except Exception as exc:
                self._log_write_failure(exc)
            finally:
                self._persist_lock.release()

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                continue
            time.sleep(min(retry_delay, remaining))
            retry_delay = min(
                retry_delay * 2.0, self._retry_max_seconds()
            )

    def shutdown(self, timeout_seconds: float | None = None) -> bool:
        """Flush latest state, stop worker, then close MongoDB connection."""
        if timeout_seconds is None:
            timeout_seconds = float(getattr(
                config, "LIVE_ORDER_SNAPSHOT_SHUTDOWN_TIMEOUT_SECONDS", 20.0
            ))

        flushed = self.flush(timeout_seconds=timeout_seconds)
        self._stop_requested.set()
        self._requested.set()

        worker = self._worker
        if worker is not None and worker.is_alive():
            worker.join(timeout=max(0.1, float(timeout_seconds)))

        worker_stopped = worker is None or not worker.is_alive()
        if not worker_stopped:
            logger.warning(
                "[live-order-snapshot] worker did not stop within %.1fs",
                timeout_seconds,
            )

        if worker_stopped and self._client is not None:
            try:
                self._client.close()
            except Exception:
                logger.exception(
                    "[live-order-snapshot] MongoDB client close failed"
                )
            finally:
                self._client = None
                self._collection = None

        return flushed and worker_stopped


live_order_snapshot_service = LiveOrderSnapshotService()
