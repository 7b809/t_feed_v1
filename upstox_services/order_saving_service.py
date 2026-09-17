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
    def __init__(self) -> None:
        self._client: MongoClient | None = None
        self._database = None
        self._collection = None
        self._connection_lock = Lock()
        self._indexes_initialized = False

        logger.info(
            "Order saving service initialized. " "enabled=%s, collection=%s",
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
                    "Order result saving is disabled. " "collection=%s",
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
                    "Order MongoDB collection initialized. "
                    "database=%s, collection=%s",
                    database_name,
                    collection_name,
                )

                return self._collection

            except Exception as exc:
                logger.exception(
                    "Failed to initialize order MongoDB "
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
        if self._collection is None or self._indexes_initialized:
            return

        try:
            self._collection.create_index(
                [("event_id", ASCENDING)],
                name="idx_event_id",
            )

            self._collection.create_index(
                [("order_id", ASCENDING)],
                name="idx_order_id",
                sparse=True,
            )

            self._collection.create_index(
                [("instrument_key", ASCENDING)],
                name="idx_instrument_key",
            )

            self._collection.create_index(
                [("trading_symbol", ASCENDING)],
                name="idx_trading_symbol",
            )

            self._collection.create_index(
                [("created_at", DESCENDING)],
                name="idx_created_at",
            )

            self._collection.create_index(
                [("updated_at", DESCENDING)],
                name="idx_updated_at",
            )

            self._collection.create_index(
                [("order_status", ASCENDING)],
                name="idx_order_status",
            )

            self._collection.create_index(
                [
                    ("event_id", ASCENDING),
                    ("instrument_key", ASCENDING),
                ],
                name="idx_event_instrument",
            )

            self._indexes_initialized = True

            logger.info("Order MongoDB indexes initialized.")

        except PyMongoError as exc:
            logger.warning(
                "Failed to initialize order MongoDB " "indexes. error=%s",
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

    @staticmethod
    def _extract_nested_order_id(
        value: Any,
    ) -> str | None:
        if not isinstance(value, dict):
            return None

        direct_order_id = (
            value.get("order_id") or value.get("orderId") or value.get("id")
        )

        normalized_direct_id = OrderSavingService._normalize_text(direct_order_id)

        if normalized_direct_id:
            return normalized_direct_id

        for nested_key in (
            "data",
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
        )

        normalized_direct_id = OrderSavingService._normalize_text(direct_order_id)

        if normalized_direct_id:
            return normalized_direct_id

        place_order_result = order_result.get("place_order_result")

        return OrderSavingService._extract_nested_order_id(place_order_result)

    @staticmethod
    def _get_instrument_details(
        payload: dict[str, Any],
        order_result: dict[str, Any],
    ) -> dict[str, Any]:
        selected_instrument = order_result.get("selected_instrument")

        if not isinstance(
            selected_instrument,
            dict,
        ):
            selected_instrument = {}

        payload_instrument = payload.get("instrument")

        if not isinstance(
            payload_instrument,
            dict,
        ):
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

    @staticmethod
    def _get_payload_metadata(
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        ema_data = payload.get("ema")

        if not isinstance(ema_data, dict):
            ema_data = {}

        duplicate_control = payload.get("duplicate_control")

        if not isinstance(
            duplicate_control,
            dict,
        ):
            duplicate_control = {}

        order_suggestion = payload.get("order_suggestion")

        if not isinstance(
            order_suggestion,
            dict,
        ):
            order_suggestion = {}

        simulation = payload.get("simulation")

        if not isinstance(simulation, dict):
            simulation = {}

        market_snapshot = payload.get("market_snapshot")

        if not isinstance(
            market_snapshot,
            dict,
        ):
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
            "order_result": deepcopy(order_result),
            "payload_metadata": (self._get_payload_metadata(payload)),
            "created_at": now,
            "updated_at": now,
            "created_at_ist": now.strftime("%Y-%m-%d %H:%M:%S %Z"),
            "updated_at_ist": now.strftime("%Y-%m-%d %H:%M:%S %Z"),
        }

    @staticmethod
    def _build_save_filter(
        document: dict[str, Any],
    ) -> dict[str, Any] | None:
        event_id = document.get("event_id")

        if event_id:
            return {
                "event_id": event_id,
            }

        order_id = document.get("order_id")

        if order_id:
            return {
                "order_id": order_id,
            }

        return None

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

            logger.info("Order result saving skipped because " "it is disabled.")

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
            result["error"] = "Order MongoDB collection is " "unavailable."
            return result

        try:
            document = self._build_order_document(
                payload=payload,
                order_result=order_result,
            )

            result["event_id"] = document.get("event_id")
            result["order_id"] = document.get("order_id")

            save_filter = self._build_save_filter(document)

            if save_filter is None:
                insert_result = collection.insert_one(document)

                document_id = str(insert_result.inserted_id)

                result.update(
                    {
                        "success": True,
                        "saved": True,
                        "created": True,
                        "updated": False,
                        "document_id": document_id,
                        "error": None,
                    }
                )

            else:
                created_at = document.pop("created_at")
                created_at_ist = document.pop("created_at_ist")

                update_result = collection.update_one(
                    save_filter,
                    {
                        "$set": document,
                        "$setOnInsert": {
                            "created_at": created_at,
                            "created_at_ist": (created_at_ist),
                        },
                    },
                    upsert=True,
                )

                created = update_result.upserted_id is not None

                if created:
                    document_id = str(update_result.upserted_id)
                else:
                    existing_document = collection.find_one(
                        save_filter,
                        {
                            "_id": 1,
                        },
                    )

                    document_id = (
                        str(existing_document.get("_id"))
                        if isinstance(
                            existing_document,
                            dict,
                        )
                        and existing_document.get("_id") is not None
                        else None
                    )

                result.update(
                    {
                        "success": True,
                        "saved": True,
                        "created": created,
                        "updated": not created,
                        "document_id": document_id,
                        "error": None,
                    }
                )

            logger.info(
                "Order result saved successfully. "
                "event_id=%s, order_id=%s, "
                "instrument_key=%s, "
                "order_status=%s, created=%s, "
                "updated=%s, document_id=%s",
                result.get("event_id"),
                result.get("order_id"),
                document.get("instrument_key"),
                document.get("order_status"),
                result.get("created"),
                result.get("updated"),
                result.get("document_id"),
            )

            return result

        except PyMongoError as exc:
            logger.exception(
                "MongoDB error while saving order "
                "result. event_id=%s, "
                "error_type=%s",
                payload.get("event_id"),
                type(exc).__name__,
            )

            result["error"] = f"{type(exc).__name__}: {exc}"

            return result

        except Exception as exc:
            logger.exception(
                "Unexpected error while saving order "
                "result. event_id=%s, "
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

                    logger.info("Order MongoDB connection closed.")

                except Exception:
                    logger.exception("Failed to close order MongoDB " "connection.")

            self._client = None
            self._database = None
            self._collection = None
            self._indexes_initialized = False


upstox_order_saving_service = OrderSavingService()
