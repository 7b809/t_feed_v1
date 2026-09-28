# services/candle_service.py
"""
Standalone candle fetching service built directly on the Upstox
HistoryV3Api.

Design goals
------------
* No dependency on chart_service / history_service.
* Historical API returns at most 7 days of data per call.
  If a caller requests a larger window, this service splits the
  range into <=7-day batches, fires them (optionally in parallel),
  and returns the concatenated candles.
* Intraday API is a single call.
* Any exception raised by the Upstox SDK (or by batching) is
  propagated to the caller. This service does NOT swallow errors.
* Provides small helpers for merging and for shaping candles for
  the frontend.

Environment
-----------
Uses the same token source as the rest of the app:
    from services.token_service import token_service

If the token is unavailable, `ValueError` is raised immediately.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from typing import Any, Optional

import upstox_client

from services.token_service import token_service

logger = logging.getLogger(__name__)

# ============================================================
# CONSTANTS
# ============================================================
DEFAULT_INTERVAL = "minutes"
DEFAULT_UNIT = "1"
DEFAULT_HISTORY_DAYS = 7  # Upstox per-request max
MAX_BATCH_WORKERS = 8  # safe under Upstox rate limits

_EXECUTOR = ThreadPoolExecutor(
    max_workers=MAX_BATCH_WORKERS,
    thread_name_prefix="candle-fetch",
)


# ============================================================
# CLIENT
# ============================================================
def _build_history_client() -> upstox_client.HistoryV3Api:
    """
    Build a fresh HistoryV3Api client.

    A new ApiClient is created on every call so the client is not
    shared across threads / event loops.
    """
    access_token = token_service.get_access_token()
    if not access_token:
        raise ValueError("Upstox access token not available")

    configuration = upstox_client.Configuration()
    configuration.access_token = access_token
    api_client = upstox_client.ApiClient(configuration)
    return upstox_client.HistoryV3Api(api_client)


# ============================================================
# HELPERS
# ============================================================
def _to_date(value: Any) -> date:
    """
    Accept date, datetime, or ISO string (YYYY-MM-DD) and return date.
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    raise TypeError(f"Unsupported date value: {value!r}")


def _split_into_batches(
    start_date: date,
    end_date: date,
    batch_days: int = DEFAULT_HISTORY_DAYS,
) -> list[tuple[date, date]]:
    """
    Split [start_date, end_date] into <=batch_days windows.

    Returned newest-first so the caller can preserve the same
    ordering convention as the existing chart pipeline.
    """
    if batch_days <= 0:
        raise ValueError("batch_days must be greater than zero")
    if end_date < start_date:
        raise ValueError("end_date must be on or after start_date")

    batches: list[tuple[date, date]] = []
    current_end = end_date
    while current_end >= start_date:
        current_start = max(
            start_date,
            current_end - timedelta(days=batch_days) + timedelta(days=1),
        )
        batches.append((current_start, current_end))
        current_end = current_start - timedelta(days=1)
    return batches


def _extract_candles(response: Any) -> list:
    """
    Pull `response.data.candles` defensively.

    Raises on unexpected response shapes so callers see a clear
    error instead of silently getting [].
    """
    if response is None:
        raise RuntimeError("Upstox returned None response")

    data = getattr(response, "data", None)
    if data is None:
        # Some SDK versions may return the payload as a dict.
        if isinstance(response, dict):
            data = response.get("data")
        if data is None:
            raise RuntimeError(
                f"Upstox response has no 'data' attribute: {type(response).__name__}"
            )

    candles = getattr(data, "candles", None)
    if candles is None and isinstance(data, dict):
        candles = data.get("candles")

    if candles is None:
        raise RuntimeError("Upstox response.data has no 'candles' field")

    return list(candles) or []


# ============================================================
# PUBLIC — SINGLE HISTORICAL WINDOW
# ============================================================
def fetch_historical_range(
    instrument_key: str,
    from_date: Any,
    to_date: Any,
    interval: str = DEFAULT_INTERVAL,
    unit: str = DEFAULT_UNIT,
) -> list:
    """
    Fetch one historical window (must be <=7 days).

    Returns a list of raw Upstox candles. Raises on any error.
    """
    fd = _to_date(from_date)
    td = _to_date(to_date)

    if td < fd:
        raise ValueError("to_date must be on or after from_date")

    api = _build_history_client()
    logger.info(
        "Historical candles request | instrument=%s | %s -> %s | interval=%s unit=%s",
        instrument_key,
        fd,
        td,
        interval,
        unit,
    )

    response = api.get_historical_candle_data1(
        instrument_key,
        interval,
        unit,
        str(td),  # to_date
        str(fd),  # from_date
    )
    return _extract_candles(response)


# ============================================================
# PUBLIC — HISTORICAL WITH AUTO-BATCHING
# ============================================================
def fetch_historical_candles(
    instrument_key: str,
    from_date: Optional[Any] = None,
    to_date: Optional[Any] = None,
    days: Optional[int] = None,
    interval: str = DEFAULT_INTERVAL,
    unit: str = DEFAULT_UNIT,
    batch_days: int = DEFAULT_HISTORY_DAYS,
    parallel: bool = True,
    max_workers: Optional[int] = None,
) -> list:
    """
    Fetch historical candles for a possibly-large window.

    Resolution of the date window (in priority order):
        1. If `from_date` and `to_date` are given, use them.
        2. Else if `days` is given, end=today-1 and start=end-days+1.
        3. Else default to DEFAULT_HISTORY_DAYS days ending yesterday.

    If the resulting window is wider than `batch_days`, it is split
    into <=batch_days chunks. Chunks are fetched in parallel when
    `parallel=True`.

    Any exception from Upstox (auth, rate-limit, network, bad input)
    is propagated to the caller.
    """
    # ---- Resolve window ----
    if from_date is not None and to_date is not None:
        start = _to_date(from_date)
        end = _to_date(to_date)
    else:
        if days is None:
            days = DEFAULT_HISTORY_DAYS
        if days <= 0:
            raise ValueError("days must be greater than zero")
        end = (
            _to_date(to_date)
            if to_date is not None
            else (date.today() - timedelta(days=1))
        )
        start = end - timedelta(days=days) + timedelta(days=1)

    if end < start:
        raise ValueError("Resolved window has end before start")

    batches = _split_into_batches(start, end, batch_days=batch_days)

    logger.info(
        "Historical fetch plan | instrument=%s | window=%s -> %s | batches=%s | parallel=%s",
        instrument_key,
        start,
        end,
        len(batches),
        parallel,
    )

    # ---- Sequential path ----
    if not parallel or len(batches) <= 1:
        all_candles: list = []
        for idx, (b_start, b_end) in enumerate(batches):
            # Any exception here is intentionally propagated.
            chunk = fetch_historical_range(
                instrument_key=instrument_key,
                from_date=b_start,
                to_date=b_end,
                interval=interval,
                unit=unit,
            )
            logger.info(
                "Historical batch %s/%s loaded | instrument=%s | %s -> %s | candles=%s",
                idx + 1,
                len(batches),
                instrument_key,
                b_start,
                b_end,
                len(chunk),
            )
            all_candles.extend(chunk)
        return all_candles

    # ---- Parallel path ----
    workers = max(1, int(max_workers or MAX_BATCH_WORKERS))
    results: list[Optional[list]] = [None] * len(batches)

    def _fetch_one(i: int, b_start: date, b_end: date):
        # Exceptions propagate to the future.
        candles = fetch_historical_range(
            instrument_key=instrument_key,
            from_date=b_start,
            to_date=b_end,
            interval=interval,
            unit=unit,
        )
        return i, b_start, b_end, candles

    use_shared = workers == _EXECUTOR._max_workers  # type: ignore[attr-defined]
    executor = (
        _EXECUTOR
        if use_shared
        else ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="candle-fetch-tmp",
        )
    )

    try:
        futures = [
            executor.submit(_fetch_one, i, s, e) for i, (s, e) in enumerate(batches)
        ]

        for fut in as_completed(futures):
            # No try/except here on purpose — we want Upstox errors
            # to bubble up and cancel the whole request.
            i, b_start, b_end, candles = fut.result()
            results[i] = candles
            logger.info(
                "Historical batch loaded | instrument=%s | %s -> %s | candles=%s",
                instrument_key,
                b_start,
                b_end,
                len(candles),
            )
    finally:
        if not use_shared:
            executor.shutdown(wait=False, cancel_futures=True)

    all_candles: list = []
    for chunk in results:
        if chunk:
            all_candles.extend(chunk)
    return all_candles


# ============================================================
# PUBLIC — INTRADAY
# ============================================================
def fetch_intraday_candles(
    instrument_key: str,
    interval: str = DEFAULT_INTERVAL,
    unit: str = DEFAULT_UNIT,
) -> list:
    """
    Fetch today's intraday candles. Raises on any error.
    """
    api = _build_history_client()
    logger.info(
        "Intraday candles request | instrument=%s | interval=%s unit=%s",
        instrument_key,
        interval,
        unit,
    )
    response = api.get_intra_day_candle_data(instrument_key, interval, unit)
    return _extract_candles(response)


# ============================================================
# MERGE / DEDUPE
# ============================================================
def merge_candles(*candle_lists: Optional[list]) -> list:
    """
    Merge multiple candle lists by timestamp (later lists win on
    collision), then sort chronologically by timestamp.
    """
    by_time: dict = {}
    for candles in candle_lists:
        if not candles:
            continue
        for candle in candles:
            if not candle:
                continue
            try:
                ts = candle[0]
            except (IndexError, TypeError):
                continue
            by_time[ts] = candle
    return sorted(by_time.values(), key=lambda c: c[0])


# ============================================================
# FRONTEND SHAPE
# ============================================================
def convert_to_chart_data(candles: Optional[list]) -> list[dict]:
    """
    Convert raw Upstox candles [ts, o, h, l, c, v, oi] into a
    frontend-friendly list of dicts.
    """
    if not candles:
        return []

    out: list[dict] = []
    for candle in candles:
        try:
            ts = candle[0]
            o = float(candle[1])
            h = float(candle[2])
            l = float(candle[3])
            c = float(candle[4])
            v = float(candle[5]) if len(candle) > 5 else 0.0
            out.append(
                {
                    "time": ts,
                    "open": o,
                    "high": h,
                    "low": l,
                    "close": c,
                    "volume": v,
                }
            )
        except Exception as exc:
            logger.error("Failed to parse candle %s: %s", candle, exc)
    return out
