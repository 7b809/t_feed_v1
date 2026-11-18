"""REST endpoints for Upstox orders."""
import logging
from fastapi import APIRouter, HTTPException, status
from app.core.config import settings
from app.models.order_models import PlaceOrderRequest, ExitSingleOrderRequest, ExitAllOrdersRequest
from app.services.order_manager import order_manager

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/orders", tags=["orders"])

@router.get("/health")
def order_health():
    return {
        "service": "order-management",
        "trading_enabled": settings.TRADING_ENABLED,
        "defaults": {
            "transaction_type": settings.DEFAULT_TRANSACTION_TYPE,
            "quantity": settings.DEFAULT_QUANTITY,
            "order_type": settings.DEFAULT_ORDER_TYPE,
            "product": settings.DEFAULT_PRODUCT,
        },
    }

@router.post("/place", status_code=status.HTTP_201_CREATED)
def place_order(payload: PlaceOrderRequest):
    try:
        return order_manager.place_order(payload)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Order placement failed")
        raise HTTPException(status_code=502, detail=f"Order placement failed: {exc}") from exc

@router.get("/book")
def get_order_book():
    try:
        return order_manager.order_book()
    except Exception as exc:
        logger.exception("Could not retrieve order book")
        raise HTTPException(status_code=502, detail=f"Could not retrieve order book: {exc}") from exc

@router.post("/exit/single")
def exit_single(payload: ExitSingleOrderRequest):
    try:
        return order_manager.exit_single(payload.order_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Single exit failed")
        raise HTTPException(status_code=502, detail=f"Single exit failed: {exc}") from exc

@router.post("/exit/all")
def exit_all(payload: ExitAllOrdersRequest):
    try:
        return order_manager.exit_all(segment=payload.segment, tag=payload.tag)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Exit-all failed")
        raise HTTPException(status_code=502, detail=f"Exit-all failed: {exc}") from exc
