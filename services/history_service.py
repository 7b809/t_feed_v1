import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, date, time as dt_time
from pathlib import Path
from threading import Lock
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import upstox_client
from upstox_client.rest import ApiException

from core import config
from core.logger import get_logger
from services.option_service import options_cache

logger = get_logger(__file__)

# ============================================================
# Thread-Safe Runtime Cache
# ============================================================

_history_cache_lock = Lock()

historical_candles_cache = {
    "last_run_at": None, "from_date": None, "to_date": None, "intraday_today_used": False,
    "interval": None, "total_instruments": 0, "success_count": 0, "failed_count": 0,
    "empty_count": 0, "insufficient_data_count": 0, "total_candles": 0,
    "ema_fast_period": None, "ema_slow_period": None, "ema_results_file_path": None,
    "live_ema_initialized": False, "data": {}, "errors": {},
}

# ============================================================
# Config Defaults
# ============================================================

DEFAULT_HISTORY_DAYS = getattr(config, "HISTORICAL_CANDLE_DAYS", 10)

DEFAULT_INTERVAL = getattr(config, "HISTORICAL_CANDLE_INTERVAL", "1minute")

DEFAULT_API_VERSION = getattr(config, "HISTORICAL_CANDLE_API_VERSION", "2.0")

DEFAULT_MAX_DAYS_PER_REQUEST = getattr(config, "HISTORICAL_CANDLE_MAX_DAYS_PER_REQUEST", 7)

DEFAULT_SLEEP_SECONDS = getattr(config, "HISTORICAL_CANDLE_REQUEST_SLEEP_SECONDS", 0.15)

DEFAULT_MAX_WORKERS = getattr(config, "HISTORICAL_CANDLE_MAX_WORKERS", 5)

DEFAULT_EMA_FAST_PERIOD = getattr(config, "EMA_FAST_PERIOD", 9)

DEFAULT_EMA_SLOW_PERIOD = getattr(config, "EMA_SLOW_PERIOD", 21)

DEFAULT_EMA_OUTPUT_FILE = getattr(config, "EMA_CROSS_OUTPUT_FILE", "data/ema_cross_results.json")

DEFAULT_MARKET_OPEN_HOUR = getattr(config, "MARKET_OPEN_HOUR", 9)

DEFAULT_MARKET_OPEN_MINUTE = getattr(config, "MARKET_OPEN_MINUTE", 15)

# ============================================================
# Basic Helpers
# ============================================================

def is_historical_candle_enabled() -> bool:
    """Returns whether historical candle fetching is enabled."""
    return bool(getattr(config, "HISTORICAL_CANDLE_ENABLED", True))

def is_test_flag_enabled() -> bool:
    """Returns TEST_FLAG value."""
    return bool(getattr(config, "TEST_FLAG", False))

def get_market_timezone():
    """Loads market timezone from config."""
    timezone_name = getattr(config, "MARKET_TIMEZONE", "Asia/Kolkata")
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        logger.error(f"Invalid MARKET_TIMEZONE configured: {timezone_name}. Falling back to Asia/Kolkata.")
        return ZoneInfo("Asia/Kolkata")

def get_now_market_time() -> datetime:
    """Returns current datetime in configured market timezone."""
    return datetime.now(get_market_timezone())

def should_fetch_intraday_today(now_market_time: datetime | None = None) -> bool:
    """Determines whether today's intraday candle API should be used."""
    now_market_time = now_market_time or get_now_market_time()
    if now_market_time.weekday() >= 5: return False
    market_open_time = dt_time(hour=int(DEFAULT_MARKET_OPEN_HOUR), minute=int(DEFAULT_MARKET_OPEN_MINUTE))
    return now_market_time.time() >= market_open_time

def get_today_date() -> date:
    """Returns today's date in market timezone."""
    return get_now_market_time().date()

def format_date(value: date) -> str:
    """Formats a date object as YYYY-MM-DD."""
    return value.strftime("%Y-%m-%d")

def response_to_dict(api_response: Any) -> dict:
    """Converts Upstox SDK response object to dictionary safely."""
    if api_response is None: return {}
    if hasattr(api_response, "to_dict"): return api_response.to_dict()
    if isinstance(api_response, dict): return api_response
    try: return dict(api_response)
    except Exception: return {}

def extract_candles_from_response(api_response: Any) -> list:
    """Extracts candle list from Upstox historical or intraday candle API response."""
    response_dict = response_to_dict(api_response)
    data = response_dict.get("data", {})
    if isinstance(data, dict):
        candles = data.get("candles", [])
        return candles if isinstance(candles, list) else []
    return []

def safe_float(value, default: float = 0.0) -> float:
    """Safely converts value to float."""
    try:
        if value is None: return default
        return float(value)
    except Exception: return default

def deduplicate_candles(candles: list) -> list:
    """Deduplicates candles by timestamp."""
    seen = set()
    unique = []
    for candle in candles:
        if isinstance(candle, list) and candle:
            key = candle[0]
        else:
            key = json.dumps(candle, default=str)
        if key not in seen:
            seen.add(key)
            unique.append(candle)
    return unique

def sort_candles(candles: list) -> list:
    """Sorts candles by timestamp if possible."""
    try:
        return sorted(candles, key=lambda item: item[0] if item else "")
    except Exception:
        return candles

# ============================================================
# Interval Helpers
# ============================================================

def get_intraday_unit_and_interval(interval: str) -> tuple[str | None, str | None]:
    """Converts historical interval string to HistoryV3Api intraday parameters."""
    interval_text = str(interval or "").strip().lower()
    if interval_text.endswith("minute"):
        value = interval_text.replace("minute", "").strip()
        value = value or "1"
        return "minutes", value
    if interval_text.endswith("minutes"):
        value = interval_text.replace("minutes", "").strip()
        value = value or "1"
        return "minutes", value
    logger.warning(f"Intraday candle fetch not supported for interval={interval}. Supported examples: 1minute, 3minute, 5minute, 15minute, 30minute.")
    return None, None

# ============================================================
# Date Batch Helpers
# ============================================================

def build_date_batches(from_date: date, to_date: date, max_days_per_request: int = DEFAULT_MAX_DAYS_PER_REQUEST) -> list:
    """Splits a date range into batches."""
    if from_date > to_date: return []
    batches = []
    current_from = from_date
    while current_from <= to_date:
        current_to = min(current_from + timedelta(days=max_days_per_request - 1), to_date)
        batches.append({"from_date": format_date(current_from), "to_date": format_date(current_to)})
        current_from = current_to + timedelta(days=1)
    return batches

def get_default_history_range(history_days: int = DEFAULT_HISTORY_DAYS) -> tuple[date, date, bool]:
    """Builds default historical date range."""
    now_market_time = get_now_market_time()
    today = now_market_time.date()
    intraday_today_required = should_fetch_intraday_today(now_market_time)
    historical_to_date = today - timedelta(days=1)
    if intraday_today_required:
        historical_days = max(1, int(history_days) - 1)
    else:
        historical_days = max(1, int(history_days))
    historical_from_date = historical_to_date - timedelta(days=historical_days - 1)
    logger.info(f"Historical date range decided. market_time={now_market_time.strftime('%Y-%m-%d %H:%M:%S %Z')}, intraday_today_required={intraday_today_required}, historical_from_date={format_date(historical_from_date)}, historical_to_date={format_date(historical_to_date)}, history_days={history_days}, historical_days={historical_days}")
    return historical_from_date, historical_to_date, intraday_today_required

# ============================================================
# Instrument Helpers
# ============================================================

def get_subscribed_instrument_keys() -> list:
    """Returns subscribed instrument keys from options_cache."""
    subscribed_keys = options_cache.get("subscribed_keys", [])
    if not subscribed_keys: return []
    return list(dict.fromkeys(subscribed_keys))

def get_contract_info_by_key(instrument_key: str) -> dict:
    """Returns contract metadata for an instrument key."""
    main_key = getattr(config, "MAIN_NIFTY_SECURITY", "NSE_INDEX|Nifty 50")
    if instrument_key == main_key:
        return {"instrument_key": instrument_key, "instrument_type": "INDEX", "strike_price": None, "expiry": None,
                "trading_symbol": "NIFTY 50", "underlying_type": "INDEX", "underlying_symbol": "NIFTY 50"}
    for item in options_cache.get("data", []):
        if item.get("instrument_key") == instrument_key:
            return item
    return {"instrument_key": instrument_key}

# ============================================================
# EMA / Crossover Helpers
# ============================================================

def calculate_ema(values: list, period: int) -> list:
    """Calculates EMA for given numeric values."""
    if not values or period <= 0: return []
    ema_values = []
    multiplier = 2 / (period + 1)
    previous_ema = None
    for value in values:
        price = safe_float(value)
        if previous_ema is None:
            previous_ema = price
        else:
            previous_ema = (price * multiplier) + (previous_ema * (1 - multiplier))
        ema_values.append(round(previous_ema, 4))
    return ema_values

def extract_close_prices_from_candles(candles: list) -> list:
    """Extracts close prices from Upstox candles."""
    closes = []
    for candle in candles:
        if isinstance(candle, list) and len(candle) >= 5:
            closes.append(safe_float(candle[4]))
    return closes

def calculate_ema_crossovers(candles: list, fast_period: int = DEFAULT_EMA_FAST_PERIOD, slow_period: int = DEFAULT_EMA_SLOW_PERIOD) -> dict:
    """Calculates EMA fast/slow values and detects EMA crossovers."""
    candles = sort_candles(deduplicate_candles(candles))
    candles_count = len(candles)
    if candles_count == 0:
        return {"status": "empty", "message": "No candles available for EMA calculation.", "candles_count": 0,
                "latest_timestamp": None, "latest_close": None, "latest_ema_fast": None, "latest_ema_slow": None,
                "latest_signal": None, "crossovers_count": 0, "last_crossover": None, "crossovers": []}
    min_required = max(fast_period, slow_period)
    if candles_count < min_required:
        latest_candle = candles[-1]
        latest_close = safe_float(latest_candle[4]) if len(latest_candle) >= 5 else None
        return {"status": "insufficient_data", "message": f"Need at least {min_required} candles for EMA calculation. Available candles: {candles_count}.", "candles_count": candles_count,
                "latest_timestamp": latest_candle[0] if latest_candle else None, "latest_close": latest_close,
                "latest_ema_fast": None, "latest_ema_slow": None, "latest_signal": None, "crossovers_count": 0,
                "last_crossover": None, "crossovers": []}
    closes = extract_close_prices_from_candles(candles)
    if len(closes) < min_required:
        return {"status": "insufficient_data", "message": f"Need at least {min_required} valid close prices. Available close prices: {len(closes)}.",
                "candles_count": candles_count, "latest_timestamp": candles[-1][0] if candles else None,
                "latest_close": closes[-1] if closes else None, "latest_ema_fast": None, "latest_ema_slow": None,
                "latest_signal": None, "crossovers_count": 0, "last_crossover": None, "crossovers": []}
    ema_fast = calculate_ema(closes, fast_period)
    ema_slow = calculate_ema(closes, slow_period)
    crossovers = []
    for index in range(1, len(closes)):
        previous_fast = ema_fast[index - 1]
        previous_slow = ema_slow[index - 1]
        current_fast = ema_fast[index]
        current_slow = ema_slow[index]
        candle = candles[index]
        timestamp = candle[0] if len(candle) > 0 else None
        close_price = closes[index]
        if previous_fast <= previous_slow and current_fast > current_slow:
            crossovers.append({"timestamp": timestamp, "type": "bullish_cross", "close": close_price, "ema_fast": current_fast, "ema_slow": current_slow})
        elif previous_fast >= previous_slow and current_fast < current_slow:
            crossovers.append({"timestamp": timestamp, "type": "bearish_cross", "close": close_price, "ema_fast": current_fast, "ema_slow": current_slow})
    latest_timestamp = candles[-1][0] if candles else None
    latest_close = closes[-1] if closes else None
    latest_ema_fast = ema_fast[-1] if ema_fast else None
    latest_ema_slow = ema_slow[-1] if ema_slow else None
    if latest_ema_fast is not None and latest_ema_slow is not None:
        latest_signal = "bullish" if latest_ema_fast > latest_ema_slow else "bearish" if latest_ema_fast < latest_ema_slow else "neutral"
    else:
        latest_signal = None
    return {"status": "success", "message": "EMA crossover calculation completed.", "candles_count": candles_count,
            "latest_timestamp": latest_timestamp, "latest_close": latest_close, "latest_ema_fast": latest_ema_fast,
            "latest_ema_slow": latest_ema_slow, "latest_signal": latest_signal, "crossovers_count": len(crossovers),
            "last_crossover": crossovers[-1] if crossovers else None, "crossovers": crossovers}

# ============================================================
# Storage Helper for EMA Results Only
# ============================================================

def save_ema_cross_results_to_file(summary: dict, output_file: str = DEFAULT_EMA_OUTPUT_FILE) -> str:
    """Saves EMA crossover summary only."""
    file_path = Path(output_file)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    with open(file_path, "w", encoding="utf-8") as file:
        json.dump(summary, file, indent=4, default=str)
    return str(file_path)

# ============================================================
# Live EMA Service Initialization Helper
# ============================================================

def initialize_live_ema_from_history(summary: dict) -> bool:
    """Initializes live EMA service from historical EMA summary."""
    try:
        if not getattr(config, "LIVE_EMA_ENABLED", True):
            logger.info("Live EMA initialization skipped because LIVE_EMA_ENABLED=False.")
            return False
        from services.live_ema_service import live_ema_service
        live_ema_service.initialize_from_history_summary(summary)
        logger.info("Live EMA service initialized successfully from historical EMA summary.")
        return True
    except ModuleNotFoundError:
        logger.warning("Live EMA service file not found. Skipping live EMA initialization. Expected file: services/live_ema_service.py")
        return False
    except Exception as ex:
        logger.error(f"Live EMA service initialization failed: {type(ex).__name__}: {ex}")
        return False

# ============================================================
# Intraday Fetch Helper
# ============================================================

def fetch_intraday_candles_for_instrument(instrument_key: str, interval: str = DEFAULT_INTERVAL) -> dict:
    """Fetches today's intraday candles using Upstox HistoryV3Api."""
    unit, intra_interval = get_intraday_unit_and_interval(interval)
    if not unit or not intra_interval:
        return {"status": "skipped", "candles": [], "candles_count": 0, "error": f"Unsupported intraday interval: {interval}"}
    try:
        api_instance = upstox_client.HistoryV3Api()
        logger.info(f"Intraday candle request: instrument_key={instrument_key}, unit={unit}, interval={intra_interval}")
        api_response = api_instance.get_intra_day_candle_data(instrument_key, unit, intra_interval)
        candles = extract_candles_from_response(api_response)
        logger.info(f"Intraday candle completed: instrument_key={instrument_key}, candles_count={len(candles)}")
        return {"status": "success" if candles else "empty", "candles": candles, "candles_count": len(candles), "unit": unit, "interval": intra_interval, "error": None}
    except ApiException as ex:
        error_body = getattr(ex, "body", str(ex))
        logger.error(f"ApiException in intraday candle fetch for {instrument_key}: {error_body}")
        return {"status": "failed", "candles": [], "candles_count": 0, "unit": unit, "interval": intra_interval, "error": error_body}
    except Exception as ex:
        error_message = f"{type(ex).__name__}: {ex}"
        logger.error(f"Exception in intraday candle fetch for {instrument_key}: {error_message}")
        return {"status": "failed", "candles": [], "candles_count": 0, "unit": unit, "interval": intra_interval, "error": error_message}

# ============================================================
# Single Instrument Historical Fetch + Intraday + EMA
# ============================================================

def fetch_historical_candles_for_instrument(
    instrument_key: str,
    interval: str = DEFAULT_INTERVAL,
    from_date: str | None = None,
    to_date: str | None = None,
    api_version: str = DEFAULT_API_VERSION,
    max_days_per_request: int = DEFAULT_MAX_DAYS_PER_REQUEST,
    save_data: bool = False,
    fetch_intraday_today: bool = False,
) -> dict:
    """Fetches historical candles for a single instrument and calculates EMA 9/21 crossover."""
    logger.info(f"Fetching candles for instrument_key={instrument_key}, interval={interval}, from_date={from_date}, to_date={to_date}, fetch_intraday_today={fetch_intraday_today}")
    if not from_date or not to_date:
        default_from, default_to, default_intraday_required = get_default_history_range(DEFAULT_HISTORY_DAYS)
        from_date = from_date or format_date(default_from)
        to_date = to_date or format_date(default_to)
        if fetch_intraday_today is False:
            fetch_intraday_today = default_intraday_required
    try:
        from_date_obj = datetime.strptime(from_date, "%Y-%m-%d").date()
        to_date_obj = datetime.strptime(to_date, "%Y-%m-%d").date()
    except ValueError as ex:
        error_message = f"Invalid date format. Expected YYYY-MM-DD. Error: {ex}"
        logger.error(error_message)
        return {"instrument_key": instrument_key, "status": "failed", "interval": interval,
                "from_date": from_date, "to_date": to_date, "intraday_today_used": False,
                "candles_count": 0, "ema_result": None, "batches": [], "errors": [error_message],
                "contract_info": get_contract_info_by_key(instrument_key)}
    batches = build_date_batches(from_date=from_date_obj, to_date=to_date_obj, max_days_per_request=max_days_per_request)
    api_instance = upstox_client.HistoryApi()
    all_candles = []
    batch_results = []
    errors = []
    intraday_today_used = False
    for batch in batches:
        batch_from = batch["from_date"]
        batch_to = batch["to_date"]
        logger.info(f"Historical batch request: instrument_key={instrument_key}, interval={interval}, from_date={batch_from}, to_date={batch_to}")
        try:
            api_response = api_instance.get_historical_candle_data1(instrument_key, interval, batch_to, batch_from, api_version)
            candles = extract_candles_from_response(api_response)
            all_candles.extend(candles)
            batch_results.append({"type": "historical", "from_date": batch_from, "to_date": batch_to, "status": "success" if candles else "empty", "candles_count": len(candles)})
            logger.info(f"Historical batch completed: instrument_key={instrument_key}, from_date={batch_from}, to_date={batch_to}, candles_count={len(candles)}")
        except ApiException as ex:
            error_body = getattr(ex, "body", str(ex))
            error_message = f"ApiException for {instrument_key}, from_date={batch_from}, to_date={batch_to}: {error_body}"
            logger.error(error_message)
            errors.append(error_message)
            batch_results.append({"type": "historical", "from_date": batch_from, "to_date": batch_to, "status": "failed", "candles_count": 0, "error": error_body})
        except Exception as ex:
            error_message = f"{type(ex).__name__} for {instrument_key}, from_date={batch_from}, to_date={batch_to}: {ex}"
            logger.error(error_message)
            errors.append(error_message)
            batch_results.append({"type": "historical", "from_date": batch_from, "to_date": batch_to, "status": "failed", "candles_count": 0, "error": error_message})
        if DEFAULT_SLEEP_SECONDS and DEFAULT_SLEEP_SECONDS > 0:
            time.sleep(DEFAULT_SLEEP_SECONDS)
    if fetch_intraday_today:
        intraday_result = fetch_intraday_candles_for_instrument(instrument_key=instrument_key, interval=interval)
        intraday_candles = intraday_result.get("candles", [])
        all_candles.extend(intraday_candles)
        if intraday_candles:
            intraday_today_used = True
        if intraday_result.get("error"):
            errors.append(f"Intraday fetch error for {instrument_key}: {intraday_result.get('error')}")
        batch_results.append({"type": "intraday_today", "date": format_date(get_today_date()), "status": intraday_result.get("status"),
                              "candles_count": intraday_result.get("candles_count", 0), "unit": intraday_result.get("unit"),
                              "interval": intraday_result.get("interval"), "error": intraday_result.get("error")})
        if DEFAULT_SLEEP_SECONDS and DEFAULT_SLEEP_SECONDS > 0:
            time.sleep(DEFAULT_SLEEP_SECONDS)
    all_candles = deduplicate_candles(all_candles)
    all_candles = sort_candles(all_candles)
    ema_result = calculate_ema_crossovers(candles=all_candles, fast_period=DEFAULT_EMA_FAST_PERIOD, slow_period=DEFAULT_EMA_SLOW_PERIOD)
    candles_count = len(all_candles)
    ema_status = ema_result.get("status")
    if errors and candles_count == 0:
        status = "failed"
    elif ema_status == "empty":
        status = "empty"
    elif ema_status == "insufficient_data":
        status = "insufficient_data"
    else:
        status = "success"
    return {
        "instrument_key": instrument_key,
        "status": status,
        "interval": interval,
        "from_date": from_date,
        "to_date": to_date,
        "intraday_today_used": intraday_today_used,
        "api_version": api_version,
        "candles_count": candles_count,
        "ema_fast_period": DEFAULT_EMA_FAST_PERIOD,
        "ema_slow_period": DEFAULT_EMA_SLOW_PERIOD,
        "ema_result": ema_result,
        "batches": batch_results,
        "errors": errors,
        "contract_info": get_contract_info_by_key(instrument_key),
        "processed_at": datetime.now().isoformat(),
    }

# ============================================================
# All Subscribed Instruments Historical Fetch + EMA
# ============================================================

def fetch_historical_candles_for_all_subscribed(
    interval: str = DEFAULT_INTERVAL,
    history_days: int = DEFAULT_HISTORY_DAYS,
    save_data: bool = True,
    max_workers: int = DEFAULT_MAX_WORKERS,
) -> dict:
    """Fetches historical + intraday candles for all subscribed instruments using parallel threads."""
    if not is_historical_candle_enabled():
        logger.info("Historical candle fetch skipped because it is disabled in config.")
        return {"status": "disabled", "message": "Historical candle fetch is disabled.",
                "total_instruments": 0, "success_count": 0, "failed_count": 0,
                "empty_count": 0, "insufficient_data_count": 0, "total_candles": 0}
    subscribed_keys = get_subscribed_instrument_keys()
    if not subscribed_keys:
        logger.warning("Historical candle fetch skipped. No subscribed instruments found.")
        return {"status": "skipped", "message": "No subscribed instruments found.",
                "total_instruments": 0, "success_count": 0, "failed_count": 0,
                "empty_count": 0, "insufficient_data_count": 0, "total_candles": 0}
    from_date_obj, to_date_obj, intraday_today_required = get_default_history_range(history_days)
    from_date = format_date(from_date_obj)
    to_date = format_date(to_date_obj)
    try:
        max_workers = int(max_workers)
    except Exception:
        max_workers = DEFAULT_MAX_WORKERS
    max_workers = max(1, max_workers)
    logger.info("================ HISTORICAL + INTRADAY EMA CROSSOVER FETCH STARTED ================")
    logger.info(f"Fetching candles and calculating EMA crossover for {len(subscribed_keys)} instruments. interval={interval}, historical_from_date={from_date}, historical_to_date={to_date}, intraday_today_required={intraday_today_required}, history_days={history_days}, max_workers={max_workers}, ema_fast={DEFAULT_EMA_FAST_PERIOD}, ema_slow={DEFAULT_EMA_SLOW_PERIOD}")
    started_at = datetime.now().isoformat()
    results = {}
    errors = {}
    success_count = 0
    failed_count = 0
    empty_count = 0
    insufficient_data_count = 0
    total_candles = 0
    intraday_used_count = 0
    completed_count = 0
    total_instruments = len(subscribed_keys)

    def worker(instrument_key: str) -> dict:
        return fetch_historical_candles_for_instrument(
            instrument_key=instrument_key,
            interval=interval,
            from_date=from_date,
            to_date=to_date,
            api_version=DEFAULT_API_VERSION,
            max_days_per_request=DEFAULT_MAX_DAYS_PER_REQUEST,
            save_data=False,
            fetch_intraday_today=intraday_today_required,
        )

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_instrument = {executor.submit(worker, instrument_key): instrument_key for instrument_key in subscribed_keys}
        for future in as_completed(future_to_instrument):
            instrument_key = future_to_instrument[future]
            completed_count += 1
            logger.info(f"Historical + intraday EMA progress: {completed_count}/{total_instruments} instrument_key={instrument_key}")
            try:
                result = future.result()
                result_status = result.get("status")
                candles_count = int(result.get("candles_count", 0) or 0)
                total_candles += candles_count
                if result.get("intraday_today_used"):
                    intraday_used_count += 1
                results[instrument_key] = {
                    "status": result_status,
                    "candles_count": candles_count,
                    "intraday_today_used": result.get("intraday_today_used", False),
                    "ema_fast_period": result.get("ema_fast_period"),
                    "ema_slow_period": result.get("ema_slow_period"),
                    "ema_result": result.get("ema_result"),
                    "batches": result.get("batches", []),
                    "errors": result.get("errors", []),
                    "contract_info": result.get("contract_info", {}),
                    "processed_at": result.get("processed_at"),
                }
                if result_status == "success":
                    success_count += 1
                elif result_status == "empty":
                    empty_count += 1
                elif result_status == "insufficient_data":
                    insufficient_data_count += 1
                else:
                    failed_count += 1
                    errors[instrument_key] = result.get("errors", [])
            except Exception as ex:
                error_message = f"{type(ex).__name__}: {ex}"
                logger.error(f"Historical + intraday EMA fetch failed for instrument_key={instrument_key}: {error_message}")
                failed_count += 1
                errors[instrument_key] = [error_message]
                results[instrument_key] = {"status": "failed", "candles_count": 0, "intraday_today_used": False,
                                           "ema_fast_period": DEFAULT_EMA_FAST_PERIOD, "ema_slow_period": DEFAULT_EMA_SLOW_PERIOD,
                                           "ema_result": None, "batches": [], "errors": [error_message],
                                           "contract_info": get_contract_info_by_key(instrument_key),
                                           "processed_at": datetime.now().isoformat()}
    completed_at = datetime.now().isoformat()
    if failed_count == 0:
        overall_status = "success"
    elif success_count > 0 or empty_count > 0 or insufficient_data_count > 0:
        overall_status = "partial_success"
    else:
        overall_status = "failed"
    summary = {
        "status": overall_status,
        "started_at": started_at,
        "completed_at": completed_at,
        "from_date": from_date,
        "to_date": to_date,
        "intraday_today_required": intraday_today_required,
        "intraday_used_count": intraday_used_count,
        "interval": interval,
        "history_days": history_days,
        "max_days_per_request": DEFAULT_MAX_DAYS_PER_REQUEST,
        "max_workers": max_workers,
        "raw_candles_saved": False,
        "ema_fast_period": DEFAULT_EMA_FAST_PERIOD,
        "ema_slow_period": DEFAULT_EMA_SLOW_PERIOD,
        "total_instruments": total_instruments,
        "success_count": success_count,
        "failed_count": failed_count,
        "empty_count": empty_count,
        "insufficient_data_count": insufficient_data_count,
        "total_candles": total_candles,
        "results": results,
        "errors": errors,
    }
    ema_results_file_path = None
    if save_data and is_test_flag_enabled():
        try:
            ema_results_file_path = save_ema_cross_results_to_file(summary)
            summary["ema_results_file_path"] = ema_results_file_path
            logger.info(f"Saved EMA crossover results to {ema_results_file_path}")
        except Exception as ex:
            logger.error(f"Failed saving EMA crossover results: {type(ex).__name__}: {ex}")
            summary["ema_results_file_error"] = f"{type(ex).__name__}: {ex}"
    else:
        logger.info(f"EMA crossover result file not saved. save_data={save_data}, TEST_FLAG={is_test_flag_enabled()}")
    live_ema_initialized = initialize_live_ema_from_history(summary)
    summary["live_ema_initialized"] = live_ema_initialized
    with _history_cache_lock:
        historical_candles_cache["last_run_at"] = completed_at
        historical_candles_cache["from_date"] = from_date
        historical_candles_cache["to_date"] = to_date
        historical_candles_cache["intraday_today_used"] = intraday_today_required
        historical_candles_cache["interval"] = interval
        historical_candles_cache["total_instruments"] = total_instruments
        historical_candles_cache["success_count"] = success_count
        historical_candles_cache["failed_count"] = failed_count
        historical_candles_cache["empty_count"] = empty_count
        historical_candles_cache["insufficient_data_count"] = insufficient_data_count
        historical_candles_cache["total_candles"] = total_candles
        historical_candles_cache["ema_fast_period"] = DEFAULT_EMA_FAST_PERIOD
        historical_candles_cache["ema_slow_period"] = DEFAULT_EMA_SLOW_PERIOD
        historical_candles_cache["ema_results_file_path"] = ema_results_file_path
        historical_candles_cache["live_ema_initialized"] = live_ema_initialized
        historical_candles_cache["data"] = results
        historical_candles_cache["errors"] = errors
    logger.info(f"Historical + intraday EMA crossover fetch completed. status={overall_status}, total_instruments={total_instruments}, success={success_count}, empty={empty_count}, insufficient_data={insufficient_data_count}, failed={failed_count}, total_candles={total_candles}, intraday_today_required={intraday_today_required}, intraday_used_count={intraday_used_count}, max_workers={max_workers}, live_ema_initialized={live_ema_initialized}")
    logger.info("================ HISTORICAL + INTRADAY EMA CROSSOVER FETCH COMPLETED ================")
    return summary

# ============================================================
# Status Helper
# ============================================================

def get_historical_candles_status() -> dict:
    """Returns latest historical candle and EMA crossover cache summary."""
    with _history_cache_lock:
        return {
            "last_run_at": historical_candles_cache.get("last_run_at"),
            "from_date": historical_candles_cache.get("from_date"),
            "to_date": historical_candles_cache.get("to_date"),
            "intraday_today_used": historical_candles_cache.get("intraday_today_used"),
            "interval": historical_candles_cache.get("interval"),
            "total_instruments": historical_candles_cache.get("total_instruments"),
            "success_count": historical_candles_cache.get("success_count"),
            "failed_count": historical_candles_cache.get("failed_count"),
            "empty_count": historical_candles_cache.get("empty_count"),
            "insufficient_data_count": historical_candles_cache.get("insufficient_data_count"),
            "total_candles": historical_candles_cache.get("total_candles"),
            "ema_fast_period": historical_candles_cache.get("ema_fast_period"),
            "ema_slow_period": historical_candles_cache.get("ema_slow_period"),
            "ema_results_file_path": historical_candles_cache.get("ema_results_file_path"),
            "live_ema_initialized": historical_candles_cache.get("live_ema_initialized"),
            "errors": historical_candles_cache.get("errors", {}),
        }