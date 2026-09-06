from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import ValidationError
from pymongo.errors import PyMongoError

from core.config import settings
from models.alert import IsolatedEmaAlert
from services.instrument_selection_service import (
    select_nearest_instrument_with_details,
)
from services.order_request_service import (
    save_order_request,
    save_test_payload,
)

router = APIRouter(
    prefix="/order-requests",
    tags=["Order Requests"],
)


def _extract_raw_ema_event(payload: dict[str, Any]) -> dict[str, Any]:
    """
    Extract the raw EMA event from the incoming API payload.

    Expected structure:

        {
            "raw_ema_event": {
                ...
            }
        }
    """

    raw_ema_event = payload.get("raw_ema_event")

    if not isinstance(raw_ema_event, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Payload must contain a 'raw_ema_event' object",
        )

    return raw_ema_event


def _to_float(value: Any) -> float | None:
    """Safely convert a value to float."""

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
    """
    Determine the price against which an available instrument
    should be selected.

    Priority:

    1. Explicit target_price in the payload.
    2. Explicit requested_price in the payload.
    3. raw_ema_event.target_price.
    4. raw_ema_event.requested_price.
    5. raw_ema_event.ltp.
    6. raw_ema_event.close.
    """

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
    """
    Extract available instruments from the incoming request.

    Supported locations, in priority order:

    1. payload["available_instruments"]
    2. payload["order_suggestion"]["budget_filter"]["instruments"]
    3. payload["order_suggestion"]["budget_range"]["instruments"]

    The producer payload currently uses:

        order_suggestion
            └── budget_filter
                  └── instruments
    """

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


def _get_instrument_type(
    payload: dict[str, Any],
    raw_ema_event: dict[str, Any],
) -> str | None:
    """
    Determine the instrument type used to filter the candidates.

    Priority:

    1. payload.instrument_type
    2. raw_ema_event.instrument_type
    3. raw_ema_event.contract_info.instrument_type
    4. order_suggestion.suggested_order_side
    5. order_suggestion.isolated_instrument_type
    """

    instrument_type = payload.get("instrument_type") or raw_ema_event.get(
        "instrument_type"
    )

    if instrument_type:
        return str(instrument_type).upper().strip()

    contract_info = raw_ema_event.get("contract_info")

    if isinstance(contract_info, dict):
        instrument_type = contract_info.get("instrument_type")

        if instrument_type:
            return str(instrument_type).upper().strip()

    order_suggestion = payload.get("order_suggestion")

    if isinstance(order_suggestion, dict):
        instrument_type = order_suggestion.get(
            "suggested_order_side"
        ) or order_suggestion.get("isolated_instrument_type")

        if instrument_type:
            return str(instrument_type).upper().strip()

    return None


def _process_order_selection(
    payload: dict[str, Any],
) -> dict[str, Any]:
    """
    Process the incoming EMA event and perform nearest-price
    instrument selection.

    Selection is delegated to the dedicated
    instrument_selection_service.

    The selected instrument is the complete instrument object,
    not just its price.

    This function does not modify live state or place an order.
    """

    raw_ema_event = _extract_raw_ema_event(payload)

    target_price = _get_target_price(
        raw_ema_event=raw_ema_event,
        payload=payload,
    )

    available_instruments = _get_available_instruments(
        payload=payload,
        raw_ema_event=raw_ema_event,
    )

    instrument_type = _get_instrument_type(
        payload=payload,
        raw_ema_event=raw_ema_event,
    )

    selection_details = select_nearest_instrument_with_details(
        instruments=available_instruments,
        target_price=target_price,
        instrument_type=instrument_type,
    )

    return {
        "raw_ema_event": raw_ema_event,
        "target_price": target_price,
        "instrument_type": instrument_type,
        "available_instrument_count": selection_details["available_instrument_count"],
        "selected_instrument": selection_details["selected_instrument"],
        "price_difference": selection_details["price_difference"],
        "selection_performed": bool(available_instruments),
    }


def _build_processing_response(
    processing_result: dict[str, Any],
) -> dict[str, Any]:
    """
    Build the processing metadata.

    This object is also persisted into MongoDB so that the
    receiver's actual selection decision is stored with the
    original order request.
    """

    return {
        "target_price": processing_result["target_price"],
        "instrument_type": processing_result["instrument_type"],
        "available_instrument_count": processing_result["available_instrument_count"],
        "selection_performed": processing_result["selection_performed"],
        "selected_instrument": processing_result["selected_instrument"],
        "price_difference": processing_result["price_difference"],
    }


def _attach_processing_result(
    payload: dict[str, Any],
    processing_result: dict[str, Any],
) -> dict[str, Any]:
    """
    Add receiver-side processing information to the payload
    before it is persisted.

    The original payload is copied so the caller's dictionary
    is not modified unexpectedly.
    """

    processed_payload = dict(payload)

    processed_payload["processing"] = _build_processing_response(
        processing_result,
    )

    return processed_payload


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
)
async def create_order_request(
    request: Request,
) -> dict[str, Any]:

    # ---------------------------------------------------------
    # READ RAW JSON
    #
    # Do not use IsolatedEmaAlert as the FastAPI parameter.
    # TEST_FLG must be checked before legacy model validation.
    # ---------------------------------------------------------
    try:
        payload = await request.json()

    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid JSON payload",
        ) from exc

    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="JSON payload must be an object",
        )

    # ---------------------------------------------------------
    # PROCESS ORDER / INSTRUMENT SELECTION
    #
    # This performs:
    #
    # 1. Extract raw_ema_event.
    # 2. Determine target price.
    # 3. Find producer-filtered instruments.
    # 4. Filter by instrument type.
    # 5. Ignore unavailable instruments.
    # 6. Ignore instruments without valid live_ltp.
    # 7. Select nearest live_ltp to target price.
    # 8. Return the COMPLETE instrument object.
    #
    # No order is placed here.
    # ---------------------------------------------------------
    try:
        processing_result = _process_order_selection(
            payload,
        )

    except HTTPException:
        raise

    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unable to process order request: {exc}",
        ) from exc

    # ---------------------------------------------------------
    # BUILD PAYLOAD WITH RECEIVER-SIDE PROCESSING
    #
    # IMPORTANT:
    #
    # Previously only the original payload was saved.
    # The selection result existed only in memory and API
    # response.
    #
    # Now processing is attached to the payload BEFORE the
    # MongoDB save.
    # ---------------------------------------------------------
    processed_payload = _attach_processing_result(
        payload=payload,
        processing_result=processing_result,
    )

    processing_response = processed_payload["processing"]

    # ---------------------------------------------------------
    # TEST MODE
    #
    # TEST_FLG=True:
    #
    # - Accept any JSON object.
    # - Do not validate against IsolatedEmaAlert.
    # - Save payload INCLUDING receiver processing result.
    # - Return instrument-selection information.
    # ---------------------------------------------------------
    if settings.test_flg:

        try:
            inserted_id = await save_test_payload(
                processed_payload,
            )

        except PyMongoError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Unable to save test payload",
            ) from exc

        return {
            "status": "success",
            "message": "Test payload saved and processed",
            "mode": "test",
            "id": str(inserted_id),
            "processing": processing_response,
        }

    # ---------------------------------------------------------
    # NORMAL / PRODUCTION MODE
    #
    # Keep the existing IsolatedEmaAlert validation for the
    # current production schema.
    # ---------------------------------------------------------
    try:
        alert = IsolatedEmaAlert.model_validate(
            processed_payload,
        )

    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=exc.errors(),
        ) from exc

    try:
        inserted_id = await save_order_request(
            alert,
        )

    except PyMongoError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Unable to save order request",
        ) from exc

    return {
        "status": "success",
        "message": "Order request saved and processed",
        "mode": "production",
        "id": str(inserted_id),
        "processing": processing_response,
    }
