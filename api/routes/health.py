from fastapi import (
    APIRouter,
    HTTPException,
    status,
)

from core.config import settings
from core.database import mongo
from services.token_service import token_service

router = APIRouter(
    tags=["Health"],
)


@router.get("/health")
async def health() -> dict:
    if mongo.client is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="MongoDB unavailable",
        )

    try:
        await mongo.client.admin.command(
            "ping",
        )

    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="MongoDB unavailable",
        ) from exc

    token_status = token_service.get_cache_status()

    return {
        "status": "healthy",
        "application": settings.app_name,
        "environment": settings.app_env,
        "database": "connected",
        "telegram_enabled": settings.tele_flg,
        "test_mode": settings.test_flg,
        "place_order_enabled": settings.PLACE_ORDER,
        "order_execution_ready": (
            settings.PLACE_ORDER is False or token_service.has_access_token()
        ),
        "upstox_token_cache": token_status.get("last_validation_status"),
    }
