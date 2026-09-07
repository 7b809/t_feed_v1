from __future__ import annotations

from datetime import date, datetime
from typing import Any

from bson import ObjectId
from bson.errors import InvalidDocument
from pymongo.errors import PyMongoError

from core.config import settings
from core.database import get_order_requests_collection
from core.logger import get_logger
from models.alert import IsolatedEmaAlert

logger = get_logger("order_requests")


def convert_to_ist_iso(
    value: Any,
) -> Any:
    """
    Recursively convert Python values into MongoDB-safe values.

    Conversion rules:

    1. datetime:
       - Naive datetime is treated as Asia/Kolkata.
       - Aware datetime is converted to Asia/Kolkata.
       - The result is stored as an ISO-formatted string.

    2. date:
       - Stored as an ISO date string such as 2026-08-27.

    3. dict, list, tuple:
       - Converted recursively.

    4. Other supported values:
       - Returned without modification.
    """

    # datetime must be checked before date because
    # datetime is a subclass of date.
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
    """Return the current datetime in application timezone."""

    return datetime.now(
        settings.timezone,
    )


def _get_document_date(
    value: datetime,
) -> str:
    """
    Return the calendar date used for daily MongoDB grouping.

    The date is always calculated after converting the datetime
    to the application timezone.
    """

    if value.tzinfo is None:
        value = value.replace(
            tzinfo=settings.timezone,
        )
    else:
        value = value.astimezone(
            settings.timezone,
        )

    return value.date().isoformat()


async def _ensure_daily_index(
    collection: Any,
) -> None:
    """
    Ensure only one MongoDB document can exist for each date.

    This makes the daily document creation safe against
    concurrent requests.
    """

    await collection.create_index(
        "document_date",
        unique=True,
        name="unique_document_date",
    )


async def _get_or_create_daily_document(
    collection: Any,
    document_date: str,
    received_at_ist: datetime,
) -> ObjectId:
    """
    Get today's daily document or create it if it does not exist.

    The parent daily document is created separately from the
    request append operation.

    This avoids the MongoDB conflict that occurs when the same
    update attempts to use both:

        $setOnInsert: {"requests": []}

    and:

        $push: {"requests": request_document}
    """

    existing_document = await collection.find_one(
        {
            "document_date": document_date,
        },
        {
            "_id": 1,
        },
    )

    if existing_document:
        return existing_document["_id"]

    daily_document = {
    "document_date": document_date,
    "received_timezone": settings.app_timezone,
    "schema_version": 1,
    "requests": [],
    "created_at": received_at_ist.isoformat(),
    "updated_at": received_at_ist.isoformat(),
}
    try:
        result = await collection.insert_one(
            daily_document,
        )

        logger.info(
            "Created new daily order-request document " "id=%s document_date=%s",
            result.inserted_id,
            document_date,
        )

        return result.inserted_id

    except PyMongoError:
        # Another request may have created the document between
        # find_one() and insert_one(). Fetch it again.
        existing_document = await collection.find_one(
            {
                "document_date": document_date,
            },
            {
                "_id": 1,
            },
        )

        if existing_document:
            return existing_document["_id"]

        logger.exception(
            "Failed to create daily order-request document " "for document_date=%s",
            document_date,
        )
        raise


async def _append_to_daily_document(
    payload: dict[str, Any],
    document_date: str,
    received_at_ist: datetime,
    test_mode: bool = False,
) -> ObjectId:
    """
    Append one processed request to the MongoDB document
    belonging to a specific IST calendar date.

    MongoDB structure:

    {
        "_id": ObjectId("..."),
        "document_date": "2026-09-07",
        "received_timezone": "Asia/Kolkata",
        "schema_version": 1,
        "test_mode": false,
        "requests": [
            {
                ...request fields...,
                "processing": {
                    "target_price": 125.5,
                    "instrument_type": "CE",
                    "selected_instrument": {...},
                    "price_difference": 5.9
                },
                "received_at": "...",
                "received_timezone": "Asia/Kolkata",
                "test_mode": true
            }
        ]
    }

    One MongoDB document is maintained for each calendar date.
    """

    if not isinstance(payload, dict):
        raise ValueError("Order request payload must be a dictionary")

    collection = get_order_requests_collection()

    await _ensure_daily_index(
        collection,
    )

    # ---------------------------------------------------------
    # Convert all datetime/date values before sending the
    # request document to MongoDB.
    # ---------------------------------------------------------
    request_document = convert_to_ist_iso(
        payload,
    )

    if not isinstance(request_document, dict):
        raise ValueError("Converted order request payload must be a dictionary")

    # ---------------------------------------------------------
    # Per-request metadata.
    # ---------------------------------------------------------
    request_document["received_at"] = received_at_ist.isoformat()

    request_document["received_timezone"] = settings.app_timezone

    request_document["receiver_test_mode"] = test_mode

    if "test_mode" in request_document:
        request_document["producer_test_mode"] = request_document.pop(
            "test_mode"
        )

    # ---------------------------------------------------------
    # Ensure the daily parent document exists first.
    #
    # IMPORTANT:
    #
    # We do NOT use $setOnInsert to create "requests"
    # while simultaneously using $push on "requests".
    # ---------------------------------------------------------
    try:
        document_id = await _get_or_create_daily_document(
            collection=collection,
            document_date=document_date,
            received_at_ist=received_at_ist,
        )

        # -----------------------------------------------------
        # Append the complete processed request.
        #
        # The payload may already contain:
        #
        # "processing": {
        #     "target_price": ...,
        #     "instrument_type": ...,
        #     "selected_instrument": {...},
        #     "price_difference": ...
        # }
        #
        # Therefore the receiver-side instrument selection is
        # persisted together with the original request.
        # -----------------------------------------------------
        result = await collection.update_one(
            {
                "_id": document_id,
            },
            {
                "$push": {
                    "requests": request_document,
                },
                "$set": {
                    "updated_at": received_at_ist.isoformat(),
                },
            },
        )

    except InvalidDocument:
        logger.exception(
            "MongoDB could not encode daily document " "for document_date=%s",
            document_date,
        )
        raise

    except PyMongoError:
        logger.exception(
            "MongoDB update failed " "for document_date=%s",
            document_date,
        )
        raise

    except Exception:
        logger.exception(
            "Unexpected error while updating daily document " "for document_date=%s",
            document_date,
        )
        raise

    if result.matched_count != 1:
        raise RuntimeError(
            "Daily MongoDB document was not found while appending "
            f"request for document_date={document_date}"
        )

    logger.info(
        "Appended order request to daily document " "id=%s document_date=%s",
        document_id,
        document_date,
    )

    return document_id


async def save_processed_order_request(
    payload: dict[str, Any],
    *,
    test_mode: bool = False,
) -> ObjectId:
    """
    Save an already-processed order request.

    This is the generic save function for the new API flow.

    The API layer can process a request such as:

        {
            "raw_ema_event": {...},
            "order_suggestion": {...},
            "processing": {
                "target_price": 125.5,
                "instrument_type": "CE",
                "selected_instrument": {...},
                "price_difference": 5.9
            }
        }

    The complete processed payload is stored as one request
    entry inside the daily MongoDB document.

    All requests received on the same IST calendar date share
    the same MongoDB parent document.
    """

    if not isinstance(payload, dict):
        raise ValueError("Processed order request payload must be a dictionary")

    received_at_ist = _get_ist_now()

    document_date = _get_document_date(
        received_at_ist,
    )

    document_id = await _append_to_daily_document(
        payload=payload,
        document_date=document_date,
        received_at_ist=received_at_ist,
        test_mode=test_mode,
    )

    logger.info(
        "Saved processed order request "
        "id=%s "
        "document_date=%s "
        "test_mode=%s "
        "received_at=%s",
        document_id,
        document_date,
        test_mode,
        received_at_ist.isoformat(),
    )

    return document_id


async def save_test_payload(
    payload: dict[str, Any],
) -> ObjectId:
    """
    Save an arbitrary JSON payload to the MongoDB daily document.

    This function is used when TEST_FLG=True.

    No IsolatedEmaAlert validation is performed.

    The payload may already contain receiver-side processing
    information, including the selected instrument.

    All test payloads received on the same IST calendar date
    are stored inside the same MongoDB document.
    """

    if not isinstance(payload, dict):
        raise ValueError("Test payload must be a dictionary")

    document_id = await save_processed_order_request(
        payload=payload,
        test_mode=True,
    )

    logger.info(
        "Saved TEST payload " "id=%s",
        document_id,
    )

    return document_id


async def save_order_request(
    alert: IsolatedEmaAlert,
) -> ObjectId:
    raw_document = alert.model_dump(
        mode="python",
    )

    document_id = await save_processed_order_request(
        payload=raw_document,
        test_mode=False,
    )

    instrument_key = None
    cross_type = None

    try:
        instrument_key = alert.raw_ema_event.instrument_key
    except Exception:
        pass

    try:
        cross_type = alert.raw_ema_event.cross_type
    except Exception:
        pass

    logger.info(
        "Saved order request id=%s instrument_key=%s cross_type=%s",
        document_id,
        instrument_key,
        cross_type,
    )

    return document_id