from copy import deepcopy
from datetime import datetime, timezone
from threading import Lock
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pymongo import ASCENDING, MongoClient
from pymongo.errors import PyMongoError

from core import config
from core.logger import get_logger

logger = get_logger(__file__)


class IsolatedInstrumentEventSavingService:
    def __init__(self):
        self._client = None
        self._database = None
        self._collection = None
        self._connection_lock = Lock()
        self._indexes_initialized = False

        logger.info(
            "Isolated instrument event saving service initialized. "
            "enabled=%s, collection=%s, event_field=%s, fail_open=%s",
            bool(
                getattr(
                    config,
                    "ISOLATED_INSTRUMENT_EVENT_ENABLED",
                    True,
                )
            ),
            str(
                getattr(
                    config,
                    "ISOLATED_INSTRUMENT_EVENT_COLLECTION",
                    "isolated_instrumentevent",
                )
            ),
            str(
                getattr(
                    config,
                    "ISOLATED_INSTRUMENT_EVENT_FIELD_NAME",
                    "events",
                )
            ),
            bool(
                getattr(
                    config,
                    "ISOLATED_INSTRUMENT_EVENT_FAIL_OPEN",
                    True,
                )
            ),
        )

    def _is_enabled(self) -> bool:
        return bool(
            getattr(
                config,
                "ISOLATED_INSTRUMENT_EVENT_ENABLED",
                True,
            )
        )

    def _is_fail_open_enabled(self) -> bool:
        return bool(
            getattr(
                config,
                "ISOLATED_INSTRUMENT_EVENT_FAIL_OPEN",
                True,
            )
        )

    def _get_collection_name(self) -> str:
        collection_name = str(
            getattr(
                config,
                "ISOLATED_INSTRUMENT_EVENT_COLLECTION",
                "isolated_instrumentevent",
            )
            or "isolated_instrumentevent"
        ).strip()

        return collection_name or "isolated_instrumentevent"

    def _get_event_field_name(self) -> str:
        field_name = str(
            getattr(
                config,
                "ISOLATED_INSTRUMENT_EVENT_FIELD_NAME",
                "events",
            )
            or "events"
        ).strip()

        if not field_name:
            return "events"

        if field_name.startswith("$") or "." in field_name:
            logger.warning(
                "Invalid isolated instrument event field name configured. "
                "configured_name=%s, fallback_name=events",
                field_name,
            )
            return "events"

        return field_name

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
                "Configured market timezone was not found. "
                "timezone=%s, fallback=Asia/Kolkata",
                timezone_name,
            )
            return ZoneInfo("Asia/Kolkata")

    def _get_market_datetime(
        self,
        timestamp_value: datetime | None = None,
    ) -> datetime:
        market_timezone = self._get_market_timezone()

        if isinstance(timestamp_value, datetime):
            if timestamp_value.tzinfo is None:
                return timestamp_value.replace(tzinfo=market_timezone)

            return timestamp_value.astimezone(market_timezone)

        return datetime.now(tz=market_timezone)

    def _get_collection(self):
        if self._collection is not None:
            return self._collection

        with self._connection_lock:
            if self._collection is not None:
                return self._collection

            mongo_uri = str(
                getattr(
                    config,
                    "MONGO_URI",
                    "",
                )
                or ""
            ).strip()

            mongo_database_name = str(
                getattr(
                    config,
                    "MONGO_DB",
                    "",
                )
                or ""
            ).strip()

            if not mongo_uri:
                raise ValueError("MONGO_URI is not configured.")

            if not mongo_database_name:
                raise ValueError("MONGO_DB is not configured.")

            collection_name = self._get_collection_name()

            logger.info(
                "Creating MongoDB connection for isolated instrument events. "
                "database=%s, collection=%s",
                mongo_database_name,
                collection_name,
            )

            self._client = MongoClient(mongo_uri)
            self._database = self._client[mongo_database_name]
            self._collection = self._database[collection_name]

            self._initialize_indexes()

            return self._collection

    def _initialize_indexes(self) -> None:
        if self._indexes_initialized or self._collection is None:
            return

        try:
            self._collection.create_index(
                [
                    ("date", ASCENDING),
                    ("instrument_key", ASCENDING),
                ],
                name="date_instrument_key_index",
            )

            self._collection.create_index(
                [("updated_at_utc", ASCENDING)],
                name="updated_at_utc_index",
            )

            self._indexes_initialized = True

            logger.info(
                "Isolated instrument event collection indexes initialized. "
                "collection=%s",
                self._get_collection_name(),
            )

        except PyMongoError:
            logger.exception(
                "Failed to initialize isolated instrument event indexes. "
                "collection=%s",
                self._get_collection_name(),
            )

            if not self._is_fail_open_enabled():
                raise

    def _get_instrument_details(self, payload: dict) -> dict:
        instrument = payload.get("instrument") or {}

        if not isinstance(instrument, dict):
            instrument = {}

        instrument_key = str(
            instrument.get("instrument_key") or ""
        ).strip()

        instrument_name = str(
            instrument.get("trading_symbol")
            or instrument.get("instrument_name")
            or instrument_key
            or ""
        ).strip()

        return {
            "instrument_key": instrument_key,
            "instrument_name": instrument_name,
            "instrument_type": (
                instrument.get("instrument_type")
                or instrument.get("option_type")
            ),
            "option_type": (
                instrument.get("option_type")
                or instrument.get("instrument_type")
            ),
            "strike_price": instrument.get("strike_price"),
            "expiry": instrument.get("expiry"),
            "lot_size": instrument.get("lot_size"),
            "underlying_symbol": instrument.get("underlying_symbol"),
        }

    def _build_processing_event(
        self,
        *,
        payload: dict,
        processing_result: dict,
        processed_at: datetime,
        processing_status: str,
    ) -> dict:
        ema_data = payload.get("ema") or {}
        duplicate_control = payload.get("duplicate_control") or {}
        market_snapshot = payload.get("market_snapshot") or {}
        order_suggestion = payload.get("order_suggestion") or {}
        simulation_data = payload.get("simulation") or {}

        if not isinstance(ema_data, dict):
            ema_data = {}

        if not isinstance(duplicate_control, dict):
            duplicate_control = {}

        if not isinstance(market_snapshot, dict):
            market_snapshot = {}

        if not isinstance(order_suggestion, dict):
            order_suggestion = {}

        if not isinstance(simulation_data, dict):
            simulation_data = {}

        return {
            "processed_at": processed_at.isoformat(),
            "processed_at_utc": datetime.now(timezone.utc),
            "processing_status": processing_status,
            "event_id": payload.get("event_id"),
            "event_type": payload.get("event_type"),
            "source": payload.get("source"),
            "is_simulation": bool(payload.get("is_simulation")),
            "dry_run": bool(simulation_data.get("dry_run")),
            "direction": duplicate_control.get("direction"),
            "cross_type": ema_data.get("cross_type"),
            "current_signal": ema_data.get("current_signal"),
            "previous_signal": ema_data.get("previous_signal"),
            "ema_timestamp": ema_data.get("timestamp"),
            "ema_calculation_mode": ema_data.get("calculation_mode"),
            "suggested_order_side": order_suggestion.get(
                "suggested_order_side"
            ),
            "nifty_ltp": market_snapshot.get("nifty_ltp"),
            "isolated_instrument_ltp": market_snapshot.get(
                "isolated_instrument_ltp"
            ),
            "minute_alert_key": duplicate_control.get(
                "minute_alert_key"
            ),
            "processing_result": {
                "success": bool(processing_result.get("success")),
                "accepted": bool(processing_result.get("accepted")),
                "processed": bool(processing_result.get("processed")),
                "skip_reason": processing_result.get("skip_reason"),
                "message": processing_result.get("message"),
                "error": processing_result.get("error"),
                "warnings": deepcopy(
                    processing_result.get("warnings") or []
                ),
            },
            "delivery": deepcopy(
                processing_result.get("delivery") or {}
            ),
            "payload": deepcopy(payload),
        }

    def save_processing_event(
        self,
        *,
        payload: dict,
        processing_result: dict | None = None,
        processing_status: str = "completed",
        processed_at: datetime | None = None,
    ) -> dict:
        result = {
            "success": False,
            "saved": False,
            "skipped": False,
            "document_id": None,
            "date": None,
            "time_key": None,
            "instrument_key": None,
            "instrument_name": None,
            "processing_status": processing_status,
            "error": None,
        }

        if not self._is_enabled():
            result["skipped"] = True
            result["error"] = "Isolated instrument event saving is disabled."

            logger.debug(
                "Isolated instrument event saving skipped. reason=disabled"
            )

            return result

        if not isinstance(payload, dict):
            result["skipped"] = True
            result["error"] = "Payload must be a dictionary."

            logger.warning(
                "Isolated instrument event saving skipped. "
                "reason=invalid_payload"
            )

            return result

        if not isinstance(processing_result, dict):
            processing_result = {}

        is_simulation = bool(payload.get("is_simulation"))
        simulation_data = payload.get("simulation") or {}

        if not isinstance(simulation_data, dict):
            simulation_data = {}

        is_dry_run = bool(simulation_data.get("dry_run"))

        if is_simulation and not bool(
            getattr(
                config,
                "ISOLATED_INSTRUMENT_EVENT_INCLUDE_SIMULATION",
                True,
            )
        ):
            result["skipped"] = True
            result["error"] = "Simulation event saving is disabled."

            logger.info(
                "Isolated instrument simulation event saving skipped. "
                "event_id=%s",
                payload.get("event_id"),
            )

            return result

        if is_dry_run and not bool(
            getattr(
                config,
                "ISOLATED_INSTRUMENT_EVENT_INCLUDE_DRY_RUN",
                True,
            )
        ):
            result["skipped"] = True
            result["error"] = "Dry-run event saving is disabled."

            logger.info(
                "Isolated instrument dry-run event saving skipped. "
                "event_id=%s",
                payload.get("event_id"),
            )

            return result

        instrument_details = self._get_instrument_details(payload)
        instrument_key = instrument_details["instrument_key"]
        instrument_name = instrument_details["instrument_name"]

        result["instrument_key"] = instrument_key or None
        result["instrument_name"] = instrument_name or None

        if not instrument_key:
            result["skipped"] = True
            result["error"] = "Instrument key is unavailable."

            logger.warning(
                "Isolated instrument event saving skipped. "
                "reason=instrument_key_unavailable, event_id=%s",
                payload.get("event_id"),
            )

            return result

        market_datetime = self._get_market_datetime(processed_at)
        utc_datetime = datetime.now(timezone.utc)

        market_date = market_datetime.strftime("%Y-%m-%d")
        time_key = market_datetime.strftime("%H_%M_%S")
        document_id = f"{market_date}::{instrument_key}"
        event_field_name = self._get_event_field_name()
        event_field_path = f"{event_field_name}.{time_key}"

        result["document_id"] = document_id
        result["date"] = market_date
        result["time_key"] = time_key

        processing_event = self._build_processing_event(
            payload=payload,
            processing_result=processing_result,
            processed_at=market_datetime,
            processing_status=processing_status,
        )

        update_document = {
            "$setOnInsert": {
                "_id": document_id,
                "date": market_date,
                "timezone": str(
                    getattr(
                        config,
                        "MARKET_TIMEZONE",
                        "Asia/Kolkata",
                    )
                    or "Asia/Kolkata"
                ),
                "instrument_key": instrument_key,
                "created_at": market_datetime.isoformat(),
                "created_at_utc": utc_datetime,
            },
            "$set": {
                "instrument_name": instrument_name,
                "instrument_type": instrument_details.get(
                    "instrument_type"
                ),
                "option_type": instrument_details.get("option_type"),
                "strike_price": instrument_details.get("strike_price"),
                "expiry": instrument_details.get("expiry"),
                "lot_size": instrument_details.get("lot_size"),
                "underlying_symbol": instrument_details.get(
                    "underlying_symbol"
                ),
                "updated_at": market_datetime.isoformat(),
                "updated_at_utc": utc_datetime,
                "last_event_time": time_key,
                "last_event_id": payload.get("event_id"),
                "last_processing_status": processing_status,
            },
            "$push": {
                event_field_path: processing_event,
            },
            "$inc": {
                "event_count": 1,
            },
        }

        logger.info(
            "Saving isolated instrument processing event. "
            "document_id=%s, instrument_key=%s, instrument_name=%s, "
            "date=%s, time_key=%s, event_id=%s, status=%s",
            document_id,
            instrument_key,
            instrument_name,
            market_date,
            time_key,
            payload.get("event_id"),
            processing_status,
        )

        try:
            collection = self._get_collection()

            update_result = collection.update_one(
                {"_id": document_id},
                update_document,
                upsert=True,
            )

            result.update(
                {
                    "success": True,
                    "saved": True,
                    "matched_count": update_result.matched_count,
                    "modified_count": update_result.modified_count,
                    "upserted_id": (
                        str(update_result.upserted_id)
                        if update_result.upserted_id is not None
                        else None
                    ),
                }
            )

            logger.info(
                "Isolated instrument processing event saved successfully. "
                "document_id=%s, time_key=%s, event_id=%s, "
                "matched_count=%s, modified_count=%s, upserted_id=%s",
                document_id,
                time_key,
                payload.get("event_id"),
                update_result.matched_count,
                update_result.modified_count,
                update_result.upserted_id,
            )

            return result

        except (PyMongoError, ValueError, TypeError) as ex:
            result["error"] = f"{type(ex).__name__}: {ex}"

            logger.exception(
                "Failed saving isolated instrument processing event. "
                "document_id=%s, instrument_key=%s, time_key=%s, "
                "event_id=%s, error_type=%s",
                document_id,
                instrument_key,
                time_key,
                payload.get("event_id"),
                type(ex).__name__,
            )

            if not self._is_fail_open_enabled():
                raise

            return result

        except Exception as ex:
            result["error"] = f"{type(ex).__name__}: {ex}"

            logger.exception(
                "Unexpected error while saving isolated instrument "
                "processing event. document_id=%s, instrument_key=%s, "
                "time_key=%s, event_id=%s, error_type=%s",
                document_id,
                instrument_key,
                time_key,
                payload.get("event_id"),
                type(ex).__name__,
            )

            if not self._is_fail_open_enabled():
                raise

            return result

    def close(self) -> None:
        with self._connection_lock:
            if self._client is None:
                return

            try:
                self._client.close()

                logger.info(
                    "Isolated instrument event MongoDB connection closed."
                )
            except Exception:
                logger.exception(
                    "Failed closing isolated instrument event "
                    "MongoDB connection."
                )
            finally:
                self._client = None
                self._database = None
                self._collection = None
                self._indexes_initialized = False


isolated_instrument_event_saving_service = (
    IsolatedInstrumentEventSavingService()
)


__all__ = [
    "IsolatedInstrumentEventSavingService",
    "isolated_instrument_event_saving_service",
]