"""
token_tasks/_profile.py

Central resolver for the Upstox profile-validation call.

The project has shipped several names for this function over time:
    get_profile_status, check_profile, validate_profile,
    fetch_profile, get_profile, validate_token, verify_token, ...

Instead of guessing at each import site, this module probes the
`upstox_app.profile.get_profile_status` module for any of those names at
first use and caches the result.

Contract of the resolved callable:
    fn(access_token: str | None) -> dict with at least {"valid": bool}

If nothing resolves, `validate_profile()` falls back to calling the
Upstox UserApi directly using the token_service cache, and if that also
fails, returns {"valid": False, "error": "<reason>"}.
"""
import threading
from typing import Any, Callable, Dict, Optional

from core.logger import get_logger

logger = get_logger(__name__)

_resolver_lock = threading.RLock()
_cached_fn: Optional[Callable[..., Any]] = None
_resolved_once = False

_CANDIDATE_MODULES = (
    "upstox_app.profile.get_profile_status",
    "upstox_app.profile",
    "upstox_app.get_profile_status",
)

_CANDIDATE_FUNCS = (
    "get_profile_status",
    "check_profile",
    "validate_profile",
    "fetch_profile",
    "get_profile",
    "validate_token",
    "verify_token",
    "check_token",
    "profile_status",
)


def _short(exc: BaseException) -> str:
    msg = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
    if len(msg) > 160:
        msg = msg[:160] + "…"
    return f"{type(exc).__name__}: {msg}" if msg else type(exc).__name__


def _resolve() -> Optional[Callable[..., Any]]:
    """Probe modules and return the first matching callable, else None."""
    global _cached_fn, _resolved_once

    with _resolver_lock:
        if _resolved_once:
            return _cached_fn

        for module_name in _CANDIDATE_MODULES:
            try:
                import importlib
                module = importlib.import_module(module_name)
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "_profile::_resolve | module import skipped | module=%s | reason=%s",
                    module_name, _short(exc),
                )
                continue

            for fn_name in _CANDIDATE_FUNCS:
                fn = getattr(module, fn_name, None)
                if callable(fn):
                    logger.info(
                        "_profile::_resolve | resolved | module=%s | func=%s",
                        module_name, fn_name,
                    )
                    _cached_fn = fn
                    _resolved_once = True
                    return _cached_fn

        logger.warning(
            "_profile::_resolve | no profile function found in any of %s",
            list(_CANDIDATE_MODULES),
        )
        _cached_fn = None
        _resolved_once = True
        return None


def _call_resolved(access_token: Optional[str]) -> Optional[Dict[str, Any]]:
    """Invoke the resolved callable with or without an explicit token."""
    fn = _resolve()
    if fn is None:
        return None

    # Try explicit-token signature first.
    if access_token:
        for call_args in ((access_token,), ()):
            try:
                result = fn(*call_args)
                if isinstance(result, dict):
                    return result
                if isinstance(result, bool):
                    return {"valid": result, "error": None}
                return {"valid": bool(result), "error": None}
            except TypeError:
                # Signature mismatch — try the next shape.
                continue
            except Exception as exc:  # noqa: BLE001
                return {"valid": False, "error": _short(exc)}
    else:
        try:
            result = fn()
            if isinstance(result, dict):
                return result
            if isinstance(result, bool):
                return {"valid": result, "error": None}
            return {"valid": bool(result), "error": None}
        except Exception as exc:  # noqa: BLE001
            return {"valid": False, "error": _short(exc)}

    return {"valid": False, "error": "profile function rejected all call shapes"}


def _fallback_sdk(access_token: Optional[str]) -> Dict[str, Any]:
    """
    Direct SDK call as a last resort. Uses the cached token when the
    caller did not provide one.
    """
    token = access_token
    if not token:
        try:
            from token_tasks.service import token_service
            token = token_service.get_access_token()
        except Exception as exc:  # noqa: BLE001
            return {"valid": False, "error": f"token cache unavailable: {_short(exc)}"}

    if not token:
        return {"valid": False, "error": "no token available"}

    try:
        import upstox_client
        configuration = upstox_client.Configuration()
        configuration.access_token = token
        api_client = upstox_client.ApiClient(configuration)
        api = upstox_client.UserApi(api_client)
        response = api.get_profile("2.0")
        # Success reaching the API means the token is valid.
        return {"valid": True, "error": None, "profile": response}
    except Exception as exc:  # noqa: BLE001
        return {"valid": False, "error": f"sdk: {_short(exc)}"}


def validate_profile(access_token: Optional[str] = None) -> Dict[str, Any]:
    """
    Return {"valid": bool, "error": str | None, ...}. Never raises.
    """
    try:
        result = _call_resolved(access_token)
        if result is not None:
            return result
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "_profile::validate_profile failed | step=resolved call | reason=%s",
            _short(exc),
        )

    try:
        return _fallback_sdk(access_token)
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "_profile::validate_profile failed | step=sdk fallback | reason=%s",
            _short(exc),
        )
        return {"valid": False, "error": f"all validation paths failed: {_short(exc)}"}