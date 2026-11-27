"""External portfolio routes.

Currently exposes today's P&L status which is used as a pre-trade sanity
check by upstream order bots.
"""

from typing import Any

from fastapi import APIRouter, HTTPException

from core.logger import get_logger
from services.daily_profit_guard_service import DailyProfitGuardError, get_today_pnl_status

logger = get_logger(__file__)

router = APIRouter(
    prefix="/portfolio",
    tags=["external-portfolio"],
    responses={503: {"description": "P&L service temporarily unavailable"}},
)


@router.post("/today-pnl")
def external_today_portfolio_pnl(body: dict[str, Any]):
    """Return today's realised + unrealised P&L snapshot.

    The router-level auth dependency already validated ``body.api_token``.
    """
    try:
        return get_today_pnl_status()
    except DailyProfitGuardError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"error": str(exc), "code": exc.code, **exc.details},
        ) from None
    except Exception:
        logger.exception("Could not calculate external today's portfolio P&L")
        raise HTTPException(
            status_code=503,
            detail={"error": "Today's portfolio P&L is temporarily unavailable."},
        ) from None