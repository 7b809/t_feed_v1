from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from threading import Lock
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pymongo import MongoClient
from pymongo.errors import PyMongoError

from core import config
from core.logger import get_logger

logger = get_logger(__file__)


class OrderSavingService:
    """
    Saves all order results in one MongoDB document per day.

    Document structure:

    {
        "_id": "YYYY-MM-DD",
        "date": "YYYY-MM-DD",
        "timezone": "Asia/Kolkata",
        "orders": {
            "HH_MM_SS": {
                ...
            }
        },
        "created_at": datetime,
        "updated_at": datetime
    }
    """

    def __init__(self) -> None:
        self._client: MongoClient | None = None
        self._database = None
        self._collection = None
        self._connection_lock = Lock()
        self._indexes_initialized = False

        logger.info(
            "Daily order saving service initialized. " "enabled=%s, collection=%s",
            self._is_enabled(),
            self._get_collection_name(),
        )

    def _is_enabled(self) -> bool:
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
                "Invalid market timezone. " "Using Asia/Kolkata. timezone=%s",
                timezone_name,
            )

            return ZoneInfo("Asia/Kolkata")

    def _get_current_datetime(self) -> datetime:
        return datetime.now(tz=self._get_market_timezone())

    def _get_collection(self):
        if self._collection is not None:
            return self._collection

        with self._connection_lock:
            if self._collection is not None:
                return self._collection

            if not self._is_enabled():
                logger.info(
                    "Daily order result saving is disabled. " "collection=%s",
                    self._get_collection_name(),
                )

                return None

            mongo_uri = self._get_mongo_uri()
            database_name = self._get_database_name()
            collection_name = self._get_collection_name()

            if not mongo_uri:
                logger.error(
                    "Order result saving skipped: " "MONGO_URL is not configured."
                )

                return None

            if not database_name:
                logger.error(
                    "Order result saving skipped: " "MONGO_DB is not configured."
                )

                return None

            try:
                self._client = MongoClient(
                    mongo_uri,
                    serverSelectionTimeoutMS=5000,
                    connectTimeoutMS=5000,
                    socketTimeoutMS=10000,
                )

                self._client.admin.command("ping")

                self._database = self._client[database_name]

                self._collection = self._database[collection_name]

                self._initialize_indexes()

                logger.info(
                    "Daily order MongoDB collection initialized. "
                    "database=%s, collection=%s",
                    database_name,
                    collection_name,
                )

                return self._collection

            except Exception as exc:
                logger.exception(
                    "Failed to initialize daily order MongoDB "
                    "collection. database=%s, "
                    "collection=%s, error_type=%s",
                    database_name,
                    collection_name,
                    type(exc).__name__,
                )

                if self._client is not None:
                    try:
                        self._client.close()

                    except Exception:
                        logger.exception(
                            "Failed to close MongoDB client "
                            "after connection failure."
                        )

                self._client = None
                self._database = None
                self._collection = None

                return None

    def _initialize_indexes(self) -> None:
        """
        No additional indexes are required for daily documents.

        The document _id is the date and is automatically indexed
        by MongoDB.
        """

        if self._collection is None or self._indexes_initialized:
            return

        try:
            self._collection.create_index(
                [
                    ("updated_at", -1),
                ],
                name="idx_daily_updated_at",
            )

            self._indexes_initialized = True

            logger.info("Daily order MongoDB indexes initialized.")

        except PyMongoError as exc:
            logger.warning(
                "Failed to initialize daily order MongoDB " "indexes. error=%s",
                exc,
            )

    @staticmethod
    def _first_not_none(
        *values: Any,
    ) -> Any:
        for value in values:
            if value is not None:
                return value

        return None

    @staticmethod
    def _normalize_text(
        value: Any,
    ) -> str | None:
        if value is None:
            return None

        normalized = str(value).strip()

        return normalized or None

    # ------------------------------------------------------------------
    # ORDER ID EXTRACTION
    # ------------------------------------------------------------------
    @staticmethod
    def _extract_nested_order_id(
        value: Any,
    ) -> str | None:
        """
        Walks a nested dictionary looking for an order id.

        Handles both the plain keys ("order_id", "orderId", "id") and
        the Upstox SDK's underscore-prefixed keys ("_order_id") and
        nested containers ("data", "_data", "response", "result").
        """
        if not isinstance(value, dict):
            return None

        direct_order_id = (
            value.get("order_id")
            or value.get("orderId")
            or value.get("id")
            or value.get("_order_id")
        )

        normalized_direct_id = OrderSavingService._normalize_text(direct_order_id)

        if normalized_direct_id:
            return normalized_direct_id

        for nested_key in (
            "data",
            "_data",
            "response",
            "result",
        ):
            nested_value = value.get(nested_key)

            if not isinstance(nested_value, dict):
                continue

            nested_order_id = OrderSavingService._extract_nested_order_id(nested_value)

            if nested_order_id:
                return nested_order_id

        return None

    @staticmethod
    def _get_order_id(
        order_result: dict[str, Any],
    ) -> str | None:
        direct_order_id = (
            order_result.get("order_id")
            or order_result.get("orderId")
            or order_result.get("id")
            or order_result.get("_order_id")
        )

        normalized_direct_id = OrderSavingService._normalize_text(direct_order_id)

        if normalized_direct_id:
            return normalized_direct_id

        place_order_result = order_result.get("place_order_result")

        return OrderSavingService._extract_nested_order_id(place_order_result)

    # ------------------------------------------------------------------
    # INSTRUMENT DETAILS
    # ------------------------------------------------------------------
    @staticmethod
    def _get_instrument_details(
        payload: dict[str, Any],
        order_result: dict[str, Any],
    ) -> dict[str, Any]:
        selected_instrument = order_result.get("selected_instrument")

        if not isinstance(selected_instrument, dict):
            selected_instrument = {}

        payload_instrument = payload.get("instrument")

        if not isinstance(payload_instrument, dict):
            payload_instrument = {}

        instrument_key = selected_instrument.get(
            "instrument_key"
        ) or payload_instrument.get("instrument_key")

        trading_symbol = selected_instrument.get(
            "trading_symbol"
        ) or payload_instrument.get("trading_symbol")

        live_ltp = OrderSavingService._first_not_none(
            selected_instrument.get("live_ltp"),
            selected_instrument.get("ltp"),
            payload_instrument.get("live_ltp"),
            payload_instrument.get("ltp"),
        )

        lot_size = OrderSavingService._first_not_none(
            selected_instrument.get("lot_size"),
            payload_instrument.get("lot_size"),
        )

        return {
            "instrument_key": (OrderSavingService._normalize_text(instrument_key)),
            "trading_symbol": (OrderSavingService._normalize_text(trading_symbol)),
            "live_ltp": live_ltp,
            "lot_size": lot_size,
        }

    # ------------------------------------------------------------------
    # PLACE ORDER REQUEST (flattened at top level)
    # ------------------------------------------------------------------
    @staticmethod
    def _get_place_order_request(
        order_result: dict[str, Any],
    ) -> dict[str, Any] | None:
        """
        Extracts the order placement request that was sent to Upstox
        and returns it as a plain dict, so it can be stored at the top
        level of the order document for easy querying.

        Returns None when the placement request is missing or invalid.
        """
        place_order_result = order_result.get("place_order_result")

        if not isinstance(place_order_result, dict):
            return None

        request = place_order_result.get("request")

        if not isinstance(request, dict):
            return None

        return deepcopy(request)

    # ------------------------------------------------------------------
    # MARGIN METADATA
    # ------------------------------------------------------------------
    @staticmethod
    def _get_margin_metadata(
        order_result: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Extracts the margin calculation result from the order result
        so it can be stored alongside the order in MongoDB.

        When margin_result is missing or invalid, a structured
        placeholder with success=False is returned.
        """
        margin_result = order_result.get("margin_result")

        if not isinstance(margin_result, dict):
            return {
                "success": False,
                "instrument_key": None,
                "quantity": None,
                "product": None,
                "transaction_type": None,
                "required_margin": None,
                "available_margin": None,
                "margin": None,
                "raw_response": None,
                "error": None,
            }

        return {
            "success": bool(margin_result.get("success")),
            "instrument_key": (
                OrderSavingService._normalize_text(margin_result.get("instrument_key"))
            ),
            "quantity": margin_result.get("quantity"),
            "product": (
                OrderSavingService._normalize_text(margin_result.get("product"))
            ),
            "transaction_type": (
                OrderSavingService._normalize_text(
                    margin_result.get("transaction_type")
                )
            ),
            "required_margin": margin_result.get("required_margin"),
            "available_margin": margin_result.get("available_margin"),
            "margin": margin_result.get("margin"),
            "raw_response": margin_result.get("raw_response"),
            "error": margin_result.get("error"),
        }

    # ------------------------------------------------------------------
    # PAYLOAD METADATA
    # ------------------------------------------------------------------
    @staticmethod
    def _get_payload_metadata(
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        ema_data = payload.get("ema")

        if not isinstance(ema_data, dict):
            ema_data = {}

        duplicate_control = payload.get("duplicate_control")

        if not isinstance(duplicate_control, dict):
            duplicate_control = {}

        order_suggestion = payload.get("order_suggestion")

        if not isinstance(order_suggestion, dict):
            order_suggestion = {}

        simulation = payload.get("simulation")

        if not isinstance(simulation, dict):
            simulation = {}

        market_snapshot = payload.get("market_snapshot")

        if not isinstance(market_snapshot, dict):
            market_snapshot = {}

        return {
            "event_id": payload.get("event_id"),
            "event_type": payload.get("event_type"),
            "source": payload.get("source"),
            "market": payload.get("market"),
            "timezone": payload.get("timezone"),
            "created_at": payload.get("created_at"),
            "timestamp": ema_data.get("timestamp"),
            "cross_type": ema_data.get("cross_type"),
            "previous_signal": ema_data.get("previous_signal"),
            "current_signal": ema_data.get("current_signal"),
            "direction": duplicate_control.get("direction"),
            "suggested_order_side": (order_suggestion.get("suggested_order_side")),
            "underlying_spot_price": (market_snapshot.get("underlying_spot_price")),
            "nifty_ltp": market_snapshot.get("nifty_ltp"),
            "is_simulation": bool(payload.get("is_simulation")),
            "dry_run": bool(simulation.get("dry_run")),
            "minute_alert_key": (duplicate_control.get("minute_alert_key")),
        }

    # ------------------------------------------------------------------
    # DOCUMENT BUILDER
    # ------------------------------------------------------------------
    def _build_order_document(
        self,
        *,
        payload: dict[str, Any],
        order_result: dict[str, Any],
    ) -> dict[str, Any]:
        now = self._get_current_datetime()

        instrument_details = self._get_instrument_details(
            payload=payload,
            order_result=order_result,
        )

        success = bool(order_result.get("success"))

        executed = bool(order_result.get("executed"))

        skipped = bool(order_result.get("skipped"))

        order_status = self._normalize_text(order_result.get("order_status"))

        if not order_status:
            if skipped:
                order_status = "SKIPPED"

            elif success:
                order_status = "SUCCESS"

            else:
                order_status = "FAILED"

        order_id = self._get_order_id(order_result)

        return {
            "event_id": self._normalize_text(payload.get("event_id")),
            "order_id": order_id,
            "instrument_key": (instrument_details.get("instrument_key")),
            "trading_symbol": (instrument_details.get("trading_symbol")),
            "instrument": instrument_details,
            "order_status": order_status,
            "success": success,
            "executed": executed,
            "skipped": skipped,
            "error": order_result.get("error"),
            "place_order_request": self._get_place_order_request(order_result),
            "margin": self._get_margin_metadata(order_result),
            "payload_metadata": (self._get_payload_metadata(payload)),
            "order_result": deepcopy(order_result),
            "created_at": now,
            "updated_at": now,
            "created_at_ist": now.strftime("%Y-%m-%d %H:%M:%S %Z"),
            "updated_at_ist": now.strftime("%Y-%m-%d %H:%M:%S %Z"),
        }

    # ------------------------------------------------------------------
    # DAILY DOCUMENT + ORDER KEY HELPERS
    # ------------------------------------------------------------------
    @staticmethod
    def _get_daily_document_id(
        now: datetime,
    ) -> str:
        """
        Returns the daily MongoDB document ID.

        Example:
            2026-09-17
        """

        return now.strftime("%Y-%m-%d")

    @staticmethod
    def _get_time_key(
        now: datetime,
    ) -> str:
        """
        Returns the initial order key.

        Example:
            10_11_32
        """

        return now.strftime("%H_%M_%S")

    def _get_unique_order_key(
        self,
        *,
        collection: Any,
        daily_document_id: str,
        base_key: str,
    ) -> str:
        """
        Creates a unique order key inside today's document.

        Examples:
            10_11_32
            10_11_32_1
            10_11_32_2
        """

        order_key = base_key
        counter = 0

        while True:
            existing_document = collection.find_one(
                {
                    "_id": daily_document_id,
                    f"orders.{order_key}": {"$exists": True},
                },
                {
                    "_id": 1,
                },
            )

            if existing_document is None:
                return order_key

            counter += 1

            order_key = f"{base_key}_{counter}"

    # ------------------------------------------------------------------
    # PUBLIC SAVE API
    # ------------------------------------------------------------------
    def save_order_result(
        self,
        *,
        payload: dict[str, Any],
        order_result: dict[str, Any],
    ) -> dict[str, Any]:
        result = {
            "success": False,
            "saved": False,
            "created": False,
            "updated": False,
            "skipped": False,
            "document_id": None,
            "order_key": None,
            "event_id": None,
            "order_id": None,
            "error": None,
        }

        if not self._is_enabled():
            result.update(
                {
                    "skipped": True,
                    "error": ("Order result saving is " "disabled by configuration."),
                }
            )

            logger.info("Daily order result saving skipped " "because it is disabled.")

            return result

        if not isinstance(payload, dict):
            result["error"] = "Payload must be a dictionary."

            return result

        if not isinstance(order_result, dict):
            result["error"] = "Order result must be a dictionary."

            return result

        if not payload:
            result["error"] = "Payload is empty."

            return result

        if not order_result:
            result["error"] = "Order result is empty."

            return result

        collection = self._get_collection()

        if collection is None:
            result["error"] = "Order MongoDB collection is unavailable."

            return result

        try:
            now = self._get_current_datetime()

            daily_document_id = self._get_daily_document_id(now)

            base_time_key = self._get_time_key(now)

            document = self._build_order_document(
                payload=payload,
                order_result=order_result,
            )

            order_key = self._get_unique_order_key(
                collection=collection,
                daily_document_id=daily_document_id,
                base_key=base_time_key,
            )

            result["document_id"] = daily_document_id

            result["order_key"] = order_key

            result["event_id"] = document.get("event_id")

            result["order_id"] = document.get("order_id")

            update_result = collection.update_one(
                {
                    "_id": daily_document_id,
                },
                {
                    "$set": {
                        f"orders.{order_key}": document,
                        "updated_at": now,
                        "updated_at_ist": now.strftime("%Y-%m-%d %H:%M:%S %Z"),
                    },
                    "$setOnInsert": {
                        "date": daily_document_id,
                        "timezone": str(self._get_market_timezone()),
                        "created_at": now,
                        "created_at_ist": now.strftime("%Y-%m-%d %H:%M:%S %Z"),
                    },
                },
                upsert=True,
            )

            created = update_result.upserted_id is not None

            result.update(
                {
                    "success": True,
                    "saved": True,
                    "created": created,
                    "updated": not created,
                    "error": None,
                }
            )

            logger.info(
                "Daily order result saved successfully. "
                "date=%s, order_key=%s, event_id=%s, "
                "order_id=%s, instrument_key=%s, "
                "order_status=%s, created=%s, "
                "updated=%s",
                daily_document_id,
                order_key,
                result.get("event_id"),
                result.get("order_id"),
                document.get("instrument_key"),
                document.get("order_status"),
                result.get("created"),
                result.get("updated"),
            )

            return result

        except PyMongoError as exc:
            logger.exception(
                "MongoDB error while saving daily "
                "order result. event_id=%s, "
                "error_type=%s",
                payload.get("event_id"),
                type(exc).__name__,
            )

            result["error"] = f"{type(exc).__name__}: {exc}"

            return result

        except Exception as exc:
            logger.exception(
                "Unexpected error while saving daily "
                "order result. event_id=%s, "
                "error_type=%s",
                payload.get("event_id"),
                type(exc).__name__,
            )

            result["error"] = f"{type(exc).__name__}: {exc}"

            return result

    def close(self) -> None:
        with self._connection_lock:
            if self._client is not None:
                try:
                    self._client.close()

                    logger.info("Daily order MongoDB connection closed.")

                except Exception:
                    logger.exception(
                        "Failed to close daily order " "MongoDB connection."
                    )

            self._client = None
            self._database = None
            self._collection = None
            self._indexes_initialized = False


upstox_order_saving_service = OrderSavingService()