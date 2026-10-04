"""
MongoDB persistence for isolated EMA alerts.

Pattern mirrors `order_storage.py`: one MongoDB document per day,
keyed by `YYYY-MM-DD`, with individual alerts stored as a sub-document
keyed by `HH_MM_SS` (uniquified with a numeric suffix when the same
second repeats).

Written BEFORE orders are placed, so the order-storage layer can
cross-reference the alert via `alert_key`.

The write path is fail-open by default — a failure to save never
interrupts the alert flow.
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
        name = str(
            getattr(
                __import__("core.config", fromlist=["config"]).config,
                "MARKET_TIMEZONE",
                "Asia/Kolkata",
            )
            or "Asia/Kolkata"
        ).strip()
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
class IsolatedAlertStorage:
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

            if not isolation_config.alert_storage_enabled:
                return None

            uri = _get_mongo_uri()
            db_name = _get_database_name()
            coll_name = isolation_config.alert_storage_collection

            if not uri or not db_name:
                logger.error(
                    "Isolated alert storage unavailable: MONGO_URI=%s MONGO_DB_NAME=%s",
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
                    "Isolated alert storage ready | db=%s | collection=%s",
                    db_name, coll_name,
                )
                return self._collection

            except Exception as exc:
                logger.exception(
                    "Isolated alert storage init failed | error=%s",
                    type(exc).__name__,
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
                [("updated_at", -1)], name="idx_iso_alert_updated_at"
            )
            self._indexes_ready = True
        except PyMongoError as exc:
            logger.warning("Failed to create isolated alert indexes | err=%s", exc)

    # ------------------------------------------------------------------
    # Document helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _daily_doc_id(now: datetime) -> str:
        return now.strftime("%Y-%m-%d")

    @staticmethod
    def _time_key(now: datetime) -> str:
        return now.strftime("%H_%M_%S")

    def _unique_alert_key(self, daily_doc_id: str, base: str) -> str:
        collection = self._get_collection()
        if collection is None:
            return base

        key = base
        counter = 0
        while True:
            existing = collection.find_one(
                {"_id": daily_doc_id, f"alerts.{key}": {"$exists": True}},
                {"_id": 1},
            )
            if existing is None:
                return key
            counter += 1
            key = f"{base}_{counter}"

    # ------------------------------------------------------------------
    # Document builder
    # ------------------------------------------------------------------
    def _build_alert_document(
        self,
        *,
        payload: Dict[str, Any],
        isolated: Any,
        alert_key: str,
    ) -> Dict[str, Any]:
        now = datetime.now(tz=_get_market_timezone())

        instrument = payload.get("instrument") or {}
        if not isinstance(instrument, dict):
            instrument = {}

        ema_block = payload.get("ema") or {}
        if not isinstance(ema_block, dict):
            ema_block = {}

        opening_range = payload.get("opening_range") or {}
        if not isinstance(opening_range, dict):
            opening_range = {}

        dup = payload.get("duplicate_control") or {}
        if not isinstance(dup, dict):
            dup = {}

        market_snapshot = payload.get("market_snapshot") or {}
        if not isinstance(market_snapshot, dict):
            market_snapshot = {}

        isolated_snapshot: Dict[str, Any] = {}
        try:
            if isolated is not None and hasattr(isolated, "to_dict"):
                isolated_snapshot = isolated.to_dict()
        except Exception:
            isolated_snapshot = {}

        return {
            "alert_key": alert_key,

            # ---- Identity ------------------------------------------------
            "event_id": payload.get("event_id"),
            "event_type": payload.get("event_type"),
            "source": payload.get("source"),
            "schema_version": payload.get("schema_version"),

            # ---- Index / instrument (top-level, queryable) ---------------
            "index_name": market_snapshot.get("index_name") or instrument.get("underlying_symbol"),
            "instrument_key": instrument.get("instrument_key"),
            "trading_symbol": instrument.get("trading_symbol"),
            "strike_price": instrument.get("strike_price"),
            "option_type": instrument.get("option_type") or instrument.get("instrument_type"),
            "expiry": instrument.get("expiry"),
            "lot_size": instrument.get("lot_size"),

            # ---- Cross (top-level, queryable) ----------------------------
            "cross_type": ema_block.get("cross_type"),
            "direction": dup.get("direction"),
            "cross_time": (ema_block.get("candle") or {}).get("time"),
            "cross_time_iso": ema_block.get("timestamp"),
            "ema_calculation_mode": ema_block.get("calculation_mode"),

            "close": ema_block.get("price"),
            "ema_fast": ema_block.get("fast_value"),
            "ema_slow": ema_block.get("slow_value"),

            # ---- Opening range (compact copy) ----------------------------
            "opening_range": {
                "selected_level": opening_range.get("selected_level"),
                "selected_level_value": opening_range.get("selected_level_value"),
                "trigger_field": opening_range.get("trigger_field"),
                "trigger_price": opening_range.get("trigger_price"),
                "touch_time": opening_range.get("touch_time"),
                "touch_time_iso": opening_range.get("touch_time_iso"),
                "levels": deepcopy(opening_range.get("levels") or {}),
            },

            "minute_alert_key": dup.get("minute_alert_key"),

            # ---- Full payload snapshot (audit-of-record) -----------------
            "payload": deepcopy(payload),
            "isolated_snapshot": isolated_snapshot,

            # ---- Timestamps ---------------------------------------------
            "created_at": now,
            "created_at_ist": now.strftime("%Y-%m-%d %H:%M:%S %Z"),
        }

    # ------------------------------------------------------------------
    # Public save
    # ------------------------------------------------------------------
    def save_alert(
        self,
        *,
        payload: Dict[str, Any],
        isolated: Any = None,
    ) -> Dict[str, Any]:
        """
        Append the alert into today's daily document.

        Returns:
            {
                "success": bool,
                "saved": bool,
                "skipped": bool,
                "document_id": "YYYY-MM-DD" | None,
                "alert_key": "HH_MM_SS" | "HH_MM_SS_N" | None,
                "error": str | None,
            }

        On success the caller can pass `alert_key` into
        `isolated_order_storage.save_order(...)` for cross-referencing.
        """
        result = {
            "success": False,
            "saved": False,
            "skipped": False,
            "document_id": None,
            "alert_key": None,
            "error": None,
        }

        if not isolation_config.alert_storage_enabled:
            result["skipped"] = True
            result["error"] = "Alert storage disabled."
            return result

        if not isinstance(payload, dict):
            result["error"] = "Invalid payload type."
            return result

        collection = self._get_collection()
        if collection is None:
            result["error"] = "MongoDB collection unavailable."
            return result

        try:
            now = datetime.now(tz=_get_market_timezone())
            daily_doc_id = self._daily_doc_id(now)
            base_key = self._time_key(now)
            alert_key = self._unique_alert_key(daily_doc_id, base_key)

            document = self._build_alert_document(
                payload=payload,
                isolated=isolated,
                alert_key=alert_key,
            )

            direction = (document.get("direction") or "").strip().lower()
            is_bullish = direction == "bullish"
            is_bearish = direction == "bearish"

            update_result = collection.update_one(
                {"_id": daily_doc_id},
                {
                    "$set": {
                        f"alerts.{alert_key}": document,
                        "updated_at": now,
                        "updated_at_ist": now.strftime("%Y-%m-%d %H:%M:%S %Z"),
                    },
                    "$inc": {
                        "counts.total":   1,
                        "counts.bullish": 1 if is_bullish else 0,
                        "counts.bearish": 1 if is_bearish else 0,
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
                    "alert_key": alert_key,
                    "created": update_result.upserted_id is not None,
                    "updated": update_result.upserted_id is None,
                }
            )
            logger.info(
                "Isolated alert saved | doc=%s | key=%s | event_id=%s | instrument=%s | dir=%s",
                daily_doc_id, alert_key,
                payload.get("event_id"),
                document.get("instrument_key"),
                document.get("direction"),
            )
            return result

        except PyMongoError as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
            logger.exception("Isolated alert save failed (PyMongo).")
            if not isolation_config.alert_storage_fail_open:
                raise
            return result

        except Exception as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
            logger.exception("Isolated alert save failed (unexpected).")
            if not isolation_config.alert_storage_fail_open:
                raise
            return result

    def close(self) -> None:
        with self._lock:
            if self._client is not None:
                try:
                    self._client.close()
                except Exception:
                    logger.exception("Failed to close isolated alert Mongo client.")
            self._client = None
            self._database = None
            self._collection = None
            self._indexes_ready = False


isolated_alert_storage = IsolatedAlertStorage()


__all__ = ["IsolatedAlertStorage", "isolated_alert_storage"]