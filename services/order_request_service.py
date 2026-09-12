from __future__ import annotations

from datetime import date, datetime
from typing import Any

from bson import ObjectId
from bson.errors import InvalidDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

from core.config import settings
from core.database import get_order_requests_collection
from core.logger import get_logger
from models.alert import IsolatedEmaAlert

logger = get_logger("order_requests")

EXCLUDED_HISTORICAL_FIELDS = frozenset(
    {
        "last_ema_alert",
    }
)


def remove_historical_alert_data(
    value: Any,
) -> tuple[Any, int]:
    removed_count = 0

    if isinstance(value, dict):
        cleaned_value: dict[str, Any] = {}

        for key, item in value.items():
            if key in EXCLUDED_HISTORICAL_FIELDS:
                removed_count += 1
                continue

            cleaned_item, nested_removed_count = remove_historical_alert_data(item)

            cleaned_value[key] = cleaned_item
            removed_count += nested_removed_count

        return cleaned_value, removed_count

    if isinstance(value, list):
        cleaned_list: list[Any] = []

        for item in value:
            cleaned_item, nested_removed_count = remove_historical_alert_data(item)

            cleaned_list.append(cleaned_item)
            removed_count += nested_removed_count

        return cleaned_list, removed_count

    if isinstance(value, tuple):
        cleaned_list: list[Any] = []

        for item in value:
            cleaned_item, nested_removed_count = remove_historical_alert_data(item)

            cleaned_list.append(cleaned_item)
            removed_count += nested_removed_count

        return cleaned_list, removed_count

    return value, removed_count


def convert_to_ist_iso(
    value: Any,
) -> Any:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            ist_datetime = value.replace(
                tzinfo=settings.timezone,
            )
        else:
            ist_datetime = value.astimezone(
                settings.timezone,
            )

        return ist_datetime.isoformat()

    if isinstance(value, date):
        return value.isoformat()

    if isinstance(value, dict):
        return {key: convert_to_ist_iso(item) for key, item in value.items()}

    if isinstance(value, list):
        return [convert_to_ist_iso(item) for item in value]

    if isinstance(value, tuple):
        return [convert_to_ist_iso(item) for item in value]

    return value


def _get_ist_now() -> datetime:
    return datetime.now(
        settings.timezone,
    )


def _get_document_date(
    value: datetime,
) -> str:
    if value.tzinfo is None:
        value = value.replace(
            tzinfo=settings.timezone,
        )
    else:
        value = value.astimezone(
            settings.timezone,
        )

    return value.date().isoformat()


async def _ensure_order_request_indexes(
    collection: Any,
) -> None:
    try:
        await collection.drop_index("unique_document_date")
        logger.info(
            "Removed obsolete unique daily document index " "index=unique_document_date"
        )
    except PyMongoError as exc:
        error_text = str(exc).lower()

        if "index not found" not in error_text and "indexnotfound" not in error_text:
            logger.warning(
                "Could not remove obsolete daily index "
                "index=unique_document_date error=%s",
                type(exc).__name__,
            )

    await collection.create_index(
        "document_date",
        name="idx_document_date",
    )

    await collection.create_index(
        "event_id",
        name="idx_event_id",
    )

    await collection.create_index(
        "received_at",
        name="idx_received_at",
    )

    await collection.create_index(
        [
            ("document_date", 1),
            ("received_at", -1),
        ],
        name="idx_document_date_received_at",
    )


async def _insert_order_request_document(
    payload: dict[str, Any],
    document_date: str,
    received_at_ist: datetime,
    test_mode: bool = False,
) -> ObjectId:
    if not isinstance(payload, dict):
        raise ValueError("Order request payload must be a dictionary")

    collection = get_order_requests_collection()

    await _ensure_order_request_indexes(
        collection,
    )

    sanitized_payload, removed_history_count = remove_historical_alert_data(payload)

    if not isinstance(sanitized_payload, dict):
        raise ValueError("Sanitized order request payload must be a dictionary")

    request_document = convert_to_ist_iso(
        sanitized_payload,
    )

    if not isinstance(request_document, dict):
        raise ValueError("Converted order request payload must be a dictionary")

    request_document["document_date"] = document_date
    request_document["received_at"] = received_at_ist.isoformat()
    request_document["received_timezone"] = settings.app_timezone
    request_document["receiver_test_mode"] = bool(test_mode)
    request_document["storage_schema_version"] = 2

    if "test_mode" in request_document:
        request_document["producer_test_mode"] = request_document.pop("test_mode")

    event_id = request_document.get("event_id")

    raw_ema_event = request_document.get("raw_ema_event")

    instrument_key = None

    if isinstance(raw_ema_event, dict):
        instrument_key = raw_ema_event.get("instrument_key")

    logger.info(
        "Saving individual order-request document "
        "event_id=%s instrument_key=%s "
        "document_date=%s test_mode=%s "
        "removed_history_fields=%s",
        event_id,
        instrument_key,
        document_date,
        test_mode,
        removed_history_count,
    )

    try:
        result = await collection.insert_one(
            request_document,
        )

    except DuplicateKeyError:
        logger.exception(
            "Duplicate order-request document rejected "
            "event_id=%s instrument_key=%s "
            "document_date=%s",
            event_id,
            instrument_key,
            document_date,
        )
        raise

    except InvalidDocument:
        logger.exception(
            "MongoDB could not encode order-request document "
            "event_id=%s instrument_key=%s "
            "document_date=%s",
            event_id,
            instrument_key,
            document_date,
        )
        raise

    except PyMongoError:
        logger.exception(
            "MongoDB insert failed for order request "
            "event_id=%s instrument_key=%s "
            "document_date=%s",
            event_id,
            instrument_key,
            document_date,
        )
        raise

    except Exception:
        logger.exception(
            "Unexpected error while inserting order request "
            "event_id=%s instrument_key=%s "
            "document_date=%s",
            event_id,
            instrument_key,
            document_date,
        )
        raise

    logger.info(
        "Inserted individual order-request document "
        "id=%s event_id=%s instrument_key=%s "
        "document_date=%s test_mode=%s",
        result.inserted_id,
        event_id,
        instrument_key,
        document_date,
        test_mode,
    )

    return result.inserted_id


async def save_processed_order_request(
    payload: dict[str, Any],
    *,
    test_mode: bool = False,
) -> ObjectId:
    if not isinstance(payload, dict):
        raise ValueError("Processed order request payload must be a dictionary")

    received_at_ist = _get_ist_now()

    document_date = _get_document_date(
        received_at_ist,
    )

    document_id = await _insert_order_request_document(
        payload=payload,
        document_date=document_date,
        received_at_ist=received_at_ist,
        test_mode=test_mode,
    )

    logger.info(
        "Saved processed order request "
        "id=%s event_id=%s document_date=%s "
        "test_mode=%s received_at=%s",
        document_id,
        payload.get("event_id"),
        document_date,
        test_mode,
        received_at_ist.isoformat(),
    )

    return document_id


async def save_test_payload(
    payload: dict[str, Any],
) -> ObjectId:
    if not isinstance(payload, dict):
        raise ValueError("Test payload must be a dictionary")

    document_id = await save_processed_order_request(
        payload=payload,
        test_mode=True,
    )

    logger.info(
        "Saved test order-request document " "id=%s event_id=%s",
        document_id,
        payload.get("event_id"),
    )

    return document_id


async def save_order_request(
    alert: IsolatedEmaAlert,
) -> ObjectId:
    if not isinstance(alert, IsolatedEmaAlert):
        raise TypeError("alert must be an IsolatedEmaAlert instance")

    raw_document = alert.model_dump(
        mode="python",
    )

    document_id = await save_processed_order_request(
        payload=raw_document,
        test_mode=False,
    )

    instrument_key = None
    cross_type = None

    if alert.raw_ema_event is not None:
        instrument_key = alert.raw_ema_event.instrument_key
        cross_type = alert.raw_ema_event.cross_type

    logger.info(
        "Saved production order-request document "
        "id=%s event_id=%s instrument_key=%s "
        "cross_type=%s",
        document_id,
        alert.event_id,
        instrument_key,
        cross_type,
    )

    return document_id
