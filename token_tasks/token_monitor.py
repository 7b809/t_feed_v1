# token_tasks/token_monitor.py
"""Token validity monitoring.

This module is deliberately conservative about what it logs and sends.
Access tokens, token fragments, and full user identifiers never reach
the log stream or Telegram at full fidelity — anything that could leak
a token or personal data goes through a masking helper first.

Rules enforced here:

    * The raw access token is never interpolated into any log line,
      Telegram message, or returned error string.
    * User identifiers (user_id, user_name) are FULLY redacted in
      logs and Telegram messages — not a single character of the
      original value is revealed.
    * Any provider error string is passed through ``_sanitize_error``,
      which redacts JWT-length runs before the string is logged or
      returned.
"""

from core import config
from core.logger import get_logger
from services.telegram_service import telegram_service
from token_tasks.token_store import token_store
from token_tasks.upstox_token_client import upstox_token_client

logger = get_logger(__file__)


# ---------------------------------------------------------------------------
# Masking / sanitising helpers
# ---------------------------------------------------------------------------

# Anything this long and unbroken is treated as a token-like value and
# redacted before it can reach a log handler or Telegram.
_JWT_MIN_LEN = 40

# Fixed placeholder returned for any non-empty PII value. Using a
# constant means length is not leaked either.
_PII_REDACTED = "[redacted]"


def _mask_pii(value) -> str:
    """Fully redact a user id / user name for safe logging.

    No characters of the original value are revealed, and the length
    of the value is not exposed either. Empty input maps to an empty
    string so callers can still distinguish "missing" from "present".

    Examples::

        _mask_pii("ABCD1234")  -> "[redacted]"
        _mask_pii("ab")        -> "[redacted]"
        _mask_pii("")          -> ""
        _mask_pii(None)        -> ""
    """
    text = str(value or "").strip()
    if not text:
        return ""
    return _PII_REDACTED


def _sanitize_error(value) -> str:
    """Redact token-like runs from a provider error string.

    Splits on whitespace and replaces any part that is 40 characters or
    longer with ``<redacted>``. This covers JWTs, bearer tokens, and
    long opaque identifiers that could end up inside an exception
    message.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    parts = text.split()
    cleaned = [
        "<redacted>" if len(part) >= _JWT_MIN_LEN else part
        for part in parts
    ]
    return " ".join(cleaned)


def _safe_int(value, default: int = 30) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def get_token_monitor_interval_minutes() -> int:
    """Return the configured token monitor interval, minimum 1 minute."""
    return _safe_int(
        getattr(config, "UPSTOX_TOKEN_CHECK_INTERVAL_MINUTES", 30)
    )


def check_upstox_token_validity(
    send_success_message: bool = False,
) -> dict:
    """Validate the stored Upstox access token against the provider.

    Never logs or returns the raw token, token fragments, or the full
    user profile in log output. All error strings are sanitised before
    they reach a logger or a Telegram message.

    Args:
        send_success_message:
            If True, sends a Telegram success message when the token is
            valid. For scheduled background jobs keep this False to
            avoid noise.

    Returns:
        Dictionary status payload.
    """
    logger.info("Starting Upstox token validity check.")

    token = token_store.get_access_token()

    if not token:
        message = (
            "Upstox access token is missing in MongoDB.\n\n"
            "Please use Telegram command /save_token and then paste the "
            "latest raw Upstox access token."
        )
        logger.warning("Upstox access token missing in MongoDB.")

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

        profile_data = (
            profile.get("data", {}) if isinstance(profile, dict) else {}
        )

        # Fully redacted identifiers — no characters of user_id /
        # user_name reach the log stream.
        logger.info(
            "Upstox token validation successful. user_id=%s, user_name=%s, broker=%s",
            _mask_pii(profile_data.get("user_id")),
            _mask_pii(profile_data.get("user_name")),
            profile_data.get("broker") or "—",
        )

        if send_success_message:
            telegram_service.send_message(
                title="Upstox Token Valid",
                message=(
                    "Current Upstox access token is valid.\n\n"
                    f"User ID: {_mask_pii(profile_data.get('user_id'))}\n"
                    f"User Name: {_mask_pii(profile_data.get('user_name'))}\n"
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

    safe_error = _sanitize_error(error) or "provider validation failed"

    token_store.update_validation_status(
        is_valid=False,
        status="profile_failed",
        error=safe_error,
        profile=None,
    )

    logger.error(
        "Upstox token validation failed. reason=%s",
        safe_error,
    )

    telegram_message = (
        "Current Upstox access token looks expired, invalid, or corrupted.\n\n"
        "Please use Telegram command /save_token and then paste the latest "
        "raw Upstox access token.\n\n"
        f"Validation Error: {safe_error}"
    )

    telegram_service.send_message(
        title="Upstox Token Expired or Invalid",
        message=telegram_message,
        level="ERROR",
    )

    return {
        "status": "failed",
        "valid": False,
        "message": "Upstox access token validation failed.",
        "error": safe_error,
    }


def force_token_status_message() -> dict:
    """Manually validate the token and always send a Telegram message."""
    return check_upstox_token_validity(send_success_message=True)


def get_token_status_snapshot() -> dict:
    """Return a safe token-status snapshot (token is always masked)."""
    return {
        "status": "success",
        "monitor_interval_minutes": get_token_monitor_interval_minutes(),
        "token_document": token_store.get_masked_token_info(),
    }