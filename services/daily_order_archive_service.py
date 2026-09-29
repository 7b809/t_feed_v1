from datetime import datetime
from threading import RLock
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pymongo import ASCENDING, MongoClient

from core import config
from core.logger import get_logger
from upstox_services.order_book_service import order_book_service

logger = get_logger(__file__)


class DailyOrderArchiveService:
    def __init__(self):
        self.enabled = bool(
            getattr(
                config,
                "DAILY_ORDER_ARCHIVE_ENABLED",
                True,
            )
        )

        self.collection_name = str(
            getattr(
                config,
                "DAILY_ORDER_ARCHIVE_COLLECTION",
                "daily_order_book_archive",
            )
            or "daily_order_book_archive"
        ).strip()

        self.mongo_uri = str(
            getattr(
                config,
                "MONGO_URI",
                "",
            )
            or ""
        ).strip()

        self.database_name = str(
            getattr(
                config,
                "MONGO_DB",
                "",
            )
            or ""
        ).strip()

        self.server_selection_timeout_ms = max(
            1000,
            int(
                getattr(
                    config,
                    "MONGODB_SERVER_SELECTION_TIMEOUT_MS",
                    10000,
                )
            ),
        )

        self.connect_timeout_ms = max(
            1000,
            int(
                getattr(
                    config,
                    "MONGODB_CONNECT_TIMEOUT_MS",
                    10000,
                )
            ),
        )

        self.market_timezone = self._load_market_timezone()
        self._lock = RLock()
        self._client: MongoClient | None = None
        self._collection = None
        self._initialized = False

        logger.info(
            "Daily order archive service created. "
            "enabled=%s, collection=%s, "
            "mongo_uri_configured=%s, database_configured=%s",
            self.enabled,
            self.collection_name,
            bool(self.mongo_uri),
            bool(self.database_name),
        )

    def _load_market_timezone(self) -> ZoneInfo:
        timezone_name = getattr(
            config,
            "MARKET_TIMEZONE",
            "Asia/Kolkata",
        )

        try:
            return ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            logger.error(
                "Invalid MARKET_TIMEZONE configured: %s. "
                "Falling back to Asia/Kolkata.",
                timezone_name,
            )
            return ZoneInfo("Asia/Kolkata")

    def _now_market_time(self) -> datetime:
        return datetime.now(self.market_timezone)

    def _serialize(self, value: Any) -> Any:
        if value is None:
            return None

        if isinstance(
            value,
            (str, int, float, bool, datetime),
        ):
            return value

        if isinstance(value, dict):
            return {
                str(key): self._serialize(item)
                for key, item in value.items()
            }

        if isinstance(value, (list, tuple, set)):
            return [
                self._serialize(item)
                for item in value
            ]

        if hasattr(value, "to_dict"):
            return self._serialize(
                value.to_dict()
            )

        if hasattr(value, "__dict__"):
            return {
                str(key): self._serialize(item)
                for key, item in vars(value).items()
                if not str(key).startswith("_")
            }

        return str(value)

    def _initialize(self) -> None:
        with self._lock:
            if self._initialized:
                return

            if not self.enabled:
                raise RuntimeError(
                    "Daily order archive service is disabled."
                )

            if not self.mongo_uri:
                raise RuntimeError(
                    "MongoDB URI is not configured. "
                    "Configure MONGO_URL in the environment."
                )

            if not self.database_name:
                raise RuntimeError(
                    "MongoDB database name is not configured. "
                    "Configure MONGO_DB in the environment."
                )

            client = None

            try:
                client = MongoClient(
                    self.mongo_uri,
                    serverSelectionTimeoutMS=(
                        self.server_selection_timeout_ms
                    ),
                    connectTimeoutMS=self.connect_timeout_ms,
                )

                client.admin.command("ping")

                database = client[self.database_name]
                collection = database[
                    self.collection_name
                ]

                collection.create_index(
                    [
                        (
                            "market_date",
                            ASCENDING,
                        )
                    ],
                    unique=True,
                    name="unique_market_date",
                )

                self._client = client
                self._collection = collection
                self._initialized = True

                logger.info(
                    "Daily order archive service initialized. "
                    "database=%s, collection=%s",
                    self.database_name,
                    self.collection_name,
                )

            except Exception:
                if client is not None:
                    try:
                        client.close()
                    except Exception:
                        logger.exception(
                            "Failed closing MongoDB client "
                            "after initialization error."
                        )

                self._client = None
                self._collection = None
                self._initialized = False

                logger.exception(
                    "Daily order archive MongoDB "
                    "initialization failed."
                )

                raise

    def get_status(self) -> dict:
        return {
            "enabled": self.enabled,
            "initialized": self._initialized,
            "database_name": self.database_name,
            "collection_name": self.collection_name,
            "mongo_uri_configured": bool(
                self.mongo_uri
            ),
            "database_configured": bool(
                self.database_name
            ),
            "market_timezone": str(
                self.market_timezone
            ),
            "server_selection_timeout_ms": (
                self.server_selection_timeout_ms
            ),
            "connect_timeout_ms": (
                self.connect_timeout_ms
            ),
        }

    def archive_today_orders(
        self,
        source: str = "daily_scheduler",
    ) -> dict:
        if not self.enabled:
            logger.info(
                "Daily order archive skipped because "
                "the service is disabled."
            )

            return {
                "success": False,
                "status": "disabled",
                "message": (
                    "Daily order archive service is disabled."
                ),
                "market_date": (
                    self._now_market_time()
                    .date()
                    .isoformat()
                ),
                "total_orders_count": 0,
            }

        with self._lock:
            self._initialize()

            if self._collection is None:
                raise RuntimeError(
                    "Daily order archive MongoDB "
                    "collection is unavailable."
                )

            now_market = self._now_market_time()
            market_date = (
                now_market.date().isoformat()
            )
            selected_source = str(
                source or "unknown"
            ).strip()

            logger.info(
                "Fetching daily order book for archive. "
                "market_date=%s, source=%s",
                market_date,
                selected_source,
            )

            order_result = (
                order_book_service.get_all_orders()
            )

            if not isinstance(order_result, dict):
                raise RuntimeError(
                    "Order book service returned an "
                    "invalid response."
                )

            if order_result.get("success") is not True:
                raise RuntimeError(
                    "Order book service did not return "
                    "a successful response."
                )

            orders = order_result.get("data") or []

            if not isinstance(orders, list):
                raise RuntimeError(
                    "Order book data must be a list."
                )

            serialized_orders = self._serialize(
                orders
            )

            if not isinstance(
                serialized_orders,
                list,
            ):
                raise RuntimeError(
                    "Serialized order data must be a list."
                )

            total_orders_count = len(
                serialized_orders
            )

            document = {
                "market_date": market_date,
                "market_timezone": str(
                    self.market_timezone
                ),
                "archive_type": "daily_order_book",
                "source": selected_source,
                "upstream_status": (
                    order_result.get("status")
                ),
                "total_orders_count": int(
                    total_orders_count
                ),
                "orders_data": serialized_orders,
                "fetched_at": now_market,
                "fetched_at_iso": (
                    now_market.isoformat()
                ),
                "updated_at": now_market,
            }

            update_result = (
                self._collection.update_one(
                    {
                        "market_date": market_date,
                    },
                    {
                        "$set": document,
                        "$setOnInsert": {
                            "created_at": now_market,
                        },
                    },
                    upsert=True,
                )
            )

            inserted = (
                update_result.upserted_id
                is not None
            )

            archive_status = (
                "inserted"
                if inserted
                else "updated"
            )

            result = {
                "success": True,
                "status": archive_status,
                "market_date": market_date,
                "market_timezone": str(
                    self.market_timezone
                ),
                "database_name": self.database_name,
                "collection_name": (
                    self.collection_name
                ),
                "total_orders_count": int(
                    total_orders_count
                ),
                "matched_count": int(
                    update_result.matched_count
                ),
                "modified_count": int(
                    update_result.modified_count
                ),
                "upserted_id": (
                    str(update_result.upserted_id)
                    if update_result.upserted_id
                    is not None
                    else None
                ),
                "fetched_at": (
                    now_market.isoformat()
                ),
                "source": selected_source,
            }

            logger.info(
                "Daily order book archived. "
                "market_date=%s, total_orders=%s, "
                "status=%s, matched_count=%s, "
                "modified_count=%s, upserted_id=%s",
                market_date,
                total_orders_count,
                archive_status,
                result["matched_count"],
                result["modified_count"],
                result["upserted_id"],
            )

            return result

    def get_archive_for_date(
        self,
        market_date: str,
    ) -> dict | None:
        normalized_date = str(
            market_date or ""
        ).strip()

        if not normalized_date:
            raise ValueError(
                "market_date is required."
            )

        try:
            datetime.strptime(
                normalized_date,
                "%Y-%m-%d",
            )
        except ValueError as ex:
            raise ValueError(
                "market_date must use YYYY-MM-DD format."
            ) from ex

        with self._lock:
            self._initialize()

            if self._collection is None:
                raise RuntimeError(
                    "Daily order archive MongoDB "
                    "collection is unavailable."
                )

            result = self._collection.find_one(
                {
                    "market_date": normalized_date,
                }
            )

        if not result:
            return None

        result["_id"] = str(result["_id"])
        return self._serialize(result)

    def get_today_archive(self) -> dict | None:
        market_date = (
            self._now_market_time()
            .date()
            .isoformat()
        )

        return self.get_archive_for_date(
            market_date
        )

    def get_latest_archive(self) -> dict | None:
        with self._lock:
            self._initialize()

            if self._collection is None:
                raise RuntimeError(
                    "Daily order archive MongoDB "
                    "collection is unavailable."
                )

            result = self._collection.find_one(
                {},
                sort=[
                    (
                        "market_date",
                        -1,
                    )
                ],
            )

        if not result:
            return None

        result["_id"] = str(result["_id"])
        return self._serialize(result)

    def close(self) -> None:
        with self._lock:
            client = self._client

            self._client = None
            self._collection = None
            self._initialized = False

            if client is not None:
                try:
                    client.close()
                except Exception:
                    logger.exception(
                        "Failed closing daily order "
                        "archive MongoDB client."
                    )
                    raise

            logger.info(
                "Daily order archive service closed."
            )


daily_order_archive_service = (
    DailyOrderArchiveService()
)