"""Read-only access to the upstream project's daily performance snapshot.

This service never writes to MongoDB. The upstream trading engine owns
creation and updates of UPSTOX_APP_V2.dummy_orders documents; this app
only reads the document whose _id matches today's market-local date.
"""
from __future__ import annotations

import base64
import logging
import math
import threading
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from bson import Decimal128, ObjectId
from pymongo import MongoClient

from core import config

logger = logging.getLogger(__name__)


def _market_timezone() -> ZoneInfo:
    try:
        return ZoneInfo(config.MARKET_TIMEZONE)
    except Exception:
        return ZoneInfo("Asia/Kolkata")


def _json_safe(value: Any) -> Any:
    """Convert BSON/Python values into values safe for a JSON response."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, Decimal128):
        return float(value.to_decimal())
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


class PerformanceSnapshotService:
    """Fetch today's upstream snapshot without mutating source data."""

    def __init__(self) -> None:
        self._client: MongoClient | None = None
        self._client_lock = threading.Lock()

    def _mongo(self) -> MongoClient:
        if self._client is None:
            with self._client_lock:
                if self._client is None:
                    if not config.PERFORMANCE_SNAPSHOT_ENABLED:
                        raise RuntimeError("Performance snapshot reading is disabled")
                    if not config.MONGO_URI:
                        raise RuntimeError("MONGO_URL/MONGO_URI is not configured")
                    self._client = MongoClient(
                        config.MONGO_URI,
                        serverSelectionTimeoutMS=config.PERFORMANCE_SNAPSHOT_CONNECT_TIMEOUT_MS,
                        connectTimeoutMS=config.PERFORMANCE_SNAPSHOT_CONNECT_TIMEOUT_MS,
                        socketTimeoutMS=config.PERFORMANCE_SNAPSHOT_CONNECT_TIMEOUT_MS,
                        retryReads=True,
                        appname="NG7-Performance-Viewer",
                    )
        return self._client

    @staticmethod
    def _market_context(now: datetime) -> dict[str, Any]:
        is_weekday = now.weekday() < 5
        current_time = now.time().replace(tzinfo=None)
        if not is_weekday:
            state = "weekend"
            label = "Weekend"
        elif current_time < time(9, 0):
            state = "pre_market"
            label = "Before market hours"
        elif current_time < time(15, 30):
            state = "market_hours"
            label = "Market hours"
        else:
            state = "after_market"
            label = "After market hours"
        return {
            "state": state,
            "label": label,
            "is_weekday": is_weekday,
            "timezone": config.MARKET_TIMEZONE,
            "checked_at": now.isoformat(),
        }

    def get_today_snapshot(self) -> dict[str, Any]:
        """Read only the document keyed by today's YYYY-MM-DD date."""
        now = datetime.now(_market_timezone())
        today = now.date().isoformat()
        base = {
            "date": today,
            "market": self._market_context(now),
            "source": {
                "database": config.PERFORMANCE_SNAPSHOT_MONGO_DB,
                "collection": config.PERFORMANCE_SNAPSHOT_COLLECTION,
                "mode": "read_only",
            },
            "checked_at": now.isoformat(),
        }

        if not config.PERFORMANCE_SNAPSHOT_ENABLED:
            return {"status": "disabled", **base}
        if not config.MONGO_URI:
            return {
                "status": "unavailable",
                "error": "MongoDB connection is not configured.",
                **base,
            }

        try:
            collection = self._mongo()[config.PERFORMANCE_SNAPSHOT_MONGO_DB][
                config.PERFORMANCE_SNAPSHOT_COLLECTION
            ]
            document = collection.find_one(
                {"_id": today},
                {
                    "_id": 1,
                    "isolated_instrument": 1,
                    "orders_data": 1,
                    "created_datetime": 1,
                    "last_updated_time": 1,
                },
            )
            if not document:
                return {"status": "not_found", **base}

            safe_document = _json_safe(document)
            return {
                "status": "found",
                "snapshot": safe_document,
                **base,
            }
        except Exception as exc:
            logger.warning(
                "[performance-viewer] today's snapshot lookup failed date=%s error=%s",
                today,
                type(exc).__name__,
            )
            return {
                "status": "unavailable",
                "error": (
                    "The upstream snapshot store is temporarily unavailable. "
                    "The live index watch remains available."
                ),
                **base,
            }

    def close(self) -> None:
        """Close the read-only MongoDB connection during app shutdown."""
        with self._client_lock:
            if self._client is not None:
                try:
                    self._client.close()
                finally:
                    self._client = None


performance_snapshot_service = PerformanceSnapshotService()
