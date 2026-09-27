from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
import logging
import os

import upstox_client

from services.token_service import token_service

logger = logging.getLogger(__name__)

# ============================================================
# DEFAULT CONFIGURATION
# ============================================================
DEFAULT_INTERVAL = "minutes"
DEFAULT_UNIT = "1"
DEFAULT_HISTORY_DAYS = 30
HISTORY_BATCH_DAYS = 7

# ------------------------------------------------------------
# PARALLEL FETCH CONFIG
# ------------------------------------------------------------
# Max concurrent Upstox API requests during batch fetching.
# Upstox rate limit is ~25 req/sec per token; keeping this at
# 8 (or lower) is safe and still gives a big speedup.
MAX_BATCH_WORKERS = int(os.getenv("CHART_MAX_BATCH_WORKERS", "8"))

# Shared thread pool — reused across calls so we don't pay the
# cost of spinning up a new executor per request.
_EXECUTOR = ThreadPoolExecutor(
    max_workers=MAX_BATCH_WORKERS,
    thread_name_prefix="chart-fetch",
)


# ============================================================
# UPSTOX HISTORY CLIENT
# ============================================================
def _create_history_client():
    """
    Create and return the Upstox History V3 API client.

    NOTE: Called inside each worker thread, so the underlying
    ApiClient is not shared across threads.
    """
    access_token = token_service.get_access_token()
    if not access_token:
        raise ValueError("Upstox access token not available")
    configuration = upstox_client.Configuration()
    configuration.access_token = access_token
    api_client = upstox_client.ApiClient(configuration)
    return upstox_client.HistoryV3Api(api_client)


# ============================================================
# SINGLE HISTORICAL RANGE
# ============================================================
def fetch_historical_candles_range(
    instrument_key: str,
    from_date: date,
    to_date: date,
    interval: str = DEFAULT_INTERVAL,
    unit: str = DEFAULT_UNIT,
):
    """
    Fetch historical candles for one date range.

    Parameters
    ----------
    instrument_key:
        Upstox instrument key.
    from_date:
        Beginning of requested historical period.
    to_date:
        End of requested historical period.
    interval:
        Upstox interval type.
    unit:
        Candle interval unit.

    Returns
    -------
    list
        Raw Upstox candles.
    """
    api = _create_history_client()
    logger.info(
        "Fetching historical candles: %s -> %s | %s | %s",
        from_date, to_date, interval, unit,
    )
    response = api.get_historical_candle_data1(
        instrument_key, interval, unit, str(to_date), str(from_date)
    )
    data = getattr(response, "data", None)
    if not data:
        return []
    candles = getattr(data, "candles", [])
    return candles or []


# ============================================================
# BATCH RANGE BUILDER  (pure, no I/O)
# ============================================================
def _build_batch_ranges(
    start_date: date,
    end_date: date,
    batch_days: int,
):
    """
    Split [start_date, end_date] into (from_date, to_date) tuples,
    each spanning at most `batch_days` days.

    Returned newest-first, matching the original loop order.
    """
    if batch_days <= 0:
        raise ValueError("batch_days must be greater than zero")

    ranges = []
    current_end = end_date
    while current_end >= start_date:
        current_start = max(
            start_date,
            current_end - timedelta(days=batch_days) + timedelta(days=1),
        )
        ranges.append((current_start, current_end))
        current_end = current_start - timedelta(days=1)
    return ranges


# ============================================================
# BATCH HISTORICAL FETCH  (PARALLEL)
# ============================================================
def fetch_historical_candles(
    instrument_key: str,
    interval: str = DEFAULT_INTERVAL,
    unit: str = DEFAULT_UNIT,
    days: int = DEFAULT_HISTORY_DAYS,
    batch_days: int = HISTORY_BATCH_DAYS,
    end_date: date | None = None,
    parallel: bool = True,
    max_workers: int | None = None,
):
    """
    Fetch historical candles in multiple batches.

    Example:
        days=30
        batch_days=7
    results in approximately:
        batch 1: 7 days
        batch 2: 7 days
        batch 3: 7 days
        batch 4: 7 days
        batch 5: 2 days

    When `parallel=True` (default), all batches are dispatched
    concurrently to a shared ThreadPoolExecutor, giving a large
    speedup for multi-batch ranges.

    end_date controls where the historical range ends.
    This is important for chart scrolling because the frontend
    can request an older range by supplying an older end_date.
    """
    if days <= 0:
        return []
    if batch_days <= 0:
        raise ValueError("batch_days must be greater than zero")
    if end_date is None:
        end_date = date.today() - timedelta(days=1)

    start_date = end_date - timedelta(days=days) + timedelta(days=1)
    ranges = _build_batch_ranges(start_date, end_date, batch_days)

    if not ranges:
        return []

    # ---- SEQUENTIAL FALLBACK (debug / throttled environments) ----
    if not parallel:
        all_candles: list = []
        for current_start, current_end in ranges:
            try:
                candles = fetch_historical_candles_range(
                    instrument_key=instrument_key,
                    from_date=current_start,
                    to_date=current_end,
                    interval=interval,
                    unit=unit,
                )
                if candles:
                    all_candles.extend(candles)
                    logger.info(
                        "Historical batch loaded for %s: %s -> %s | candles=%s",
                        instrument_key, current_start, current_end, len(candles),
                    )
                else:
                    logger.info(
                        "No historical candles for %s: %s -> %s",
                        instrument_key, current_start, current_end,
                    )
            except Exception as exc:
                logger.exception(
                    "Historical batch fetch failed for %s: %s -> %s: %s",
                    instrument_key, current_start, current_end, exc,
                )
        return all_candles

    # ---- PARALLEL PATH ----
    workers = max(1, int(max_workers or MAX_BATCH_WORKERS))
    logger.info(
        "Parallel historical fetch starting | instrument=%s | batches=%s | workers=%s | range=%s -> %s",
        instrument_key, len(ranges), workers, start_date, end_date,
    )

    # Results indexed by batch position so we can preserve order
    # even if the futures complete out of order.
    results: list[list] = [None] * len(ranges)  # type: ignore[list-item]

    def _fetch_one(idx: int, current_start: date, current_end: date):
        candles = fetch_historical_candles_range(
            instrument_key=instrument_key,
            from_date=current_start,
            to_date=current_end,
            interval=interval,
            unit=unit,
        )
        return idx, current_start, current_end, candles

    # Reuse the shared executor when the requested worker count
    # matches its size; otherwise spin up a temporary one.
    use_shared = (workers == _EXECUTOR._max_workers)  # type: ignore[attr-defined]
    executor = _EXECUTOR if use_shared else ThreadPoolExecutor(
        max_workers=workers,
        thread_name_prefix="chart-fetch-tmp",
    )

    try:
        futures = [
            executor.submit(_fetch_one, idx, s, e)
            for idx, (s, e) in enumerate(ranges)
        ]

        for fut in as_completed(futures):
            try:
                idx, current_start, current_end, candles = fut.result()
            except Exception as exc:
                # Should be rare — _fetch_one already returns via the SDK;
                # any exception here is unexpected.
                logger.exception(
                    "Parallel historical batch raised for %s: %s",
                    instrument_key, exc,
                )
                continue

            if candles:
                results[idx] = candles
                logger.info(
                    "Historical batch loaded for %s: %s -> %s | candles=%s",
                    instrument_key, current_start, current_end, len(candles),
                )
            else:
                results[idx] = []
                logger.info(
                    "No historical candles for %s: %s -> %s",
                    instrument_key, current_start, current_end,
                )
    finally:
        if not use_shared:
            executor.shutdown(wait=False, cancel_futures=True)

    # Flatten preserving batch order (which is newest-first).
    all_candles: list = []
    for batch in results:
        if batch:
            all_candles.extend(batch)
    return all_candles


# ============================================================
# INTRADAY CANDLES
# ============================================================
def fetch_intraday_candles(
    instrument_key: str,
    interval: str = DEFAULT_INTERVAL,
    unit: str = DEFAULT_UNIT,
):
    """
    Fetch today's intraday candles.
    """
    api = _create_history_client()
    logger.info("Fetching intraday candles for %s", instrument_key)
    response = api.get_intra_day_candle_data(instrument_key, interval, unit)
    data = getattr(response, "data", None)
    if not data:
        return []
    return getattr(data, "candles", []) or []


# ============================================================
# MERGE / DEDUPLICATE  (optimized)
# ============================================================
def merge_candles(historical_candles, intraday_candles):
    """
    Merge historical and intraday candles.
    Duplicate timestamps are removed.
    Final result is sorted chronologically.

    Uses a dict keyed by timestamp for O(n) dedup instead of
    set + list. Dict preserves insertion order in Py3.7+, but
    we sort at the end anyway to be safe.
    """
    by_time: dict = {}

    # Intraday last so it wins on collision (fresher data).
    for candle in historical_candles or []:
        if not candle:
            continue
        try:
            timestamp = candle[0]
        except (IndexError, TypeError):
            continue
        by_time[timestamp] = candle

    for candle in intraday_candles or []:
        if not candle:
            continue
        try:
            timestamp = candle[0]
        except (IndexError, TypeError):
            continue
        by_time[timestamp] = candle

    return sorted(by_time.values(), key=lambda x: x[0])


# ============================================================
# CONVERT TO FRONTEND CHART DATA  (optimized)
# ============================================================
def convert_to_chart_data(candles):
    """
    Convert raw Upstox candles into a frontend-friendly chart
    representation.

    Fast-path for the common 6-element candle shape; falls back
    to a defensive path for anything malformed.
    """
    if not candles:
        return []

    out = []
    append = out.append
    logger_error = logger.error

    for candle in candles:
        try:
            # Fast path — most Upstox candles are:
            # [timestamp, open, high, low, close, volume, oi]
            timestamp = candle[0]
            open_ = float(candle[1])
            high = float(candle[2])
            low = float(candle[3])
            close = float(candle[4])

            if len(candle) > 5:
                volume = float(candle[5])
            else:
                volume = 0.0

            append({
                "time": timestamp,
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
            })
        except Exception as exc:
            logger_error("Failed to parse candle %s: %s", candle, exc)

    return out


# ============================================================
# GET INITIAL CHART DATA  (historical + intraday in parallel)
# ============================================================
def get_chart_data(
    instrument_key: str,
    interval: str = DEFAULT_INTERVAL,
    unit: str = DEFAULT_UNIT,
    days: int = DEFAULT_HISTORY_DAYS,
    batch_days: int = HISTORY_BATCH_DAYS,
    parallel: bool = True,
    max_workers: int | None = None,
):
    """
    Get the initial chart dataset.
    Default:
        30 days historical
        fetched in 7-day batches
        + today's intraday candles

    Historical batch fetching and intraday fetching run
    concurrently when `parallel=True`, so total wall time is
    ~max(historical, intraday) instead of the sum.
    """
    historical_candles: list = []
    intraday_candles: list = []

    def _fetch_historical():
        try:
            return fetch_historical_candles(
                instrument_key=instrument_key,
                interval=interval,
                unit=unit,
                days=days,
                batch_days=batch_days,
                parallel=parallel,
                max_workers=max_workers,
            )
        except Exception as exc:
            logger.exception(
                "Historical candle fetch failed for %s: %s",
                instrument_key, exc,
            )
            return []

    def _fetch_intraday():
        try:
            return fetch_intraday_candles(
                instrument_key=instrument_key,
                interval=interval,
                unit=unit,
            )
        except Exception as exc:
            logger.exception(
                "Intraday candle fetch failed for %s: %s",
                instrument_key, exc,
            )
            return []

    if parallel:
        # Historical itself uses the shared executor internally.
        # Running the two tasks concurrently at the top level gives
        # one more layer of overlap for intraday vs. the last batch.
        with ThreadPoolExecutor(
            max_workers=2,
            thread_name_prefix="chart-initial",
        ) as top_executor:
            fut_hist = top_executor.submit(_fetch_historical)
            fut_intra = top_executor.submit(_fetch_intraday)

            for fut, label in (
                (fut_hist, "historical"),
                (fut_intra, "intraday"),
            ):
                try:
                    value = fut.result()
                except Exception as exc:
                    logger.exception(
                        "%s fetch raised at top level for %s: %s",
                        label, instrument_key, exc,
                    )
                    value = []

                if label == "historical":
                    historical_candles = value or []
                else:
                    intraday_candles = value or []
    else:
        historical_candles = _fetch_historical()
        intraday_candles = _fetch_intraday()

    merged_candles = merge_candles(historical_candles, intraday_candles)
    chart_data = convert_to_chart_data(merged_candles)

    return {
        "instrument_key": instrument_key,
        "total_candles": len(chart_data),
        "candles": chart_data,
        "history_days": days,
        "batch_days": batch_days,
    }


# ============================================================
# LOAD OLDER CHART DATA
# ============================================================
def get_older_chart_data(
    instrument_key: str,
    before_date: date,
    days: int = DEFAULT_HISTORY_DAYS,
    batch_days: int = HISTORY_BATCH_DAYS,
    interval: str = DEFAULT_INTERVAL,
    unit: str = DEFAULT_UNIT,
    parallel: bool = True,
    max_workers: int | None = None,
):
    """
    Load an older historical section.
    The frontend calls this when the user scrolls toward
    the oldest currently loaded candle.

    Example:
        Current chart:
            2026-08-05 -> 2026-09-03
        User scrolls left.
        Frontend sends:
            before_date=2026-08-05
        Backend returns approximately:
            2026-07-06 -> 2026-08-04
    """
    if days <= 0:
        return {
            "instrument_key": instrument_key,
            "candles": [],
            "total_candles": 0,
            "has_more": False,
        }

    end_date = before_date - timedelta(days=1)

    historical_candles = fetch_historical_candles(
        instrument_key=instrument_key,
        interval=interval,
        unit=unit,
        days=days,
        batch_days=batch_days,
        end_date=end_date,
        parallel=parallel,
        max_workers=max_workers,
    )

    chart_data = convert_to_chart_data(merge_candles(historical_candles, []))

    return {
        "instrument_key": instrument_key,
        "candles": chart_data,
        "total_candles": len(chart_data),
        "history_days": days,
        "batch_days": batch_days,
        "has_more": bool(chart_data),
    }