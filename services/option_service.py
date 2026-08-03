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
# Helpers
# ============================================================
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
    (e.g., STRIKE_FROM <= strike_price <= STRIKE_TO) for both CE and PE.
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
# Feed Discovery Helpers
# ============================================================
def get_available_feeds() -> list:
    """Returns all feeds available to client websocket consumers."""
    feeds = [{**NIFTY_INDEX_FEED, "supported_intervals": NIFTY_SUPPORTED_INTERVALS}]

    with _cache_lock:
        cached_data = list(options_cache.get("data", []))

    for item in cached_data:
        feeds.append({**item, "supported_intervals": OPTION_SUPPORTED_INTERVALS})

    return feeds


def get_feed_by_instrument_key(instrument_key: str) -> dict | None:
    """Returns feed metadata by instrument key."""
    main_key = getattr(config, "MAIN_NIFTY_SECURITY", "NSE_INDEX|Nifty 50")
    if instrument_key == main_key:
        return {**NIFTY_INDEX_FEED, "supported_intervals": NIFTY_SUPPORTED_INTERVALS}

    with _cache_lock:
        cached_data = list(options_cache.get("data", []))

    for item in cached_data:
        if item.get("instrument_key") == instrument_key:
            return {**item, "supported_intervals": OPTION_SUPPORTED_INTERVALS}

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
    updates the in-memory cache with contracts and subscription keys, and optionally exports to JSON.
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

        api_response = api_instance.get_option_contracts(instrument_key, **kwargs)

        response_dict = (
            api_response.to_dict()
            if hasattr(api_response, "to_dict")
            else api_response
        )
        all_contracts = response_dict.get("data", [])

        if filter_nearest and not expiry_date:
            nearest_expiry, filtered_contracts = get_nearest_expiry_contracts(all_contracts)

            logger.info(
                f"Nearest Expiry Identified: {nearest_expiry} "
                f"({len(filtered_contracts)} contracts between {getattr(config, 'STRIKE_FROM', 'N/A')} and {getattr(config, 'STRIKE_TO', 'N/A')})"
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
        option_keys = [item["instrument_key"] for item in final_output.get("data", []) if item.get("instrument_key")]
        keys_to_subscribe = list(dict.fromkeys([instrument_key] + option_keys))

        # Update cache safely with lock
        with _cache_lock:
            options_cache["nearest_expiry"] = final_output.get("nearest_expiry")
            options_cache["total_contracts"] = final_output.get("total_contracts", 0)
            options_cache["subscribed_keys"] = keys_to_subscribe
            options_cache["data"] = final_output.get("data", [])

        logger.info(f"Updated global options cache. Total keys to subscribe: {len(keys_to_subscribe)}")

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

    

# # Manual local test execution
# if __name__ == "__main__":
#     logger.info("Executing option contracts fetch & filtering...")
#     result = get_options_contracts(
#         instrument_key="NSE_INDEX|Nifty 50",
#         output_filename="data/nearest_nifty_option_contracts.json",
#         filter_nearest=True,
#         save_data=True,
#     )

#     if result:
#         print("\n--- Processed Options Contracts ---")
#         print(f"Target Expiry Date: {result.get('nearest_expiry')}")
#         print(f"Total Contracts: {result.get('total_contracts')}")
#         if result.get("data"):
#             print("Sample Contract Format:")
#             print(json.dumps(result["data"][0], indent=4))