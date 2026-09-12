from __future__ import annotations

from datetime import datetime
from typing import Any

from bson import ObjectId
from bson.errors import InvalidDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

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


async def _remove_obsolete_daily_unique_indexes(
    collection: Any,
) -> None:
    indexes = await collection.index_information()

    for index_name, index_details in indexes.items():
        if index_name == "_id_":
            continue

        index_keys = index_details.get("key", [])
        is_unique = bool(
            index_details.get("unique", False)
        )

        if (
            is_unique
            and index_keys == [("document_date", 1)]
        ):
            try:
                await collection.drop_index(
                    index_name
                )

                logger.info(
                    "Removed obsolete unique daily execution index "
                    "index=%s",
                    index_name,
                )
            except PyMongoError:
                logger.exception(
                    "Failed removing obsolete unique daily "
                    "execution index index=%s",
                    index_name,
                )
                raise


async def _ensure_order_execution_indexes(
    collection: Any,
) -> None:
    await _remove_obsolete_daily_unique_indexes(
        collection
    )

    await collection.create_index(
        "document_date",
        name="idx_execution_document_date",
    )

    await collection.create_index(
        "event_id",
        name="idx_execution_event_id",
    )

    await collection.create_index(
        "created_at",
        name="idx_execution_created_at",
    )

    await collection.create_index(
        "order_status",
        name="idx_execution_order_status",
    )

    await collection.create_index(
        "instrument_key",
        name="idx_execution_instrument_key",
    )

    await collection.create_index(
        [
            ("document_date", 1),
            ("created_at", -1),
        ],
        name="idx_execution_date_created_at",
    )

    await collection.create_index(
        [
            ("event_id", 1),
            ("created_at", -1),
        ],
        name="idx_execution_event_created_at",
    )


def _get_selected_instrument(
    execution_result: dict[str, Any],
) -> dict[str, Any]:
    selected_instrument = execution_result.get(
        "selected_instrument"
    )

    if not isinstance(selected_instrument, dict):
        return {}

    return selected_instrument


def _build_order_execution_document(
    payload: dict[str, Any],
    execution_result: dict[str, Any],
    created_at: datetime,
    document_date: str,
) -> dict[str, Any]:
    selected_instrument = _get_selected_instrument(
        execution_result
    )

    return {
        "storage_schema_version": 2,
        "document_date": document_date,
        "event_id": payload.get("event_id"),
        "order_status": execution_result.get(
            "order_status"
        ),
        "reason": execution_result.get("reason"),
        "success": bool(
            execution_result.get("success", False)
        ),
        "instrument_key": selected_instrument.get(
            "instrument_key"
        ),
        "trading_symbol": selected_instrument.get(
            "trading_symbol"
        ),
        "instrument_type": selected_instrument.get(
            "instrument_type"
        ),
        "strike_price": selected_instrument.get(
            "strike_price"
        ),
        "lot_size": selected_instrument.get(
            "lot_size"
        ),
        "live_ltp": selected_instrument.get(
            "live_ltp"
        ),
        "exit_result": execution_result.get(
            "exit_result"
        ),
        "place_order_result": execution_result.get(
            "place_order_result"
        ),
        "error": execution_result.get("error"),
        "created_at": created_at.isoformat(),
        "received_timezone": settings.app_timezone,
    }


async def save_order_execution(
    payload: dict[str, Any],
    execution_result: dict[str, Any] | None,
) -> ObjectId | None:
    if not isinstance(payload, dict):
        raise ValueError(
            "Order execution payload must be a dictionary"
        )

    if not execution_result:
        logger.info(
            "Order execution save skipped because execution "
            "result is empty event_id=%s",
            payload.get("event_id"),
        )
        return None

    if not isinstance(execution_result, dict):
        raise ValueError(
            "Execution result must be a dictionary"
        )

    collection = get_order_executions_collection()

    try:
        await _ensure_order_execution_indexes(
            collection
        )
    except PyMongoError:
        logger.exception(
            "Failed ensuring order execution indexes"
        )
        raise

    created_at = _get_ist_now()

    document_date = _get_document_date(
        created_at
    )

    order_document = _build_order_execution_document(
        payload=payload,
        execution_result=execution_result,
        created_at=created_at,
        document_date=document_date,
    )

    event_id = order_document.get("event_id")
    instrument_key = order_document.get(
        "instrument_key"
    )
    order_status = order_document.get(
        "order_status"
    )

    logger.info(
        "Saving individual order execution "
        "event_id=%s instrument_key=%s "
        "status=%s document_date=%s",
        event_id,
        instrument_key,
        order_status,
        document_date,
    )

    try:
        result = await collection.insert_one(
            order_document
        )

    except DuplicateKeyError:
        logger.exception(
            "Duplicate order execution rejected "
            "event_id=%s instrument_key=%s "
            "status=%s document_date=%s",
            event_id,
            instrument_key,
            order_status,
            document_date,
        )
        raise

    except InvalidDocument:
        logger.exception(
            "MongoDB could not encode order execution "
            "event_id=%s instrument_key=%s "
            "status=%s document_date=%s",
            event_id,
            instrument_key,
            order_status,
            document_date,
        )
        raise

    except PyMongoError:
        logger.exception(
            "MongoDB insert failed for order execution "
            "event_id=%s instrument_key=%s "
            "status=%s document_date=%s",
            event_id,
            instrument_key,
            order_status,
            document_date,
        )
        raise

    except Exception:
        logger.exception(
            "Unexpected error while saving order execution "
            "event_id=%s instrument_key=%s "
            "status=%s document_date=%s",
            event_id,
            instrument_key,
            order_status,
            document_date,
        )
        raise

    logger.info(
        "Saved individual order execution "
        "id=%s event_id=%s instrument_key=%s "
        "status=%s document_date=%s",
        result.inserted_id,
        event_id,
        instrument_key,
        order_status,
        document_date,
    )

    return result.inserted_id