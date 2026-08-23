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


async def save_test_payload(
    payload: dict[str, Any],
) -> ObjectId:
    """
    Save an arbitrary JSON payload directly to MongoDB.

    This function is used when TEST_FLG=True.

    No IsolatedEmaAlert validation is performed.
    The incoming JSON is stored as received, with
    application metadata added.
    """

    # Convert any datetime/date values that may exist
    # inside the incoming payload.
    document = convert_to_ist_iso(
        payload,
    )

    received_at_ist = datetime.now(
        settings.timezone,
    )

    document["received_at"] = received_at_ist.isoformat()

    document["received_timezone"] = settings.app_timezone

    document["schema_version"] = 1

    document["test_mode"] = True

    try:
        collection = get_order_requests_collection()

        result = await collection.insert_one(
            document,
        )

    except InvalidDocument:
        logger.exception("MongoDB could not encode test payload.")
        raise

    except PyMongoError:
        logger.exception("MongoDB insert failed while saving " "test payload.")
        raise

    except Exception:
        logger.exception("Unexpected error while saving " "test payload.")
        raise

    logger.info(
        "Saved TEST payload " "id=%s",
        result.inserted_id,
    )

    return result.inserted_id


async def save_order_request(
    alert: IsolatedEmaAlert,
) -> ObjectId:
    """
    Validate, prepare, and save an isolated EMA alert
    to MongoDB.

    This is the normal/production save path.
    """

    # Keep Python values initially so that we can explicitly
    # control date and datetime serialization.
    raw_document = alert.model_dump(
        mode="python",
    )

    # Convert datetime.date and datetime.datetime values
    # into MongoDB-safe IST ISO strings.
    document = convert_to_ist_iso(
        raw_document,
    )

    received_at_ist = datetime.now(
        settings.timezone,
    )

    document["received_at"] = received_at_ist.isoformat()

    document["received_timezone"] = settings.app_timezone

    document["schema_version"] = 1

    try:
        collection = get_order_requests_collection()

        result = await collection.insert_one(
            document,
        )

    except InvalidDocument:
        logger.exception(
            "MongoDB could not encode the document " "for instrument_key=%s",
            alert.instrument_key,
        )
        raise

    except PyMongoError:
        logger.exception(
            "MongoDB insert failed " "for instrument_key=%s",
            alert.instrument_key,
        )
        raise

    except Exception:
        logger.exception(
            "Unexpected error while saving order request " "for instrument_key=%s",
            alert.instrument_key,
        )
        raise

    logger.info(
        "Saved order request "
        "id=%s "
        "instrument_key=%s "
        "cross_type=%s "
        "received_at=%s",
        result.inserted_id,
        alert.instrument_key,
        alert.ema_event.cross_type,
        document["received_at"],
    )

    return result.inserted_id
