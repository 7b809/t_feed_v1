"""
upstox_app/get_profile_status.py

Standalone Upstox profile-status service.

Responsibilities:
    - Validate an Upstox access token using the User Profile API.
    - If no token is explicitly supplied, use the token from the
      project's in-memory cache.
    - Never log access tokens or token previews.
    - Never expose sensitive HTTP headers or cookies in logs.

Public API:
    get_profile(access_token: Optional[str] = None) -> dict
    validate_upstox_token(access_token: Optional[str] = None) -> dict
"""

from typing import Any, Dict, Optional

import upstox_client
from upstox_client.rest import ApiException

from core.logger import get_logger
from token_tasks.service import token_service

logger = get_logger(__name__)

UPSTOX_BROKER = "UPSTOX"
API_VERSION = "2.0"


def _resolve_token(access_token: Optional[str]) -> Optional[str]:
    """
    Resolve which token should be validated.

    Priority:

        1. Explicitly supplied token.
        2. Token from in-memory cache.

    Returns:
        Token string or None.

    Security:
        The token value is never logged.
    """

    # ---------------------------------------------------------
    # Explicit token
    # ---------------------------------------------------------

    if access_token and access_token.strip():
        logger.debug("Using explicitly supplied token for profile validation")
        return access_token.strip()

    # ---------------------------------------------------------
    # Cached token
    # ---------------------------------------------------------

    try:
        cached = token_service.get_access_token()
    except Exception:  # noqa: BLE001
        logger.exception("Failed to read token from in-memory cache")
        return None

    if cached and cached.strip():
        logger.info("Using cached token for profile validation")
        return cached.strip()

    logger.warning("No token available for profile validation")
    return None


def _safe_api_error_message(exc: ApiException) -> str:
    """
    Convert an Upstox ApiException into a safe log/message string.

    Important:
        Do NOT include the full exception because it may contain
        HTTP headers, cookies, request metadata, or other details
        that are unnecessary for application logs.
    """

    status_code = getattr(exc, "status", None)

    if status_code == 401:
        return "Upstox access token is invalid or unauthorized"

    if status_code == 403:
        return "Upstox API access forbidden"

    if status_code == 429:
        return "Upstox API rate limit exceeded"

    if status_code:
        return f"Upstox API request failed with HTTP {status_code}"

    return "Upstox API request failed"


def get_profile(access_token: Optional[str] = None) -> Dict[str, Any]:
    """
    Fetch the Upstox user profile to validate an access token.

    Args:
        access_token:
            Optional.

            If supplied:
                The supplied token is validated.

            If omitted or empty:
                The token currently stored in the project's
                in-memory cache is validated.

    Returns:
        {
            "valid": bool,
            "status": "success" | "error",
            "status_text": str,
            "user_id": str | None,
            "user_name": str | None,
            "broker": "UPSTOX",
            "message": str,
            "token_source": "explicit" | "cache" | "none",
            "raw": dict | None,
        }

    Security:
        The access token is never included in logs or returned
        by this function.
    """

    # ---------------------------------------------------------
    # Resolve token
    # ---------------------------------------------------------

    token_from_explicit = bool(access_token and access_token.strip())
    resolved = _resolve_token(access_token)
    token_source = (
        "explicit" if token_from_explicit else ("cache" if resolved else "none")
    )

    # ---------------------------------------------------------
    # No token
    # ---------------------------------------------------------

    if not resolved:
        logger.warning(
            "Profile validation skipped | reason=no_token | source=%s", token_source
        )
        return {
            "valid": False,
            "status": "error",
            "status_text": "token_empty",
            "user_id": None,
            "user_name": None,
            "broker": UPSTOX_BROKER,
            "message": "Access token is empty",
            "token_source": token_source,
            "raw": None,
        }

    # ---------------------------------------------------------
    # Call Upstox Profile API
    # ---------------------------------------------------------

    try:
        configuration = upstox_client.Configuration()

        # The token is used internally only.
        # NEVER log this value.
        configuration.access_token = resolved

        api_instance = upstox_client.UserApi(upstox_client.ApiClient(configuration))
        response = api_instance.get_profile(API_VERSION)

        # -----------------------------------------------------
        # Normalize SDK response
        # -----------------------------------------------------

        if hasattr(response, "to_dict"):
            response_data = response.to_dict()
        elif isinstance(response, dict):
            response_data = response
        else:
            response_data = {}

        status = response_data.get("status")
        data = response_data.get("data") or {}
        user_id = data.get("user_id")
        user_name = data.get("user_name")

        # -----------------------------------------------------
        # Successful validation
        # -----------------------------------------------------

        if (
            status == "success"
            and user_id
            and str(user_id).strip()
            and user_name
            and str(user_name).strip()
        ):
            logger.info(
                "Upstox profile validation successful | "
                "source=%s | user_id_present=true | "
                "user_name_present=true",
                token_source,
            )
            return {
                "valid": True,
                "status": "success",
                "status_text": "profile_success",
                "user_id": str(user_id),
                "user_name": str(user_name),
                "broker": UPSTOX_BROKER,
                "message": "Upstox token is valid",
                "token_source": token_source,
                # Keep raw response out of normal API result.
                "raw": None,
            }

        # -----------------------------------------------------
        # Invalid profile response
        # -----------------------------------------------------

        logger.warning(
            "Upstox profile validation failed | "
            "source=%s | status=%s | "
            "user_id_present=%s | "
            "user_name_present=%s",
            token_source,
            status,
            bool(user_id),
            bool(user_name),
        )
        return {
            "valid": False,
            "status": "error",
            "status_text": "profile_failed",
            "user_id": str(user_id) if user_id else None,
            "user_name": str(user_name) if user_name else None,
            "broker": UPSTOX_BROKER,
            "message": "Invalid Upstox profile response",
            "token_source": token_source,
            "raw": None,
        }

    # ---------------------------------------------------------
    # Upstox API error
    # ---------------------------------------------------------

    except ApiException as exc:
        status_code = getattr(exc, "status", None)
        safe_message = _safe_api_error_message(exc)

        if status_code == 401:
            logger.warning(
                "Upstox token validation failed | "
                "source=%s | status=401 | "
                "reason=unauthorized",
                token_source,
            )
        else:
            logger.error(
                "Upstox profile API failed | " "source=%s | status=%s",
                token_source,
                status_code,
            )

        return {
            "valid": False,
            "status": "error",
            "status_text": "unauthorized" if status_code == 401 else "api_error",
            "user_id": None,
            "user_name": None,
            "broker": UPSTOX_BROKER,
            "message": safe_message,
            "token_source": token_source,
            "raw": None,
        }

    # ---------------------------------------------------------
    # Unexpected error
    # ---------------------------------------------------------

    except Exception as exc:  # noqa: BLE001
        logger.exception("Unexpected error during Upstox profile validation")
        return {
            "valid": False,
            "status": "error",
            "status_text": "unexpected_error",
            "user_id": None,
            "user_name": None,
            "broker": UPSTOX_BROKER,
            "message": "Unexpected error during Upstox profile validation",
            "token_source": token_source,
            "raw": None,
        }


# =============================================================
# Backwards-compatible alias
# =============================================================


def validate_upstox_token(access_token: Optional[str] = None) -> Dict[str, Any]:
    """
    Backwards-compatible alias for get_profile().

    Validates either:
        - explicitly supplied token, or
        - cached token.
    """

    return get_profile(access_token)


# =============================================================
# Optional self-test
# =============================================================

# if __name__ == "__main__":
#
#     import json
#
#     logger.info(
#         "Running get_profile_status.py self-test"
#     )
#
#     result = get_profile()
#
#     print(
#         json.dumps(
#             result,
#             indent=2,
#             default=str,
#         )
#     )
