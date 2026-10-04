"""
MongoDB persistence for placed orders.

Pattern is ported from the reference `order_saving_service.py`: one
MongoDB document per day, keyed by `YYYY-MM-DD`, with individual orders
stored as a sub-document keyed by `HH_MM_SS` (uniquified with a
numeric suffix when the same second repeats).

Orders carry `alert_key` — a cross-reference into the `alerts.<key>`
sub-document of the matching day in the `ema_isolated_alerts`
collection.

The write path is fail-open by default — a failure to save never
interrupts alert flow.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from threading import Lock
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pymongo import MongoClient
from pymongo.errors import PyMongoError

from core.logger import get_logger
from ema_app.isolation.config import isolation_config

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _get_market_timezone() -> ZoneInfo:
    try:
        name = str(getattr(__import__("core.config", fromlist=["config"]).config,
                           "MARKET_TIMEZONE", "Asia/Kolkata") or "Asia/Kolkata").strip()
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, Exception):
        return ZoneInfo("Asia/Kolkata")


def _get_mongo_uri() -> str:
    try:
        from core.config import core_config  # type: ignore

        return str(
            getattr(core_config, "MONGO_URI", None)
            or getattr(core_config, "MONGO_URL", "")
            or ""
        ).strip()
    except Exception:
        return ""


def _get_database_name() -> str:
    try:
        from core.config import core_config  # type: ignore

        return str(getattr(core_config, "MONGO_DB_NAME", "") or "").strip()
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------
class IsolatedOrderStorage:
    def __init__(self) -> None:
        self._client: Optional[MongoClient] = None
        self._database = None
        self._collection = None
        self._lock = Lock()
        self._indexes_ready = False

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------
    def _get_collection(self):
        if self._collection is not None:
            return self._collection

        with self._lock:
            if self._collection is not None:
                return self._collection

            if not isolation_config.storage_enabled:
                return None

            uri = _get_mongo_uri()
            db_name = _get_database_name()
            coll_name = isolation_config.storage_collection

            if not uri or not db_name:
                logger.error(
                    "Isolated order storage unavailable: MONGO_URI=%s MONGO_DB_NAME=%s",
                    bool(uri), bool(db_name),
                )
                return None

            try:
                self._client = MongoClient(
                    uri,
                    serverSelectionTimeoutMS=5000,
                    connectTimeoutMS=5000,
                    socketTimeoutMS=10000,
                )
                self._client.admin.command("ping")
                self._database = self._client[db_name]
                self._collection = self._database[coll_name]

                self._initialize_indexes()
                logger.info(
                    "Isolated order storage ready | db=%s | collection=%s",
                    db_name, coll_name,
                )
                return self._collection

            except Exception as exc:
                logger.exception(
                    "Isolated order storage init failed | error=%s", type(exc).__name__
                )
                self._client = None
                self._database = None
                self._collection = None
                return None

    def _initialize_indexes(self) -> None:
        if self._collection is None or self._indexes_ready:
            return
        try:
            self._collection.create_index(
                [("updated_at", -1)], name="idx_iso_order_updated_at"
            )
            self._collection.create_index(
                [("instrument_key", 1), ("order_status", 1)],
                name="idx_iso_order_instrument_status",
            )
            self._indexes_ready = True
        except PyMongoError as exc:
            logger.warning("Failed to create isolated order indexes | err=%s", exc)

    # ------------------------------------------------------------------
    # Document helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _daily_doc_id(now: datetime) -> str:
        return now.strftime("%Y-%m-%d")

    @staticmethod
    def _time_key(now: datetime) -> str:
        return now.strftime("%H_%M_%S")

    def _unique_order_key(self, daily_doc_id: str, base: str) -> str:
        collection = self._get_collection()
        if collection is None:
            return base

        key = base
        counter = 0
        while True:
            existing = collection.find_one(
                {"_id": daily_doc_id, f"orders.{key}": {"$exists": True}},
                {"_id": 1},
            )
            if existing is None:
                return key
            counter += 1
            key = f"{base}_{counter}"

    # ------------------------------------------------------------------
    # Document builder
    # ------------------------------------------------------------------
    def _build_order_document(
        self,
        *,
        payload: Dict[str, Any],
        placement_result: Dict[str, Any],
        order: Dict[str, Any],
        alert_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        now = datetime.now(tz=_get_market_timezone())

        instrument = payload.get("instrument") or {}
        if not isinstance(instrument, dict):
            instrument = {}

        ema_block = payload.get("ema") or {}
        if not isinstance(ema_block, dict):
            ema_block = {}

        dup = payload.get("duplicate_control") or {}
        if not isinstance(dup, dict):
            dup = {}

        budget_inst = (
            order.get("budget_instrument") or order.get("selected_instrument") or {}
        )
        if not isinstance(budget_inst, dict):
            budget_inst = {}

        return {
            # ---- Cross-reference into the alerts document ---------------
            # `alert_key` matches a key under `alerts.<key>` in the
            # same-day document in the alert collection. Read via
            # `GET /ema-app/isolation/alerts/{date}`.
            "alert_key": alert_key,

            "event_id": payload.get("event_id"),
            "event_type": payload.get("event_type"),
            "source": payload.get("source"),

            "mode": placement_result.get("mode"),
            "order_id": self._extract_order_id(order),
            "instrument_key": instrument.get("instrument_key")
            or budget_inst.get("instrument_key"),
            "trading_symbol": instrument.get("trading_symbol")
            or budget_inst.get("trading_symbol"),

            "instrument": deepcopy(instrument),
            "budget_instrument": deepcopy(budget_inst),

            "order_status": "SUCCESS" if order.get("success") else "FAILED",
            "success": bool(order.get("success")),
            "error": order.get("error"),

            "place_order_request": order.get("request"),
            "place_order_response": order.get("response"),

            # Explicit, queryable transaction side (used for counters)
            "transaction_type": order.get("transaction_type"),

            "cross_type": ema_block.get("cross_type"),
            "direction": dup.get("direction"),
            "minute_alert_key": dup.get("minute_alert_key"),

            "payload_metadata": {
                "event_id": payload.get("event_id"),
                "event_type": payload.get("event_type"),
                "source": payload.get("source"),
                "market": payload.get("market"),
                "timezone": payload.get("timezone"),
                "created_at": payload.get("created_at"),
                "cross_type": ema_block.get("cross_type"),
                "direction": dup.get("direction"),
                "minute_alert_key": dup.get("minute_alert_key"),
            },

            "order_result": deepcopy(order),
            "placement_result": deepcopy(placement_result),

            "created_at": now,
            "updated_at": now,
            "created_at_ist": now.strftime("%Y-%m-%d %H:%M:%S %Z"),
            "updated_at_ist": now.strftime("%Y-%m-%d %H:%M:%S %Z"),
        }

    @staticmethod
    def _extract_order_id(order: Dict[str, Any]) -> Optional[str]:
        response = order.get("response")
        if not isinstance(response, dict):
            return None

        def _walk(d: Dict[str, Any]) -> Optional[str]:
            direct = (
                d.get("order_id")
                or d.get("orderId")
                or d.get("id")
                or d.get("_order_id")
            )
            if direct:
                return str(direct)
            for nested_key in ("data", "_data", "response", "result"):
                nested = d.get(nested_key)
                if isinstance(nested, dict):
                    found = _walk(nested)
                    if found:
                        return found
            return None

        return _walk(response)

    # ------------------------------------------------------------------
    # Public save
    # ------------------------------------------------------------------
    def save_order(
        self,
        *,
        payload: Dict[str, Any],
        placement_result: Dict[str, Any],
        order: Dict[str, Any],
        alert_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        result = {
            "success": False,
            "saved": False,
            "skipped": False,
            "document_id": None,
            "order_key": None,
            "error": None,
        }

        if not isolation_config.storage_enabled:
            result["skipped"] = True
            result["error"] = "Storage disabled."
            return result

        if not isinstance(payload, dict) or not isinstance(order, dict):
            result["error"] = "Invalid payload/order types."
            return result

        collection = self._get_collection()
        if collection is None:
            result["error"] = "MongoDB collection unavailable."
            return result

        try:
            now = datetime.now(tz=_get_market_timezone())
            daily_doc_id = self._daily_doc_id(now)
            base_key = self._time_key(now)
            order_key = self._unique_order_key(daily_doc_id, base_key)

            document = self._build_order_document(
                payload=payload,
                placement_result=placement_result,
                order=order,
                alert_key=alert_key,
            )

            # Derive per-day counter increments from the order itself.
            is_success = bool(order.get("success"))
            txn_type = str(order.get("transaction_type") or "").upper()
            is_buy = txn_type == "BUY"
            is_sell = txn_type == "SELL"

            update_result = collection.update_one(
                {"_id": daily_doc_id},
                {
                    "$set": {
                        f"orders.{order_key}": document,
                        "updated_at": now,
                        "updated_at_ist": now.strftime("%Y-%m-%d %H:%M:%S %Z"),
                    },
                    "$inc": {
                        "counts.total":   1,
                        "counts.success": 1 if is_success else 0,
                        "counts.failed":  0 if is_success else 1,
                        "counts.buy":     1 if is_buy else 0,
                        "counts.sell":    1 if is_sell else 0,
                    },
                    "$setOnInsert": {
                        "date": daily_doc_id,
                        "timezone": str(_get_market_timezone()),
                        "created_at": now,
                        "created_at_ist": now.strftime("%Y-%m-%d %H:%M:%S %Z"),
                    },
                },
                upsert=True,
            )

            result.update(
                {
                    "success": True,
                    "saved": True,
                    "document_id": daily_doc_id,
                    "order_key": order_key,
                    "created": update_result.upserted_id is not None,
                    "updated": update_result.upserted_id is None,
                }
            )
            logger.info(
                "Isolated order saved | doc=%s | key=%s | alert_key=%s | event_id=%s | order_id=%s",
                daily_doc_id, order_key, alert_key,
                payload.get("event_id"), document.get("order_id"),
            )
            return result

        except PyMongoError as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
            logger.exception("Isolated order save failed (PyMongo).")
            if not isolation_config.storage_fail_open:
                raise
            return result

        except Exception as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
            logger.exception("Isolated order save failed (unexpected).")
            if not isolation_config.storage_fail_open:
                raise
            return result

    def close(self) -> None:
        with self._lock:
            if self._client is not None:
                try:
                    self._client.close()
                except Exception:
                    logger.exception("Failed to close isolated order Mongo client.")
            self._client = None
            self._database = None
            self._collection = None
            self._indexes_ready = False


isolated_order_storage = IsolatedOrderStorage()


__all__ = ["IsolatedOrderStorage", "isolated_order_storage"]