from __future__ import annotations

from datetime import datetime
from typing import Any

from bson import ObjectId
from pymongo.errors import PyMongoError

from core.config import settings
from core.database import get_order_executions_collection
from core.logger import get_logger

logger = get_logger("order_execution_log")


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


async def _ensure_daily_index(
    collection: Any,
) -> None:
    await collection.create_index(
        "document_date",
        unique=True,
    )


async def _get_or_create_daily_document(
    collection: Any,
    document_date: str,
    created_at: datetime,
) -> ObjectId:

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
        "orders": [],
        "created_at": created_at.isoformat(),
        "updated_at": created_at.isoformat(),
    }

    try:
        result = await collection.insert_one(
            daily_document,
        )

        logger.info(
            "Created order execution document id=%s document_date=%s",
            result.inserted_id,
            document_date,
        )

        return result.inserted_id

    except PyMongoError:

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

        raise


async def save_order_execution(
    payload: dict[str, Any],
    execution_result: dict[str, Any] | None,
) -> ObjectId | None:

    if not execution_result:
        return None

    collection = get_order_executions_collection()

    await _ensure_daily_index(
        collection,
    )

    created_at = _get_ist_now()

    document_date = _get_document_date(
        created_at,
    )

    event_id = payload.get("event_id")

    selected_instrument = execution_result.get(
        "selected_instrument",
        {},
    )

    order_document = {
        "event_id": event_id,
        "order_status": execution_result.get("order_status"),
        "reason": execution_result.get("reason"),
        "success": execution_result.get("success"),
        "instrument_key": selected_instrument.get("instrument_key"),
        "trading_symbol": selected_instrument.get("trading_symbol"),
        "lot_size": selected_instrument.get("lot_size"),
        "live_ltp": selected_instrument.get("live_ltp"),
        "exit_result": execution_result.get("exit_result"),
        "place_order_result": execution_result.get("place_order_result"),
        "created_at": created_at.isoformat(),
        "received_timezone": settings.app_timezone,
    }

    document_id = await _get_or_create_daily_document(
        collection=collection,
        document_date=document_date,
        created_at=created_at,
    )

    await collection.update_one(
        {
            "_id": document_id,
        },
        {
            "$push": {
                "orders": order_document,
            },
            "$set": {
                "updated_at": created_at.isoformat(),
            },
        },
    )

    logger.info(
        "Saved order execution event_id=%s status=%s",
        event_id,
        execution_result.get("order_status"),
    )

    return document_id
