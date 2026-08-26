"""
Intraday candle fetching for the Opening Range package.

This module fetches the current trading day's intraday candles from
the Upstox HistoryV3 API and returns normalized candle records.

It does not directly modify Opening Range runtime state. The main
service module is responsible for storing calculation results in the
shared cache.
"""

from typing import Any

import upstox_client
from upstox_client.rest import ApiException

from core.logger import get_logger

from .candle_utils import (
    extract_candles_from_response,
    normalize_candles,
)
from .constants import (
    DEFAULT_INTRADAY_INTERVAL,
    DEFAULT_INTRADAY_UNIT,
)

logger = get_logger(__file__)


# ============================================================
# Result Builders
# ============================================================


def _build_intraday_result(
    *,
    status: str,
    instrument_key: str,
    unit: str,
    interval: str,
    candles: list | None = None,
    error: Any = None,
) -> dict:
    """
    Builds a consistent intraday candle fetch result.

    Possible statuses:
        success
        empty
        failed
    """
    normalized_candles = candles if isinstance(candles, list) else []

    normalized_error = None

    if error is not None:
        if isinstance(error, str):
            normalized_error = error
        else:
            normalized_error = str(error)

    return {
        "status": status,
        "instrument_key": instrument_key,
        "unit": unit,
        "interval": interval,
        "candles": normalized_candles,
        "candles_count": len(normalized_candles),
        "error": normalized_error,
    }


def _extract_api_exception_error(
    exception: ApiException,
) -> str:
    """
    Extracts a readable error from an Upstox ApiException.

    Preference:
        1. Exception body
        2. Exception reason
        3. String representation
        4. Exception class name
    """
    error_body = getattr(exception, "body", None)

    if error_body:
        return str(error_body)

    error_reason = getattr(exception, "reason", None)

    if error_reason:
        return str(error_reason)

    error_text = str(exception).strip()

    if error_text:
        return error_text

    return type(exception).__name__


# ============================================================
# Intraday Fetch
# ============================================================


def fetch_intraday_candles_for_instrument(
    instrument_key: str,
    unit: str = DEFAULT_INTRADAY_UNIT,
    interval: str = DEFAULT_INTRADAY_INTERVAL,
) -> dict:
    """
    Fetches today's intraday candles using Upstox HistoryV3Api.

    The returned candles are:

        Parsed
        Timezone-aware
        Deduplicated
        Sorted in ascending timestamp order

    Return format:

        {
            "status": "success" | "empty" | "failed",
            "instrument_key": "...",
            "unit": "minutes",
            "interval": "1",
            "candles": [...],
            "candles_count": 0,
            "error": None,
        }
    """
    normalized_instrument_key = str(instrument_key or "").strip()

    normalized_unit = str(unit or DEFAULT_INTRADAY_UNIT).strip()

    normalized_interval = str(interval or DEFAULT_INTRADAY_INTERVAL).strip()

    if not normalized_unit:
        normalized_unit = DEFAULT_INTRADAY_UNIT

    if not normalized_interval:
        normalized_interval = DEFAULT_INTRADAY_INTERVAL

    if not normalized_instrument_key:
        error_message = "instrument_key is required for intraday candle fetch."

        logger.error(
            "Opening Range intraday request rejected. " "error=%s",
            error_message,
        )

        return _build_intraday_result(
            status="failed",
            instrument_key=normalized_instrument_key,
            unit=normalized_unit,
            interval=normalized_interval,
            candles=[],
            error=error_message,
        )

    try:
        api_instance = upstox_client.HistoryV3Api()

        logger.info(
            "Opening Range intraday request started. "
            "instrument_key=%s, unit=%s, interval=%s",
            normalized_instrument_key,
            normalized_unit,
            normalized_interval,
        )

        api_response = api_instance.get_intra_day_candle_data(
            normalized_instrument_key,
            normalized_unit,
            normalized_interval,
        )

        raw_candles = extract_candles_from_response(api_response)

        normalized_candles = normalize_candles(raw_candles)

        status = "success" if normalized_candles else "empty"

        logger.info(
            "Opening Range intraday request completed. "
            "instrument_key=%s, status=%s, "
            "raw_candles_count=%s, "
            "normalized_candles_count=%s",
            normalized_instrument_key,
            status,
            len(raw_candles),
            len(normalized_candles),
        )

        return _build_intraday_result(
            status=status,
            instrument_key=normalized_instrument_key,
            unit=normalized_unit,
            interval=normalized_interval,
            candles=normalized_candles,
            error=None,
        )

    except ApiException as ex:
        error_message = _extract_api_exception_error(ex)

        status_code = getattr(ex, "status", None)

        logger.error(
            "Upstox ApiException during Opening Range "
            "intraday fetch. instrument_key=%s, "
            "unit=%s, interval=%s, status_code=%s, "
            "error=%s",
            normalized_instrument_key,
            normalized_unit,
            normalized_interval,
            status_code,
            error_message,
        )

        return _build_intraday_result(
            status="failed",
            instrument_key=normalized_instrument_key,
            unit=normalized_unit,
            interval=normalized_interval,
            candles=[],
            error=error_message,
        )

    except Exception as ex:
        error_message = f"{type(ex).__name__}: {ex}"

        logger.exception(
            "Unexpected exception during Opening Range "
            "intraday fetch. instrument_key=%s, "
            "unit=%s, interval=%s, error=%s",
            normalized_instrument_key,
            normalized_unit,
            normalized_interval,
            error_message,
        )

        return _build_intraday_result(
            status="failed",
            instrument_key=normalized_instrument_key,
            unit=normalized_unit,
            interval=normalized_interval,
            candles=[],
            error=error_message,
        )


# ============================================================
# Public API
# ============================================================


__all__ = [
    "fetch_intraday_candles_for_instrument",
]
