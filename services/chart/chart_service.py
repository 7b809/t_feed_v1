from datetime import date, timedelta
import logging

import upstox_client

from services.token_service import token_service


logger = logging.getLogger(__name__)


# ============================================================
# DEFAULT CONFIGURATION
# ============================================================

DEFAULT_INTERVAL = "minutes"
DEFAULT_UNIT = "1"

# Initial amount of historical data to load.
DEFAULT_HISTORY_DAYS = 30

# Historical data is fetched in batches.
HISTORY_BATCH_DAYS = 7


# ============================================================
# UPSTOX HISTORY CLIENT
# ============================================================

def _create_history_client():
    """
    Create and return the Upstox History V3 API client.
    """

    access_token = token_service.get_access_token()

    if not access_token:
        raise ValueError(
            "Upstox access token not available"
        )

    configuration = upstox_client.Configuration()

    configuration.access_token = access_token

    api_client = upstox_client.ApiClient(
        configuration
    )

    return upstox_client.HistoryV3Api(
        api_client
    )


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
        "Fetching historical candles: "
        "%s -> %s | %s | %s",
        from_date,
        to_date,
        interval,
        unit,
    )

    response = api.get_historical_candle_data1(
        instrument_key,
        interval,
        unit,
        str(to_date),
        str(from_date),
    )

    data = getattr(
        response,
        "data",
        None,
    )

    if not data:
        return []

    candles = getattr(
        data,
        "candles",
        [],
    )

    return candles or []


# ============================================================
# BATCH HISTORICAL FETCH
# ============================================================

def fetch_historical_candles(
    instrument_key: str,
    interval: str = DEFAULT_INTERVAL,
    unit: str = DEFAULT_UNIT,
    days: int = DEFAULT_HISTORY_DAYS,
    batch_days: int = HISTORY_BATCH_DAYS,
    end_date: date | None = None,
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

    end_date controls where the historical range ends.

    This is important for chart scrolling because the frontend
    can request an older range by supplying an older end_date.
    """

    if days <= 0:
        return []

    if batch_days <= 0:
        raise ValueError(
            "batch_days must be greater than zero"
        )

    if end_date is None:
        # Do not request today's historical endpoint as the
        # intraday endpoint handles today's live session.
        end_date = date.today() - timedelta(days=1)

    start_date = (
        end_date
        - timedelta(days=days)
        + timedelta(days=1)
    )

    all_candles = []

    current_end = end_date

    while current_end >= start_date:

        current_start = max(
            start_date,
            current_end
            - timedelta(days=batch_days)
            + timedelta(days=1),
        )

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
                    "Historical batch loaded for %s: "
                    "%s -> %s | candles=%s",
                    instrument_key,
                    current_start,
                    current_end,
                    len(candles),
                )

            else:
                logger.info(
                    "No historical candles for %s: "
                    "%s -> %s",
                    instrument_key,
                    current_start,
                    current_end,
                )

        except Exception as exc:
            logger.exception(
                "Historical batch fetch failed for %s: "
                "%s -> %s: %s",
                instrument_key,
                current_start,
                current_end,
                exc,
            )

        # Move backwards.
        current_end = (
            current_start
            - timedelta(days=1)
        )

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

    logger.info(
        "Fetching intraday candles for %s",
        instrument_key,
    )

    response = api.get_intra_day_candle_data(
        instrument_key,
        interval,
        unit,
    )

    data = getattr(
        response,
        "data",
        None,
    )

    if not data:
        return []

    return getattr(
        data,
        "candles",
        [],
    ) or []


# ============================================================
# MERGE / DEDUPLICATE
# ============================================================

def merge_candles(
    historical_candles,
    intraday_candles,
):
    """
    Merge historical and intraday candles.

    Duplicate timestamps are removed.

    Final result is sorted chronologically.
    """

    merged = []

    seen = set()

    for candle in (
        historical_candles
        + intraday_candles
    ):

        if not candle:
            continue

        try:
            timestamp = candle[0]

        except (IndexError, TypeError):
            continue

        if timestamp in seen:
            continue

        seen.add(timestamp)

        merged.append(candle)

    merged.sort(
        key=lambda x: x[0]
    )

    return merged


# ============================================================
# CONVERT TO FRONTEND CHART DATA
# ============================================================

def convert_to_chart_data(
    candles,
):
    """
    Convert raw Upstox candles into a frontend-friendly
    chart representation.
    """

    chart_data = []

    for candle in candles:

        try:
            chart_data.append(
                {
                    "time": candle[0],

                    "open": float(
                        candle[1]
                    ),

                    "high": float(
                        candle[2]
                    ),

                    "low": float(
                        candle[3]
                    ),

                    "close": float(
                        candle[4]
                    ),

                    "volume": (
                        float(candle[5])
                        if len(candle) > 5
                        else 0
                    ),
                }
            )

        except Exception as exc:
            logger.error(
                "Failed to parse candle %s: %s",
                candle,
                exc,
            )

    return chart_data


# ============================================================
# GET INITIAL CHART DATA
# ============================================================

def get_chart_data(
    instrument_key: str,
    interval: str = DEFAULT_INTERVAL,
    unit: str = DEFAULT_UNIT,
    days: int = DEFAULT_HISTORY_DAYS,
    batch_days: int = HISTORY_BATCH_DAYS,
):
    """
    Get the initial chart dataset.

    Default:

        30 days historical
        fetched in 7-day batches
        + today's intraday candles
    """

    historical_candles = []

    intraday_candles = []

    # --------------------------------------------------------
    # Historical
    # --------------------------------------------------------

    try:
        historical_candles = fetch_historical_candles(
            instrument_key=instrument_key,
            interval=interval,
            unit=unit,
            days=days,
            batch_days=batch_days,
        )

    except Exception as exc:
        logger.exception(
            "Historical candle fetch failed for %s: %s",
            instrument_key,
            exc,
        )

    # --------------------------------------------------------
    # Today's intraday
    # --------------------------------------------------------

    try:
        intraday_candles = fetch_intraday_candles(
            instrument_key=instrument_key,
            interval=interval,
            unit=unit,
        )

    except Exception as exc:
        logger.exception(
            "Intraday candle fetch failed for %s: %s",
            instrument_key,
            exc,
        )

    # --------------------------------------------------------
    # Merge
    # --------------------------------------------------------

    merged_candles = merge_candles(
        historical_candles,
        intraday_candles,
    )

    # --------------------------------------------------------
    # Convert
    # --------------------------------------------------------

    chart_data = convert_to_chart_data(
        merged_candles
    )

    return {
        "instrument_key": instrument_key,

        "total_candles": len(
            chart_data
        ),

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

    # Do not include before_date itself because it is already
    # expected to exist in the currently loaded dataset.
    end_date = (
        before_date
        - timedelta(days=1)
    )

    historical_candles = fetch_historical_candles(
        instrument_key=instrument_key,
        interval=interval,
        unit=unit,
        days=days,
        batch_days=batch_days,
        end_date=end_date,
    )

    chart_data = convert_to_chart_data(
        merge_candles(
            historical_candles,
            [],
        )
    )

    return {
        "instrument_key": instrument_key,

        "candles": chart_data,

        "total_candles": len(
            chart_data
        ),

        "history_days": days,

        "batch_days": batch_days,

        "has_more": bool(
            chart_data
        ),
    }