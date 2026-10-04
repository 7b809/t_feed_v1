"""
upstox_app/market_quote/fetch_quote.py

Low-level wrapper around `upstox_client.MarketQuoteV3Api`.

Interval codes accepted by the Upstox OHLC endpoint:

    I1     -> 1 minute
    I1_5   -> 5 minutes
    I1_15  -> 15 minutes
    I1_30  -> 30 minutes
    I1_H   -> 1 hour
    I1_D   -> 1 day
    I1_W   -> 1 week
    I1_MO  -> 1 month

The default is `I1` (1 minute).

Improvements over the raw SDK snippet:
  - Pulls the access token from token_service (never from env at call time).
  - Logs one compact line per call, no full tracebacks.
  - Returns a normalised dict {"status", "data", "raw"} shape.
  - Raises a single `QuoteFetchError` so callers can branch on a stable
    exception type instead of a generic ApiException.
"""
from typing import Any, Dict, Optional

import upstox_client
from upstox_client.rest import ApiException

from core.logger import get_logger
from token_tasks.service import token_service

logger = get_logger(__name__)

DEFAULT_INTERVAL = "I1"

VALID_INTERVALS = {
    "I1", "I1_5", "I1_15", "I1_30", "I1_H", "I1_D", "I1_W", "I1_MO",
}

# Map human-friendly aliases to the SDK's canonical code.
_INTERVAL_ALIASES = {
    "1m": "I1",
    "1minute": "I1",
    "1min": "I1",
    "5m": "I1_5",
    "15m": "I1_15",
    "30m": "I1_30",
    "1h": "I1_H",
    "1d": "I1_D",
    "1w": "I1_W",
    "1mo": "I1_MO",
}


class QuoteFetchError(Exception):
    """Raised when a single MarketQuoteV3Api call fails."""

    def __init__(self, message: str, *, status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def _short(exc: BaseException) -> str:
    msg = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
    if len(msg) > 160:
        msg = msg[:160] + "…"
    return f"{type(exc).__name__}: {msg}" if msg else type(exc).__name__


def _status_code_of(exc: BaseException) -> Optional[int]:
    for attr in ("status", "status_code", "code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    text = str(exc)
    for token in ("(429)", "(401)", "(400)", "(500)", "(502)", "(503)"):
        if token in text:
            try:
                return int(token.strip("()"))
            except ValueError:
                return None
    return None


def normalise_interval(value: Optional[str]) -> str:
    """
    Return the SDK-canonical interval code.

    Accepts `I1`, `I1_5`, and human aliases like `1m`, `1minute`, `1d`.
    Falls back to `DEFAULT_INTERVAL` (I1) when the input is empty or
    unrecognised.
    """
    if not value:
        return DEFAULT_INTERVAL
    text = str(value).strip()
    if not text:
        return DEFAULT_INTERVAL
    if text in VALID_INTERVALS:
        return text
    return _INTERVAL_ALIASES.get(text.lower(), DEFAULT_INTERVAL)


def _build_client() -> "upstox_client.MarketQuoteV3Api":
    token = token_service.get_access_token()
    if not token:
        raise QuoteFetchError("no access token available in cache")

    configuration = upstox_client.Configuration()
    configuration.access_token = token
    api_client = upstox_client.ApiClient(configuration)
    return upstox_client.MarketQuoteV3Api(api_client)


def _normalise(response: Any) -> Dict[str, Any]:
    if response is None:
        return {"status": "empty", "data": {}, "raw": None}

    if isinstance(response, dict):
        payload = response
    elif hasattr(response, "to_dict"):
        try:
            payload = response.to_dict()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "fetch_quote::_normalise | step=to_dict | reason=%s", _short(exc)
            )
            payload = {"status": "unknown", "data": {}, "raw": None}
    else:
        payload = {"status": "unknown", "data": {}, "raw": response}

    data = payload.get("data") or {}
    status = payload.get("status", "success" if data else "empty")
    return {"status": status, "data": data, "raw": payload}


def fetch_ohlc_raw(
    instrument_keys: str,
    interval: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Fetch OHLC quotes for one or more comma-separated instrument keys.

    `interval` is normalised to the SDK code (default `I1`).
    This is a single SDK call: no batching, no retry, no rate limiting.

    Raises:
        QuoteFetchError on any SDK failure (auth, network, HTTP, parse).
    """
    if not instrument_keys:
        raise QuoteFetchError("empty instrument_keys")

    sdk_interval = normalise_interval(interval)

    try:
        api = _build_client()
    except QuoteFetchError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise QuoteFetchError(f"client build failed: {_short(exc)}") from exc

    try:
        response = api.get_market_quote_ohlc(
            sdk_interval, instrument_key=instrument_keys
        )
    except ApiException as exc:
        status = _status_code_of(exc)
        logger.error(
            "fetch_quote::fetch_ohlc_raw failed | step=sdk call "
            "| interval=%s | status=%s | reason=%s",
            sdk_interval, status, _short(exc),
        )
        raise QuoteFetchError(_short(exc), status_code=status) from exc
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "fetch_quote::fetch_ohlc_raw failed | step=sdk call "
            "| interval=%s | reason=%s",
            sdk_interval, _short(exc),
        )
        raise QuoteFetchError(_short(exc)) from exc

    return _normalise(response)


def fetch_ltp_raw(instrument_keys: str) -> Dict[str, Any]:
    """LTP variant. Same contract as fetch_ohlc_raw, no interval."""
    if not instrument_keys:
        raise QuoteFetchError("empty instrument_keys")
    try:
        api = _build_client()
        response = api.get_market_quote_ltp(instrument_key=instrument_keys)
    except QuoteFetchError:
        raise
    except ApiException as exc:
        status = _status_code_of(exc)
        logger.error(
            "fetch_quote::fetch_ltp_raw failed | step=sdk call "
            "| status=%s | reason=%s",
            status, _short(exc),
        )
        raise QuoteFetchError(_short(exc), status_code=status) from exc
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "fetch_quote::fetch_ltp_raw failed | step=sdk call | reason=%s",
            _short(exc),
        )
        raise QuoteFetchError(_short(exc)) from exc
    return _normalise(response)