import json
import logging
from pathlib import Path
from datetime import datetime, date

import upstox_client
from upstox_client.rest import ApiException

from app.config import settings
from app.database import (
    connect_to_mongo,
    load_upstox_token,
    token_state,
)

logger = logging.getLogger("uvicorn")

# ============================================================
# Runtime Cache
# ============================================================

options_cache = {
    "nearest_expiry": None,
    "total_contracts": 0,
    "data": [],
}

# ============================================================
# Feed Constants
# ============================================================

NIFTY_INDEX_FEED = {
    "instrument_key": "NSE_INDEX|Nifty 50",
    "instrument_type": "INDEX",
    "strike_price": None,
    "expiry": None,
    "trading_symbol": "NIFTY 50",
    "underlying_type": "INDEX",
    "underlying_symbol": "NIFTY 50",
}

# Nifty index is only for live tick websocket feed.
NIFTY_SUPPORTED_INTERVALS = [
    0,  # Live Tick only
]

# Option contracts support live ticks and candle intervals.
OPTION_SUPPORTED_INTERVALS = [
    0,  # Live Tick
    1,  # 1 Minute
    3,  # 3 Minute
    5,  # 5 Minute
]

# Backward-compatible common intervals if any existing code imports this.
SUPPORTED_INTERVALS = OPTION_SUPPORTED_INTERVALS


# ============================================================
# Helpers
# ============================================================


def parse_expiry_date(expiry_val) -> date | None:
    """
    Safely convert expiry value to date.
    """

    if not expiry_val:
        return None

    if isinstance(expiry_val, datetime):
        return expiry_val.date()

    if isinstance(expiry_val, date):
        return expiry_val

    if isinstance(expiry_val, str):
        try:
            date_part = expiry_val.split()[0]

            return datetime.strptime(
                date_part,
                "%Y-%m-%d",
            ).date()

        except ValueError as ex:
            logger.warning(f"Failed to parse date string '{expiry_val}': {ex}")

            return None

    return None


def clean_contract_data(item: dict) -> dict:
    """
    Extract only required fields.
    """

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


def get_nearest_expiry_contracts(
    contracts: list,
) -> tuple[str | None, list]:
    """
    Find nearest expiry contracts and filter by configured strike range.
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

    matching_contracts = []

    for item in contracts:
        exp_date = parse_expiry_date(item.get("expiry"))

        if exp_date == nearest_date:
            matching_contracts.append(clean_contract_data(item))

    if hasattr(settings, "STRIKE_FROM") and hasattr(settings, "STRIKE_TO"):
        strike_from = float(settings.STRIKE_FROM)
        strike_to = float(settings.STRIKE_TO)

        matching_contracts = [
            item
            for item in matching_contracts
            if item.get("strike_price") is not None
            and strike_from <= float(item["strike_price"]) <= strike_to
        ]

    return (
        nearest_date_str,
        matching_contracts,
    )


# ============================================================
# Feed Discovery Helpers
# ============================================================


def get_available_feeds():
    """
    Returns all feeds available to client websocket consumers.

    Includes:
        - Nifty Index with interval 0 only
        - All filtered options with intervals 0,1,3,5
    """

    feeds = []

    # Nifty Index: live tick only
    feeds.append(
        {
            **NIFTY_INDEX_FEED,
            "supported_intervals": NIFTY_SUPPORTED_INTERVALS,
        }
    )

    # Options: live tick + candle intervals
    for item in options_cache.get("data", []):
        feeds.append(
            {
                **item,
                "supported_intervals": OPTION_SUPPORTED_INTERVALS,
            }
        )

    return feeds


def get_feed_by_instrument_key(
    instrument_key: str,
):
    """
    Returns feed metadata by instrument key.
    """

    if instrument_key == NIFTY_INDEX_FEED["instrument_key"]:
        return {
            **NIFTY_INDEX_FEED,
            "supported_intervals": NIFTY_SUPPORTED_INTERVALS,
        }

    for item in options_cache.get("data", []):
        if item.get("instrument_key") == instrument_key:
            return {
                **item,
                "supported_intervals": OPTION_SUPPORTED_INTERVALS,
            }

    return None


def is_nifty_index_feed(
    instrument_key: str,
) -> bool:
    """
    Returns True if the given instrument key is Nifty Index.
    """

    return instrument_key == NIFTY_INDEX_FEED["instrument_key"]


def is_valid_feed_interval(
    instrument_key: str,
    interval: int,
) -> bool:
    """
    Validates whether an interval is allowed for an instrument.

    Nifty:
        0 only

    Options:
        0,1,3,5
    """

    if is_nifty_index_feed(instrument_key):
        return interval in NIFTY_SUPPORTED_INTERVALS

    return interval in OPTION_SUPPORTED_INTERVALS


# ============================================================
# Upstox Options Fetch
# ============================================================


def get_options_contracts(
    instrument_key: str = "NSE_INDEX|Nifty 50",
    expiry_date: str = None,
    output_filename: str = "data/nearest_nifty_option_contracts.json",
    filter_nearest: bool = True,
    save_data: bool = False,
) -> dict | None:
    """
    Fetches option contracts for an instrument, filters nearest expiry,
    trims fields, updates the in-memory cache, and optionally exports
    to JSON if save_data=True.
    """

    global options_cache

    if not token_state.access_token:
        logger.info("Access token not found in memory. Loading from MongoDB...")

        connect_to_mongo()

        load_upstox_token()

    access_token = token_state.access_token

    if not access_token:
        logger.error("Failed to retrieve access token.")

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

        if hasattr(api_response, "to_dict"):
            response_dict = api_response.to_dict()
        else:
            response_dict = api_response

        all_contracts = response_dict.get(
            "data",
            [],
        )

        if filter_nearest and not expiry_date:
            nearest_expiry, filtered_contracts = get_nearest_expiry_contracts(
                all_contracts
            )

            logger.info(
                f"Nearest Expiry Identified: "
                f"{nearest_expiry} "
                f"({len(filtered_contracts)} contracts)"
            )

            final_output = {
                "status": response_dict.get(
                    "status",
                    "success",
                ),
                "nearest_expiry": nearest_expiry,
                "total_contracts": len(filtered_contracts),
                "data": filtered_contracts,
            }

        else:
            cleaned_all = [clean_contract_data(c) for c in all_contracts]

            final_output = {
                "status": response_dict.get(
                    "status",
                    "success",
                ),
                "nearest_expiry": expiry_date,
                "total_contracts": len(cleaned_all),
                "data": cleaned_all,
            }

        options_cache["nearest_expiry"] = final_output.get("nearest_expiry")

        options_cache["total_contracts"] = final_output.get(
            "total_contracts",
            0,
        )

        options_cache["data"] = final_output.get(
            "data",
            [],
        )

        logger.info("Updated global options cache.")

        if save_data:
            output_path = Path(output_filename)

            output_path.parent.mkdir(
                parents=True,
                exist_ok=True,
            )

            with open(
                output_path,
                "w",
                encoding="utf-8",
            ) as file:
                json.dump(
                    final_output,
                    file,
                    indent=4,
                    default=str,
                )

            logger.info(f"Saved options response to {output_path.resolve()}")

        return final_output

    except ApiException as ex:
        logger.error(
            "Exception when calling OptionsApi->get_option_contracts: %s",
            ex.body,
        )

        return None


# Keep this comments here only for testing purpose
# if __name__ == "__main__":
#     logging.basicConfig(level=logging.INFO)
#     logger.info("Executing option contracts fetch & filtering...")
#
#     # Set save_data=True if you want to write to disk
#     result = get_options_contracts(
#         instrument_key="NSE_INDEX|Nifty 50",
#         output_filename="data/nearest_nifty_option_contracts.json",
#         filter_nearest=True,
#         save_data=True
#     )
#
#     if result:
#         print("\n--- Processed Options Contracts ---")
#         print(f"Target Expiry Date: {result.get('nearest_expiry')}")
#         print(f"Total Contracts: {result.get('total_contracts')}")
#         if result.get("data"):
#             print("Sample Contract Format:")
#             print(json.dumps(result["data"][0], indent=4))
