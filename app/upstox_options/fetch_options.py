import sys
import json
import logging
from pathlib import Path
from datetime import datetime, date
import upstox_client
from upstox_client.rest import ApiException

# Ensure project root is in python path so imports work smoothly
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.append(str(PROJECT_ROOT))

from app.database import connect_to_mongo, load_upstox_token, token_state
from app.config import settings

logger = logging.getLogger("uvicorn")

# Global in-memory cache accessible across other modules
options_cache = {
    "nearest_expiry": None,
    "total_contracts": 0,
    "data": []
}


def parse_expiry_date(expiry_val) -> date | None:
    """Helper function to safely extract a datetime.date from strings or datetime objects."""
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
        except ValueError as e:
            logger.warning(f"Failed to parse date string '{expiry_val}': {e}")
            return None

    return None


def clean_contract_data(item: dict) -> dict:
    """Extracts and formats only the essential keys from a contract item."""
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
    Parses contracts, finds the nearest upcoming/today expiry date,
    and returns trimmed contract dictionaries matching that expiry.
    """
    if not contracts:
        return None, []

    today = datetime.now().date()
    valid_expiries = set()

    # Step 1: Collect valid future or today expiry dates
    for item in contracts:
        exp_date = parse_expiry_date(item.get("expiry"))
        if exp_date and exp_date >= today:
            valid_expiries.add(exp_date)

    if not valid_expiries:
        logger.warning("No valid future expiry dates found in contracts.")
        return None, []

    # Step 2: Identify the closest date to today
    nearest_date = min(valid_expiries)
    nearest_date_str = nearest_date.strftime("%Y-%m-%d")

    # Step 3: Filter & clean contracts matching the nearest expiry
    matching_contracts = []
    for item in contracts:
        exp_date = parse_expiry_date(item.get("expiry"))
        if exp_date == nearest_date:
            # Clean and keep only specified fields
            cleaned_item = clean_contract_data(item)
            matching_contracts.append(cleaned_item)

    # Step 4: Optional filter by STRIKE_FROM and STRIKE_TO if defined in settings
    if hasattr(settings, "STRIKE_FROM") and hasattr(settings, "STRIKE_TO"):
        strike_from = float(settings.STRIKE_FROM)
        strike_to = float(settings.STRIKE_TO)
        matching_contracts = [
            item for item in matching_contracts
            if item.get("strike_price") is not None
            and strike_from <= float(item.get("strike_price")) <= strike_to
        ]

    return nearest_date_str, matching_contracts


def get_options_contracts(
    instrument_key: str = "NSE_INDEX|Nifty 50",
    expiry_date: str = None,
    output_filename: str = "data/nearest_nifty_option_contracts.json",
    filter_nearest: bool = True,
    save_data: bool = False
) -> dict | None:
    """
    Fetches option contracts for an instrument, filters nearest expiry, trims fields,
    updates the in-memory cache, and optionally exports to JSON if save_data=True.
    """
    global options_cache

    # 1. Connect to Mongo & load token if not in memory
    if not token_state.access_token:
        logger.info("Access token not found in memory. Loading from MongoDB...")
        connect_to_mongo()
        load_upstox_token()

    access_token = token_state.access_token
    if not access_token:
        logger.error("Failed to retrieve access token. Check MongoDB connection.")
        return None

    # 2. Configure Upstox API client
    configuration = upstox_client.Configuration()
    configuration.access_token = access_token
    api_client = upstox_client.ApiClient(configuration)

    api_instance = upstox_client.OptionsApi(api_client)

    # 3. Call Upstox Options API
    try:
        kwargs = {}
        if expiry_date:
            kwargs['expiry_date'] = expiry_date

        api_response = api_instance.get_option_contracts(instrument_key, **kwargs)
        
        # Convert API response object to dictionary
        if hasattr(api_response, "to_dict"):
            response_dict = api_response.to_dict()
        else:
            response_dict = api_response

        all_contracts = response_dict.get("data", [])

        # 4. Filter for nearest expiry & trim contracts
        if filter_nearest and not expiry_date:
            nearest_expiry, filtered_contracts = get_nearest_expiry_contracts(all_contracts)
            logger.info(
                f"Nearest Expiry Identified: {nearest_expiry} "
                f"({len(filtered_contracts)} contracts matched)"
            )
            final_output = {
                "status": response_dict.get("status", "success"),
                "nearest_expiry": nearest_expiry,
                "total_contracts": len(filtered_contracts),
                "data": filtered_contracts
            }
        else:
            # If not filtering nearest, clean all contracts individually
            cleaned_all = [clean_contract_data(c) for c in all_contracts]
            final_output = {
                "status": response_dict.get("status", "success"),
                "nearest_expiry": expiry_date,
                "total_contracts": len(cleaned_all),
                "data": cleaned_all
            }

        # 5. Update global in-memory cache
        options_cache["nearest_expiry"] = final_output.get("nearest_expiry")
        options_cache["total_contracts"] = final_output.get("total_contracts", 0)
        options_cache["data"] = final_output.get("data", [])
        logger.info("Updated global in-memory options_cache.")

        # 6. Save to disk ONLY if save_data is True
        if save_data:
            output_path = Path(output_filename)
            output_path.parent.mkdir(parents=True, exist_ok=True)

            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(final_output, f, indent=4, default=str)

            logger.info(f"Successfully saved trimmed options response to {output_path.resolve()}")

        return final_output

    except ApiException as e:
        logger.error("Exception when calling OptionsApi->get_option_contracts: %s\n", e.body)
        return None


# if __name__ == "__main__":
#     logging.basicConfig(level=logging.INFO)
#     logger.info("Executing option contracts fetch & filtering...")

#     # Set save_data=True if you want to write to disk
#     result = get_options_contracts(
#         instrument_key="NSE_INDEX|Nifty 50",
#         output_filename="data/nearest_nifty_option_contracts.json",
#         filter_nearest=True,
#         save_data=True
#     )

#     if result:
#         print("\n--- Processed Options Contracts ---")
#         print(f"Target Expiry Date: {result.get('nearest_expiry')}")
#         print(f"Total Contracts: {result.get('total_contracts')}")
#         if result.get("data"):
#             print("Sample Contract Format:")
#             print(json.dumps(result["data"][0], indent=4))