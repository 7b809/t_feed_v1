from fastapi import HTTPException, status
from app.database import token_state, db_instance

async def get_access_token() -> str:
    if not token_state.access_token:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Upstox Access Token is not available in memory."
        )
    return token_state.access_token

async def get_db():
    return db_instance.db