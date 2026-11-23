"""Portfolio endpoints for current-day Upstox P&L."""
from fastapi import APIRouter, HTTPException

from core.logger import get_logger
from services.daily_profit_guard_service import DailyProfitGuardError, get_today_pnl_status
from services.portfolio_service import PortfolioServiceError, get_portfolio_overview, get_positions, get_holdings

logger = get_logger(__file__)
router = APIRouter(prefix="/api/v1/portfolio", tags=["portfolio"])


@router.get("/today-pnl")
def today_pnl():
    try:
        return get_today_pnl_status()
    except DailyProfitGuardError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"error": str(exc), "code": exc.code, **exc.details},
        ) from None
    except Exception:
        logger.exception("Could not calculate today's portfolio P&L")
        raise HTTPException(
            status_code=503,
            detail={"error": "Today's portfolio P&L is temporarily unavailable."},
        ) from None


@router.get("/overview")
def portfolio_overview():
    try:
        return get_portfolio_overview()
    except PortfolioServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"error": str(exc), **exc.details}) from None
    except Exception:
        logger.exception("Could not retrieve Upstox portfolio overview")
        raise HTTPException(status_code=503, detail={"error": "Upstox portfolio overview is temporarily unavailable."}) from None


@router.get("/positions")
def portfolio_positions():
    try:
        return get_positions()
    except PortfolioServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"error": str(exc), **exc.details}) from None


@router.get("/holdings")
def portfolio_holdings():
    try:
        return get_holdings()
    except PortfolioServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"error": str(exc), **exc.details}) from None
