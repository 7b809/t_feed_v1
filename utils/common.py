import math
import socket
import time
from datetime import datetime
from typing import Any, Callable

from upstox_client.rest import ApiException

from core import config


def safe_float(value: Any, default: float | None = None) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError, OverflowError):
        return default


def safe_int(value: Any, default: int = 0) -> int:
    value = safe_float(value)
    return int(value) if value is not None else default


def object_to_dict(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    method = getattr(value, "to_dict", None)
    if callable(method):
        result = method()
        return result if isinstance(result, dict) else {}
    return {}


def parse_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)):
        number = float(value)
        if abs(number) > 10_000_000_000:
            number /= 1000
        try:
            parsed = datetime.fromtimestamp(number, tz=config.MARKET_TIMEZONE)
        except (ValueError, OSError, OverflowError):
            return None
    else:
        text = str(value).strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=config.MARKET_TIMEZONE)
    return parsed.astimezone(config.MARKET_TIMEZONE)


def api_error(ex: Exception) -> dict:
    status = getattr(ex, "status", None)
    body = getattr(ex, "body", None)
    if isinstance(body, bytes):
        body = body.decode("utf-8", errors="replace")
    parsed = {}
    if isinstance(body, str):
        try:
            import json
            parsed = json.loads(body)
        except Exception:
            parsed = {"message": body}
    elif isinstance(body, dict):
        parsed = body
    errors = parsed.get("errors", []) if isinstance(parsed, dict) else []
    first = errors[0] if errors and isinstance(errors[0], dict) else parsed
    return {
        "type": type(ex).__name__,
        "http_status": int(status) if str(status).isdigit() else None,
        "error_code": first.get("errorCode") or first.get("error_code") if isinstance(first, dict) else None,
        "message": first.get("message") if isinstance(first, dict) else str(ex),
        "body": parsed or body,
    }


def retryable(ex: Exception) -> bool:
    if isinstance(ex, ApiException):
        status = getattr(ex, "status", None)
        return status == 429 or (isinstance(status, int) and status >= 500)
    return isinstance(ex, (TimeoutError, ConnectionResetError, ConnectionAbortedError, socket.timeout))


def call_with_retry(name: str, call: Callable[[], Any]) -> Any:
    for attempt in range(1, config.API_MAX_RETRIES + 1):
        try:
            result = call()
            time.sleep(config.API_CALL_DELAY_SECONDS)
            return result
        except Exception as ex:
            if not retryable(ex) or attempt >= config.API_MAX_RETRIES:
                raise
            time.sleep(config.API_RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1)))
    raise RuntimeError(f"{name} exhausted retries")


def chunks(items: list, size: int):
    for index in range(0, len(items), size):
        yield items[index:index + size]
