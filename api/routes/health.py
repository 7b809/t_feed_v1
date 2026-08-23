from fastapi import APIRouter, HTTPException, status

from core.database import mongo

router = APIRouter(tags=["Health"])


@router.get("/health")
async def health() -> dict[str, str]:
    if mongo.client is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="MongoDB unavailable")
    try:
        await mongo.client.admin.command("ping")
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="MongoDB unavailable") from exc
    return {"status": "healthy", "database": "connected"}
