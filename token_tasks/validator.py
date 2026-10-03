"""
token_tasks/validator.py

Thin adapter that delegates token validation to
`upstox_app.get_profile_status.get_profile`.

Kept as a separate module so callers (service.py) keep a stable import path
and so we have a single place to normalize the response shape.

NOTE:
    `upstox_app.get_profile_status` imports `token_tasks.service` at module
    level. To avoid a circular import (service -> validator -> upstox_app ->
    service), we import `get_profile` LAZILY inside the function.
"""
from typing import Any, Dict, Optional

from core.logger import get_logger

logger = get_logger(__name__)

UPSTOX_BROKER = "UPSTOX"
API_VERSION = "2.0"


def _get_profile_fn():
    """Lazy import to break the circular dependency chain."""
    from upstox_app.profile.get_profile_status import get_profile  # noqa: WPS433
    return get_profile


def validate_upstox_token(access_token: Optional[str] = None) -> Dict[str, Any]:
    """
    Validate an Upstox access token via the shared upstox_app service.

    Delegates to `upstox_app.get_profile_status.get_profile`, which itself
    falls back to the in-memory token cache when no token is passed.

    Returns the same shape as `get_profile`, minus the raw payload
    (kept for compatibility with prior callers).
    """
    logger.info(
        "validate_upstox_token invoked | mode=%s",
        "explicit" if access_token and access_token.strip() else "cache",
    )

    try:
        get_profile = _get_profile_fn()
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to import upstox_app.get_profile_status: %s", exc)
        return {
            "valid": False,
            "status": "error",
            "status_text": "import_error",
            "user_id": None,
            "user_name": None,
            "broker": UPSTOX_BROKER,
            "message": f"Validator import failed: {exc}",
            "token_source": "none",
        }

    result = get_profile(access_token)

    # Drop the raw payload before handing to callers — the service stores
    # only the normalized fields and the router never exposes raw anyway.
    result.pop("raw", None)

    return result