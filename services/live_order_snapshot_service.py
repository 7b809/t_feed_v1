"""Persist combined live isolation and dummy-order state to MongoDB.

One document is maintained per market-local trading date in the
configured collection (default: UPSTOX_APP_V2.dummy_orders).

The local JSON files remain the primary runtime/recovery state. This
service asynchronously mirrors their latest contents to MongoDB so
filesystem writes and market-data processing never wait on database I/O.
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
    """Debounced, asynchronous daily snapshot writer."""

    def __init__(self) -> None:
        self._client: MongoClient | None = None
        self._collection = None
        self._client_lock = threading.Lock()
        self._worker_lock = threading.Lock()
        self._requested = threading.Event()
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
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(
                    target=self._worker_loop,
                    name="live-order-mongo-snapshot",
                    daemon=True,
                )
                self._worker.start()

        self._requested.set()

    def _worker_loop(self) -> None:
        debounce = max(
            0.1,
            float(getattr(
                config, "LIVE_ORDER_SNAPSHOT_DEBOUNCE_SECONDS", 0.5
            )),
        )
        while True:
            self._requested.wait()
            self._requested.clear()

            # Trailing-edge debounce: merge bursts of isolation/order writes
            # and snapshot the newest complete pair of files only once.
            while self._requested.wait(timeout=debounce):
                self._requested.clear()

            try:
                self._persist_latest()
            except Exception as exc:
                now = time.monotonic()
                if now - self._last_error_log_at >= 60:
                    logger.exception(
                        "[live-order-snapshot] MongoDB write failed: %s",
                        exc,
                    )
                    self._last_error_log_at = now

    def _persist_latest(self) -> None:
        isolation_path, orders_path = self._source_paths()
        isolated_data = self._read_json(isolation_path)
        orders_data = self._read_json(orders_path)

        # Do not create an empty daily record before either runtime source
        # has produced data. Missing one source does not erase its prior value.
        if isolated_data is None and orders_data is None:
            return

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
            return

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


live_order_snapshot_service = LiveOrderSnapshotService()
