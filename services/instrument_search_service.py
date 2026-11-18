"""Upstox instrument search HTTP client with a bounded local JSON cache."""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

import requests

from core.config import (
    DATA_DIR,
    UPSTOX_INSTRUMENT_CACHE_ENABLED,
    UPSTOX_INSTRUMENT_CACHE_MAX_STALE_SECONDS,
    UPSTOX_HTTP_TIMEOUT_SECONDS,
    UPSTOX_USER_AGENT,
)
from core.logger import get_logger
from core.token_service import fetch_access_token

logger = get_logger(__file__)
UPSTOX_SEARCH_URL = "https://api.upstox.com/v2/instruments/search"
_CACHE_DIR = DATA_DIR / "instruments" / "search_cache"
_CACHE_LOCK = threading.RLock()


class InstrumentSearchError(Exception):
    """Expected upstream/search failure with safe client-facing diagnostics."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 502,
        details: dict | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.details = details or {}


class UpstoxAccessBlocked(InstrumentSearchError):
    """Upstox/Cloudflare rejected the server-side HTTP client signature."""


def _cache_path(params: dict[str, Any]) -> Path:
    encoded = json.dumps(params, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    return _CACHE_DIR / f"{digest}.json"


def _read_cache(params: dict[str, Any]) -> tuple[dict | None, int | None]:
    if not UPSTOX_INSTRUMENT_CACHE_ENABLED:
        return None, None
    path = _cache_path(params)
    try:
        with _CACHE_LOCK:
            if not path.is_file():
                return None, None
            cached = json.loads(path.read_text(encoding="utf-8"))
        saved_at = float(cached.get("cached_at", 0))
        age = max(0, int(time.time() - saved_at))
        if age > UPSTOX_INSTRUMENT_CACHE_MAX_STALE_SECONDS:
            return None, age
        payload = cached.get("payload")
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            return None, age
        return payload, age
    except (OSError, ValueError, TypeError):
        logger.warning("Ignoring unreadable instrument-search cache entry: %s", path.name)
        return None, None


def _write_cache(params: dict[str, Any], payload: dict) -> None:
    if not UPSTOX_INSTRUMENT_CACHE_ENABLED:
        return
    path = _cache_path(params)
    temp_path = path.with_suffix(".tmp")
    record = {"cached_at": time.time(), "payload": payload}
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with _CACHE_LOCK:
            temp_path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
            os.replace(temp_path, path)
    except OSError:
        logger.exception("Could not persist instrument-search cache")


def _cache_response(payload: dict, *, age: int, stale: bool) -> dict:
    return {
        "status": payload.get("status", "success"),
        "data": payload.get("data", []),
        "meta_data": payload.get("meta_data", {}),
        "cache": {"source": "local", "stale": stale, "age_seconds": age},
    }


def _extract_upstream_error(response: requests.Response) -> dict:
    """Pull the useful parts out of an Upstox error body.

    Returns a dict with ``status``, ``error_code``, ``error_name``,
    ``message``, and ``body_excerpt``. Never raises; missing/broken bodies
    yield ``None`` fields and an empty excerpt.
    """
    result: dict[str, Any] = {
        "status": response.status_code,
        "error_code": None,
        "error_name": None,
        "message": None,
        "body_excerpt": "",
    }
    try:
        result["body_excerpt"] = (response.text or "")[:2000]
    except Exception:
        pass
    try:
        body = response.json()
    except (ValueError, TypeError):
        body = None
    if isinstance(body, dict):
        result["error_name"] = body.get("error_name") or body.get("status")
        errors = body.get("errors")
        if isinstance(errors, list) and errors and isinstance(errors[0], dict):
            first = errors[0]
            result["error_code"] = (
                first.get("errorCode") or first.get("error_code")
            )
            result["message"] = first.get("message")
        if not result["message"] and isinstance(body.get("message"), str):
            result["message"] = body["message"]
    return result


def _build_hint(params: dict[str, Any], upstream: dict[str, Any]) -> str:
    """Return a context-aware, human-readable hint for a 4xx failure.

    Hints are matched against the upstream error code / message, not against
    the query alone. Previously a numeric-only query always appended the
    "try a trading symbol" hint — which was actively misleading when the
    real failure was ``UDAPI100500`` "Please Enter Correct Segment And
    Exchange Values". This function now prefers the upstream signal.
    """
    error_code = (upstream.get("error_code") or "").upper()
    error_message = (upstream.get("message") or "").lower()

    # Upstox rejects the literal string "ALL" for exchanges/segments — the
    # documented "Default: ALL" means "omit this parameter", not "send ALL".
    if (
        error_code == "UDAPI100500"
        or "segment" in error_message
        or "exchange" in error_message
    ):
        return (
            " Omit exchanges/segments to search across all values "
            "(Upstox rejects the literal string 'ALL')."
        )

    # Numeric-only queries are valid but usually match many contracts; the
    # hint applies only when the failure is *not* about filters.
    query = params.get("query")
    if isinstance(query, str) and query.strip().isdigit():
        return (
            " Numeric-only queries usually match many contracts; "
            "try a trading symbol like 'NIFTY' or 'NIFTY24500CE' "
            "instead of a strike price."
        )

    return ""


def search_upstox_instruments(params: dict[str, Any]) -> dict:
    """Search Upstox; on upstream failure serve a recent cached result if available."""
    cached_payload, cache_age = _read_cache(params)
    try:
        token = fetch_access_token()
    except Exception as exc:
        if cached_payload is not None:
            logger.warning(
                "Serving cached instrument results because the MongoDB token could not be loaded; age_seconds=%s",
                cache_age,
            )
            return _cache_response(cached_payload, age=cache_age or 0, stale=True)
        logger.exception("Could not load Upstox access token for instrument search")
        raise InstrumentSearchError(
            "Upstox access token is unavailable and no usable cached result exists.",
            status_code=503,
            details={"upstream": "token_store", "retryable": True},
        ) from exc

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
        "User-Agent": UPSTOX_USER_AGENT,
    }

    try:
        response = requests.get(
            UPSTOX_SEARCH_URL,
            params=params,
            headers=headers,
            timeout=(min(5, UPSTOX_HTTP_TIMEOUT_SECONDS), UPSTOX_HTTP_TIMEOUT_SECONDS),
        )
        if response.status_code == 403:
            try:
                error_payload = response.json()
            except ValueError:
                error_payload = {"detail": response.text[:1000]}
            if not isinstance(error_payload, dict):
                error_payload = {"detail": str(error_payload)[:1000]}
            error_code = error_payload.get("error_code")
            error_name = error_payload.get("error_name")
            if error_code == 1010 or error_name == "browser_signature_banned":
                ray_id = response.headers.get("cf-ray") or error_payload.get("instance")
                logger.error(
                    "Upstox Cloudflare access block (1010); ray_id=%s; endpoint=%s; "
                    "client_user_agent=%s. Automatic retry intentionally disabled.",
                    ray_id, UPSTOX_SEARCH_URL, UPSTOX_USER_AGENT,
                )
                raise UpstoxAccessBlocked(
                    "Upstox/Cloudflare blocked this server-side API request (Error 1010). "
                    "The API owner must review/allow the request signature.",
                    status_code=502,
                    details={
                        "upstream": "upstox",
                        "upstream_status": 403,
                        "error_code": 1010,
                        "error_name": "browser_signature_banned",
                        "cloudflare_ray_id": ray_id,
                        "retryable": False,
                        "owner_action_required": True,
                    },
                )
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as exc:
            raise InstrumentSearchError(
                "Upstox returned a non-JSON instrument-search response.",
                details={"upstream": "upstox", "upstream_status": response.status_code},
            ) from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("data", []), list):
            raise InstrumentSearchError(
                "Upstox returned an unexpected instrument-search response format.",
                details={"upstream": "upstox", "upstream_status": response.status_code},
            )
        normalized = {
            "status": payload.get("status", "success"),
            "data": payload.get("data", []),
            "meta_data": payload.get("meta_data", {}),
        }
        _write_cache(params, normalized)
        return {**normalized, "cache": {"source": "upstox", "stale": False, "age_seconds": 0}}

    except UpstoxAccessBlocked as exc:
        if cached_payload is not None:
            logger.warning(
                "Serving cached instrument results after Upstox Error 1010; cache_age_seconds=%s",
                cache_age,
            )
            return _cache_response(cached_payload, age=cache_age or 0, stale=True)
        raise
    except InstrumentSearchError as exc:
        if cached_payload is not None:
            logger.warning(
                "Serving cached instrument results after an upstream response error; age_seconds=%s",
                cache_age,
            )
            return _cache_response(cached_payload, age=cache_age or 0, stale=True)
        raise
    except requests.Timeout as exc:
        if cached_payload is not None:
            logger.warning("Serving cached instrument results after Upstox timeout; age_seconds=%s", cache_age)
            return _cache_response(cached_payload, age=cache_age or 0, stale=True)
        logger.warning("Upstox instrument search timed out")
        raise InstrumentSearchError(
            "Upstox instrument search timed out and no usable cached result is available.",
            status_code=504,
            details={"upstream": "upstox", "retryable": True},
        ) from exc

    # ------------------------------------------------------------------
    # HTTP error handling.
    #
    # * extracts the upstream error_code / error_name / message,
    # * maps 4xx (except 401/403/429) to 400,
    # * propagates 401/403/429 unchanged,
    # * maps 5xx to 502,
    # * logs the full params dict and a body excerpt so the failure is
    #   reproducible from the log alone,
    # * appends a context-aware hint (see _build_hint).
    # ------------------------------------------------------------------
    except requests.HTTPError as exc:
        upstream = _extract_upstream_error(exc.response) if exc.response is not None else {
            "status": None, "error_code": None, "error_name": None,
            "message": None, "body_excerpt": "",
        }
        upstream_status = upstream["status"]

        logger.warning(
            "Upstox instrument search HTTP failure; status=%s; params=%r; "
            "upstream_error_code=%s; upstream_error_name=%s; "
            "upstream_message=%s; body_excerpt=%r",
            upstream_status, params,
            upstream["error_code"], upstream["error_name"],
            upstream["message"], upstream["body_excerpt"][:500],
        )

        # Cache fallback only makes sense when the failure is not clearly
        # caused by the request itself. A 400 will fail again; serving a
        # previously successful result for the same query would mask a real
        # upstream change. 5xx and 429 are transient — cache is useful there.
        cache_eligible = upstream_status is None or upstream_status >= 500 or upstream_status == 429
        if cached_payload is not None and cache_eligible:
            logger.warning(
                "Serving cached instrument results after upstream HTTP failure; age_seconds=%s",
                cache_age,
            )
            return _cache_response(cached_payload, age=cache_age or 0, stale=True)

        if upstream_status in (401, 403):
            client_status = upstream_status
            retryable = False
            message = (
                f"Upstox rejected the instrument-search request (HTTP {upstream_status}). "
                "Check the access token in MongoDB."
            )
            if upstream["message"]:
                message += f" Upstox said: {upstream['message']}"
        elif upstream_status == 429:
            client_status = 429
            retryable = True
            message = "Upstox rate limit reached on instrument search. Retry shortly."
        elif upstream_status is not None and 400 <= upstream_status < 500:
            client_status = 400
            retryable = False
            message = (
                f"Upstox rejected the instrument-search request (HTTP {upstream_status}). "
                "Check the query parameters."
            )
            if upstream["message"]:
                message += f" Upstox said: {upstream['message']}"
            message += _build_hint(params, upstream)
        else:
            client_status = 502
            retryable = True
            message = "Upstox instrument search is temporarily unavailable."

        raise InstrumentSearchError(
            message,
            status_code=client_status,
            details={
                "upstream": "upstox",
                "upstream_status": upstream_status,
                "upstream_error_code": upstream["error_code"],
                "upstream_error_name": upstream["error_name"],
                "upstream_message": upstream["message"],
                "retryable": retryable,
            },
        ) from exc

    except requests.RequestException as exc:
        logger.warning("Upstox instrument search network failure: %s", type(exc).__name__)
        if cached_payload is not None:
            logger.warning("Serving cached instrument results after network failure; age_seconds=%s", cache_age)
            return _cache_response(cached_payload, age=cache_age or 0, stale=True)
        raise InstrumentSearchError(
            "Upstox instrument search is temporarily unavailable and no usable cached result exists.",
            status_code=503,
            details={"upstream": "upstox", "retryable": True},
        ) from exc