from datetime import date, datetime
from typing import Any

import upstox_client
from upstox_client.rest import ApiException

from core import config
from core.logger import get_logger
from services.opening_range.intraday import fetch_intraday_candles_for_instrument
from services.option_service import options_cache
from services.token_service import token_service

logger = get_logger(__file__)

# ============================================================
# Value Conversion Helpers
# ============================================================

def _safe_float(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None: return default
        return float(value)
    except (TypeError, ValueError, OverflowError): return default

def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None: return default
        return int(float(value))
    except (TypeError, ValueError, OverflowError): return default

# ============================================================
# Option Type Helpers
# ============================================================

def _normalize_option_type(option_type: Any) -> str | None:
    normalized_value = str(option_type or "").strip().upper()
    mapping = {"CE": "CE", "CALL": "CE", "C": "CE", "PE": "PE", "PUT": "PE", "P": "PE"}
    return mapping.get(normalized_value)

# ============================================================
# Configuration Helpers
# ============================================================

def _get_default_nearest_instruments_count() -> int:
    count = _safe_int(getattr(config, "MAIN_INDEX_NEAREST_INSTRUMENTS_COUNT", 3), 3)
    return count if count > 0 else 3

def _get_budget_min_price() -> float:
    minimum_price = _safe_float(getattr(config, "EMA_ALERT_BUDGET_MIN_PRICE", 20.0), 20.0)
    return 20.0 if minimum_price is None else minimum_price

def _get_budget_max_price() -> float:
    maximum_price = _safe_float(getattr(config, "EMA_ALERT_BUDGET_MAX_PRICE", 30.0), 30.0)
    return 30.0 if maximum_price is None else maximum_price

def _get_budget_max_instruments() -> int:
    maximum_instruments = _safe_int(getattr(config, "EMA_ALERT_BUDGET_MAX_INSTRUMENTS", 10), 10)
    return maximum_instruments if maximum_instruments > 0 else 10

def _get_budget_sort_mode() -> str:
    sort_mode = str(getattr(config, "EMA_ALERT_BUDGET_SORT_MODE", "nearest_price") or "nearest_price").strip().lower()
    supported_modes = {"nearest_price", "strike_ascending", "ltp_ascending", "ltp_descending"}
    return sort_mode if sort_mode in supported_modes else "nearest_price"

# ============================================================
# JSON-Safe SDK Conversion
# ============================================================

def _convert_sdk_response_to_dict(value: Any) -> Any:
    if value is None: return None
    if isinstance(value, (str, int, float, bool)): return value
    if isinstance(value, (datetime, date)): return value.isoformat()
    if isinstance(value, dict): return {str(key): _convert_sdk_response_to_dict(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)): return [_convert_sdk_response_to_dict(item) for item in value]
    if hasattr(value, "to_dict"):
        try: return _convert_sdk_response_to_dict(value.to_dict())
        except Exception: pass
    if hasattr(value, "__dict__"):
        try:
            return {str(key): _convert_sdk_response_to_dict(item) for key, item in vars(value).items() if not str(key).startswith("_")}
        except Exception: pass
    return str(value)

# ============================================================
# Expiry Helpers
# ============================================================

def _normalize_expiry_date(expiry: Any) -> str | None:
    if expiry is None: return None
    if isinstance(expiry, datetime): return expiry.date().isoformat()
    if isinstance(expiry, date): return expiry.isoformat()
    expiry_text = str(expiry).strip()
    if not expiry_text: return None
    try: return datetime.fromisoformat(expiry_text.replace("Z", "+00:00")).date().isoformat()
    except ValueError: pass
    try: return date.fromisoformat(expiry_text[:10]).isoformat()
    except ValueError: return None

def _get_loaded_nearest_expiry() -> str | None:
    cached_expiry = _normalize_expiry_date(options_cache.get("nearest_expiry"))
    if cached_expiry: return cached_expiry
    loaded_instruments = options_cache.get("data", [])
    if not isinstance(loaded_instruments, list): return None
    expiry_values = set()
    for instrument in loaded_instruments:
        if isinstance(instrument, dict):
            expiry = _normalize_expiry_date(instrument.get("expiry"))
            if expiry: expiry_values.add(expiry)
    if not expiry_values: return None
    return sorted(expiry_values)[0]

# ============================================================
# Loaded Instrument Lookup
# ============================================================

def _get_loaded_instrument_by_key(instrument_key: str) -> dict | None:
    normalized_key = str(instrument_key or "").strip()
    if not normalized_key: return None
    loaded_instruments = options_cache.get("data", [])
    if not isinstance(loaded_instruments, list): return None
    for instrument in loaded_instruments:
        if isinstance(instrument, dict) and str(instrument.get("instrument_key") or "").strip() == normalized_key:
            return instrument
    return None

def _get_loaded_instrument_map() -> dict:
    loaded_instruments = options_cache.get("data", [])
    if not isinstance(loaded_instruments, list): return {}
    instrument_map = {}
    for instrument in loaded_instruments:
        if isinstance(instrument, dict):
            instrument_key = str(instrument.get("instrument_key") or "").strip()
            if instrument_key: instrument_map[instrument_key] = instrument
    return instrument_map

# ============================================================
# Main Index Intraday Candle Helpers
# ============================================================

def _get_latest_candle(candles: list) -> dict | None:
    if not isinstance(candles, list) or not candles: return None
    latest_candle = candles[-1]
    return latest_candle if isinstance(latest_candle, dict) else None

def _build_main_index_ltp_result(*, status: str, instrument_key: str, unit: str, interval: str, ltp: float | None = None, latest_candle: dict | None = None, candles_count: int = 0, error: str | None = None) -> dict:
    return {"status": status, "success": status == "success", "instrument_key": instrument_key, "ltp": ltp, "source": "latest_intraday_candle_close", "unit": unit, "interval": interval, "candles_count": candles_count, "latest_candle": latest_candle, "error": error}

def get_main_index_ltp(unit: str | None = None, interval: str | None = None) -> dict:
    instrument_key = str(getattr(config, "MAIN_NIFTY_SECURITY", "NSE_INDEX|Nifty 50") or "").strip()
    selected_unit = str(unit or getattr(config, "OPENING_RANGE_INTRADAY_UNIT", "minutes") or "minutes").strip()
    selected_interval = str(interval or getattr(config, "OPENING_RANGE_INTRADAY_INTERVAL", "1") or "1").strip()
    if not instrument_key:
        return _build_main_index_ltp_result(status="failed", instrument_key=instrument_key, unit=selected_unit, interval=selected_interval, error="MAIN_NIFTY_SECURITY is not configured.")
    logger.info("Main index LTP fetch started. instrument_key=%s, unit=%s, interval=%s", instrument_key, selected_unit, selected_interval)
    try:
        intraday_result = fetch_intraday_candles_for_instrument(instrument_key=instrument_key, unit=selected_unit, interval=selected_interval)
        if not isinstance(intraday_result, dict):
            return _build_main_index_ltp_result(status="failed", instrument_key=instrument_key, unit=selected_unit, interval=selected_interval, error="Intraday candle service returned an invalid response.")
        candles = intraday_result.get("candles", [])
        if not isinstance(candles, list): candles = []
        candles_count = len(candles)
        intraday_status = str(intraday_result.get("status") or "").lower()
        if intraday_status == "failed":
            return _build_main_index_ltp_result(status="failed", instrument_key=instrument_key, unit=selected_unit, interval=selected_interval, candles_count=candles_count, error=str(intraday_result.get("error") or "Intraday candle fetch failed."))
        latest_candle = _get_latest_candle(candles)
        if latest_candle is None:
            return _build_main_index_ltp_result(status="empty", instrument_key=instrument_key, unit=selected_unit, interval=selected_interval, candles_count=candles_count, error="No intraday candles are available for the configured main index.")
        latest_close = _safe_float(latest_candle.get("close"))
        if latest_close is None or latest_close <= 0:
            return _build_main_index_ltp_result(status="failed", instrument_key=instrument_key, unit=selected_unit, interval=selected_interval, candles_count=candles_count, latest_candle=latest_candle, error="The latest main index candle does not contain a valid close price.")
        logger.info("Main index LTP fetched successfully. instrument_key=%s, ltp=%s, candles_count=%s", instrument_key, latest_close, candles_count)
        return _build_main_index_ltp_result(status="success", instrument_key=instrument_key, unit=selected_unit, interval=selected_interval, ltp=latest_close, latest_candle=latest_candle, candles_count=candles_count, error=None)
    except Exception as ex:
        error_message = f"{type(ex).__name__}: {ex}"
        logger.exception("Unexpected error while fetching main index LTP. instrument_key=%s, error=%s", instrument_key, error_message)
        return _build_main_index_ltp_result(status="failed", instrument_key=instrument_key, unit=selected_unit, interval=selected_interval, error=error_message)

def get_main_index_ltp_value(unit: str | None = None, interval: str | None = None) -> float | None:
    result = get_main_index_ltp(unit=unit, interval=interval)
    return _safe_float(result.get("ltp")) if result.get("success") else None

# ============================================================
# Option Chain API Helpers
# ============================================================

def _get_api_exception_message(exception: ApiException) -> str:
    body = getattr(exception, "body", None)
    if body: return str(body)
    reason = getattr(exception, "reason", None)
    return str(reason) if reason else str(exception)

def get_main_index_option_chain(expiry_date: str | None = None) -> dict:
    underlying_instrument_key = str(getattr(config, "MAIN_NIFTY_SECURITY", "NSE_INDEX|Nifty 50") or "").strip()
    if not underlying_instrument_key:
        error_message = "MAIN_NIFTY_SECURITY is not configured."
        return {"status": "failed", "success": False, "message": error_message, "underlying_instrument_key": None, "expiry_date": None, "expiry_source": None, "nearest_expiry": None, "option_chain_count": 0, "option_chain": [], "error": error_message}
    requested_expiry = _normalize_expiry_date(expiry_date) if expiry_date is not None else None
    if expiry_date is not None and not requested_expiry:
        error_message = "Invalid expiry_date. Expected YYYY-MM-DD format."
        return {"status": "failed", "success": False, "message": error_message, "underlying_instrument_key": underlying_instrument_key, "expiry_date": None, "expiry_source": "query_parameter", "nearest_expiry": options_cache.get("nearest_expiry"), "option_chain_count": 0, "option_chain": [], "error": error_message}
    if requested_expiry:
        selected_expiry = requested_expiry; expiry_source = "query_parameter"
    else:
        selected_expiry = _get_loaded_nearest_expiry(); expiry_source = "options_cache"
    if not selected_expiry:
        error_message = "No valid option expiry is available in options_cache."
        return {"status": "empty", "success": False, "message": error_message, "underlying_instrument_key": underlying_instrument_key, "expiry_date": None, "expiry_source": expiry_source, "nearest_expiry": options_cache.get("nearest_expiry"), "option_chain_count": 0, "option_chain": [], "error": error_message}
    access_token = token_service.get_access_token()
    if not access_token:
        logger.info("No access token found in memory. Refreshing token cache before option-chain request.")
        token_service.refresh_tokens()
        access_token = token_service.get_access_token()
    if not access_token:
        error_message = "No Upstox access token is available."
        return {"status": "failed", "success": False, "message": error_message, "underlying_instrument_key": underlying_instrument_key, "expiry_date": selected_expiry, "expiry_source": expiry_source, "nearest_expiry": options_cache.get("nearest_expiry"), "option_chain_count": 0, "option_chain": [], "error": error_message}
    logger.info("Option chain request started. instrument_key=%s, expiry_date=%s, expiry_source=%s", underlying_instrument_key, selected_expiry, expiry_source)
    try:
        configuration = upstox_client.Configuration()
        configuration.access_token = access_token
        api_client = upstox_client.ApiClient(configuration)
        api_instance = upstox_client.OptionsApi(api_client)
        api_response = api_instance.get_put_call_option_chain(underlying_instrument_key, selected_expiry)
        converted_response = _convert_sdk_response_to_dict(api_response)
        option_chain = []
        if isinstance(converted_response, dict):
            response_data = converted_response.get("data")
            if isinstance(response_data, list): option_chain = response_data
            elif response_data is not None: option_chain = [response_data]
        elif isinstance(converted_response, list): option_chain = converted_response
        logger.info("Option chain request completed. instrument_key=%s, expiry_date=%s, option_chain_count=%s", underlying_instrument_key, selected_expiry, len(option_chain))
        return {"status": "success", "success": True, "message": "Option chain returned successfully.", "underlying_instrument_key": underlying_instrument_key, "expiry_date": selected_expiry, "expiry_source": expiry_source, "nearest_expiry": options_cache.get("nearest_expiry"), "option_chain_count": len(option_chain), "option_chain": option_chain, "error": None}
    except ApiException as ex:
        error_message = _get_api_exception_message(ex)
        status_code = getattr(ex, "status", None)
        logger.error("Upstox option-chain request failed. instrument_key=%s, expiry_date=%s, status_code=%s, error=%s", underlying_instrument_key, selected_expiry, status_code, error_message)
        return {"status": "failed", "success": False, "message": "Upstox option-chain API request failed.", "underlying_instrument_key": underlying_instrument_key, "expiry_date": selected_expiry, "expiry_source": expiry_source, "nearest_expiry": options_cache.get("nearest_expiry"), "status_code": status_code, "option_chain_count": 0, "option_chain": [], "error": error_message}
    except Exception as ex:
        error_message = f"{type(ex).__name__}: {ex}"
        logger.exception("Unexpected option-chain request failure. instrument_key=%s, expiry_date=%s, error=%s", underlying_instrument_key, selected_expiry, error_message)
        return {"status": "failed", "success": False, "message": "Unexpected option-chain request failure.", "underlying_instrument_key": underlying_instrument_key, "expiry_date": selected_expiry, "expiry_source": expiry_source, "nearest_expiry": options_cache.get("nearest_expiry"), "option_chain_count": 0, "option_chain": [], "error": error_message}

# ============================================================
# Option Chain Instrument Conversion
# ============================================================

def _build_option_chain_instrument(*, chain_item: dict, option_type: str, loaded_instrument_map: dict) -> dict | None:
    if not isinstance(chain_item, dict): return None
    option_field = "call_options" if option_type == "CE" else "put_options"
    option_data = chain_item.get(option_field)
    if not isinstance(option_data, dict): return None
    instrument_key = str(option_data.get("instrument_key") or "").strip()
    if not instrument_key: return None
    strike_price = _safe_float(chain_item.get("strike_price"))
    if strike_price is None: return None
    market_data = option_data.get("market_data") if isinstance(option_data.get("market_data"), dict) else {}
    option_greeks = option_data.get("option_greeks") if isinstance(option_data.get("option_greeks"), dict) else {}
    loaded_instrument = loaded_instrument_map.get(instrument_key, {}) if isinstance(loaded_instrument_map.get(instrument_key), dict) else {}
    expiry = _normalize_expiry_date(chain_item.get("expiry")) or _normalize_expiry_date(loaded_instrument.get("expiry"))
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

def _extract_option_type_instruments(*, option_chain: list, option_type: str) -> list:
    loaded_instrument_map = _get_loaded_instrument_map()
    instruments = []
    for chain_item in option_chain:
        instrument = _build_option_chain_instrument(chain_item=chain_item, option_type=option_type, loaded_instrument_map=loaded_instrument_map)
        if instrument is not None: instruments.append(instrument)
    instruments.sort(key=lambda inst: (_safe_float(inst.get("strike_price"), 0.0), str(inst.get("instrument_key") or "")))
    return instruments

def _get_option_chain_spot_price(option_chain: list) -> float | None:
    for chain_item in option_chain:
        if isinstance(chain_item, dict):
            spot_price = _safe_float(chain_item.get("underlying_spot_price"))
            if spot_price is not None and spot_price > 0: return spot_price
    return None

# ============================================================
# Nearest Instrument Filtering
# ============================================================

def _select_nearest_instruments(*, instruments: list, underlying_spot_price: float, count: int) -> list:
    if count <= 0: return []
    valid_instruments = []
    for instrument in instruments:
        if isinstance(instrument, dict) and _safe_float(instrument.get("strike_price")) is not None:
            valid_instruments.append(instrument)
    nearest_by_distance = sorted(valid_instruments, key=lambda inst: (abs(_safe_float(inst.get("strike_price"), 0.0) - underlying_spot_price), _safe_float(inst.get("strike_price"), 0.0)))
    selected = nearest_by_distance[:count]
    selected.sort(key=lambda inst: _safe_float(inst.get("strike_price"), 0.0))
    return selected

# ============================================================
# Budget Range Filtering
# ============================================================

def _sort_budget_instruments(*, instruments: list, underlying_spot_price: float, budget_min_price: float, budget_max_price: float, sort_mode: str) -> list:
    budget_midpoint = (budget_min_price + budget_max_price) / 2.0
    if sort_mode == "strike_ascending":
        return sorted(instruments, key=lambda inst: _safe_float(inst.get("strike_price"), 0.0))
    if sort_mode == "ltp_ascending":
        return sorted(instruments, key=lambda inst: _safe_float(inst.get("ltp"), float("inf")))
    if sort_mode == "ltp_descending":
        return sorted(instruments, key=lambda inst: -_safe_float(inst.get("ltp"), 0.0))
    return sorted(instruments, key=lambda inst: (abs(_safe_float(inst.get("ltp"), budget_midpoint) - budget_midpoint), abs(_safe_float(inst.get("strike_price"), underlying_spot_price) - underlying_spot_price), _safe_float(inst.get("strike_price"), 0.0)))

def _select_budget_range_instruments(*, instruments: list, underlying_spot_price: float, budget_min_price: float, budget_max_price: float, maximum_instruments: int, sort_mode: str) -> list:
    if budget_min_price > budget_max_price:
        budget_min_price, budget_max_price = budget_max_price, budget_min_price
    matched = []
    for instrument in instruments:
        if isinstance(instrument, dict):
            option_ltp = _safe_float(instrument.get("ltp"))
            if option_ltp is not None and option_ltp > 0 and budget_min_price <= option_ltp <= budget_max_price:
                matched.append(instrument)
    sorted_instruments = _sort_budget_instruments(instruments=matched, underlying_spot_price=underlying_spot_price, budget_min_price=budget_min_price, budget_max_price=budget_max_price, sort_mode=sort_mode)
    return sorted_instruments if maximum_instruments <= 0 else sorted_instruments[:maximum_instruments]

# ============================================================
# Combined Nearest and Budget Result
# ============================================================

def _build_filtered_option_chain_result(*, status: str, option_type: str | None, requested_count: int, expiry_date: str | None = None, expiry_source: str | None = None, underlying_instrument_key: str | None = None, underlying_spot_price: float | None = None, total_chain_items: int = 0, available_instruments: list | None = None, nearest_instruments: list | None = None, budget_instruments: list | None = None, budget_min_price: float = 20.0, budget_max_price: float = 30.0, budget_max_instruments: int = 10, budget_sort_mode: str = "nearest_price", error: str | None = None) -> dict:
    normalized_available = available_instruments if isinstance(available_instruments, list) else []
    normalized_nearest = nearest_instruments if isinstance(nearest_instruments, list) else []
    normalized_budget = budget_instruments if isinstance(budget_instruments, list) else []
    nearest_strikes = [inst.get("strike_price") for inst in normalized_nearest if isinstance(inst, dict)]
    return {
        "status": status,
        "success": status == "success",
        "message": "Nearest and budget-range option instruments returned successfully from the option chain." if status == "success" else (error or "Could not return filtered option instruments."),
        "data_source": "upstox_option_chain",
        "underlying_instrument_key": underlying_instrument_key,
        "underlying_spot_price": underlying_spot_price,
        "expiry_date": expiry_date,
        "expiry_source": expiry_source,
        "nearest_expiry": options_cache.get("nearest_expiry"),
        "option_type": option_type,
        "option_chain_count": total_chain_items,
        "available_instruments_count": len(normalized_available),
        "nearest": {"requested_count": requested_count, "returned_count": len(normalized_nearest), "strikes": nearest_strikes, "instruments": normalized_nearest},
        "budget_range": {
            "enabled": bool(getattr(config, "EMA_ALERT_BUDGET_RANGE_ENABLED", True)),
            "minimum_price": budget_min_price,
            "maximum_price": budget_max_price,
            "range_inclusive": True,
            "maximum_instruments": budget_max_instruments,
            "sort_mode": budget_sort_mode,
            "returned_count": len(normalized_budget),
            "instruments": normalized_budget,
        },
        "error": error,
    }

def get_nearest_option_instruments(option_type: str, count: int | None = None, unit: str | None = None, interval: str | None = None, expiry_date: str | None = None) -> dict:
    del unit, interval
    normalized_option_type = _normalize_option_type(option_type)
    if normalized_option_type is None:
        return _build_filtered_option_chain_result(status="failed", option_type=None, requested_count=0, error="Invalid option type. Supported values are CE and PE.")
    requested_count = _safe_int(count, 0) if count is not None else _get_default_nearest_instruments_count()
    if requested_count <= 0:
        return _build_filtered_option_chain_result(status="failed", option_type=normalized_option_type, requested_count=requested_count, error="Nearest instruments count must be greater than zero.")
    budget_min_price = _get_budget_min_price()
    budget_max_price = _get_budget_max_price()
    if budget_min_price > budget_max_price: budget_min_price, budget_max_price = budget_max_price, budget_min_price
    budget_max_instruments = _get_budget_max_instruments()
    budget_sort_mode = _get_budget_sort_mode()
    logger.info("Filtered option-chain request started. option_type=%s, nearest_count=%s, budget_min=%s, budget_max=%s, budget_max_instruments=%s", normalized_option_type, requested_count, budget_min_price, budget_max_price, budget_max_instruments)
    option_chain_result = get_main_index_option_chain(expiry_date=expiry_date)
    if not option_chain_result.get("success"):
        error_message = option_chain_result.get("error") or "Option-chain request failed."
        return _build_filtered_option_chain_result(status="failed", option_type=normalized_option_type, requested_count=requested_count, expiry_date=option_chain_result.get("expiry_date"), expiry_source=option_chain_result.get("expiry_source"), underlying_instrument_key=option_chain_result.get("underlying_instrument_key"), budget_min_price=budget_min_price, budget_max_price=budget_max_price, budget_max_instruments=budget_max_instruments, budget_sort_mode=budget_sort_mode, error=str(error_message))
    option_chain = option_chain_result.get("option_chain", [])
    if not isinstance(option_chain, list): option_chain = []
    if not option_chain:
        return _build_filtered_option_chain_result(status="empty", option_type=normalized_option_type, requested_count=requested_count, expiry_date=option_chain_result.get("expiry_date"), expiry_source=option_chain_result.get("expiry_source"), underlying_instrument_key=option_chain_result.get("underlying_instrument_key"), budget_min_price=budget_min_price, budget_max_price=budget_max_price, budget_max_instruments=budget_max_instruments, budget_sort_mode=budget_sort_mode, error="The option-chain response does not contain any data.")
    underlying_spot_price = _get_option_chain_spot_price(option_chain)
    if underlying_spot_price is None or underlying_spot_price <= 0:
        return _build_filtered_option_chain_result(status="failed", option_type=normalized_option_type, requested_count=requested_count, expiry_date=option_chain_result.get("expiry_date"), expiry_source=option_chain_result.get("expiry_source"), underlying_instrument_key=option_chain_result.get("underlying_instrument_key"), total_chain_items=len(option_chain), budget_min_price=budget_min_price, budget_max_price=budget_max_price, budget_max_instruments=budget_max_instruments, budget_sort_mode=budget_sort_mode, error="The option chain does not contain a valid underlying spot price.")
    available_instruments = _extract_option_type_instruments(option_chain=option_chain, option_type=normalized_option_type)
    if not available_instruments:
        return _build_filtered_option_chain_result(status="empty", option_type=normalized_option_type, requested_count=requested_count, expiry_date=option_chain_result.get("expiry_date"), expiry_source=option_chain_result.get("expiry_source"), underlying_instrument_key=option_chain_result.get("underlying_instrument_key"), underlying_spot_price=underlying_spot_price, total_chain_items=len(option_chain), budget_min_price=budget_min_price, budget_max_price=budget_max_price, budget_max_instruments=budget_max_instruments, budget_sort_mode=budget_sort_mode, error=f"No {normalized_option_type} instruments were found in the option-chain response.")
    nearest_instruments = _select_nearest_instruments(instruments=available_instruments, underlying_spot_price=underlying_spot_price, count=requested_count)
    budget_enabled = bool(getattr(config, "EMA_ALERT_BUDGET_RANGE_ENABLED", True))
    budget_instruments = _select_budget_range_instruments(instruments=available_instruments, underlying_spot_price=underlying_spot_price, budget_min_price=budget_min_price, budget_max_price=budget_max_price, maximum_instruments=budget_max_instruments, sort_mode=budget_sort_mode) if budget_enabled else []
    logger.info("Filtered option-chain request completed. option_type=%s, spot_price=%s, available=%s, nearest=%s, budget=%s", normalized_option_type, underlying_spot_price, len(available_instruments), len(nearest_instruments), len(budget_instruments))
    return _build_filtered_option_chain_result(status="success", option_type=normalized_option_type, requested_count=requested_count, expiry_date=option_chain_result.get("expiry_date"), expiry_source=option_chain_result.get("expiry_source"), underlying_instrument_key=option_chain_result.get("underlying_instrument_key"), underlying_spot_price=underlying_spot_price, total_chain_items=len(option_chain), available_instruments=available_instruments, nearest_instruments=nearest_instruments, budget_instruments=budget_instruments, budget_min_price=budget_min_price, budget_max_price=budget_max_price, budget_max_instruments=budget_max_instruments, budget_sort_mode=budget_sort_mode, error=None)

def get_nearest_option_instruments_list(option_type: str, count: int | None = None, unit: str | None = None, interval: str | None = None, expiry_date: str | None = None) -> list:
    result = get_nearest_option_instruments(option_type=option_type, count=count, unit=unit, interval=interval, expiry_date=expiry_date)
    if not result.get("success"): return []
    nearest = result.get("nearest", {})
    if not isinstance(nearest, dict): return []
    instruments = nearest.get("instruments", [])
    return instruments if isinstance(instruments, list) else []

def get_budget_range_option_instruments_list(option_type: str, expiry_date: str | None = None) -> list:
    result = get_nearest_option_instruments(option_type=option_type, expiry_date=expiry_date)
    if not result.get("success"): return []
    budget_range = result.get("budget_range", {})
    if not isinstance(budget_range, dict): return []
    instruments = budget_range.get("instruments", [])
    return instruments if isinstance(instruments, list) else []

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