from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import ValidationError
from pymongo.errors import PyMongoError

from core.config import settings
from core.logger import get_logger
from models.alert import IsolatedEmaAlert
from services.instrument_selection_service import (
    select_nearest_instrument_with_details,
)
from services.order_execution_log_service import (
    save_order_execution,
)
from services.order_request_service import (
    save_order_request,
    save_test_payload,
)
from upstox_services.process_order import (
    process_selected_instrument,
)

logger = get_logger("order_requests_api")

router = APIRouter(
    prefix="/order-requests",
    tags=["Order Requests"],
)


def _extract_raw_ema_event(
    payload: dict[str, Any],
) -> dict[str, Any]:
    raw_ema_event = payload.get("raw_ema_event")

    if not isinstance(raw_ema_event, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Payload must contain a 'raw_ema_event' object",
        )

    return raw_ema_event


def _to_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None

    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _get_target_price(
    raw_ema_event: dict[str, Any],
    payload: dict[str, Any],
) -> float:
    candidate_values = (
        payload.get("target_price"),
        payload.get("requested_price"),
        raw_ema_event.get("target_price"),
        raw_ema_event.get("requested_price"),
        raw_ema_event.get("ltp"),
        raw_ema_event.get("close"),
    )

    for value in candidate_values:
        converted = _to_float(value)

        if converted is not None:
            return converted

    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=(
            "Unable to determine target price. "
            "Provide target_price/requested_price or "
            "a numeric ltp/close in raw_ema_event."
        ),
    )


def _get_available_instruments(
    payload: dict[str, Any],
    raw_ema_event: dict[str, Any],
) -> list[dict[str, Any]]:
    del raw_ema_event

    instruments = payload.get("available_instruments")

    if isinstance(instruments, list):
        return [
            instrument for instrument in instruments if isinstance(instrument, dict)
        ]

    order_suggestion = payload.get("order_suggestion")

    if not isinstance(order_suggestion, dict):
        return []

    budget_filter = order_suggestion.get("budget_filter")

    if isinstance(budget_filter, dict):
        instruments = budget_filter.get("instruments")

        if isinstance(instruments, list):
            return [
                instrument for instrument in instruments if isinstance(instrument, dict)
            ]

    budget_range = order_suggestion.get("budget_range")

    if isinstance(budget_range, dict):
        instruments = budget_range.get("instruments")

        if isinstance(instruments, list):
            return [
                instrument for instrument in instruments if isinstance(instrument, dict)
            ]

    return []


def _normalize_instrument_type(
    value: Any,
) -> str | None:
    if value is None:
        return None

    normalized_value = str(value).upper().strip()

    if normalized_value in {"CE", "PE"}:
        return normalized_value

    return None


def _get_instrument_type(
    payload: dict[str, Any],
    raw_ema_event: dict[str, Any],
) -> str | None:
    instrument_type = _normalize_instrument_type(payload.get("instrument_type"))

    if instrument_type:
        return instrument_type

    order_suggestion = payload.get("order_suggestion")

    if isinstance(order_suggestion, dict):
        instrument_type = _normalize_instrument_type(
            order_suggestion.get("suggested_order_side")
        )

        if instrument_type:
            return instrument_type

    instrument_type = _normalize_instrument_type(raw_ema_event.get("instrument_type"))

    if instrument_type:
        return instrument_type

    contract_info = raw_ema_event.get("contract_info")

    if isinstance(contract_info, dict):
        instrument_type = _normalize_instrument_type(
            contract_info.get("instrument_type")
        )

        if instrument_type:
            return instrument_type

        instrument_type = _normalize_instrument_type(contract_info.get("option_type"))

        if instrument_type:
            return instrument_type

    if isinstance(order_suggestion, dict):
        instrument_type = _normalize_instrument_type(
            order_suggestion.get("isolated_instrument_type")
        )

        if instrument_type:
            return instrument_type

    return None


def _process_order_selection(
    payload: dict[str, Any],
) -> dict[str, Any]:
    event_id = payload.get("event_id")

    raw_ema_event = _extract_raw_ema_event(payload)

    target_price = _get_target_price(
        raw_ema_event=raw_ema_event,
        payload=payload,
    )

    candidate_instruments = _get_available_instruments(
        payload=payload,
        raw_ema_event=raw_ema_event,
    )

    instrument_type = _get_instrument_type(
        payload=payload,
        raw_ema_event=raw_ema_event,
    )

    logger.info(
        "Starting instrument selection "
        "event_id=%s target_price=%s "
        "instrument_type=%s candidates=%s",
        event_id,
        target_price,
        instrument_type,
        len(candidate_instruments),
    )

    selection_details = select_nearest_instrument_with_details(
        instruments=candidate_instruments,
        target_price=target_price,
        instrument_type=instrument_type,
    )

    selected_instrument = selection_details.get("selected_instrument")

    execution_result = None

    if selected_instrument:
        logger.info(
            "Instrument selected event_id=%s "
            "instrument_key=%s symbol=%s live_ltp=%s",
            event_id,
            selected_instrument.get("instrument_key"),
            selected_instrument.get("trading_symbol"),
            selected_instrument.get("live_ltp"),
        )

        try:
            execution_result = process_selected_instrument(selected_instrument)
        except Exception as exc:
            logger.exception(
                "Order processing failed event_id=%s " "instrument_key=%s",
                event_id,
                selected_instrument.get("instrument_key"),
            )

            execution_result = {
                "success": False,
                "order_status": "PROCESSING_FAILED",
                "reason": "Unexpected order processing error",
                "selected_instrument": selected_instrument,
                "exit_result": None,
                "place_order_result": None,
                "error": str(exc),
            }
    else:
        logger.warning(
            "No instrument selected event_id=%s " "target_price=%s instrument_type=%s",
            event_id,
            target_price,
            instrument_type,
        )

    return {
        "raw_ema_event": raw_ema_event,
        "target_price": target_price,
        "instrument_type": instrument_type,
        "candidate_instrument_count": len(candidate_instruments),
        "available_instrument_count": selection_details.get(
            "available_instrument_count",
            0,
        ),
        "selection_attempted": bool(candidate_instruments),
        "selection_performed": selected_instrument is not None,
        "selected_instrument": selected_instrument,
        "price_difference": selection_details.get("price_difference"),
        "execution_result": execution_result,
    }


def _build_processing_response(
    processing_result: dict[str, Any],
) -> dict[str, Any]:
    price_difference = processing_result.get("price_difference")

    if isinstance(price_difference, float):
        price_difference = round(price_difference, 2)

    return {
        "target_price": processing_result["target_price"],
        "instrument_type": processing_result["instrument_type"],
        "candidate_instrument_count": processing_result["candidate_instrument_count"],
        "available_instrument_count": processing_result["available_instrument_count"],
        "selection_attempted": processing_result["selection_attempted"],
        "selection_performed": processing_result["selection_performed"],
        "selected_instrument": processing_result["selected_instrument"],
        "price_difference": price_difference,
        "execution_result": processing_result.get("execution_result"),
    }


def _attach_processing_result(
    payload: dict[str, Any],
    processing_result: dict[str, Any],
) -> dict[str, Any]:
    processed_payload = dict(payload)

    processed_payload["processing"] = _build_processing_response(processing_result)

    return processed_payload


async def _save_execution_log(
    payload: dict[str, Any],
    execution_result: dict[str, Any] | None,
) -> str | None:
    event_id = payload.get("event_id")

    if execution_result is None:
        logger.info(
            "Order execution log skipped because no execution "
            "result exists event_id=%s",
            event_id,
        )
        return None

    try:
        execution_document_id = await save_order_execution(
            payload=payload,
            execution_result=execution_result,
        )

        if execution_document_id is None:
            logger.warning(
                "Order execution log was not saved " "event_id=%s status=%s",
                event_id,
                execution_result.get("order_status"),
            )
            return None

        logger.info(
            "Order execution log saved " "event_id=%s status=%s document_id=%s",
            event_id,
            execution_result.get("order_status"),
            execution_document_id,
        )

        return str(execution_document_id)

    except PyMongoError:
        logger.exception(
            "MongoDB error while saving order execution " "event_id=%s status=%s",
            event_id,
            execution_result.get("order_status"),
        )
        return None

    except Exception:
        logger.exception(
            "Unexpected error while saving order execution " "event_id=%s status=%s",
            event_id,
            execution_result.get("order_status"),
        )
        return None


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
)
async def create_order_request(
    request: Request,
) -> dict[str, Any]:
    try:
        payload = await request.json()
    except Exception as exc:
        logger.warning(
            "Invalid JSON request path=%s client=%s",
            request.url.path,
            request.client.host if request.client else None,
        )

        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid JSON payload",
        ) from exc

    if not isinstance(payload, dict):
        logger.warning(
            "Non-object JSON request path=%s payload_type=%s",
            request.url.path,
            type(payload).__name__,
        )

        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="JSON payload must be an object",
        )

    event_id = payload.get("event_id")
    raw_ema_event = payload.get("raw_ema_event")

    instrument_key = None

    if isinstance(raw_ema_event, dict):
        instrument_key = raw_ema_event.get("instrument_key")

    logger.info(
        "Order request received "
        "event_id=%s instrument_key=%s "
        "test_mode=%s place_order=%s",
        event_id,
        instrument_key,
        settings.test_flg,
        settings.PLACE_ORDER,
    )

    try:
        processing_result = _process_order_selection(payload)
    except HTTPException:
        logger.warning(
            "Order request processing rejected " "event_id=%s instrument_key=%s",
            event_id,
            instrument_key,
        )
        raise
    except Exception as exc:
        logger.exception(
            "Unexpected order request processing error "
            "event_id=%s instrument_key=%s",
            event_id,
            instrument_key,
        )

        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unable to process order request: {exc}",
        ) from exc

    processed_payload = _attach_processing_result(
        payload=payload,
        processing_result=processing_result,
    )

    processing_response = processed_payload["processing"]

    execution_result = processing_result.get("execution_result")

    execution_document_id = await _save_execution_log(
        payload=payload,
        execution_result=execution_result,
    )

    if settings.test_flg:
        try:
            inserted_id = await save_test_payload(processed_payload)
        except PyMongoError as exc:
            logger.exception(
                "Unable to save test request " "event_id=%s instrument_key=%s",
                event_id,
                instrument_key,
            )

            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Unable to save test payload",
            ) from exc
        except Exception as exc:
            logger.exception(
                "Unexpected test-request save error " "event_id=%s instrument_key=%s",
                event_id,
                instrument_key,
            )

            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Unable to save test payload",
            ) from exc

        logger.info(
            "Test order request completed successfully "
            "event_id=%s request_document_id=%s "
            "execution_document_id=%s order_status=%s",
            event_id,
            inserted_id,
            execution_document_id,
            (execution_result.get("order_status") if execution_result else None),
        )

        return {
            "status": "success",
            "message": "Test payload saved and processed",
            "mode": "test",
            "id": str(inserted_id),
            "execution_log_id": execution_document_id,
            "processing": processing_response,
        }

    try:
        alert = IsolatedEmaAlert.model_validate(processed_payload)
    except ValidationError as exc:
        logger.warning(
            "Production payload validation failed "
            "event_id=%s instrument_key=%s errors=%s",
            event_id,
            instrument_key,
            exc.errors(),
        )

        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=exc.errors(),
        ) from exc

    try:
        inserted_id = await save_order_request(alert)
    except PyMongoError as exc:
        logger.exception(
            "Unable to save production order request " "event_id=%s instrument_key=%s",
            event_id,
            instrument_key,
        )

        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Unable to save order request",
        ) from exc
    except Exception as exc:
        logger.exception(
            "Unexpected production-request save error " "event_id=%s instrument_key=%s",
            event_id,
            instrument_key,
        )

        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Unable to save order request",
        ) from exc

    logger.info(
        "Production order request completed successfully "
        "event_id=%s request_document_id=%s "
        "execution_document_id=%s order_status=%s",
        event_id,
        inserted_id,
        execution_document_id,
        (execution_result.get("order_status") if execution_result else None),
    )

    return {
        "status": "success",
        "message": "Order request saved and processed",
        "mode": "production",
        "id": str(inserted_id),
        "execution_log_id": execution_document_id,
        "processing": processing_response,
    }
