from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import ValidationError
from pymongo.errors import PyMongoError

from core.config import settings
from models.alert import IsolatedEmaAlert
from services.order_request_service import (
    save_order_request,
    save_test_payload,
)

router = APIRouter(
    prefix="/order-requests",
    tags=["Order Requests"],
)


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
)
async def create_order_request(
    request: Request,
) -> dict[str, Any]:

    # ---------------------------------------------------------
    # Read the raw JSON payload first.
    # Do NOT use IsolatedEmaAlert as the FastAPI parameter,
    # otherwise FastAPI will validate it before TEST_FLG can
    # be checked.
    # ---------------------------------------------------------
    try:
        payload = await request.json()

    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid JSON payload",
        ) from exc

    # ---------------------------------------------------------
    # TEST MODE
    #
    # When TEST_FLG=True:
    # - Accept any JSON object
    # - Do not validate against IsolatedEmaAlert
    # - Save the payload directly to MongoDB
    # ---------------------------------------------------------
    if settings.test_flg:

        if not isinstance(payload, dict):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="JSON payload must be an object",
            )

        try:
            inserted_id = await save_test_payload(
                payload,
            )

        except PyMongoError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Unable to save test payload",
            ) from exc

        return {
            "status": "success",
            "message": "Test payload saved",
            "mode": "test",
            "id": str(inserted_id),
        }

    # ---------------------------------------------------------
    # NORMAL MODE
    #
    # When TEST_FLG=False:
    # - Validate against IsolatedEmaAlert
    # - Save only valid alert payloads
    # ---------------------------------------------------------
    try:
        alert = IsolatedEmaAlert.model_validate(
            payload,
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
        "message": "Order request saved",
        "mode": "production",
        "id": str(inserted_id),
    }
