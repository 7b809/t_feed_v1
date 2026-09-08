from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from token_tasks.token_store import token_store
from core.logger import get_logger

router = APIRouter()

logger = get_logger(__file__)


class TestTokenRequest(BaseModel):
    access_token: str


@router.post("/test/save-token")
async def test_save_token(request: TestTokenRequest):
    logger.info("[TEST TOKEN API] Received request to save Upstox access token.")

    try:
        if not request.access_token or not request.access_token.strip():
            logger.warning("[TEST TOKEN API] Access token was empty.")

            raise HTTPException(
                status_code=400,
                detail="access_token is required and cannot be empty.",
            )

        logger.info(
            "[TEST TOKEN API] Attempting to save access token "
            "using TokenStore. source=thunderclient_test"
        )

        saved = token_store.save_access_token(
            access_token=request.access_token,
            source="thunderclient_test",
        )

        logger.info(
            "[TEST TOKEN API] Access token saved successfully. "
            f"created_at={saved.get('created_at')}, "
            f"updated_at={saved.get('updated_at')}, "
            f"source={saved.get('source')}"
        )

        return {
            "success": True,
            "message": "Access token saved successfully.",
            "token": saved.get("access_token"),
            "created_at": saved.get("created_at"),
            "updated_at": saved.get("updated_at"),
            "source": saved.get("source"),
        }

    except HTTPException:
        raise

    except ValueError as ex:
        logger.warning(
            "[TEST TOKEN API] Invalid access token request. "
            f"error={type(ex).__name__}: {ex}"
        )

        raise HTTPException(
            status_code=400,
            detail=str(ex),
        )

    except Exception as ex:
        logger.exception(
            "[TEST TOKEN API] Unexpected error while saving access token. "
            f"error={type(ex).__name__}: {ex}"
        )

        raise HTTPException(
            status_code=500,
            detail=("Failed to save access token: " f"{type(ex).__name__}: {ex}"),
        )
