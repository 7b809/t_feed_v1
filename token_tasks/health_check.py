"""
token_tasks/health_check.py

Single source of truth for "is the token usable right now".

Order:
  1. Local cache (token_service.get_access_token)
  2. MongoDB reload (token_service.load_token)
  3. Profile validation (via token_tasks._profile.validate_profile)

Never raises.
"""
from typing import Any, Dict

from core.logger import get_logger
from token_tasks._profile import validate_profile
from token_tasks.service import token_service

logger = get_logger(__name__)


def _short(exc: BaseException) -> str:
    msg = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
    if len(msg) > 160:
        msg = msg[:160] + "…"
    return f"{type(exc).__name__}: {msg}" if msg else type(exc).__name__


def check_token_health() -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "valid": False,
        "cached": False,
        "token_source": "none",     # "cache" | "mongo" | "none"
        "validation_status": "not_run",
        "error": None,
    }

    token = None

    # 1) Local cache
    try:
        token = token_service.get_access_token()
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "health_check::cache read failed | step=get_access_token | reason=%s",
            _short(exc),
        )

    # 2) Mongo reload if cache empty
    if not token:
        try:
            token_service.load_token()
            token = token_service.get_access_token()
            if token:
                result["token_source"] = "mongo"
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "health_check::mongo load failed | step=load_token | reason=%s",
                _short(exc),
            )
    else:
        result["token_source"] = "cache"

    result["cached"] = bool(token)
    if not token:
        result["validation_status"] = "no_token"
        return result

    # 3) Profile validation via the shared resolver
    try:
        profile = validate_profile(token)
        valid = bool(profile.get("valid")) if isinstance(profile, dict) else False
        result["valid"] = valid
        result["validation_status"] = "ok" if valid else "rejected"
        if not valid:
            result["error"] = (profile or {}).get("error") if isinstance(profile, dict) else None
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "health_check::validation failed | step=profile check | reason=%s",
            _short(exc),
        )
        result["validation_status"] = "error"
        result["error"] = _short(exc)

    return result