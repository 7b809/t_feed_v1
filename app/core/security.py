"""API-key dependency for protecting broker order operations."""
import hmac
from fastapi import Header, HTTPException, status
from app.core.config import settings

def require_order_api_key(x_order_api_key: str | None = Header(default=None)) -> None:
    """Reject protected order routes unless a configured API key matches."""
    configured_key = settings.ORDER_API_KEY
    if not configured_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="ORDER_API_KEY is not configured; protected order endpoints are disabled.",
        )
    if not x_order_api_key or not hmac.compare_digest(x_order_api_key, configured_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid X-Order-API-Key header.",
        )
