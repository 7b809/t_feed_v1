"""Read-only access to the upstream project's daily performance snapshot.

This service never writes to MongoDB. The upstream trading engine owns
creation and updates of UPSTOX_APP_V2.dummy_orders documents; this app
only reads the document whose _id matches today's market-local date.
"""
from __future__ import annotations

import base64
import logging
import math
import json
import re
import threading
from datetime import date, datetime, time, timezone
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
        # PyMongo decodes BSON datetimes as naive UTC unless tz_aware is set.
        # Preserve that UTC meaning so browser rendering does not shift it
        # as though it were already in the viewer's local timezone.
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
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
                        tz_aware=True,
                        tzinfo=timezone.utc,
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


    @staticmethod
    def _extract_orders(orders_data: Any) -> list[dict[str, Any]]:
        """Normalize supported daily-document order slots into one list."""
        if not isinstance(orders_data, dict):
            return []

        candidates: list[Any] = []
        listed = orders_data.get("orders")
        if isinstance(listed, list):
            candidates.extend(listed)

        for key in ("order", "post_1130_order"):
            item = orders_data.get(key)
            if isinstance(item, dict):
                candidates.append(item)

        output: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in candidates:
            if not isinstance(item, dict):
                continue
            safe = _json_safe(item)
            identity = str(
                safe.get("order_id")
                or safe.get("trade_id")
                or json.dumps(safe, sort_keys=True, separators=(",", ":"))
            )
            if identity in seen:
                continue
            seen.add(identity)
            output.append(safe)
        return output

    @staticmethod
    def _orders_summary(orders: list[dict[str, Any]]) -> dict[str, Any]:
        open_count = 0
        closed_count = 0
        realised_points = 0.0
        for order in orders:
            status = str(
                order.get("status")
                or ("CLOSED" if order.get("closed_at") or order.get("closed_time") else "OPEN")
            ).upper()
            is_open = status in {"OPEN", "ACTIVE", "PENDING"}
            if is_open:
                open_count += 1
            else:
                closed_count += 1
                try:
                    value = float(
                        order.get("closed_profit_pts")
                        if order.get("closed_profit_pts") is not None
                        else order.get("pnl_points", 0)
                    )
                    if math.isfinite(value):
                        realised_points += value
                except (TypeError, ValueError):
                    pass
        return {
            "total_orders": len(orders),
            "open_orders": open_count,
            "closed_orders": closed_count,
            "realised_points": round(realised_points, 4),
        }

    def get_today_orders(self) -> dict[str, Any]:
        """Read today's saved dummy-order document and normalize its orders."""
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
            return {"status": "disabled", "orders": [], **base}
        if not config.MONGO_URI:
            return {
                "status": "unavailable",
                "orders": [],
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
                    "orders_data": 1,
                    "created_datetime": 1,
                    "last_updated_time": 1,
                },
            )
            if not document:
                return {"status": "not_found", "orders": [], **base}

            orders = self._extract_orders(document.get("orders_data"))
            return {
                "status": "found",
                "orders": orders,
                "summary": self._orders_summary(orders),
                "document": {
                    "created_datetime": _json_safe(document.get("created_datetime")),
                    "last_updated_time": _json_safe(document.get("last_updated_time")),
                },
                **base,
            }
        except Exception as exc:
            logger.warning(
                "[orders-viewer] today's orders lookup failed date=%s error=%s",
                today,
                type(exc).__name__,
            )
            return {
                "status": "unavailable",
                "orders": [],
                "error": "Today's order data is temporarily unavailable.",
                **base,
            }

    def get_order_history(
        self,
        page: int = 1,
        page_size: int = 20,
        search: str | None = None,
    ) -> dict[str, Any]:
        """Paginate flattened order records across daily documents."""
        page = max(1, int(page))
        page_size = max(1, min(100, int(page_size)))
        base = {
            "page": page,
            "page_size": page_size,
            "source": {
                "database": config.PERFORMANCE_SNAPSHOT_MONGO_DB,
                "collection": config.PERFORMANCE_SNAPSHOT_COLLECTION,
                "mode": "read_only",
            },
        }
        if not config.PERFORMANCE_SNAPSHOT_ENABLED:
            return {"status": "disabled", "items": [], "total": 0, **base}
        if not config.MONGO_URI:
            return {
                "status": "unavailable",
                "items": [],
                "total": 0,
                "error": "MongoDB connection is not configured.",
                **base,
            }

        try:
            collection = self._mongo()[config.PERFORMANCE_SNAPSHOT_MONGO_DB][
                config.PERFORMANCE_SNAPSHOT_COLLECTION
            ]
            pipeline: list[dict[str, Any]] = [
                {
                    "$project": {
                        "trading_date": "$_id",
                        "document_created_at": "$created_datetime",
                        "document_updated_at": "$last_updated_time",
                        "_orders": {
                            "$setUnion": [
                                {
                                    "$cond": [
                                        {"$isArray": "$orders_data.orders"},
                                        "$orders_data.orders",
                                        [],
                                    ]
                                },
                                {
                                    "$cond": [
                                        {"$eq": [{"$type": "$orders_data.order"}, "object"]},
                                        ["$orders_data.order"],
                                        [],
                                    ]
                                },
                                {
                                    "$cond": [
                                        {"$eq": [{"$type": "$orders_data.post_1130_order"}, "object"]},
                                        ["$orders_data.post_1130_order"],
                                        [],
                                    ]
                                },
                            ]
                        },
                    }
                },
                {"$unwind": "$_orders"},
            ]

            query = (search or "").strip()[:100]
            if query:
                pattern = re.escape(query)
                pipeline.append(
                    {
                        "$match": {
                            "$or": [
                                {"_orders.trading_symbol": {"$regex": pattern, "$options": "i"}},
                                {"_orders.instrument_key": {"$regex": pattern, "$options": "i"}},
                                {"_orders.status": {"$regex": pattern, "$options": "i"}},
                                {"_orders.order_status": {"$regex": pattern, "$options": "i"}},
                                {"_orders.order_id": {"$regex": pattern, "$options": "i"}},
                                {"_orders.strategy": {"$regex": pattern, "$options": "i"}},
                                {"_orders.execution_path": {"$regex": pattern, "$options": "i"}},
                            ]
                        }
                    }
                )

            count_result = list(
                collection.aggregate(pipeline + [{"$count": "total"}], allowDiskUse=True)
            )
            total = int(count_result[0]["total"]) if count_result else 0
            data_pipeline = pipeline + [
                {
                    "$sort": {
                        "trading_date": -1,
                        "_orders.entry_time": -1,
                        "_orders.created_at": -1,
                    }
                },
                {"$skip": (page - 1) * page_size},
                {"$limit": page_size},
                {
                    "$project": {
                        "_id": 0,
                        "trading_date": 1,
                        "document_created_at": 1,
                        "document_updated_at": 1,
                        "order": "$_orders",
                    }
                },
            ]
            items = [
                _json_safe(item)
                for item in collection.aggregate(data_pipeline, allowDiskUse=True)
            ]
            total_pages = math.ceil(total / page_size) if total else 0
            return {
                "status": "ok",
                "items": items,
                "total": total,
                "page": page,
                "page_size": page_size,
                "total_pages": total_pages,
                "has_previous": page > 1 and total > 0,
                "has_next": page < total_pages,
                **{"source": base["source"]},
            }
        except Exception as exc:
            logger.warning(
                "[orders-viewer] order history lookup failed page=%s error=%s",
                page,
                type(exc).__name__,
            )
            return {
                "status": "unavailable",
                "items": [],
                "total": 0,
                "page": page,
                "page_size": page_size,
                "error": "Order history is temporarily unavailable.",
                **{"source": base["source"]},
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
