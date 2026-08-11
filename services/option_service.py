import json
from datetime import datetime, date
from pathlib import Path
from threading import Lock

import upstox_client
from upstox_client.rest import ApiException

from core import config
from core.logger import get_logger
from services.token_service import token_service

# File-specific logger (logs to logs/option_service.log)
logger = get_logger(__file__)

# ============================================================
# Thread-Safe Runtime Cache
# ============================================================

_cache_lock = Lock()

options_cache = {
    "nearest_expiry": None,
    "total_contracts": 0,
    "subscribed_keys": [],
    "data": [],
}

# ============================================================
# Feed Constants
# ============================================================

NIFTY_INDEX_FEED = {
    "instrument_key": getattr(config, "MAIN_NIFTY_SECURITY", "NSE_INDEX|Nifty 50"),
    "instrument_type": "INDEX",
    "strike_price": None,
    "expiry": None,
    "trading_symbol": "NIFTY 50",
    "underlying_type": "INDEX",
    "underlying_symbol": "NIFTY 50",
}

# Nifty index is only for live tick websocket feed.
NIFTY_SUPPORTED_INTERVALS = [0]  # Live Tick only

# Option contracts support live ticks and candle intervals.
OPTION_SUPPORTED_INTERVALS = [
    0,  # Live Tick
    1,  # 1 Minute
    3,  # 3 Minute
    5,  # 5 Minute
]

SUPPORTED_INTERVALS = OPTION_SUPPORTED_INTERVALS


# ============================================================
# Basic Helpers
# ============================================================

def safe_float(value, default: float = 0.0) -> float:
    """Safely converts value to float."""

    try:
        if value is None:
            return default

        return float(value)

    except Exception:
        return default


def parse_expiry_date(expiry_val) -> date | None:
    """Safely convert expiry value to date object."""

    if not expiry_val:
        return None

    if isinstance(expiry_val, datetime):
        return expiry_val.date()

    if isinstance(expiry_val, date):
        return expiry_val

    if isinstance(expiry_val, str):
        try:
            date_part = expiry_val.split()[0]
            return datetime.strptime(date_part, "%Y-%m-%d").date()

        except ValueError as ex:
            logger.warning(f"Failed to parse date string '{expiry_val}': {ex}")
            return None

    return None


def clean_contract_data(item: dict) -> dict:
    """Extract and standardize required contract fields."""

    exp_date = parse_expiry_date(item.get("expiry"))
    expiry_str = exp_date.strftime("%Y-%m-%d") if exp_date else str(item.get("expiry"))

    return {
        "instrument_key": item.get("instrument_key"),
        "instrument_type": item.get("instrument_type"),
        "strike_price": item.get("strike_price"),
        "expiry": expiry_str,
        "trading_symbol": item.get("trading_symbol"),
        "underlying_type": item.get("underlying_type"),
        "underlying_symbol": item.get("underlying_symbol"),
    }


def get_nearest_expiry_contracts(contracts: list) -> tuple[str | None, list]:
    """
    Find nearest expiry contracts and filter by configured strike range
    for both CE and PE.

    Example:
        STRIKE_FROM <= strike_price <= STRIKE_TO
    """

    if not contracts:
        return None, []

    today = datetime.now().date()
    valid_expiries = set()

    for item in contracts:
        exp_date = parse_expiry_date(item.get("expiry"))

        if exp_date and exp_date >= today:
            valid_expiries.add(exp_date)

    if not valid_expiries:
        logger.warning("No valid future expiry dates found.")
        return None, []

    nearest_date = min(valid_expiries)
    nearest_date_str = nearest_date.strftime("%Y-%m-%d")

    matching_contracts = [
        clean_contract_data(item)
        for item in contracts
        if parse_expiry_date(item.get("expiry")) == nearest_date
    ]

    # Filter strike range if defined in core/config.py
    if hasattr(config, "STRIKE_FROM") and hasattr(config, "STRIKE_TO"):
        try:
            strike_from = float(config.STRIKE_FROM)
            strike_to = float(config.STRIKE_TO)

            matching_contracts = [
                item
                for item in matching_contracts
                if item.get("strike_price") is not None
                and strike_from <= float(item["strike_price"]) <= strike_to
            ]

        except (ValueError, TypeError) as ex:
            logger.error(f"Error filtering strike range from config: {ex}")

    return nearest_date_str, matching_contracts


# ============================================================
# Thread-Safe Cache Helpers
# ============================================================

def get_subscribed_instrument_keys() -> list:
    """
    Returns subscribed instrument keys from options_cache safely.

    Includes:
        1. Main NIFTY index instrument key
        2. Filtered option instrument keys
    """

    with _cache_lock:
        return list(options_cache.get("subscribed_keys", []))


def get_cached_option_contracts() -> list:
    """
    Returns cached option contract data safely.

    This does not include the NIFTY index feed.
    """

    with _cache_lock:
        return list(options_cache.get("data", []))


def get_all_cached_instruments() -> list:
    """
    Returns all cached instruments.

    Includes:
        1. NIFTY index feed
        2. Cached option contracts
    """

    with _cache_lock:
        cached_data = list(options_cache.get("data", []))

    return [
        NIFTY_INDEX_FEED,
        *cached_data,
    ]


def get_contract_info_by_instrument_key(instrument_key: str) -> dict | None:
    """
    Returns basic metadata for an instrument key.

    Used by:
        - Historical candle service
        - Debug/status helpers
        - Any future candle preload logic
    """

    main_key = getattr(config, "MAIN_NIFTY_SECURITY", "NSE_INDEX|Nifty 50")

    if instrument_key == main_key:
        return NIFTY_INDEX_FEED.copy()

    with _cache_lock:
        cached_data = list(options_cache.get("data", []))

    for item in cached_data:
        if item.get("instrument_key") == instrument_key:
            return item.copy()

    return None


# ============================================================
# Strategy / Option Lookup Helpers
# ============================================================

def get_option_contracts_by_type(option_type: str) -> list:
    """
    Returns cached option contracts filtered by CE or PE.

    Example:
        get_option_contracts_by_type("CE")
    """

    option_type = str(option_type or "").upper()

    if option_type not in ["CE", "PE"]:
        return []

    with _cache_lock:
        cached_data = list(options_cache.get("data", []))

    return [
        item.copy()
        for item in cached_data
        if str(item.get("instrument_type", "")).upper() == option_type
    ]


def get_option_contracts_by_strike_window(
    strike_from: float,
    strike_to: float,
    option_type: str | None = None,
) -> list:
    """
    Returns cached option contracts within a strike window.

    Args:
        strike_from:
            Lower strike boundary.

        strike_to:
            Upper strike boundary.

        option_type:
            Optional CE or PE filter.

    Example:
        get_option_contracts_by_strike_window(24000, 25000)
        get_option_contracts_by_strike_window(24000, 25000, "CE")
    """

    try:
        strike_from = float(strike_from)
        strike_to = float(strike_to)

    except Exception:
        logger.error(
            f"Invalid strike window. strike_from={strike_from}, strike_to={strike_to}"
        )
        return []

    if strike_from > strike_to:
        strike_from, strike_to = strike_to, strike_from

    selected_option_type = str(option_type or "").upper()

    with _cache_lock:
        cached_data = list(options_cache.get("data", []))

    output = []

    for item in cached_data:
        try:
            strike = safe_float(item.get("strike_price"))
            item_type = str(item.get("instrument_type", "")).upper()

            if item_type not in ["CE", "PE"]:
                continue

            if selected_option_type in ["CE", "PE"] and item_type != selected_option_type:
                continue

            if strike_from <= strike <= strike_to:
                output.append(item.copy())

        except Exception:
            continue

    output.sort(
        key=lambda item: (
            safe_float(item.get("strike_price")),
            str(item.get("instrument_type", "")),
        )
    )

    return output


def get_option_contract_by_strike_and_type(
    strike_price: float,
    option_type: str,
) -> dict | None:
    """
    Returns one cached option contract by exact strike and CE/PE type.

    Example:
        get_option_contract_by_strike_and_type(24500, "CE")
    """

    option_type = str(option_type or "").upper()

    if option_type not in ["CE", "PE"]:
        return None

    target_strike = safe_float(strike_price)

    with _cache_lock:
        cached_data = list(options_cache.get("data", []))

    for item in cached_data:
        try:
            item_strike = safe_float(item.get("strike_price"))
            item_type = str(item.get("instrument_type", "")).upper()

            if item_strike == target_strike and item_type == option_type:
                return item.copy()

        except Exception:
            continue

    return None


def get_nearest_option_contracts_by_spot(
    spot: float,
    option_type: str,
    count: int = 3,
    mode: str = "nearest_around_spot",
    strike_from: float | None = None,
    strike_to: float | None = None,
) -> list:
    """
    Returns nearest option contracts based on current NIFTY spot.

    Args:
        spot:
            Current NIFTY spot.

        option_type:
            CE or PE.

        count:
            Number of contracts to return.

        mode:
            nearest_around_spot:
                Sort by absolute distance from spot.

            equal_or_below:
                Return strikes <= spot, nearest first.
                Example:
                    spot=24648
                    returns 24600, 24550, 24500

            equal_or_above:
                Return strikes >= spot, nearest first.
                Example:
                    spot=24648
                    returns 24650, 24700, 24750

        strike_from:
            Optional lower strike boundary.

        strike_to:
            Optional upper strike boundary.

    Returns:
        List of contract dictionaries with an added distance_from_spot field.
    """

    spot = safe_float(spot)

    if spot <= 0:
        return []

    option_type = str(option_type or "").upper()

    if option_type not in ["CE", "PE"]:
        return []

    try:
        count = max(1, int(count or 3))
    except Exception:
        count = 3

    mode = str(mode or "nearest_around_spot").lower()

    with _cache_lock:
        cached_data = list(options_cache.get("data", []))

    candidates = []

    for item in cached_data:
        try:
            item_type = str(item.get("instrument_type", "")).upper()

            if item_type != option_type:
                continue

            strike = safe_float(item.get("strike_price"))

            if strike <= 0:
                continue

            if strike_from is not None and strike < safe_float(strike_from):
                continue

            if strike_to is not None and strike > safe_float(strike_to):
                continue

            item_copy = item.copy()
            item_copy["distance_from_spot"] = round(abs(strike - spot), 4)

            candidates.append(item_copy)

        except Exception:
            continue

    if mode == "equal_or_below":
        filtered = [
            item
            for item in candidates
            if safe_float(item.get("strike_price")) <= spot
        ]

        filtered.sort(
            key=lambda item: safe_float(item.get("strike_price")),
            reverse=True,
        )

    elif mode == "equal_or_above":
        filtered = [
            item
            for item in candidates
            if safe_float(item.get("strike_price")) >= spot
        ]

        filtered.sort(
            key=lambda item: safe_float(item.get("strike_price")),
        )

    else:
        filtered = candidates
        filtered.sort(
            key=lambda item: (
                safe_float(item.get("distance_from_spot")),
                safe_float(item.get("strike_price")),
            )
        )

    return filtered[:count]


def get_strategy_eligible_option_contracts(
    opening_range_average: float,
    window_points: float = 500,
    option_type: str | None = None,
) -> dict:
    """
    Builds strategy eligible option list based on Opening Range average.

    Formula:
        raw_from = opening_range_average - window_points
        raw_to = opening_range_average + window_points

        final_from = max(STRIKE_FROM, raw_from)
        final_to = min(STRIKE_TO, raw_to)

    Example:
        Opening Range average = 24560
        window = 500
        raw = 24060 to 25060

        STRIKE_TO = 25000
        final = 24060 to 25000
    """

    avg = safe_float(opening_range_average)
    window_points = safe_float(window_points, default=500)

    if avg <= 0:
        return {
            "status": "failed",
            "message": "Invalid opening_range_average.",
            "opening_range_average": opening_range_average,
            "window_points": window_points,
            "strike_from": None,
            "strike_to": None,
            "contracts_count": 0,
            "contracts": [],
        }

    raw_from = avg - window_points
    raw_to = avg + window_points

    strike_from = max(
        safe_float(getattr(config, "STRIKE_FROM", raw_from)),
        raw_from,
    )

    strike_to = min(
        safe_float(getattr(config, "STRIKE_TO", raw_to)),
        raw_to,
    )

    contracts = get_option_contracts_by_strike_window(
        strike_from=strike_from,
        strike_to=strike_to,
        option_type=option_type,
    )

    return {
        "status": "success",
        "message": "Strategy eligible option contracts resolved.",
        "opening_range_average": round(avg, 4),
        "window_points": round(window_points, 4),
        "raw_strike_from": round(raw_from, 4),
        "raw_strike_to": round(raw_to, 4),
        "strike_from": round(strike_from, 4),
        "strike_to": round(strike_to, 4),
        "option_type": str(option_type).upper() if option_type else None,
        "contracts_count": len(contracts),
        "contracts": contracts,
    }


# ============================================================
# Feed Discovery Helpers
# ============================================================

def get_available_feeds() -> list:
    """Returns all feeds available to client websocket consumers."""

    feeds = [
        {
            **NIFTY_INDEX_FEED,
            "supported_intervals": NIFTY_SUPPORTED_INTERVALS,
        }
    ]

    with _cache_lock:
        cached_data = list(options_cache.get("data", []))

    for item in cached_data:
        feeds.append(
            {
                **item,
                "supported_intervals": OPTION_SUPPORTED_INTERVALS,
            }
        )

    return feeds


def get_feed_by_instrument_key(instrument_key: str) -> dict | None:
    """Returns feed metadata by instrument key."""

    main_key = getattr(config, "MAIN_NIFTY_SECURITY", "NSE_INDEX|Nifty 50")

    if instrument_key == main_key:
        return {
            **NIFTY_INDEX_FEED,
            "supported_intervals": NIFTY_SUPPORTED_INTERVALS,
        }

    with _cache_lock:
        cached_data = list(options_cache.get("data", []))

    for item in cached_data:
        if item.get("instrument_key") == instrument_key:
            return {
                **item,
                "supported_intervals": OPTION_SUPPORTED_INTERVALS,
            }

    return None


def is_nifty_index_feed(instrument_key: str) -> bool:
    """Returns True if the given instrument key is Nifty Index."""

    main_key = getattr(config, "MAIN_NIFTY_SECURITY", "NSE_INDEX|Nifty 50")
    return instrument_key == main_key


def is_valid_feed_interval(instrument_key: str, interval: int) -> bool:
    """Validates whether an interval is allowed for an instrument."""

    if is_nifty_index_feed(instrument_key):
        return interval in NIFTY_SUPPORTED_INTERVALS

    return interval in OPTION_SUPPORTED_INTERVALS


# ============================================================
# Upstox Options Fetch
# ============================================================

def get_options_contracts(
    instrument_key: str = None,
    expiry_date: str = None,
    output_filename: str = "data/nearest_nifty_option_contracts.json",
    filter_nearest: bool = True,
    save_data: bool = False,
) -> dict | None:
    """
    Fetches option contracts for an instrument, filters nearest expiry and strike range,
    updates the in-memory cache with contracts and subscription keys, and optionally
    exports to JSON.
    """

    if not instrument_key:
        instrument_key = getattr(config, "MAIN_NIFTY_SECURITY", "NSE_INDEX|Nifty 50")

    access_token = token_service.get_access_token()

    if not access_token:
        logger.error("Failed to retrieve access token from token_service.")
        return None

    configuration = upstox_client.Configuration()
    configuration.access_token = access_token

    api_client = upstox_client.ApiClient(configuration)
    api_instance = upstox_client.OptionsApi(api_client)

    try:
        kwargs = {}

        if expiry_date:
            kwargs["expiry_date"] = expiry_date

        api_response = api_instance.get_option_contracts(
            instrument_key,
            **kwargs,
        )

        response_dict = (
            api_response.to_dict()
            if hasattr(api_response, "to_dict")
            else api_response
        )

        all_contracts = response_dict.get("data", [])

        if filter_nearest and not expiry_date:
            nearest_expiry, filtered_contracts = get_nearest_expiry_contracts(
                all_contracts
            )

            logger.info(
                f"Nearest Expiry Identified: {nearest_expiry} "
                f"({len(filtered_contracts)} contracts between "
                f"{getattr(config, 'STRIKE_FROM', 'N/A')} and "
                f"{getattr(config, 'STRIKE_TO', 'N/A')})"
            )

            final_output = {
                "status": response_dict.get("status", "success"),
                "nearest_expiry": nearest_expiry,
                "total_contracts": len(filtered_contracts),
                "data": filtered_contracts,
            }

        else:
            cleaned_all = [clean_contract_data(c) for c in all_contracts]

            final_output = {
                "status": response_dict.get("status", "success"),
                "nearest_expiry": expiry_date,
                "total_contracts": len(cleaned_all),
                "data": cleaned_all,
            }

        # Extract keys to subscribe: Index Key + Filtered Option Keys
        option_keys = [
            item["instrument_key"]
            for item in final_output.get("data", [])
            if item.get("instrument_key")
        ]

        keys_to_subscribe = list(dict.fromkeys([instrument_key] + option_keys))

        # Update cache safely with lock
        with _cache_lock:
            options_cache["nearest_expiry"] = final_output.get("nearest_expiry")
            options_cache["total_contracts"] = final_output.get("total_contracts", 0)
            options_cache["subscribed_keys"] = keys_to_subscribe
            options_cache["data"] = final_output.get("data", [])

        logger.info(
            f"Updated global options cache. "
            f"Total keys to subscribe: {len(keys_to_subscribe)}"
        )

        if save_data:
            output_path = Path(output_filename)
            output_path.parent.mkdir(parents=True, exist_ok=True)

            with open(output_path, "w", encoding="utf-8") as file:
                json.dump(final_output, file, indent=4, default=str)

            logger.info(f"Saved options response to {output_path.resolve()}")

        return final_output

    except ApiException as ex:
        logger.error(
            "Exception when calling OptionsApi->get_option_contracts: %s",
            getattr(ex, "body", str(ex)),
        )
        return None

    except Exception as ex:
        logger.error(
            f"Unexpected error while fetching option contracts: "
            f"{type(ex).__name__}: {ex}"
        )
        return None


# # Manual local test execution keep this comments for testing purpose
# if __name__ == "__main__":
#     logger.info("Executing option contracts fetch & filtering...")
#     result = get_options_contracts(
#         instrument_key="NSE_INDEX|Nifty 50",
#         output_filename="data/nearest_nifty_option_contracts.json",
#         filter_nearest=True,
#         save_data=True,
#     )
#
#     if result:
#         print("\n--- Processed Options Contracts ---")
#         print(f"Target Expiry Date: {result.get('nearest_expiry')}")
#         print(f"Total Contracts: {result.get('total_contracts')}")
#         if result.get("data"):
#             print("Sample Contract Format:")
#             print(json.dumps(result["data"][0], indent=4))