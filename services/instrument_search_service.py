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

    def __init__(self, message: str, *, status_code: int = 502, details: dict | None = None):
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
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else None
        logger.warning("Upstox instrument search HTTP failure; status=%s", status)
        if cached_payload is not None:
            logger.warning("Serving cached instrument results after upstream HTTP failure; age_seconds=%s", cache_age)
            return _cache_response(cached_payload, age=cache_age or 0, stale=True)
        raise InstrumentSearchError(
            "Upstox rejected or failed the instrument-search request.",
            status_code=502,
            details={"upstream": "upstox", "upstream_status": status, "retryable": status in (408, 429, 500, 502, 503, 504)},
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
