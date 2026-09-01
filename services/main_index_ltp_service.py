from datetime import date, datetime
from typing import Any

import upstox_client
from upstox_client.rest import ApiException

from core import config
from core.logger import get_logger
from services.opening_range.intraday import (
    fetch_intraday_candles_for_instrument,
)
from services.option_service import options_cache
from services.token_service import token_service

logger = get_logger(__file__)


# ============================================================
# Value Conversion Helpers
# ============================================================


def _safe_float(
    value: Any,
    default: float | None = None,
) -> float | None:
    """
    Safely converts a value to float.
    """

    try:
        if value is None:
            return default

        return float(value)

    except (TypeError, ValueError, OverflowError):
        return default


def _safe_int(
    value: Any,
    default: int = 0,
) -> int:
    """
    Safely converts a value to int.
    """

    try:
        if value is None:
            return default

        return int(float(value))

    except (TypeError, ValueError, OverflowError):
        return default


# ============================================================
# Option Type Helpers
# ============================================================


def _normalize_option_type(
    option_type: Any,
) -> str | None:
    """
    Normalizes option type to CE or PE.

    Supported values:

        CE
        PE
        CALL
        PUT
        C
        P
    """

    normalized_value = str(option_type or "").strip().upper()

    mapping = {
        "CE": "CE",
        "CALL": "CE",
        "C": "CE",
        "PE": "PE",
        "PUT": "PE",
        "P": "PE",
    }

    return mapping.get(
        normalized_value,
    )


# ============================================================
# Configuration Helpers
# ============================================================


def _get_default_nearest_instruments_count() -> int:
    """
    Returns the configured number of nearest option strikes.

    Config setting:

        MAIN_INDEX_NEAREST_INSTRUMENTS_COUNT

    Default:

        3
    """

    count = _safe_int(
        getattr(
            config,
            "MAIN_INDEX_NEAREST_INSTRUMENTS_COUNT",
            3,
        ),
        3,
    )

    if count <= 0:
        return 3

    return count


def _get_budget_min_price() -> float:
    """
    Returns the configured minimum option LTP.
    """

    minimum_price = _safe_float(
        getattr(
            config,
            "EMA_ALERT_BUDGET_MIN_PRICE",
            20.0,
        ),
        20.0,
    )

    if minimum_price is None:
        return 20.0

    return minimum_price


def _get_budget_max_price() -> float:
    """
    Returns the configured maximum option LTP.
    """

    maximum_price = _safe_float(
        getattr(
            config,
            "EMA_ALERT_BUDGET_MAX_PRICE",
            30.0,
        ),
        30.0,
    )

    if maximum_price is None:
        return 30.0

    return maximum_price


def _get_budget_max_instruments() -> int:
    """
    Returns the maximum number of budget-range instruments.

    Config setting:

        EMA_ALERT_BUDGET_MAX_INSTRUMENTS

    Default:

        10
    """

    maximum_instruments = _safe_int(
        getattr(
            config,
            "EMA_ALERT_BUDGET_MAX_INSTRUMENTS",
            10,
        ),
        10,
    )

    if maximum_instruments <= 0:
        return 10

    return maximum_instruments


def _get_budget_sort_mode() -> str:
    """
    Returns the configured budget instrument sorting mode.

    Supported modes:

        nearest_price
        strike_ascending
        ltp_ascending
        ltp_descending

    Default:

        nearest_price
    """

    sort_mode = (
        str(
            getattr(
                config,
                "EMA_ALERT_BUDGET_SORT_MODE",
                "nearest_price",
            )
            or "nearest_price"
        )
        .strip()
        .lower()
    )

    supported_modes = {
        "nearest_price",
        "strike_ascending",
        "ltp_ascending",
        "ltp_descending",
    }

    if sort_mode not in supported_modes:
        return "nearest_price"

    return sort_mode


# ============================================================
# JSON-Safe SDK Conversion
# ============================================================


def _convert_sdk_response_to_dict(
    value: Any,
) -> Any:
    """
    Recursively converts an Upstox SDK response into JSON-safe data.
    """

    if value is None:
        return None

    if isinstance(
        value,
        (
            str,
            int,
            float,
            bool,
        ),
    ):
        return value

    if isinstance(
        value,
        (
            datetime,
            date,
        ),
    ):
        return value.isoformat()

    if isinstance(value, dict):
        return {
            str(key): _convert_sdk_response_to_dict(
                item,
            )
            for key, item in value.items()
        }

    if isinstance(
        value,
        (
            list,
            tuple,
            set,
        ),
    ):
        return [_convert_sdk_response_to_dict(item) for item in value]

    if hasattr(value, "to_dict"):
        try:
            return _convert_sdk_response_to_dict(
                value.to_dict(),
            )
        except Exception:
            pass

    if hasattr(value, "__dict__"):
        try:
            return {
                str(key): _convert_sdk_response_to_dict(
                    item,
                )
                for key, item in vars(value).items()
                if not str(key).startswith("_")
            }
        except Exception:
            pass

    return str(value)


# ============================================================
# Expiry Helpers
# ============================================================


def _normalize_expiry_date(
    expiry: Any,
) -> str | None:
    """
    Converts an expiry value into YYYY-MM-DD format.
    """

    if expiry is None:
        return None

    if isinstance(expiry, datetime):
        return expiry.date().isoformat()

    if isinstance(expiry, date):
        return expiry.isoformat()

    expiry_text = str(expiry).strip()

    if not expiry_text:
        return None

    try:
        parsed_expiry = datetime.fromisoformat(
            expiry_text.replace(
                "Z",
                "+00:00",
            )
        )

        return parsed_expiry.date().isoformat()

    except ValueError:
        pass

    try:
        parsed_expiry = date.fromisoformat(expiry_text[:10])

        return parsed_expiry.isoformat()

    except ValueError:
        return None


def _get_loaded_nearest_expiry() -> str | None:
    """
    Returns the nearest expiry from options_cache.

    Preference:

        1. options_cache["nearest_expiry"]
        2. Earliest expiry found in options_cache["data"]
    """

    cached_expiry = _normalize_expiry_date(
        options_cache.get(
            "nearest_expiry",
        )
    )

    if cached_expiry:
        return cached_expiry

    loaded_instruments = options_cache.get(
        "data",
        [],
    )

    if not isinstance(
        loaded_instruments,
        list,
    ):
        return None

    expiry_values = set()

    for instrument in loaded_instruments:
        if not isinstance(instrument, dict):
            continue

        expiry = _normalize_expiry_date(instrument.get("expiry"))

        if expiry:
            expiry_values.add(expiry)

    if not expiry_values:
        return None

    return sorted(expiry_values)[0]


# ============================================================
# Loaded Instrument Lookup
# ============================================================


def _get_loaded_instrument_by_key(
    instrument_key: str,
) -> dict | None:
    """
    Returns a loaded instrument from options_cache by instrument key.
    """

    normalized_key = str(instrument_key or "").strip()

    if not normalized_key:
        return None

    loaded_instruments = options_cache.get(
        "data",
        [],
    )

    if not isinstance(
        loaded_instruments,
        list,
    ):
        return None

    for instrument in loaded_instruments:
        if not isinstance(instrument, dict):
            continue

        loaded_key = str(instrument.get("instrument_key") or "").strip()

        if loaded_key == normalized_key:
            return instrument

    return None


def _get_loaded_instrument_map() -> dict:
    """
    Builds an instrument-key lookup from options_cache.
    """

    loaded_instruments = options_cache.get(
        "data",
        [],
    )

    if not isinstance(
        loaded_instruments,
        list,
    ):
        return {}

    instrument_map = {}

    for instrument in loaded_instruments:
        if not isinstance(instrument, dict):
            continue

        instrument_key = str(instrument.get("instrument_key") or "").strip()

        if not instrument_key:
            continue

        instrument_map[instrument_key] = instrument

    return instrument_map


# ============================================================
# Main Index Intraday Candle Helpers
# ============================================================


def _get_latest_candle(
    candles: list,
) -> dict | None:
    """
    Returns the final candle from an ascending candle list.
    """

    if not isinstance(candles, list):
        return None

    if not candles:
        return None

    latest_candle = candles[-1]

    if not isinstance(latest_candle, dict):
        return None

    return latest_candle


def _build_main_index_ltp_result(
    *,
    status: str,
    instrument_key: str,
    unit: str,
    interval: str,
    ltp: float | None = None,
    latest_candle: dict | None = None,
    candles_count: int = 0,
    error: str | None = None,
) -> dict:
    """
    Builds a consistent main index LTP result.
    """

    return {
        "status": status,
        "success": status == "success",
        "instrument_key": instrument_key,
        "ltp": ltp,
        "source": "latest_intraday_candle_close",
        "unit": unit,
        "interval": interval,
        "candles_count": candles_count,
        "latest_candle": latest_candle,
        "error": error,
    }


def get_main_index_ltp(
    unit: str | None = None,
    interval: str | None = None,
) -> dict:
    """
    Returns the latest main NIFTY intraday candle close.

    This function remains available for callers that specifically
    require the latest intraday candle and its close price.
    """

    instrument_key = str(
        getattr(
            config,
            "MAIN_NIFTY_SECURITY",
            "NSE_INDEX|Nifty 50",
        )
        or ""
    ).strip()

    selected_unit = str(
        unit
        or getattr(
            config,
            "OPENING_RANGE_INTRADAY_UNIT",
            "minutes",
        )
        or "minutes"
    ).strip()

    selected_interval = str(
        interval
        or getattr(
            config,
            "OPENING_RANGE_INTRADAY_INTERVAL",
            "1",
        )
        or "1"
    ).strip()

    if not instrument_key:
        error_message = "MAIN_NIFTY_SECURITY is not configured."

        return _build_main_index_ltp_result(
            status="failed",
            instrument_key=instrument_key,
            unit=selected_unit,
            interval=selected_interval,
            error=error_message,
        )

    logger.info(
        "Main index LTP fetch started. " "instrument_key=%s, unit=%s, interval=%s",
        instrument_key,
        selected_unit,
        selected_interval,
    )

    try:
        intraday_result = fetch_intraday_candles_for_instrument(
            instrument_key=instrument_key,
            unit=selected_unit,
            interval=selected_interval,
        )

        if not isinstance(
            intraday_result,
            dict,
        ):
            error_message = "Intraday candle service returned " "an invalid response."

            return _build_main_index_ltp_result(
                status="failed",
                instrument_key=instrument_key,
                unit=selected_unit,
                interval=selected_interval,
                error=error_message,
            )

        candles = intraday_result.get(
            "candles",
            [],
        )

        if not isinstance(candles, list):
            candles = []

        candles_count = len(candles)

        intraday_status = str(intraday_result.get("status") or "").lower()

        if intraday_status == "failed":
            error_message = (
                intraday_result.get("error") or "Intraday candle fetch failed."
            )

            return _build_main_index_ltp_result(
                status="failed",
                instrument_key=instrument_key,
                unit=selected_unit,
                interval=selected_interval,
                candles_count=candles_count,
                error=str(error_message),
            )

        latest_candle = _get_latest_candle(
            candles,
        )

        if latest_candle is None:
            error_message = (
                "No intraday candles are available " "for the configured main index."
            )

            return _build_main_index_ltp_result(
                status="empty",
                instrument_key=instrument_key,
                unit=selected_unit,
                interval=selected_interval,
                candles_count=candles_count,
                error=error_message,
            )

        latest_close = _safe_float(
            latest_candle.get("close"),
        )

        if latest_close is None or latest_close <= 0:
            error_message = (
                "The latest main index candle does not " "contain a valid close price."
            )

            return _build_main_index_ltp_result(
                status="failed",
                instrument_key=instrument_key,
                unit=selected_unit,
                interval=selected_interval,
                candles_count=candles_count,
                latest_candle=latest_candle,
                error=error_message,
            )

        logger.info(
            "Main index LTP fetched successfully. "
            "instrument_key=%s, ltp=%s, candles_count=%s",
            instrument_key,
            latest_close,
            candles_count,
        )

        return _build_main_index_ltp_result(
            status="success",
            instrument_key=instrument_key,
            unit=selected_unit,
            interval=selected_interval,
            ltp=latest_close,
            latest_candle=latest_candle,
            candles_count=candles_count,
            error=None,
        )

    except Exception as ex:
        error_message = f"{type(ex).__name__}: {ex}"

        logger.exception(
            "Unexpected error while fetching main index LTP. "
            "instrument_key=%s, error=%s",
            instrument_key,
            error_message,
        )

        return _build_main_index_ltp_result(
            status="failed",
            instrument_key=instrument_key,
            unit=selected_unit,
            interval=selected_interval,
            error=error_message,
        )


def get_main_index_ltp_value(
    unit: str | None = None,
    interval: str | None = None,
) -> float | None:
    """
    Returns only the latest main-index intraday candle close.
    """

    result = get_main_index_ltp(
        unit=unit,
        interval=interval,
    )

    if not result.get("success"):
        return None

    return _safe_float(
        result.get("ltp"),
    )


# ============================================================
# Option Chain API Helpers
# ============================================================


def _get_api_exception_message(
    exception: ApiException,
) -> str:
    """
    Extracts the most useful message from an Upstox ApiException.
    """

    body = getattr(
        exception,
        "body",
        None,
    )

    if body:
        return str(body)

    reason = getattr(
        exception,
        "reason",
        None,
    )

    if reason:
        return str(reason)

    return str(exception)


def get_main_index_option_chain(
    expiry_date: str | None = None,
) -> dict:
    """
    Fetches the Upstox put/call option chain for the configured
    main NIFTY index.

    When expiry_date is omitted, the nearest expiry from
    options_cache is used.
    """

    underlying_instrument_key = str(
        getattr(
            config,
            "MAIN_NIFTY_SECURITY",
            "NSE_INDEX|Nifty 50",
        )
        or ""
    ).strip()

    if not underlying_instrument_key:
        error_message = "MAIN_NIFTY_SECURITY is not configured."

        return {
            "status": "failed",
            "success": False,
            "message": error_message,
            "underlying_instrument_key": None,
            "expiry_date": None,
            "expiry_source": None,
            "nearest_expiry": None,
            "option_chain_count": 0,
            "option_chain": [],
            "error": error_message,
        }

    requested_expiry = (
        _normalize_expiry_date(expiry_date) if expiry_date is not None else None
    )

    if expiry_date is not None and not requested_expiry:
        error_message = "Invalid expiry_date. Expected YYYY-MM-DD format."

        return {
            "status": "failed",
            "success": False,
            "message": error_message,
            "underlying_instrument_key": (underlying_instrument_key),
            "expiry_date": None,
            "expiry_source": "query_parameter",
            "nearest_expiry": options_cache.get(
                "nearest_expiry",
            ),
            "option_chain_count": 0,
            "option_chain": [],
            "error": error_message,
        }

    if requested_expiry:
        selected_expiry = requested_expiry
        expiry_source = "query_parameter"
    else:
        selected_expiry = _get_loaded_nearest_expiry()
        expiry_source = "options_cache"

    if not selected_expiry:
        error_message = "No valid option expiry is available in options_cache."

        return {
            "status": "empty",
            "success": False,
            "message": error_message,
            "underlying_instrument_key": (underlying_instrument_key),
            "expiry_date": None,
            "expiry_source": expiry_source,
            "nearest_expiry": options_cache.get(
                "nearest_expiry",
            ),
            "option_chain_count": 0,
            "option_chain": [],
            "error": error_message,
        }

    access_token = token_service.get_access_token()

    if not access_token:
        logger.info(
            "No access token found in memory. "
            "Refreshing token cache before option-chain request."
        )

        token_service.refresh_tokens()

        access_token = token_service.get_access_token()

    if not access_token:
        error_message = "No Upstox access token is available."

        return {
            "status": "failed",
            "success": False,
            "message": error_message,
            "underlying_instrument_key": (underlying_instrument_key),
            "expiry_date": selected_expiry,
            "expiry_source": expiry_source,
            "nearest_expiry": options_cache.get(
                "nearest_expiry",
            ),
            "option_chain_count": 0,
            "option_chain": [],
            "error": error_message,
        }

    logger.info(
        "Option chain request started. "
        "instrument_key=%s, expiry_date=%s, "
        "expiry_source=%s",
        underlying_instrument_key,
        selected_expiry,
        expiry_source,
    )

    try:
        configuration = upstox_client.Configuration()

        configuration.access_token = access_token

        api_client = upstox_client.ApiClient(configuration)

        api_instance = upstox_client.OptionsApi(api_client)

        api_response = api_instance.get_put_call_option_chain(
            underlying_instrument_key,
            selected_expiry,
        )

        converted_response = _convert_sdk_response_to_dict(api_response)

        option_chain = []

        if isinstance(
            converted_response,
            dict,
        ):
            response_data = converted_response.get("data")

            if isinstance(
                response_data,
                list,
            ):
                option_chain = response_data

            elif response_data is not None:
                option_chain = [response_data]

        elif isinstance(
            converted_response,
            list,
        ):
            option_chain = converted_response

        logger.info(
            "Option chain request completed. "
            "instrument_key=%s, expiry_date=%s, "
            "option_chain_count=%s",
            underlying_instrument_key,
            selected_expiry,
            len(option_chain),
        )

        return {
            "status": "success",
            "success": True,
            "message": ("Option chain returned successfully."),
            "underlying_instrument_key": (underlying_instrument_key),
            "expiry_date": selected_expiry,
            "expiry_source": expiry_source,
            "nearest_expiry": options_cache.get(
                "nearest_expiry",
            ),
            "option_chain_count": len(option_chain),
            "option_chain": option_chain,
            "error": None,
        }

    except ApiException as ex:
        error_message = _get_api_exception_message(ex)

        status_code = getattr(
            ex,
            "status",
            None,
        )

        logger.error(
            "Upstox option-chain request failed. "
            "instrument_key=%s, expiry_date=%s, "
            "status_code=%s, error=%s",
            underlying_instrument_key,
            selected_expiry,
            status_code,
            error_message,
        )

        return {
            "status": "failed",
            "success": False,
            "message": ("Upstox option-chain API request failed."),
            "underlying_instrument_key": (underlying_instrument_key),
            "expiry_date": selected_expiry,
            "expiry_source": expiry_source,
            "nearest_expiry": options_cache.get(
                "nearest_expiry",
            ),
            "status_code": status_code,
            "option_chain_count": 0,
            "option_chain": [],
            "error": error_message,
        }

    except Exception as ex:
        error_message = f"{type(ex).__name__}: {ex}"

        logger.exception(
            "Unexpected option-chain request failure. "
            "instrument_key=%s, expiry_date=%s, error=%s",
            underlying_instrument_key,
            selected_expiry,
            error_message,
        )

        return {
            "status": "failed",
            "success": False,
            "message": ("Unexpected option-chain request failure."),
            "underlying_instrument_key": (underlying_instrument_key),
            "expiry_date": selected_expiry,
            "expiry_source": expiry_source,
            "nearest_expiry": options_cache.get(
                "nearest_expiry",
            ),
            "option_chain_count": 0,
            "option_chain": [],
            "error": error_message,
        }


# ============================================================
# Option Chain Instrument Conversion
# ============================================================


def _build_option_chain_instrument(
    *,
    chain_item: dict,
    option_type: str,
    loaded_instrument_map: dict,
) -> dict | None:
    """
    Converts one call_options or put_options object into the
    project's standard instrument format.

    Option-chain market data is returned directly. It is not
    labelled as a candle because option-chain data is a market
    snapshot rather than an OHLC intraday candle.
    """

    if not isinstance(chain_item, dict):
        return None

    option_field = "call_options" if option_type == "CE" else "put_options"

    option_data = chain_item.get(option_field)

    if not isinstance(option_data, dict):
        return None

    instrument_key = str(option_data.get("instrument_key") or "").strip()

    if not instrument_key:
        return None

    strike_price = _safe_float(chain_item.get("strike_price"))

    if strike_price is None:
        return None

    market_data = option_data.get("market_data")

    if not isinstance(market_data, dict):
        market_data = {}

    option_greeks = option_data.get("option_greeks")

    if not isinstance(option_greeks, dict):
        option_greeks = {}

    loaded_instrument = loaded_instrument_map.get(
        instrument_key,
        {},
    )

    if not isinstance(
        loaded_instrument,
        dict,
    ):
        loaded_instrument = {}

    expiry = _normalize_expiry_date(chain_item.get("expiry"))

    if not expiry:
        expiry = _normalize_expiry_date(loaded_instrument.get("expiry"))

    option_ltp = _safe_float(market_data.get("ltp"))

    return {
        "instrument_key": instrument_key,
        "instrument_type": option_type,
        "option_type": option_type,
        "strike_price": strike_price,
        "expiry": expiry,
        "trading_symbol": loaded_instrument.get("trading_symbol"),
        "underlying_type": loaded_instrument.get("underlying_type"),
        "underlying_symbol": loaded_instrument.get("underlying_symbol"),
        "lot_size": loaded_instrument.get("lot_size"),
        "underlying_key": chain_item.get("underlying_key"),
        "underlying_spot_price": _safe_float(chain_item.get("underlying_spot_price")),
        "pcr": _safe_float(chain_item.get("pcr")),
        "ltp": option_ltp,
        "close_price": _safe_float(market_data.get("close_price")),
        "market_data": market_data,
        "option_greeks": option_greeks,
        "data_source": "upstox_option_chain",
    }


def _extract_option_type_instruments(
    *,
    option_chain: list,
    option_type: str,
) -> list:
    """
    Extracts CE or PE instruments from the option-chain response.
    """

    loaded_instrument_map = _get_loaded_instrument_map()

    instruments = []

    for chain_item in option_chain:
        instrument = _build_option_chain_instrument(
            chain_item=chain_item,
            option_type=option_type,
            loaded_instrument_map=(loaded_instrument_map),
        )

        if instrument is None:
            continue

        instruments.append(instrument)

    instruments.sort(
        key=lambda instrument: (
            _safe_float(
                instrument.get("strike_price"),
                0.0,
            ),
            str(instrument.get("instrument_key") or ""),
        )
    )

    return instruments


def _get_option_chain_spot_price(
    option_chain: list,
) -> float | None:
    """
    Returns the first valid underlying spot price from the chain.
    """

    for chain_item in option_chain:
        if not isinstance(chain_item, dict):
            continue

        spot_price = _safe_float(chain_item.get("underlying_spot_price"))

        if spot_price is not None and spot_price > 0:
            return spot_price

    return None


# ============================================================
# Nearest Instrument Filtering
# ============================================================


def _select_nearest_instruments(
    *,
    instruments: list,
    underlying_spot_price: float,
    count: int,
) -> list:
    """
    Selects instruments whose strikes are nearest to spot price.

    Instruments are selected by absolute strike distance and then
    returned in ascending strike order.
    """

    if count <= 0:
        return []

    valid_instruments = []

    for instrument in instruments:
        if not isinstance(instrument, dict):
            continue

        strike_price = _safe_float(instrument.get("strike_price"))

        if strike_price is None:
            continue

        valid_instruments.append(instrument)

    nearest_by_distance = sorted(
        valid_instruments,
        key=lambda instrument: (
            abs(
                _safe_float(
                    instrument.get("strike_price"),
                    0.0,
                )
                - underlying_spot_price
            ),
            _safe_float(
                instrument.get("strike_price"),
                0.0,
            ),
        ),
    )

    selected_instruments = nearest_by_distance[:count]

    selected_instruments.sort(
        key=lambda instrument: (
            _safe_float(
                instrument.get("strike_price"),
                0.0,
            )
        )
    )

    return selected_instruments


# ============================================================
# Budget Range Filtering
# ============================================================


def _sort_budget_instruments(
    *,
    instruments: list,
    underlying_spot_price: float,
    budget_min_price: float,
    budget_max_price: float,
    sort_mode: str,
) -> list:
    """
    Sorts budget-range instruments using the configured mode.
    """

    budget_midpoint = (budget_min_price + budget_max_price) / 2.0

    if sort_mode == "strike_ascending":
        return sorted(
            instruments,
            key=lambda instrument: (
                _safe_float(
                    instrument.get("strike_price"),
                    0.0,
                )
            ),
        )

    if sort_mode == "ltp_ascending":
        return sorted(
            instruments,
            key=lambda instrument: (
                _safe_float(
                    instrument.get("ltp"),
                    float("inf"),
                )
            ),
        )

    if sort_mode == "ltp_descending":
        return sorted(
            instruments,
            key=lambda instrument: (
                -_safe_float(
                    instrument.get("ltp"),
                    0.0,
                )
            ),
        )

    return sorted(
        instruments,
        key=lambda instrument: (
            abs(
                _safe_float(
                    instrument.get("ltp"),
                    budget_midpoint,
                )
                - budget_midpoint
            ),
            abs(
                _safe_float(
                    instrument.get("strike_price"),
                    underlying_spot_price,
                )
                - underlying_spot_price
            ),
            _safe_float(
                instrument.get("strike_price"),
                0.0,
            ),
        ),
    )


def _select_budget_range_instruments(
    *,
    instruments: list,
    underlying_spot_price: float,
    budget_min_price: float,
    budget_max_price: float,
    maximum_instruments: int,
    sort_mode: str,
) -> list:
    """
    Filters instruments whose option-chain LTP is inside the
    configured budget range.

    Both minimum and maximum values are inclusive.
    """

    if budget_min_price > budget_max_price:
        budget_min_price, budget_max_price = (
            budget_max_price,
            budget_min_price,
        )

    matched_instruments = []

    for instrument in instruments:
        if not isinstance(instrument, dict):
            continue

        option_ltp = _safe_float(instrument.get("ltp"))

        if option_ltp is None:
            continue

        if option_ltp <= 0:
            continue

        if not (budget_min_price <= option_ltp <= budget_max_price):
            continue

        matched_instruments.append(instrument)

    sorted_instruments = _sort_budget_instruments(
        instruments=matched_instruments,
        underlying_spot_price=(underlying_spot_price),
        budget_min_price=budget_min_price,
        budget_max_price=budget_max_price,
        sort_mode=sort_mode,
    )

    if maximum_instruments <= 0:
        return sorted_instruments

    return sorted_instruments[:maximum_instruments]


# ============================================================
# Combined Nearest and Budget Result
# ============================================================


def _build_filtered_option_chain_result(
    *,
    status: str,
    option_type: str | None,
    requested_count: int,
    expiry_date: str | None = None,
    expiry_source: str | None = None,
    underlying_instrument_key: str | None = None,
    underlying_spot_price: float | None = None,
    total_chain_items: int = 0,
    available_instruments: list | None = None,
    nearest_instruments: list | None = None,
    budget_instruments: list | None = None,
    budget_min_price: float = 20.0,
    budget_max_price: float = 30.0,
    budget_max_instruments: int = 10,
    budget_sort_mode: str = "nearest_price",
    error: str | None = None,
) -> dict:
    """
    Builds the final nearest and budget-range result.
    """

    normalized_available = (
        available_instruments
        if isinstance(
            available_instruments,
            list,
        )
        else []
    )

    normalized_nearest = (
        nearest_instruments
        if isinstance(
            nearest_instruments,
            list,
        )
        else []
    )

    normalized_budget = (
        budget_instruments
        if isinstance(
            budget_instruments,
            list,
        )
        else []
    )

    nearest_strikes = [
        instrument.get("strike_price")
        for instrument in normalized_nearest
        if isinstance(instrument, dict)
    ]

    return {
        "status": status,
        "success": status == "success",
        "message": (
            "Nearest and budget-range option instruments "
            "returned successfully from the option chain."
            if status == "success"
            else (error or "Could not return filtered option instruments.")
        ),
        "data_source": "upstox_option_chain",
        "underlying_instrument_key": (underlying_instrument_key),
        "underlying_spot_price": (underlying_spot_price),
        "expiry_date": expiry_date,
        "expiry_source": expiry_source,
        "nearest_expiry": options_cache.get("nearest_expiry"),
        "option_type": option_type,
        "option_chain_count": total_chain_items,
        "available_instruments_count": len(normalized_available),
        "nearest": {
            "requested_count": requested_count,
            "returned_count": len(normalized_nearest),
            "strikes": nearest_strikes,
            "instruments": normalized_nearest,
        },
        "budget_range": {
            "enabled": bool(
                getattr(
                    config,
                    "EMA_ALERT_BUDGET_RANGE_ENABLED",
                    True,
                )
            ),
            "minimum_price": budget_min_price,
            "maximum_price": budget_max_price,
            "range_inclusive": True,
            "maximum_instruments": (budget_max_instruments),
            "sort_mode": budget_sort_mode,
            "returned_count": len(normalized_budget),
            "instruments": normalized_budget,
        },
        "error": error,
    }


def get_nearest_option_instruments(
    option_type: str,
    count: int | None = None,
    unit: str | None = None,
    interval: str | None = None,
    expiry_date: str | None = None,
) -> dict:
    """
    Fetches the option chain once and returns:

        1. Nearest CE or PE instruments by strike distance
        2. CE or PE instruments whose option LTP is within the
           configured budget price range

    The unit and interval parameters remain for backward
    compatibility. They are not used because this function now
    uses option-chain market data instead of intraday candles.
    """

    del unit
    del interval

    normalized_option_type = _normalize_option_type(option_type)

    if normalized_option_type is None:
        error_message = "Invalid option type. Supported values are CE and PE."

        return _build_filtered_option_chain_result(
            status="failed",
            option_type=None,
            requested_count=0,
            error=error_message,
        )

    if count is None:
        requested_count = _get_default_nearest_instruments_count()
    else:
        requested_count = _safe_int(
            count,
            0,
        )

    if requested_count <= 0:
        error_message = "Nearest instruments count must be greater than zero."

        return _build_filtered_option_chain_result(
            status="failed",
            option_type=normalized_option_type,
            requested_count=requested_count,
            error=error_message,
        )

    budget_min_price = _get_budget_min_price()

    budget_max_price = _get_budget_max_price()

    if budget_min_price > budget_max_price:
        budget_min_price, budget_max_price = (
            budget_max_price,
            budget_min_price,
        )

    budget_max_instruments = _get_budget_max_instruments()

    budget_sort_mode = _get_budget_sort_mode()

    logger.info(
        "Filtered option-chain request started. "
        "option_type=%s, nearest_count=%s, "
        "budget_min=%s, budget_max=%s, "
        "budget_max_instruments=%s",
        normalized_option_type,
        requested_count,
        budget_min_price,
        budget_max_price,
        budget_max_instruments,
    )

    option_chain_result = get_main_index_option_chain(
        expiry_date=expiry_date,
    )

    if not option_chain_result.get("success"):
        error_message = (
            option_chain_result.get("error") or "Option-chain request failed."
        )

        return _build_filtered_option_chain_result(
            status="failed",
            option_type=normalized_option_type,
            requested_count=requested_count,
            expiry_date=(option_chain_result.get("expiry_date")),
            expiry_source=(option_chain_result.get("expiry_source")),
            underlying_instrument_key=(
                option_chain_result.get("underlying_instrument_key")
            ),
            budget_min_price=budget_min_price,
            budget_max_price=budget_max_price,
            budget_max_instruments=(budget_max_instruments),
            budget_sort_mode=budget_sort_mode,
            error=str(error_message),
        )

    option_chain = option_chain_result.get(
        "option_chain",
        [],
    )

    if not isinstance(
        option_chain,
        list,
    ):
        option_chain = []

    if not option_chain:
        error_message = "The option-chain response does not contain any data."

        return _build_filtered_option_chain_result(
            status="empty",
            option_type=normalized_option_type,
            requested_count=requested_count,
            expiry_date=(option_chain_result.get("expiry_date")),
            expiry_source=(option_chain_result.get("expiry_source")),
            underlying_instrument_key=(
                option_chain_result.get("underlying_instrument_key")
            ),
            budget_min_price=budget_min_price,
            budget_max_price=budget_max_price,
            budget_max_instruments=(budget_max_instruments),
            budget_sort_mode=budget_sort_mode,
            error=error_message,
        )

    underlying_spot_price = _get_option_chain_spot_price(option_chain)

    if underlying_spot_price is None or underlying_spot_price <= 0:
        error_message = (
            "The option chain does not contain a valid " "underlying spot price."
        )

        return _build_filtered_option_chain_result(
            status="failed",
            option_type=normalized_option_type,
            requested_count=requested_count,
            expiry_date=(option_chain_result.get("expiry_date")),
            expiry_source=(option_chain_result.get("expiry_source")),
            underlying_instrument_key=(
                option_chain_result.get("underlying_instrument_key")
            ),
            total_chain_items=len(option_chain),
            budget_min_price=budget_min_price,
            budget_max_price=budget_max_price,
            budget_max_instruments=(budget_max_instruments),
            budget_sort_mode=budget_sort_mode,
            error=error_message,
        )

    available_instruments = _extract_option_type_instruments(
        option_chain=option_chain,
        option_type=normalized_option_type,
    )

    if not available_instruments:
        error_message = (
            f"No {normalized_option_type} instruments were "
            f"found in the option-chain response."
        )

        return _build_filtered_option_chain_result(
            status="empty",
            option_type=normalized_option_type,
            requested_count=requested_count,
            expiry_date=(option_chain_result.get("expiry_date")),
            expiry_source=(option_chain_result.get("expiry_source")),
            underlying_instrument_key=(
                option_chain_result.get("underlying_instrument_key")
            ),
            underlying_spot_price=(underlying_spot_price),
            total_chain_items=len(option_chain),
            budget_min_price=budget_min_price,
            budget_max_price=budget_max_price,
            budget_max_instruments=(budget_max_instruments),
            budget_sort_mode=budget_sort_mode,
            error=error_message,
        )

    nearest_instruments = _select_nearest_instruments(
        instruments=available_instruments,
        underlying_spot_price=(underlying_spot_price),
        count=requested_count,
    )

    budget_enabled = bool(
        getattr(
            config,
            "EMA_ALERT_BUDGET_RANGE_ENABLED",
            True,
        )
    )

    if budget_enabled:
        budget_instruments = _select_budget_range_instruments(
            instruments=available_instruments,
            underlying_spot_price=(underlying_spot_price),
            budget_min_price=(budget_min_price),
            budget_max_price=(budget_max_price),
            maximum_instruments=(budget_max_instruments),
            sort_mode=budget_sort_mode,
        )
    else:
        budget_instruments = []

    logger.info(
        "Filtered option-chain request completed. "
        "option_type=%s, spot_price=%s, "
        "available=%s, nearest=%s, budget=%s",
        normalized_option_type,
        underlying_spot_price,
        len(available_instruments),
        len(nearest_instruments),
        len(budget_instruments),
    )

    return _build_filtered_option_chain_result(
        status="success",
        option_type=normalized_option_type,
        requested_count=requested_count,
        expiry_date=(option_chain_result.get("expiry_date")),
        expiry_source=(option_chain_result.get("expiry_source")),
        underlying_instrument_key=(
            option_chain_result.get("underlying_instrument_key")
        ),
        underlying_spot_price=(underlying_spot_price),
        total_chain_items=len(option_chain),
        available_instruments=(available_instruments),
        nearest_instruments=(nearest_instruments),
        budget_instruments=(budget_instruments),
        budget_min_price=budget_min_price,
        budget_max_price=budget_max_price,
        budget_max_instruments=(budget_max_instruments),
        budget_sort_mode=budget_sort_mode,
        error=None,
    )


def get_nearest_option_instruments_list(
    option_type: str,
    count: int | None = None,
    unit: str | None = None,
    interval: str | None = None,
    expiry_date: str | None = None,
) -> list:
    """
    Returns only the nearest instrument list.

    This helper is maintained for backward compatibility.
    """

    result = get_nearest_option_instruments(
        option_type=option_type,
        count=count,
        unit=unit,
        interval=interval,
        expiry_date=expiry_date,
    )

    if not result.get("success"):
        return []

    nearest = result.get(
        "nearest",
        {},
    )

    if not isinstance(nearest, dict):
        return []

    instruments = nearest.get(
        "instruments",
        [],
    )

    if not isinstance(instruments, list):
        return []

    return instruments


def get_budget_range_option_instruments_list(
    option_type: str,
    expiry_date: str | None = None,
) -> list:
    """
    Returns only budget-range instruments from the option chain.
    """

    result = get_nearest_option_instruments(
        option_type=option_type,
        expiry_date=expiry_date,
    )

    if not result.get("success"):
        return []

    budget_range = result.get(
        "budget_range",
        {},
    )

    if not isinstance(
        budget_range,
        dict,
    ):
        return []

    instruments = budget_range.get(
        "instruments",
        [],
    )

    if not isinstance(instruments, list):
        return []

    return instruments


# ============================================================
# Public API
# ============================================================


__all__ = [
    "get_main_index_ltp",
    "get_main_index_ltp_value",
    "get_main_index_option_chain",
    "get_nearest_option_instruments",
    "get_nearest_option_instruments_list",
    "get_budget_range_option_instruments_list",
]


# # ============================================================
# # Manual Testing
# # ============================================================


# if __name__ == "__main__":
#     import json

#     test_result = get_nearest_option_instruments(
#         option_type="CE",
#     )

#     print(
#         json.dumps(
#             test_result,
#             indent=2,
#             default=str,
#         )
#     )
