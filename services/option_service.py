import json
from datetime import datetime, date
from pathlib import Path
from threading import Lock

import upstox_client
from upstox_client.rest import ApiException

from core import config
from core.logger import get_logger
from services.token_service import token_service

# File-specific logger logs to logs/option_service.log
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
    # Fast lookup indexes rebuilt whenever option contracts are refreshed.
    "contracts_by_key": {},
    "contracts_by_strike_type": {},
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
NIFTY_SUPPORTED_INTERVALS = [0]

# Option contracts support live ticks and candle intervals.
OPTION_SUPPORTED_INTERVALS = [
    0,
    1,
    3,
    5,
]

SUPPORTED_INTERVALS = OPTION_SUPPORTED_INTERVALS


# ============================================================
# Basic Helpers
# ============================================================


def safe_float(value, default: float = 0.0) -> float:
    """Safely converts a value to float."""

    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def safe_int(value, default: int = 0) -> int:
    """Safely converts a value to int."""

    try:
        if value is None:
            return default
        return int(value)
    except Exception:
        return default


def normalize_option_type(option_type: str | None) -> str | None:
    """Normalizes option type to CE or PE."""

    if not option_type:
        return None

    value = str(option_type).strip().upper()

    if value in ["CE", "CALL"]:
        return "CE"

    if value in ["PE", "PUT"]:
        return "PE"

    return None


def get_opposite_option_type(option_type: str | None) -> str | None:
    """
    Returns the opposite option type.

    Examples:
        CE -> PE
        PE -> CE
    """

    normalized_type = normalize_option_type(option_type)

    if normalized_type == "CE":
        return "PE"

    if normalized_type == "PE":
        return "CE"

    return None


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

    instrument_type = normalize_option_type(item.get("instrument_type"))

    return {
        "instrument_key": item.get("instrument_key"),
        "instrument_type": instrument_type or item.get("instrument_type"),
        "strike_price": item.get("strike_price"),
        "expiry": expiry_str,
        "trading_symbol": item.get("trading_symbol"),
        "underlying_type": item.get("underlying_type"),
        "underlying_symbol": item.get("underlying_symbol"),
    }


def build_strike_type_key(strike_price, instrument_type: str) -> str | None:
    """
    Builds lookup key for strike plus CE/PE.

    Example:
        24500.0_CE
    """

    option_type = normalize_option_type(instrument_type)

    if strike_price is None or not option_type:
        return None

    try:
        return f"{float(strike_price)}_{option_type}"
    except Exception:
        return None


def round_to_nearest_strike(value, strike_step: int | None = None) -> int:
    """
    Rounds a spot value to nearest configured strike step.

    Example:
        value=24333, strike_step=50 -> 24350
    """

    step = safe_int(
        strike_step,
        safe_int(getattr(config, "EMA_ALERT_STRIKE_STEP", 50), 50),
    )

    if step <= 0:
        step = 50

    spot = safe_float(value)

    return int(round(spot / step) * step)


def clamp_strike_to_filter_range(strike_value: float) -> float:
    """
    Clamps strike inside configured STRIKE_FROM and STRIKE_TO.
    """

    strike_from = safe_float(getattr(config, "STRIKE_FROM", strike_value))
    strike_to = safe_float(getattr(config, "STRIKE_TO", strike_value))

    return max(strike_from, min(float(strike_value), strike_to))


def is_strike_inside_filter_range(strike_value) -> bool:
    """
    Returns True if strike is inside global configured filter range.
    """

    if strike_value is None:
        return False

    strike = safe_float(strike_value)

    strike_from = safe_float(getattr(config, "STRIKE_FROM", 0.0))
    strike_to = safe_float(getattr(config, "STRIKE_TO", 0.0))

    return strike_from <= strike <= strike_to


def is_strike_inside_window(strike_value, lower_limit, upper_limit) -> bool:
    """
    Returns True if strike is inside provided window.
    """

    if strike_value is None:
        return False

    strike = safe_float(strike_value)
    lower = safe_float(lower_limit)
    upper = safe_float(upper_limit)

    return lower <= strike <= upper


# ============================================================
# Contract Filtering Helpers
# ============================================================


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


def _build_cache_indexes(data: list) -> tuple[dict, dict]:
    """
    Builds fast lookup indexes from cached contract data.

    Returns:
        contracts_by_key
        contracts_by_strike_type
    """

    contracts_by_key = {}
    contracts_by_strike_type = {}

    for item in data or []:
        if not isinstance(item, dict):
            continue

        instrument_key = item.get("instrument_key")

        if instrument_key:
            contracts_by_key[instrument_key] = item.copy()

        strike_type_key = build_strike_type_key(
            item.get("strike_price"),
            item.get("instrument_type"),
        )

        if strike_type_key:
            contracts_by_strike_type[strike_type_key] = item.copy()

    return contracts_by_key, contracts_by_strike_type


def _refresh_cache_indexes_locked():
    """
    Rebuilds option cache lookup indexes.

    Caller must hold _cache_lock.
    """

    data = options_cache.get("data", [])

    contracts_by_key, contracts_by_strike_type = _build_cache_indexes(data)

    options_cache["contracts_by_key"] = contracts_by_key
    options_cache["contracts_by_strike_type"] = contracts_by_strike_type


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
        return [item.copy() for item in options_cache.get("data", [])]


def get_all_cached_instruments() -> list:
    """
    Returns all cached instruments.

    Includes:
        1. NIFTY index feed
        2. Cached option contracts
    """

    with _cache_lock:
        cached_data = [item.copy() for item in options_cache.get("data", [])]

    return [
        NIFTY_INDEX_FEED.copy(),
        *cached_data,
    ]


def get_contract_info_by_instrument_key(instrument_key: str) -> dict | None:
    """
    Returns basic metadata for an instrument key.

    Used by:
        - Historical candle service
        - Debug/status helpers
        - Opening Range service
        - EMA Telegram alert order suggestion logic
    """

    main_key = getattr(config, "MAIN_NIFTY_SECURITY", "NSE_INDEX|Nifty 50")

    if instrument_key == main_key:
        return NIFTY_INDEX_FEED.copy()

    with _cache_lock:
        contracts_by_key = options_cache.get("contracts_by_key", {})

        item = contracts_by_key.get(instrument_key)

        if item:
            return item.copy()

        cached_data = list(options_cache.get("data", []))

    for item in cached_data:
        if item.get("instrument_key") == instrument_key:
            return item.copy()

    return None


def get_contract_info_by_strike_type(
    strike_price: float,
    instrument_type: str,
) -> dict | None:
    """
    Returns cached option contract by strike and CE/PE.

    Example:
        get_contract_info_by_strike_type(24500, "CE")
    """

    option_type = normalize_option_type(instrument_type)

    if strike_price is None or not option_type:
        return None

    strike_type_key = build_strike_type_key(strike_price, option_type)

    if not strike_type_key:
        return None

    with _cache_lock:
        item = options_cache.get("contracts_by_strike_type", {}).get(strike_type_key)

        if item:
            return item.copy()

        cached_data = list(options_cache.get("data", []))

    for contract in cached_data:
        if (
            safe_float(contract.get("strike_price")) == safe_float(strike_price)
            and normalize_option_type(contract.get("instrument_type")) == option_type
        ):
            return contract.copy()

    return None


def get_option_contracts_in_strike_window(
    lower_limit: float,
    upper_limit: float,
    option_types: list[str] | None = None,
) -> list:
    """
    Returns cached option contracts inside given strike window.

    Used for isolated instrument selection based on:
        Opening Range average +/- configured window points.
    """

    normalized_types = None

    if option_types:
        normalized_types = {
            normalize_option_type(item)
            for item in option_types
            if normalize_option_type(item)
        }

    lower = safe_float(lower_limit)
    upper = safe_float(upper_limit)

    with _cache_lock:
        cached_data = [item.copy() for item in options_cache.get("data", [])]

    output = []

    for item in cached_data:
        strike = item.get("strike_price")
        option_type = normalize_option_type(item.get("instrument_type"))

        if strike is None or not option_type:
            continue

        if normalized_types and option_type not in normalized_types:
            continue

        if is_strike_inside_window(strike, lower, upper):
            output.append(item)

    return output


def get_option_contracts_in_average_window(
    average_value: float,
    window_points: float | None = None,
    option_types: list[str] | None = None,
) -> dict:
    """
    Returns option contracts inside Opening Range average +/- window,
    clamped to global STRIKE_FROM and STRIKE_TO.

    Example:
        average=24570
        window=500
        raw range = 24070 to 25070
        config range = 23000 to 25000
        final range = 24070 to 25000
    """

    window = safe_float(
        window_points,
        safe_float(
            getattr(config, "OPENING_RANGE_ISOLATION_AVERAGE_WINDOW_POINTS", 500.0),
            500.0,
        ),
    )

    average = safe_float(average_value)

    configured_from = safe_float(getattr(config, "STRIKE_FROM", 0.0))
    configured_to = safe_float(getattr(config, "STRIKE_TO", 0.0))

    raw_lower = average - window
    raw_upper = average + window

    final_lower = max(configured_from, raw_lower)
    final_upper = min(configured_to, raw_upper)

    contracts = get_option_contracts_in_strike_window(
        lower_limit=final_lower,
        upper_limit=final_upper,
        option_types=option_types,
    )

    return {
        "average": average,
        "window_points": window,
        "configured_from": configured_from,
        "configured_to": configured_to,
        "raw_lower": raw_lower,
        "raw_upper": raw_upper,
        "final_lower": final_lower,
        "final_upper": final_upper,
        "contracts_count": len(contracts),
        "contracts": contracts,
    }


def get_nearest_contract_to_average(
    contracts: list,
    average_value: float,
) -> dict | None:
    """
    From given contracts, returns contract nearest to average based on strike price.
    """

    if not contracts:
        return None

    average = safe_float(average_value)

    valid_contracts = [
        item
        for item in contracts
        if isinstance(item, dict) and item.get("strike_price") is not None
    ]

    if not valid_contracts:
        return None

    return min(
        valid_contracts,
        key=lambda item: abs(safe_float(item.get("strike_price")) - average),
    ).copy()


# ============================================================
# EMA Alert Order Instrument Helpers
# ============================================================


def get_order_option_type_for_ema_cross(cross_type: str) -> str | None:
    """
    Backward-compatible order option type resolver.

    Old behavior:
        bullish_cross -> CE
        bearish_cross -> PE

    This is kept as fallback when isolated instrument type is not available.
    """

    cross_text = str(cross_type or "").lower()

    if "bullish" in cross_text:
        return normalize_option_type(
            getattr(config, "EMA_ALERT_BULLISH_OPTION_TYPE", "CE")
        )

    if "bearish" in cross_text:
        return normalize_option_type(
            getattr(config, "EMA_ALERT_BEARISH_OPTION_TYPE", "PE")
        )

    return None


def get_order_option_type_for_isolated_ema_cross(
    cross_type: str,
    isolated_instrument_type: str | None,
) -> str | None:
    """
    Resolves order option type using isolated instrument side.

    New requirement:
        bullish_cross -> same side as isolated instrument
        bearish_cross -> opposite side of isolated instrument

    Examples:
        isolated CE + bullish_cross -> CE
        isolated CE + bearish_cross -> PE
        isolated PE + bullish_cross -> PE
        isolated PE + bearish_cross -> CE
    """

    isolated_type = normalize_option_type(isolated_instrument_type)

    if not isolated_type:
        return None

    cross_text = str(cross_type or "").lower()

    if "bullish" in cross_text:
        return isolated_type

    if "bearish" in cross_text:
        return get_opposite_option_type(isolated_type)

    return None


def build_nearest_order_strikes(
    current_nifty_ltp: float,
    strike_step: int | None = None,
    offsets: list[int] | None = None,
    clamp_to_filter_range: bool | None = None,
):
    """
    Builds nearest order strike list around current NIFTY spot.

    Example:
        current_nifty_ltp = 24333
        strike_step = 50
        nearest rounded strike = 24350
        offsets = [-50, 0, 50]
        output = [24300, 24350, 24400]
    """

    step = safe_int(
        strike_step,
        safe_int(getattr(config, "EMA_ALERT_STRIKE_STEP", 50), 50),
    )

    if step <= 0:
        step = 50

    selected_offsets = offsets

    if selected_offsets is None:
        selected_offsets = getattr(
            config,
            "EMA_ALERT_NEAREST_STRIKE_OFFSETS",
            [-50, 0, 50],
        )

    should_clamp = (
        bool(clamp_to_filter_range)
        if clamp_to_filter_range is not None
        else bool(
            getattr(config, "EMA_ALERT_ORDER_STRIKES_CLAMP_TO_FILTER_RANGE", True)
        )
    )

    nearest = round_to_nearest_strike(current_nifty_ltp, step)

    output = []

    for offset in selected_offsets:
        strike = nearest + safe_int(offset)

        if should_clamp:
            strike = int(clamp_strike_to_filter_range(strike))

        if strike not in output and is_strike_inside_filter_range(strike):
            output.append(strike)

    return output


def get_nearest_order_instruments_for_ema_cross(
    current_nifty_ltp: float,
    cross_type: str,
    isolated_instrument_type: str | None = None,
) -> list:
    """
    Returns nearest cached CE/PE option instruments for Telegram EMA alert.

    New requirement:
        bullish_cross -> same side as isolated instrument
        bearish_cross -> opposite side of isolated instrument

    Fallback behavior when isolated_instrument_type is not supplied:
        bullish_cross -> CE
        bearish_cross -> PE

    Strike selection is always based on current NIFTY spot.
    """

    if isolated_instrument_type:
        option_type = get_order_option_type_for_isolated_ema_cross(
            cross_type=cross_type,
            isolated_instrument_type=isolated_instrument_type,
        )
    else:
        option_type = get_order_option_type_for_ema_cross(cross_type)

    if not option_type:
        return []

    strikes = build_nearest_order_strikes(current_nifty_ltp)

    output = []

    for strike in strikes:
        contract = get_contract_info_by_strike_type(strike, option_type)

        if not contract:
            output.append(
                {
                    "strike_price": float(strike),
                    "instrument_type": option_type,
                    "instrument_key": None,
                    "trading_symbol": f"NIFTY {strike} {option_type}",
                    "available": False,
                    "live_ltp": None,
                }
            )
            continue

        output.append(
            {
                **contract,
                "available": True,
                "live_ltp": None,
            }
        )

    max_items = safe_int(getattr(config, "EMA_ALERT_MAX_ORDER_INSTRUMENTS", 3), 3)

    return output[:max_items]


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
        cached_data = [item.copy() for item in options_cache.get("data", [])]

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
        contracts_by_key = options_cache.get("contracts_by_key", {})

        item = contracts_by_key.get(instrument_key)

        if item:
            return {
                **item,
                "supported_intervals": OPTION_SUPPORTED_INTERVALS,
            }

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
            api_response.to_dict() if hasattr(api_response, "to_dict") else api_response
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

        option_keys = [
            item["instrument_key"]
            for item in final_output.get("data", [])
            if item.get("instrument_key")
        ]

        keys_to_subscribe = list(dict.fromkeys([instrument_key] + option_keys))

        contracts_by_key, contracts_by_strike_type = _build_cache_indexes(
            final_output.get("data", [])
        )

        with _cache_lock:
            options_cache["nearest_expiry"] = final_output.get("nearest_expiry")
            options_cache["total_contracts"] = final_output.get("total_contracts", 0)
            options_cache["subscribed_keys"] = keys_to_subscribe
            options_cache["data"] = final_output.get("data", [])
            options_cache["contracts_by_key"] = contracts_by_key
            options_cache["contracts_by_strike_type"] = contracts_by_strike_type

        logger.info(
            f"Updated global options cache. "
            f"Total keys to subscribe: {len(keys_to_subscribe)}, "
            f"contracts_by_key={len(contracts_by_key)}, "
            f"contracts_by_strike_type={len(contracts_by_strike_type)}"
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


# ============================================================
# Debug / Summary Helpers
# ============================================================


def get_options_cache_summary() -> dict:
    """
    Returns lightweight option cache summary.
    """

    with _cache_lock:
        return {
            "nearest_expiry": options_cache.get("nearest_expiry"),
            "total_contracts": options_cache.get("total_contracts"),
            "subscribed_keys_count": len(options_cache.get("subscribed_keys", [])),
            "contracts_by_key_count": len(options_cache.get("contracts_by_key", {})),
            "contracts_by_strike_type_count": len(
                options_cache.get("contracts_by_strike_type", {})
            ),
            "sample_subscribed_keys": options_cache.get("subscribed_keys", [])[:5],
            "sample_contract": (
                options_cache.get("data", [None])[0]
                if options_cache.get("data")
                else None
            ),
        }


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
