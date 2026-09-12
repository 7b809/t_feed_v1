from copy import deepcopy
from time import perf_counter
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


# ------------------------------------------------------------------
# Detailed request logging helpers
# ------------------------------------------------------------------

def _log_step(
    step: str,
    event_id: Any = None,
    instrument_key: Any = None,
    **details: Any,
) -> None:
    """
    Write a consistent step-level log entry.

    Example:
        STEP=INSTRUMENT_SELECTION_STARTED
        event_id=123
        instrument_key=...
    """

    detail_parts = [
        f"event_id={event_id}",
        f"instrument_key={instrument_key}",
    ]

    for key, value in details.items():
        detail_parts.append(f"{key}={value}")

    logger.info(
        "STEP=%s %s",
        step,
        " ".join(detail_parts),
    )


def _log_debug_step(
    step: str,
    event_id: Any = None,
    instrument_key: Any = None,
    **details: Any,
) -> None:
    """Write detailed debug-level step information."""

    detail_parts = [
        f"event_id={event_id}",
        f"instrument_key={instrument_key}",
    ]

    for key, value in details.items():
        detail_parts.append(f"{key}={value}")

    logger.debug(
        "STEP=%s %s",
        step,
        " ".join(detail_parts),
    )


def _log_step_duration(
    step: str,
    started_at: float,
    event_id: Any = None,
    instrument_key: Any = None,
    **details: Any,
) -> None:
    """Log a completed step with execution time."""

    elapsed_ms = round(
        (perf_counter() - started_at) * 1000,
        2,
    )

    _log_step(
        step=step,
        event_id=event_id,
        instrument_key=instrument_key,
        duration_ms=elapsed_ms,
        **details,
    )


# ------------------------------------------------------------------
# Payload sanitization
# ------------------------------------------------------------------

def _remove_historical_alerts(
    value: Any,
) -> tuple[Any, int]:
    removed_count = 0

    if isinstance(value, dict):
        cleaned_value: dict[str, Any] = {}

        for key, item in value.items():
            if key == "last_ema_alert":
                removed_count += 1
                continue

            cleaned_item, nested_removed_count = (
                _remove_historical_alerts(item)
            )

            cleaned_value[key] = cleaned_item
            removed_count += nested_removed_count

        return cleaned_value, removed_count

    if isinstance(value, list):
        cleaned_list = []

        for item in value:
            cleaned_item, nested_removed_count = (
                _remove_historical_alerts(item)
            )

            cleaned_list.append(cleaned_item)
            removed_count += nested_removed_count

        return cleaned_list, removed_count

    return value, removed_count


def _sanitize_order_request_payload(
    payload: dict[str, Any],
) -> tuple[dict[str, Any], int]:
    started_at = perf_counter()

    payload_copy = deepcopy(payload)

    sanitized_payload, removed_count = (
        _remove_historical_alerts(payload_copy)
    )

    if not isinstance(sanitized_payload, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="JSON payload must be an object",
        )

    _log_step_duration(
        step="PAYLOAD_SANITIZED",
        started_at=started_at,
        event_id=payload.get("event_id"),
        removed_historical_fields=removed_count,
    )

    return sanitized_payload, removed_count


# ------------------------------------------------------------------
# Payload extraction
# ------------------------------------------------------------------

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
            instrument
            for instrument in instruments
            if isinstance(instrument, dict)
        ]

    order_suggestion = payload.get("order_suggestion")

    if not isinstance(order_suggestion, dict):
        return []

    budget_filter = order_suggestion.get("budget_filter")

    if isinstance(budget_filter, dict):
        instruments = budget_filter.get("instruments")

        if isinstance(instruments, list):
            return [
                instrument
                for instrument in instruments
                if isinstance(instrument, dict)
            ]

    budget_range = order_suggestion.get("budget_range")

    if isinstance(budget_range, dict):
        instruments = budget_range.get("instruments")

        if isinstance(instruments, list):
            return [
                instrument
                for instrument in instruments
                if isinstance(instrument, dict)
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
    instrument_type = _normalize_instrument_type(
        payload.get("instrument_type")
    )

    if instrument_type:
        return instrument_type

    order_suggestion = payload.get("order_suggestion")

    if isinstance(order_suggestion, dict):
        instrument_type = _normalize_instrument_type(
            order_suggestion.get("suggested_order_side")
        )

        if instrument_type:
            return instrument_type

    instrument_type = _normalize_instrument_type(
        raw_ema_event.get("instrument_type")
    )

    if instrument_type:
        return instrument_type

    contract_info = raw_ema_event.get("contract_info")

    if isinstance(contract_info, dict):
        instrument_type = _normalize_instrument_type(
            contract_info.get("instrument_type")
        )

        if instrument_type:
            return instrument_type

        instrument_type = _normalize_instrument_type(
            contract_info.get("option_type")
        )

        if instrument_type:
            return instrument_type

    if isinstance(order_suggestion, dict):
        instrument_type = _normalize_instrument_type(
            order_suggestion.get("isolated_instrument_type")
        )

        if instrument_type:
            return instrument_type

    return None


# ------------------------------------------------------------------
# Instrument selection + order processing
# ------------------------------------------------------------------

def _process_order_selection(
    payload: dict[str, Any],
) -> dict[str, Any]:
    started_at = perf_counter()

    event_id = payload.get("event_id")

    _log_step(
        "PROCESSING_STARTED",
        event_id=event_id,
    )

    # --------------------------------------------------------------
    # Extract EMA event
    # --------------------------------------------------------------

    raw_ema_event = _extract_raw_ema_event(payload)

    _log_step(
        "RAW_EMA_EVENT_EXTRACTED",
        event_id=event_id,
        instrument_key=raw_ema_event.get("instrument_key"),
        event_type=raw_ema_event.get("event_type"),
    )

    # --------------------------------------------------------------
    # Target price
    # --------------------------------------------------------------

    target_price = _get_target_price(
        raw_ema_event=raw_ema_event,
        payload=payload,
    )

    _log_step(
        "TARGET_PRICE_DETERMINED",
        event_id=event_id,
        instrument_key=raw_ema_event.get("instrument_key"),
        target_price=target_price,
    )

    # --------------------------------------------------------------
    # Available instruments
    # --------------------------------------------------------------

    candidate_instruments = _get_available_instruments(
        payload=payload,
        raw_ema_event=raw_ema_event,
    )

    _log_step(
        "CANDIDATE_INSTRUMENTS_IDENTIFIED",
        event_id=event_id,
        instrument_key=raw_ema_event.get("instrument_key"),
        candidate_count=len(candidate_instruments),
    )

    # --------------------------------------------------------------
    # Instrument type
    # --------------------------------------------------------------

    instrument_type = _get_instrument_type(
        payload=payload,
        raw_ema_event=raw_ema_event,
    )

    _log_step(
        "INSTRUMENT_TYPE_DETERMINED",
        event_id=event_id,
        instrument_key=raw_ema_event.get("instrument_key"),
        instrument_type=instrument_type,
    )

    # --------------------------------------------------------------
    # Instrument selection
    # --------------------------------------------------------------

    selection_started_at = perf_counter()

    _log_step(
        "INSTRUMENT_SELECTION_STARTED",
        event_id=event_id,
        instrument_key=raw_ema_event.get("instrument_key"),
        target_price=target_price,
        instrument_type=instrument_type,
        candidates=len(candidate_instruments),
    )

    selection_details = (
        select_nearest_instrument_with_details(
            instruments=candidate_instruments,
            target_price=target_price,
            instrument_type=instrument_type,
        )
    )

    selected_instrument = selection_details.get(
        "selected_instrument"
    )

    _log_step_duration(
        "INSTRUMENT_SELECTION_COMPLETED",
        started_at=selection_started_at,
        event_id=event_id,
        instrument_key=(
            selected_instrument.get("instrument_key")
            if selected_instrument
            else raw_ema_event.get("instrument_key")
        ),
        selected=bool(selected_instrument),
        available_count=selection_details.get(
            "available_instrument_count",
            0,
        ),
        price_difference=selection_details.get(
            "price_difference"
        ),
    )

    execution_result = None

    # --------------------------------------------------------------
    # Selected instrument
    # --------------------------------------------------------------

    if selected_instrument:
        selected_instrument_key = selected_instrument.get(
            "instrument_key"
        )

        selected_symbol = selected_instrument.get(
            "trading_symbol"
        )

        selected_live_ltp = selected_instrument.get(
            "live_ltp"
        )

        _log_step(
            "INSTRUMENT_SELECTED",
            event_id=event_id,
            instrument_key=selected_instrument_key,
            trading_symbol=selected_symbol,
            live_ltp=selected_live_ltp,
            target_price=target_price,
            price_difference=selection_details.get(
                "price_difference"
            ),
        )

        # ----------------------------------------------------------
        # Order processing
        # ----------------------------------------------------------

        execution_started_at = perf_counter()

        _log_step(
            "ORDER_PROCESSING_STARTED",
            event_id=event_id,
            instrument_key=selected_instrument_key,
            trading_symbol=selected_symbol,
            live_ltp=selected_live_ltp,
            test_mode=settings.test_flg,
            place_order=settings.PLACE_ORDER,
        )

        try:
            execution_result = process_selected_instrument(
                selected_instrument
            )

            order_status = (
                execution_result.get("order_status")
                if isinstance(execution_result, dict)
                else None
            )

            success = (
                execution_result.get("success")
                if isinstance(execution_result, dict)
                else None
            )

            _log_step_duration(
                "ORDER_PROCESSING_COMPLETED",
                started_at=execution_started_at,
                event_id=event_id,
                instrument_key=selected_instrument_key,
                trading_symbol=selected_symbol,
                success=success,
                order_status=order_status,
            )

        except Exception as exc:
            logger.exception(
                "STEP=ORDER_PROCESSING_FAILED "
                "event_id=%s instrument_key=%s "
                "trading_symbol=%s error=%s",
                event_id,
                selected_instrument_key,
                selected_symbol,
                exc,
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
        _log_step(
            "NO_INSTRUMENT_SELECTED",
            event_id=event_id,
            instrument_key=raw_ema_event.get(
                "instrument_key"
            ),
            target_price=target_price,
            instrument_type=instrument_type,
            candidates=len(candidate_instruments),
        )

    # --------------------------------------------------------------
    # Processing completed
    # --------------------------------------------------------------

    _log_step_duration(
        "PROCESSING_COMPLETED",
        started_at=started_at,
        event_id=event_id,
        instrument_key=(
            selected_instrument.get("instrument_key")
            if selected_instrument
            else raw_ema_event.get("instrument_key")
        ),
        selection_performed=bool(selected_instrument),
        execution_performed=execution_result is not None,
        order_status=(
            execution_result.get("order_status")
            if execution_result
            else None
        ),
    )

    return {
        "raw_ema_event": raw_ema_event,
        "target_price": target_price,
        "instrument_type": instrument_type,
        "candidate_instrument_count": len(
            candidate_instruments
        ),
        "available_instrument_count": selection_details.get(
            "available_instrument_count",
            0,
        ),
        "selection_attempted": bool(
            candidate_instruments
        ),
        "selection_performed": (
            selected_instrument is not None
        ),
        "selected_instrument": selected_instrument,
        "price_difference": selection_details.get(
            "price_difference"
        ),
        "execution_result": execution_result,
    }


# ------------------------------------------------------------------
# Response construction
# ------------------------------------------------------------------

def _build_processing_response(
    processing_result: dict[str, Any],
) -> dict[str, Any]:
    price_difference = processing_result.get(
        "price_difference"
    )

    if isinstance(price_difference, float):
        price_difference = round(
            price_difference,
            2,
        )

    return {
        "target_price": processing_result[
            "target_price"
        ],
        "instrument_type": processing_result[
            "instrument_type"
        ],
        "candidate_instrument_count": (
            processing_result[
                "candidate_instrument_count"
            ]
        ),
        "available_instrument_count": (
            processing_result[
                "available_instrument_count"
            ]
        ),
        "selection_attempted": processing_result[
            "selection_attempted"
        ],
        "selection_performed": processing_result[
            "selection_performed"
        ],
        "selected_instrument": processing_result[
            "selected_instrument"
        ],
        "price_difference": price_difference,
        "execution_result": processing_result.get(
            "execution_result"
        ),
    }


def _attach_processing_result(
    payload: dict[str, Any],
    processing_result: dict[str, Any],
) -> dict[str, Any]:
    processed_payload = deepcopy(payload)

    processed_payload["processing"] = (
        _build_processing_response(
            processing_result
        )
    )

    return processed_payload


# ------------------------------------------------------------------
# Execution log
# ------------------------------------------------------------------

async def _save_execution_log(
    payload: dict[str, Any],
    execution_result: dict[str, Any] | None,
) -> str | None:
    event_id = payload.get("event_id")

    instrument_key = None

    selected_instrument = (
        execution_result.get("selected_instrument")
        if isinstance(execution_result, dict)
        else None
    )

    if isinstance(selected_instrument, dict):
        instrument_key = selected_instrument.get(
            "instrument_key"
        )

    if execution_result is None:
        _log_step(
            "EXECUTION_LOG_SKIPPED",
            event_id=event_id,
            instrument_key=instrument_key,
            reason="no_execution_result",
        )

        return None

    execution_started_at = perf_counter()

    _log_step(
        "EXECUTION_LOG_SAVE_STARTED",
        event_id=event_id,
        instrument_key=instrument_key,
        order_status=execution_result.get(
            "order_status"
        ),
    )

    try:
        execution_document_id = (
            await save_order_execution(
                payload=payload,
                execution_result=execution_result,
            )
        )

        if execution_document_id is None:
            _log_step_duration(
                "EXECUTION_LOG_SAVE_COMPLETED_WITHOUT_ID",
                started_at=execution_started_at,
                event_id=event_id,
                instrument_key=instrument_key,
                order_status=execution_result.get(
                    "order_status"
                ),
            )

            return None

        _log_step_duration(
            "EXECUTION_LOG_SAVED",
            started_at=execution_started_at,
            event_id=event_id,
            instrument_key=instrument_key,
            order_status=execution_result.get(
                "order_status"
            ),
            document_id=execution_document_id,
        )

        return str(execution_document_id)

    except PyMongoError:
        logger.exception(
            "STEP=EXECUTION_LOG_MONGODB_ERROR "
            "event_id=%s instrument_key=%s "
            "order_status=%s",
            event_id,
            instrument_key,
            execution_result.get("order_status"),
        )

        return None

    except Exception:
        logger.exception(
            "STEP=EXECUTION_LOG_UNEXPECTED_ERROR "
            "event_id=%s instrument_key=%s "
            "order_status=%s",
            event_id,
            instrument_key,
            execution_result.get("order_status"),
        )

        return None


# ------------------------------------------------------------------
# Create order request
# ------------------------------------------------------------------

@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
)
async def create_order_request(
    request: Request,
) -> dict[str, Any]:
    request_started_at = perf_counter()

    client_host = (
        request.client.host
        if request.client
        else None
    )

    # --------------------------------------------------------------
    # Request received
    # --------------------------------------------------------------

    _log_step(
        "REQUEST_RECEIVED",
        path=request.url.path,
        method=request.method,
        client=client_host,
    )

    # --------------------------------------------------------------
    # Parse JSON
    # --------------------------------------------------------------

    json_started_at = perf_counter()

    try:
        incoming_payload = await request.json()

        _log_step_duration(
            "JSON_PARSED",
            started_at=json_started_at,
            payload_type=type(
                incoming_payload
            ).__name__,
        )

    except Exception as exc:
        logger.warning(
            "STEP=JSON_PARSE_FAILED "
            "path=%s client=%s error=%s",
            request.url.path,
            client_host,
            exc,
        )

        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid JSON payload",
        ) from exc

    # --------------------------------------------------------------
    # Validate payload type
    # --------------------------------------------------------------

    if not isinstance(incoming_payload, dict):
        logger.warning(
            "STEP=PAYLOAD_TYPE_INVALID "
            "path=%s payload_type=%s",
            request.url.path,
            type(incoming_payload).__name__,
        )

        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="JSON payload must be an object",
        )

    incoming_event_id = incoming_payload.get(
        "event_id"
    )

    raw_incoming_event = incoming_payload.get(
        "raw_ema_event"
    )

    incoming_instrument_key = None

    if isinstance(raw_incoming_event, dict):
        incoming_instrument_key = (
            raw_incoming_event.get(
                "instrument_key"
            )
        )

    _log_step(
        "PAYLOAD_ACCEPTED",
        event_id=incoming_event_id,
        instrument_key=incoming_instrument_key,
        field_count=len(incoming_payload),
    )

    # --------------------------------------------------------------
    # Sanitize
    # --------------------------------------------------------------

    try:
        payload, removed_history_count = (
            _sanitize_order_request_payload(
                incoming_payload
            )
        )

    except HTTPException:
        raise

    except Exception as exc:
        logger.exception(
            "STEP=PAYLOAD_SANITIZATION_FAILED "
            "event_id=%s error=%s",
            incoming_event_id,
            exc,
        )

        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Unable to sanitize "
                "order request payload"
            ),
        ) from exc

    event_id = payload.get("event_id")

    raw_ema_event = payload.get(
        "raw_ema_event"
    )

    instrument_key = None

    if isinstance(raw_ema_event, dict):
        instrument_key = raw_ema_event.get(
            "instrument_key"
        )

    if removed_history_count:
        _log_step(
            "HISTORICAL_ALERTS_REMOVED",
            event_id=event_id,
            instrument_key=instrument_key,
            removed_fields=removed_history_count,
        )
    else:
        _log_debug_step(
            "NO_HISTORICAL_ALERTS_FOUND",
            event_id=event_id,
            instrument_key=instrument_key,
        )

    # --------------------------------------------------------------
    # Request configuration
    # --------------------------------------------------------------

    _log_step(
        "REQUEST_PROCESSING_STARTED",
        event_id=event_id,
        instrument_key=instrument_key,
        test_mode=settings.test_flg,
        place_order=settings.PLACE_ORDER,
    )

    # --------------------------------------------------------------
    # Process order
    # --------------------------------------------------------------

    try:
        processing_result = _process_order_selection(
            payload
        )

    except HTTPException:
        logger.warning(
            "STEP=REQUEST_PROCESSING_REJECTED "
            "event_id=%s instrument_key=%s",
            event_id,
            instrument_key,
        )

        raise

    except Exception as exc:
        logger.exception(
            "STEP=REQUEST_PROCESSING_FAILED "
            "event_id=%s instrument_key=%s "
            "error=%s",
            event_id,
            instrument_key,
            exc,
        )

        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Unable to process "
                f"order request: {exc}"
            ),
        ) from exc

    # --------------------------------------------------------------
    # Attach processing result
    # --------------------------------------------------------------

    processed_payload = _attach_processing_result(
        payload=payload,
        processing_result=processing_result,
    )

    processing_response = (
        processed_payload["processing"]
    )

    execution_result = processing_result.get(
        "execution_result"
    )

    selected_instrument = processing_result.get(
        "selected_instrument"
    )

    if isinstance(selected_instrument, dict):
        instrument_key = selected_instrument.get(
            "instrument_key"
        )

    _log_step(
        "PROCESSING_RESULT_ATTACHED",
        event_id=event_id,
        instrument_key=instrument_key,
        selection_performed=processing_result.get(
            "selection_performed"
        ),
        execution_available=(
            execution_result is not None
        ),
        order_status=(
            execution_result.get("order_status")
            if execution_result
            else None
        ),
    )

    # --------------------------------------------------------------
    # Save execution log
    # --------------------------------------------------------------

    execution_document_id = (
        await _save_execution_log(
            payload=payload,
            execution_result=execution_result,
        )
    )

    # --------------------------------------------------------------
    # Test mode
    # --------------------------------------------------------------

    if settings.test_flg:
        _log_step(
            "TEST_MODE_SAVE_STARTED",
            event_id=event_id,
            instrument_key=instrument_key,
        )

        test_save_started_at = perf_counter()

        try:
            inserted_id = await save_test_payload(
                processed_payload
            )

        except PyMongoError as exc:
            logger.exception(
                "STEP=TEST_PAYLOAD_MONGODB_ERROR "
                "event_id=%s instrument_key=%s "
                "error=%s",
                event_id,
                instrument_key,
                exc,
            )

            raise HTTPException(
                status_code=(
                    status.HTTP_503_SERVICE_UNAVAILABLE
                ),
                detail="Unable to save test payload",
            ) from exc

        except Exception as exc:
            logger.exception(
                "STEP=TEST_PAYLOAD_SAVE_FAILED "
                "event_id=%s instrument_key=%s "
                "error=%s",
                event_id,
                instrument_key,
                exc,
            )

            raise HTTPException(
                status_code=(
                    status.HTTP_503_SERVICE_UNAVAILABLE
                ),
                detail="Unable to save test payload",
            ) from exc

        _log_step_duration(
            "TEST_PAYLOAD_SAVED",
            started_at=test_save_started_at,
            event_id=event_id,
            instrument_key=instrument_key,
            request_document_id=inserted_id,
            execution_document_id=(
                execution_document_id
            ),
        )

        _log_step_duration(
            "REQUEST_COMPLETED",
            started_at=request_started_at,
            event_id=event_id,
            instrument_key=instrument_key,
            mode="test",
            status="success",
            order_status=(
                execution_result.get(
                    "order_status"
                )
                if execution_result
                else None
            ),
        )

        return {
            "status": "success",
            "message": (
                "Test payload saved "
                "and processed"
            ),
            "mode": "test",
            "id": str(inserted_id),
            "execution_log_id": (
                execution_document_id
            ),
            "processing": processing_response,
        }

    # --------------------------------------------------------------
    # Production validation
    # --------------------------------------------------------------

    _log_step(
        "PRODUCTION_VALIDATION_STARTED",
        event_id=event_id,
        instrument_key=instrument_key,
    )

    validation_started_at = perf_counter()

    try:
        alert = IsolatedEmaAlert.model_validate(
            processed_payload
        )

        _log_step_duration(
            "PRODUCTION_VALIDATION_COMPLETED",
            started_at=validation_started_at,
            event_id=event_id,
            instrument_key=instrument_key,
            validation="passed",
        )

    except ValidationError as exc:
        logger.warning(
            "STEP=PRODUCTION_VALIDATION_FAILED "
            "event_id=%s instrument_key=%s "
            "errors=%s",
            event_id,
            instrument_key,
            exc.errors(),
        )

        raise HTTPException(
            status_code=(
                status.HTTP_422_UNPROCESSABLE_ENTITY
            ),
            detail=exc.errors(),
        ) from exc

    # --------------------------------------------------------------
    # Save production request
    # --------------------------------------------------------------

    _log_step(
        "PRODUCTION_REQUEST_SAVE_STARTED",
        event_id=event_id,
        instrument_key=instrument_key,
    )

    production_save_started_at = perf_counter()

    try:
        inserted_id = await save_order_request(
            alert
        )

    except PyMongoError as exc:
        logger.exception(
            "STEP=PRODUCTION_REQUEST_MONGODB_ERROR "
            "event_id=%s instrument_key=%s "
            "error=%s",
            event_id,
            instrument_key,
            exc,
        )

        raise HTTPException(
            status_code=(
                status.HTTP_503_SERVICE_UNAVAILABLE
            ),
            detail="Unable to save order request",
        ) from exc

    except Exception as exc:
        logger.exception(
            "STEP=PRODUCTION_REQUEST_SAVE_FAILED "
            "event_id=%s instrument_key=%s "
            "error=%s",
            event_id,
            instrument_key,
            exc,
        )

        raise HTTPException(
            status_code=(
                status.HTTP_503_SERVICE_UNAVAILABLE
            ),
            detail="Unable to save order request",
        ) from exc

    _log_step_duration(
        "PRODUCTION_REQUEST_SAVED",
        started_at=production_save_started_at,
        event_id=event_id,
        instrument_key=instrument_key,
        request_document_id=inserted_id,
        execution_document_id=(
            execution_document_id
        ),
    )

    # --------------------------------------------------------------
    # Final response
    # --------------------------------------------------------------

    _log_step_duration(
        "REQUEST_COMPLETED",
        started_at=request_started_at,
        event_id=event_id,
        instrument_key=instrument_key,
        mode="production",
        status="success",
        request_document_id=inserted_id,
        execution_document_id=(
            execution_document_id
        ),
        order_status=(
            execution_result.get("order_status")
            if execution_result
            else None
        ),
        selection_performed=(
            processing_result.get(
                "selection_performed"
            )
        ),
    )

    return {
        "status": "success",
        "message": (
            "Order request saved "
            "and processed"
        ),
        "mode": "production",
        "id": str(inserted_id),
        "execution_log_id": (
            execution_document_id
        ),
        "processing": processing_response,
    }
