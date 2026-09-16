"""
Upstox Order Saving Service

Saves complete order workflow results into a separate MongoDB collection.

Environment variables:
    MONGO_URL
    MONGO_DB
    UPSTOX_ORDER_ENABLED
    UPSTOX_ORDER_COLLECTION

Default collection:
    upstox_orders
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from threading import Lock
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import PyMongoError

from core import config
from core.logger import get_logger

logger = get_logger(__file__)


class OrderSavingService:
    """
    Stores Upstox order workflow results in a dedicated MongoDB collection.

    Order saving is independent of isolated instrument event saving.
    Failures are returned as a result and do not crash the main workflow.
    """

    def __init__(self) -> None:
        self._client: MongoClient | None = None
        self._database = None
        self._collection = None
        self._connection_lock = Lock()
        self._indexes_initialized = False

        logger.info(
            "Order saving service initialized. enabled=%s, collection=%s",
            self._is_enabled(),
            self._get_collection_name(),
        )

    # ==========================================================
    # CONFIGURATION
    # ==========================================================

    def _is_enabled(self) -> bool:
        """
        Check whether order-result saving is enabled.
        """
        return bool(
            getattr(
                config,
                "UPSTOX_ORDER_ENABLED",
                True,
            )
        )

    def _get_mongo_uri(self) -> str:
        return str(
            getattr(
                config,
                "MONGO_URI",
                None,
            )
            or getattr(
                config,
                "MONGO_URL",
                "",
            )
            or ""
        ).strip()

    def _get_database_name(self) -> str:
        return str(
            getattr(
                config,
                "MONGO_DB",
                "",
            )
            or ""
        ).strip()

    def _get_collection_name(self) -> str:
        collection_name = str(
            getattr(
                config,
                "UPSTOX_ORDER_COLLECTION",
                "upstox_orders",
            )
            or "upstox_orders"
        ).strip()

        return collection_name or "upstox_orders"

    def _get_market_timezone(self) -> ZoneInfo:
        timezone_name = str(
            getattr(
                config,
                "MARKET_TIMEZONE",
                "Asia/Kolkata",
            )
            or "Asia/Kolkata"
        ).strip()

        try:
            return ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            logger.warning(
                "Invalid market timezone. Using Asia/Kolkata. timezone=%s",
                timezone_name,
            )
            return ZoneInfo("Asia/Kolkata")

    def _get_current_datetime(self) -> datetime:
        return datetime.now(tz=self._get_market_timezone())

    # ==========================================================
    # MONGODB CONNECTION
    # ==========================================================

    def _get_collection(self):
        """
        Lazily initialize the MongoDB collection.
        """

        if self._collection is not None:
            return self._collection

        with self._connection_lock:
            if self._collection is not None:
                return self._collection

            if not self._is_enabled():
                logger.info(
                    "Order result saving is disabled. collection=%s",
                    self._get_collection_name(),
                )
                return None

            mongo_uri = self._get_mongo_uri()
            database_name = self._get_database_name()
            collection_name = self._get_collection_name()

            if not mongo_uri:
                logger.error(
                    "Order result saving skipped: MONGO_URL is not configured."
                )
                return None

            if not database_name:
                logger.error("Order result saving skipped: MONGO_DB is not configured.")
                return None

            try:
                self._client = MongoClient(
                    mongo_uri,
                    serverSelectionTimeoutMS=5000,
                    connectTimeoutMS=5000,
                    socketTimeoutMS=10000,
                )

                # Check the connection immediately.
                self._client.admin.command("ping")

                self._database = self._client[database_name]
                self._collection = self._database[collection_name]

                self._initialize_indexes()

                logger.info(
                    "Order MongoDB collection initialized. "
                    "database=%s, collection=%s",
                    database_name,
                    collection_name,
                )

                return self._collection

            except Exception as exc:
                logger.exception(
                    "Failed to initialize order MongoDB collection. "
                    "database=%s, collection=%s, error_type=%s",
                    database_name,
                    collection_name,
                    type(exc).__name__,
                )

                self._client = None
                self._database = None
                self._collection = None

                return None

    def _initialize_indexes(self) -> None:
        """
        Create indexes for commonly queried fields.
        """

        if self._collection is None or self._indexes_initialized:
            return

        try:
            self._collection.create_index(
                [("event_id", ASCENDING)],
                name="idx_event_id",
            )

            self._collection.create_index(
                [("instrument_key", ASCENDING)],
                name="idx_instrument_key",
            )

            self._collection.create_index(
                [("created_at", DESCENDING)],
                name="idx_created_at",
            )

            self._collection.create_index(
                [("order_status", ASCENDING)],
                name="idx_order_status",
            )

            self._indexes_initialized = True

            logger.info("Order MongoDB indexes initialized.")

        except PyMongoError as exc:
            logger.warning(
                "Failed to initialize order MongoDB indexes. error=%s",
                exc,
            )

    # ==========================================================
    # DATA PREPARATION
    # ==========================================================

    @staticmethod
    def _get_instrument_details(
        payload: dict[str, Any],
        order_result: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Extract instrument details from the payload or order result.
        """

        selected_instrument = order_result.get("selected_instrument")

        if not isinstance(selected_instrument, dict):
            selected_instrument = {}

        payload_instrument = payload.get("instrument")

        if not isinstance(payload_instrument, dict):
            payload_instrument = {}

        return {
            "instrument_key": (
                selected_instrument.get("instrument_key")
                or payload_instrument.get("instrument_key")
            ),
            "trading_symbol": (
                selected_instrument.get("trading_symbol")
                or payload_instrument.get("trading_symbol")
            ),
            "live_ltp": (
                selected_instrument.get("live_ltp")
                or payload_instrument.get("live_ltp")
            ),
            "lot_size": (
                selected_instrument.get("lot_size")
                or payload_instrument.get("lot_size")
            ),
        }

    def _build_order_document(
        self,
        *,
        payload: dict[str, Any],
        order_result: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Build the MongoDB document for an order workflow result.
        """

        now = self._get_current_datetime()

        instrument_details = self._get_instrument_details(
            payload=payload,
            order_result=order_result,
        )

        success = bool(order_result.get("success"))

        order_status = order_result.get("order_status")

        if not order_status:
            order_status = "SUCCESS" if success else "FAILED"

        document = {
            "event_id": payload.get("event_id"),
            "instrument_key": instrument_details.get("instrument_key"),
            "trading_symbol": instrument_details.get("trading_symbol"),
            "instrument": instrument_details,
            "order_status": order_status,
            "success": success,
            "order_result": deepcopy(order_result),
            "payload_metadata": {
                "event_id": payload.get("event_id"),
                "timestamp": payload.get("timestamp"),
                "event_type": payload.get("event_type"),
                "signal": payload.get("signal"),
                "direction": payload.get("direction"),
            },
            "created_at": now,
            "created_at_ist": now.strftime("%Y-%m-%d %H:%M:%S %Z"),
        }

        return document

    # ==========================================================
    # SAVE ORDER RESULT
    # ==========================================================

    def save_order_result(
        self,
        *,
        payload: dict[str, Any],
        order_result: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Save a complete order workflow result.

        Returns:
            {
                "success": bool,
                "saved": bool,
                "document_id": str | None,
                "error": str | None,
            }
        """

        result = {
            "success": False,
            "saved": False,
            "document_id": None,
            "error": None,
        }

        if not self._is_enabled():
            result["error"] = "Order result saving is disabled by configuration."

            logger.info("Order result saving skipped because it is disabled.")

            return result

        if not isinstance(payload, dict):
            result["error"] = "Payload must be a dictionary."
            return result

        if not isinstance(order_result, dict):
            result["error"] = "Order result must be a dictionary."
            return result

        collection = self._get_collection()

        if collection is None:
            result["error"] = "Order MongoDB collection is unavailable."
            return result

        try:
            document = self._build_order_document(
                payload=payload,
                order_result=order_result,
            )

            insert_result = collection.insert_one(document)

            document_id = str(insert_result.inserted_id)

            result.update(
                {
                    "success": True,
                    "saved": True,
                    "document_id": document_id,
                    "error": None,
                }
            )

            logger.info(
                "Order result saved successfully. "
                "event_id=%s, instrument_key=%s, "
                "order_status=%s, document_id=%s",
                payload.get("event_id"),
                document.get("instrument_key"),
                document.get("order_status"),
                document_id,
            )

            return result

        except PyMongoError as exc:
            logger.exception(
                "MongoDB error while saving order result. "
                "event_id=%s, error_type=%s",
                payload.get("event_id"),
                type(exc).__name__,
            )

            result["error"] = f"{type(exc).__name__}: {exc}"
            return result

        except Exception as exc:
            logger.exception(
                "Unexpected error while saving order result. "
                "event_id=%s, error_type=%s",
                payload.get("event_id"),
                type(exc).__name__,
            )

            result["error"] = f"{type(exc).__name__}: {exc}"
            return result

    # ==========================================================
    # CONNECTION CLEANUP
    # ==========================================================

    def close(self) -> None:
        """
        Close the MongoDB connection.
        """

        with self._connection_lock:
            if self._client is not None:
                try:
                    self._client.close()
                    logger.info("Order MongoDB connection closed.")
                except Exception:
                    logger.exception("Failed to close order MongoDB connection.")

            self._client = None
            self._database = None
            self._collection = None
            self._indexes_initialized = False


upstox_order_saving_service = OrderSavingService()
