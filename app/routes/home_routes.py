from fastapi import APIRouter, Depends
from app.config import settings
from app.database import token_state
from app.dependencies import get_access_token

router = APIRouter()


@router.get("/")
async def root():
    return {
        "status": "running",
        "app_mode": settings.UPSTOX_MODE,
        "strike_range": f"{settings.STRIKE_FROM} - {settings.STRIKE_TO}",
        "candle_intervals": settings.parsed_candle_intervals,
    }


@router.get("/token")
async def read_token(token: str = Depends(get_access_token)):
    return {
        "access_token": token,
        "updated_at": token_state.updated_at,
    }
