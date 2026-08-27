from core import config
from core.logger import get_logger
from services.telegram_service import telegram_service
from token_tasks.token_store import token_store
from token_tasks.upstox_token_client import upstox_token_client

logger = get_logger(__file__)


def get_token_monitor_interval_minutes() -> int:
    """
    Returns token monitor interval in minutes.

    Default:
        30 minutes

    Config:
        UPSTOX_TOKEN_CHECK_INTERVAL_MINUTES
    """

    try:
        interval = int(
            getattr(
                config,
                "UPSTOX_TOKEN_CHECK_INTERVAL_MINUTES",
                30,
            )
        )

        return max(1, interval)

    except Exception:
        return 30


def check_upstox_token_validity(
    send_success_message: bool = False,
) -> dict:
    """
    Background token validity check.

    Logic:
        1. Read current access token from MongoDB.
        2. Call Upstox get_profile API using current token.
        3. If response status is success, token is valid.
        4. If response fails, token is expired, invalid, or corrupted.
        5. Update MongoDB validation metadata.
        6. Send Telegram alert only when token is missing or invalid.

    Args:
        send_success_message:
            If True, sends Telegram success message when token is valid.
            For scheduled background jobs, keep this False to avoid noise.

    Returns:
        Dictionary status payload.
    """

    logger.info("Starting Upstox token validity check.")

    token = token_store.get_access_token()

    if not token:
        message = (
            "Upstox access token is missing in MongoDB.\n\n"
            "Please use Telegram command /save-token and then paste the latest "
            "raw Upstox access token."
        )

        logger.warning(message)

        token_store.update_validation_status(
            is_valid=False,
            status="missing_token",
            error="access_token missing in MongoDB token document",
            profile=None,
        )

        telegram_service.send_message(
            title="Upstox Token Missing",
            message=message,
            level="WARNING",
        )

        return {
            "status": "missing",
            "valid": False,
            "message": message,
        }

    is_valid, profile, error = upstox_token_client.validate_token(token)

    if is_valid:
        token_store.update_validation_status(
            is_valid=True,
            status="profile_success",
            error=None,
            profile=profile,
        )

        profile_data = profile.get("data", {}) if isinstance(profile, dict) else {}

        logger.info(
            f"Upstox token validation successful. "
            f"user_id={profile_data.get('user_id')}, "
            f"user_name={profile_data.get('user_name')}, "
            f"broker={profile_data.get('broker')}"
        )

        if send_success_message:
            telegram_service.send_message(
                title="Upstox Token Valid",
                message=(
                    "Current Upstox access token is valid.\n\n"
                    f"User ID: {profile_data.get('user_id')}\n"
                    f"User Name: {profile_data.get('user_name')}\n"
                    f"Broker: {profile_data.get('broker')}\n"
                    f"Is Active: {profile_data.get('is_active')}"
                ),
                level="SUCCESS",
            )

        return {
            "status": "success",
            "valid": True,
            "message": "Upstox access token is valid.",
            "profile": profile,
        }

    token_store.update_validation_status(
        is_valid=False,
        status="profile_failed",
        error=error,
        profile=None,
    )

    message = (
        "Current Upstox access token looks expired, invalid, or corrupted.\n\n"
        "Please use Telegram command /save_token and then paste the latest "
        "raw Upstox access token.\n\n"
        f"Validation Error: {str(error)[:25]}"
    )

    logger.error(message)

    telegram_service.send_message(
        title="Upstox Token Expired or Invalid",
        message=message,
        level="ERROR",
    )

    return {
        "status": "failed",
        "valid": False,
        "message": "Upstox access token validation failed.",
        "error": error,
    }


def force_token_status_message() -> dict:
    """
    Manually validates token and sends Telegram message even if token is valid.

    Useful for:
        - Telegram /token-status extended checks
        - Manual debugging
        - API/debug route if added later
    """

    return check_upstox_token_validity(
        send_success_message=True,
    )


def get_token_status_snapshot() -> dict:
    """
    Returns current token status snapshot without calling Upstox API.

    This is safe for display because token_store masks access_token.
    """

    return {
        "status": "success",
        "monitor_interval_minutes": get_token_monitor_interval_minutes(),
        "token_document": token_store.get_masked_token_info(),
    }
