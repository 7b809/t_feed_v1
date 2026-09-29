import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, time as dt_time
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

_history_cache_lock = Lock()
_ema_cross_file_lock = Lock()

historical_candles_cache = {
    "last_run_at": None,
    "from_date": None,
    "to_date": None,
    "intraday_today_used": False,
    "interval": None,
    "total_instruments": 0,
    "success_count": 0,
    "failed_count": 0,
    "empty_count": 0,
    "insufficient_data_count": 0,
    "total_candles": 0,
    "ema_fast_period": None,
    "ema_slow_period": None,
    "ema_results_file_path": None,
    "ema_crosses_directory": None,
    "historical_crosses_saved": 0,
    "intraday_crosses_saved": 0,
    "live_ema_initialized": False,
    "data": {},
    "errors": {},
}

DEFAULT_HISTORY_DAYS = getattr(
    config,
    "HISTORICAL_CANDLE_DAYS",
    10,
)

DEFAULT_INTERVAL = getattr(
    config,
    "HISTORICAL_CANDLE_INTERVAL",
    "1minute",
)

DEFAULT_API_VERSION = getattr(
    config,
    "HISTORICAL_CANDLE_API_VERSION",
    "2.0",
)

DEFAULT_MAX_DAYS_PER_REQUEST = getattr(
    config,
    "HISTORICAL_CANDLE_MAX_DAYS_PER_REQUEST",
    7,
)

DEFAULT_SLEEP_SECONDS = getattr(
    config,
    "HISTORICAL_CANDLE_REQUEST_SLEEP_SECONDS",
    0.15,
)

DEFAULT_MAX_WORKERS = getattr(
    config,
    "HISTORICAL_CANDLE_MAX_WORKERS",
    5,
)

DEFAULT_EMA_FAST_PERIOD = getattr(
    config,
    "EMA_FAST_PERIOD",
    9,
)

DEFAULT_EMA_SLOW_PERIOD = getattr(
    config,
    "EMA_SLOW_PERIOD",
    21,
)

DEFAULT_EMA_OUTPUT_FILE = getattr(
    config,
    "EMA_CROSS_OUTPUT_FILE",
    "data/ema_cross_results.json",
)

DEFAULT_EMA_CROSSES_DIRECTORY = getattr(
    config,
    "EMA_CROSSES_DIRECTORY",
    "data/ema_crosses",
)

DEFAULT_MARKET_OPEN_HOUR = getattr(
    config,
    "MARKET_OPEN_HOUR",
    9,
)

DEFAULT_MARKET_OPEN_MINUTE = getattr(
    config,
    "MARKET_OPEN_MINUTE",
    15,
)


def is_historical_candle_enabled() -> bool:
    return bool(
        getattr(
            config,
            "HISTORICAL_CANDLE_ENABLED",
            True,
        )
    )


def is_test_flag_enabled() -> bool:
    return bool(
        getattr(
            config,
            "TEST_FLAG",
            False,
        )
    )


def get_market_timezone():
    timezone_name = getattr(
        config,
        "MARKET_TIMEZONE",
        "Asia/Kolkata",
    )

    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        logger.error(
            "Invalid MARKET_TIMEZONE configured: %s. " "Falling back to Asia/Kolkata.",
            timezone_name,
        )
        return ZoneInfo("Asia/Kolkata")


def get_now_market_time() -> datetime:
    return datetime.now(get_market_timezone())


def should_fetch_intraday_today(
    now_market_time: datetime | None = None,
) -> bool:
    now_market_time = now_market_time or get_now_market_time()

    if now_market_time.weekday() >= 5:
        return False

    market_open_time = dt_time(
        hour=int(DEFAULT_MARKET_OPEN_HOUR),
        minute=int(DEFAULT_MARKET_OPEN_MINUTE),
    )

    return now_market_time.time() >= market_open_time


def get_today_date() -> date:
    return get_now_market_time().date()


def format_date(value: date) -> str:
    return value.strftime("%Y-%m-%d")


def response_to_dict(api_response: Any) -> dict:
    if api_response is None:
        return {}

    if hasattr(api_response, "to_dict"):
        return api_response.to_dict()

    if isinstance(api_response, dict):
        return api_response

    try:
        return dict(api_response)
    except Exception:
        return {}


def extract_candles_from_response(api_response: Any) -> list:
    response_dict = response_to_dict(api_response)
    data = response_dict.get("data", {})

    if not isinstance(data, dict):
        return []

    candles = data.get("candles", [])

    if not isinstance(candles, list):
        return []

    return candles


def safe_float(
    value: Any,
    default: float = 0.0,
) -> float:
    try:
        if value is None:
            return default

        return float(value)
    except Exception:
        return default


def deduplicate_candles(candles: list) -> list:
    candles_by_timestamp = {}

    for candle in candles:
        if isinstance(candle, (list, tuple)) and candle:
            key = str(candle[0])
        else:
            key = json.dumps(
                candle,
                sort_keys=True,
                default=str,
            )

        candles_by_timestamp[key] = candle

    return list(candles_by_timestamp.values())


def sort_candles(candles: list) -> list:
    try:
        return sorted(
            candles,
            key=lambda item: str(item[0]) if item else "",
        )
    except Exception:
        return candles


def normalize_candles(candles: list) -> list:
    normalized = []

    for candle in candles:
        if not isinstance(candle, (list, tuple)):
            continue

        if len(candle) < 5:
            continue

        if candle[0] is None or candle[4] is None:
            continue

        normalized.append(list(candle))

    return sort_candles(deduplicate_candles(normalized))


def get_intraday_unit_and_interval(
    interval: str,
) -> tuple[str | None, str | None]:
    interval_text = str(interval or "").strip().lower()

    if interval_text.endswith("minutes"):
        value = interval_text.removesuffix("minutes").strip()
        return "minutes", value or "1"

    if interval_text.endswith("minute"):
        value = interval_text.removesuffix("minute").strip()
        return "minutes", value or "1"

    logger.warning(
        "Intraday candle fetch not supported for interval=%s. "
        "Supported examples: 1minute, 3minute, 5minute, "
        "15minute, 30minute.",
        interval,
    )

    return None, None


def build_date_batches(
    from_date: date,
    to_date: date,
    max_days_per_request: int = DEFAULT_MAX_DAYS_PER_REQUEST,
) -> list:
    if from_date > to_date:
        return []

    max_days_per_request = max(
        1,
        int(max_days_per_request),
    )

    batches = []
    current_from = from_date

    while current_from <= to_date:
        current_to = min(
            current_from + timedelta(days=max_days_per_request - 1),
            to_date,
        )

        batches.append(
            {
                "from_date": format_date(current_from),
                "to_date": format_date(current_to),
            }
        )

        current_from = current_to + timedelta(days=1)

    return batches


def get_default_history_range(
    history_days: int = DEFAULT_HISTORY_DAYS,
) -> tuple[date, date, bool]:
    now_market_time = get_now_market_time()
    today = now_market_time.date()
    intraday_today_required = should_fetch_intraday_today(now_market_time)
    historical_to_date = today - timedelta(days=1)

    if intraday_today_required:
        historical_days = max(
            1,
            int(history_days) - 1,
        )
    else:
        historical_days = max(
            1,
            int(history_days),
        )

    historical_from_date = historical_to_date - timedelta(days=historical_days - 1)

    logger.info(
        "Historical date range decided. market_time=%s, "
        "intraday_today_required=%s, "
        "historical_from_date=%s, historical_to_date=%s, "
        "history_days=%s, historical_days=%s",
        now_market_time.strftime("%Y-%m-%d %H:%M:%S %Z"),
        intraday_today_required,
        format_date(historical_from_date),
        format_date(historical_to_date),
        history_days,
        historical_days,
    )

    return (
        historical_from_date,
        historical_to_date,
        intraday_today_required,
    )


def get_subscribed_instrument_keys() -> list:
    subscribed_keys = options_cache.get(
        "subscribed_keys",
        [],
    )

    if not subscribed_keys:
        return []

    return list(dict.fromkeys(subscribed_keys))


def get_contract_info_by_key(
    instrument_key: str,
) -> dict:
    main_key = getattr(
        config,
        "MAIN_NIFTY_SECURITY",
        "NSE_INDEX|Nifty 50",
    )

    if instrument_key == main_key:
        return {
            "instrument_key": instrument_key,
            "instrument_type": "INDEX",
            "option_type": "INDEX",
            "strike_price": None,
            "expiry": None,
            "trading_symbol": "NIFTY 50",
            "underlying_type": "INDEX",
            "underlying_symbol": "NIFTY 50",
        }

    for item in options_cache.get("data", []):
        if item.get("instrument_key") == instrument_key:
            return dict(item)

    return {
        "instrument_key": instrument_key,
    }


def normalize_option_type(
    contract_info: dict,
) -> str:
    option_type = (
        contract_info.get("option_type")
        or contract_info.get("instrument_type")
        or contract_info.get("strike_type")
        or "UNKNOWN"
    )

    option_type = str(option_type).strip().upper()

    if option_type == "CALL":
        return "CE"

    if option_type == "PUT":
        return "PE"

    return option_type


def format_strike_value(
    strike_price: Any,
) -> str:
    if strike_price is None:
        return "NO_STRIKE"

    try:
        numeric_value = float(strike_price)

        if numeric_value.is_integer():
            return str(int(numeric_value))

        return str(numeric_value).rstrip("0").rstrip(".")
    except Exception:
        return str(strike_price)


def sanitize_path_component(
    value: Any,
) -> str:
    text = str(value or "UNKNOWN").strip()
    text = re.sub(
        r"[^A-Za-z0-9._-]+",
        "_",
        text,
    )
    text = text.strip("._")

    return text or "UNKNOWN"


def get_instrument_crosses_directory(
    instrument_key: str,
    contract_info: dict | None = None,
) -> Path:
    contract_info = contract_info or get_contract_info_by_key(instrument_key)

    strike_price = contract_info.get("strike_price")
    option_type = normalize_option_type(contract_info)

    if strike_price is not None and option_type in {"CE", "PE"}:
        folder_name = f"{format_strike_value(strike_price)}_" f"{option_type}"
    else:
        folder_name = sanitize_path_component(
            contract_info.get("trading_symbol") or instrument_key
        )

    return Path(DEFAULT_EMA_CROSSES_DIRECTORY) / folder_name


def parse_candle_timestamp(
    value: Any,
) -> datetime | None:
    if value is None:
        return None

    market_timezone = get_market_timezone()

    if isinstance(value, datetime):
        parsed = value
    else:
        timestamp_text = str(value).strip()

        if not timestamp_text:
            return None

        if timestamp_text.endswith("Z"):
            timestamp_text = f"{timestamp_text[:-1]}+00:00"

        try:
            parsed = datetime.fromisoformat(timestamp_text)
        except ValueError:
            parsed = None

            formats = (
                "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d",
            )

            for timestamp_format in formats:
                try:
                    parsed = datetime.strptime(
                        timestamp_text,
                        timestamp_format,
                    )
                    break
                except ValueError:
                    continue

            if parsed is None:
                return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=market_timezone)

    return parsed.astimezone(market_timezone)


def is_timestamp_on_market_date(
    timestamp: Any,
    market_date: date,
) -> bool:
    parsed_timestamp = parse_candle_timestamp(timestamp)

    if parsed_timestamp is None:
        return False

    return parsed_timestamp.date() == market_date


def calculate_ema(
    values: list,
    period: int,
) -> list:
    if not values or period <= 0:
        return []

    multiplier = 2 / (period + 1)
    previous_ema = None
    ema_values = []

    for value in values:
        price = safe_float(value)

        if previous_ema is None:
            previous_ema = price
        else:
            previous_ema = price * multiplier + previous_ema * (1 - multiplier)

        ema_values.append(previous_ema)

    return ema_values


def extract_close_prices_from_candles(
    candles: list,
) -> list:
    closes = []

    for candle in candles:
        if (
            isinstance(candle, (list, tuple))
            and len(candle) >= 5
            and candle[4] is not None
        ):
            closes.append(safe_float(candle[4]))

    return closes


def calculate_ema_crossovers(
    candles: list,
    fast_period: int = DEFAULT_EMA_FAST_PERIOD,
    slow_period: int = DEFAULT_EMA_SLOW_PERIOD,
) -> dict:
    candles = normalize_candles(candles)
    candles_count = len(candles)

    empty_result = {
        "status": "empty",
        "message": "No candles available for EMA calculation.",
        "candles_count": 0,
        "latest_timestamp": None,
        "latest_close": None,
        "latest_ema_fast": None,
        "latest_ema_slow": None,
        "latest_signal": None,
        "crossovers_count": 0,
        "last_crossover": None,
        "crossovers": [],
    }

    if candles_count == 0:
        return empty_result

    min_required = max(
        fast_period,
        slow_period,
    )

    if candles_count < min_required:
        latest_candle = candles[-1]

        return {
            "status": "insufficient_data",
            "message": (
                f"Need at least {min_required} candles for "
                f"EMA calculation. Available candles: "
                f"{candles_count}."
            ),
            "candles_count": candles_count,
            "latest_timestamp": latest_candle[0],
            "latest_close": safe_float(latest_candle[4]),
            "latest_ema_fast": None,
            "latest_ema_slow": None,
            "latest_signal": None,
            "crossovers_count": 0,
            "last_crossover": None,
            "crossovers": [],
        }

    closes = extract_close_prices_from_candles(candles)

    if len(closes) < min_required:
        return {
            "status": "insufficient_data",
            "message": (
                f"Need at least {min_required} valid close "
                f"prices. Available close prices: "
                f"{len(closes)}."
            ),
            "candles_count": candles_count,
            "latest_timestamp": candles[-1][0],
            "latest_close": (closes[-1] if closes else None),
            "latest_ema_fast": None,
            "latest_ema_slow": None,
            "latest_signal": None,
            "crossovers_count": 0,
            "last_crossover": None,
            "crossovers": [],
        }

    ema_fast_values = calculate_ema(
        closes,
        fast_period,
    )
    ema_slow_values = calculate_ema(
        closes,
        slow_period,
    )

    crossovers = []

    for index in range(1, len(closes)):
        previous_fast = ema_fast_values[index - 1]
        previous_slow = ema_slow_values[index - 1]
        current_fast = ema_fast_values[index]
        current_slow = ema_slow_values[index]
        candle = candles[index]
        timestamp = candle[0]
        close_price = closes[index]

        if previous_fast <= previous_slow and current_fast > current_slow:
            crossovers.append(
                {
                    "timestamp": timestamp,
                    "type": "bullish_cross",
                    "close": close_price,
                    "ema_fast": current_fast,
                    "ema_slow": current_slow,
                }
            )

        elif previous_fast >= previous_slow and current_fast < current_slow:
            crossovers.append(
                {
                    "timestamp": timestamp,
                    "type": "bearish_cross",
                    "close": close_price,
                    "ema_fast": current_fast,
                    "ema_slow": current_slow,
                }
            )

    latest_timestamp = candles[-1][0]
    latest_close = closes[-1]
    latest_ema_fast = ema_fast_values[-1]
    latest_ema_slow = ema_slow_values[-1]

    if latest_ema_fast > latest_ema_slow:
        latest_signal = "bullish"
    elif latest_ema_fast < latest_ema_slow:
        latest_signal = "bearish"
    else:
        latest_signal = "neutral"

    return {
        "status": "success",
        "message": "EMA crossover calculation completed.",
        "candles_count": candles_count,
        "latest_timestamp": latest_timestamp,
        "latest_close": latest_close,
        "latest_ema_fast": latest_ema_fast,
        "latest_ema_slow": latest_ema_slow,
        "latest_signal": latest_signal,
        "crossovers_count": len(crossovers),
        "last_crossover": (crossovers[-1] if crossovers else None),
        "crossovers": crossovers,
    }


def split_crossovers_by_market_date(
    crossovers: list,
    market_date: date | None = None,
) -> tuple[list, list]:
    market_date = market_date or get_today_date()
    historical_crosses = []
    intraday_crosses = []

    for crossover in crossovers:
        if not isinstance(crossover, dict):
            continue

        if is_timestamp_on_market_date(
            crossover.get("timestamp"),
            market_date,
        ):
            intraday_crosses.append(crossover)
        else:
            historical_crosses.append(crossover)

    return historical_crosses, intraday_crosses


def build_cross_record(
    instrument_key: str,
    crossover: dict,
    contract_info: dict,
    source: str,
) -> dict:
    return {
        "instrument_key": instrument_key,
        "strike_price": contract_info.get("strike_price"),
        "option_type": normalize_option_type(contract_info),
        "trading_symbol": contract_info.get("trading_symbol"),
        "expiry": contract_info.get("expiry"),
        "timestamp": (
            crossover.get("timestamp")
            or crossover.get("candle_timestamp")
            or crossover.get("time")
        ),
        "type": (
            crossover.get("type")
            or crossover.get("cross_type")
            or crossover.get("signal")
        ),
        "close": crossover.get(
            "close",
            crossover.get("price"),
        ),
        "ema_fast": crossover.get(
            "ema_fast",
            crossover.get("fast_ema"),
        ),
        "ema_slow": crossover.get(
            "ema_slow",
            crossover.get("slow_ema"),
        ),
        "source": source,
        "saved_at": get_now_market_time().isoformat(),
    }


def get_cross_record_identity(
    cross_record: dict,
) -> str:
    return "|".join(
        [
            str(
                cross_record.get(
                    "instrument_key",
                    "",
                )
            ),
            str(
                cross_record.get(
                    "timestamp",
                    "",
                )
            ),
            str(
                cross_record.get(
                    "type",
                    "",
                )
            ),
        ]
    )


def merge_cross_records(
    existing_crosses: list,
    incoming_crosses: list,
) -> list:
    merged = {}

    for crossover in list(existing_crosses or []) + list(incoming_crosses or []):
        if not isinstance(crossover, dict):
            continue

        identity = get_cross_record_identity(crossover)
        merged[identity] = crossover

    result = list(merged.values())

    result.sort(key=lambda item: str(item.get("timestamp") or ""))

    return result


def read_json_file(
    file_path: Path,
    default: Any,
) -> Any:
    if not file_path.exists():
        return default

    try:
        with open(
            file_path,
            "r",
            encoding="utf-8",
        ) as file:
            return json.load(file)
    except Exception as ex:
        logger.error(
            "Failed reading JSON file %s: %s: %s",
            file_path,
            type(ex).__name__,
            ex,
        )
        return default


def write_json_file_atomic(
    file_path: Path,
    payload: Any,
) -> str:
    file_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    temporary_path = file_path.with_name(f"{file_path.name}.{os.getpid()}.tmp")

    try:
        with open(
            temporary_path,
            "w",
            encoding="utf-8",
        ) as file:
            json.dump(
                payload,
                file,
                indent=4,
                ensure_ascii=False,
                default=str,
            )
            file.flush()
            os.fsync(file.fileno())

        os.replace(
            temporary_path,
            file_path,
        )
    finally:
        if temporary_path.exists():
            try:
                temporary_path.unlink()
            except Exception:
                pass

    return str(file_path)


def save_cross_records(
    instrument_key: str,
    crossovers: list,
    file_name: str,
    source: str,
    contract_info: dict | None = None,
) -> dict:
    contract_info = contract_info or get_contract_info_by_key(instrument_key)

    instrument_directory = get_instrument_crosses_directory(
        instrument_key,
        contract_info,
    )

    file_path = instrument_directory / file_name

    incoming_records = [
        build_cross_record(
            instrument_key=instrument_key,
            crossover=crossover,
            contract_info=contract_info,
            source=source,
        )
        for crossover in crossovers
        if isinstance(crossover, dict)
    ]

    with _ema_cross_file_lock:
        current_payload = read_json_file(
            file_path,
            {},
        )

        if isinstance(current_payload, dict):
            existing_crosses = current_payload.get(
                "crosses",
                [],
            )
        elif isinstance(current_payload, list):
            existing_crosses = current_payload
        else:
            existing_crosses = []

        merged_crosses = merge_cross_records(
            existing_crosses,
            incoming_records,
        )

        payload = {
            "instrument_key": instrument_key,
            "strike_price": contract_info.get("strike_price"),
            "option_type": normalize_option_type(contract_info),
            "trading_symbol": contract_info.get("trading_symbol"),
            "expiry": contract_info.get("expiry"),
            "ema_fast_period": DEFAULT_EMA_FAST_PERIOD,
            "ema_slow_period": DEFAULT_EMA_SLOW_PERIOD,
            "source": source,
            "crosses_count": len(merged_crosses),
            "last_cross": (merged_crosses[-1] if merged_crosses else None),
            "updated_at": get_now_market_time().isoformat(),
            "crosses": merged_crosses,
        }

        saved_path = write_json_file_atomic(
            file_path,
            payload,
        )

    return {
        "file_path": saved_path,
        "received_count": len(incoming_records),
        "saved_count": len(merged_crosses),
        "last_cross": (merged_crosses[-1] if merged_crosses else None),
    }


def save_instrument_ema_crosses(
    instrument_key: str,
    ema_result: dict | None,
    contract_info: dict | None = None,
) -> dict:
    contract_info = contract_info or get_contract_info_by_key(instrument_key)

    ema_result = ema_result or {}
    crossovers = ema_result.get(
        "crossovers",
        [],
    )

    if not isinstance(crossovers, list):
        crossovers = []

    (
        historical_crosses,
        intraday_crosses,
    ) = split_crossovers_by_market_date(crossovers)

    historical_result = save_cross_records(
        instrument_key=instrument_key,
        crossovers=historical_crosses,
        file_name="historical_crosses.json",
        source="historical_and_intraday_initialization",
        contract_info=contract_info,
    )

    intraday_result = save_cross_records(
        instrument_key=instrument_key,
        crossovers=intraday_crosses,
        file_name="intraday_crosses.json",
        source="intraday_initialization",
        contract_info=contract_info,
    )

    return {
        "directory": str(
            get_instrument_crosses_directory(
                instrument_key,
                contract_info,
            )
        ),
        "historical": historical_result,
        "intraday": intraday_result,
    }


def save_runtime_intraday_ema_cross(
    instrument_key: str,
    crossover: dict,
    contract_info: dict | None = None,
) -> dict:
    if not instrument_key:
        raise ValueError("instrument_key is required")

    if not isinstance(crossover, dict):
        raise ValueError("crossover must be a dictionary")

    normalized_crossover = dict(crossover)

    if not normalized_crossover.get("timestamp"):
        normalized_crossover["timestamp"] = get_now_market_time().isoformat()

    result = save_cross_records(
        instrument_key=instrument_key,
        crossovers=[normalized_crossover],
        file_name="intraday_crosses.json",
        source="runtime_live_ema",
        contract_info=contract_info,
    )

    logger.info(
        "Runtime intraday EMA cross saved. "
        "instrument_key=%s, cross_type=%s, "
        "timestamp=%s, file_path=%s",
        instrument_key,
        (normalized_crossover.get("type") or normalized_crossover.get("cross_type")),
        normalized_crossover.get("timestamp"),
        result.get("file_path"),
    )

    return result


def save_ema_cross_results_to_file(
    summary: dict,
    output_file: str = DEFAULT_EMA_OUTPUT_FILE,
) -> str:
    file_path = Path(output_file)

    with _ema_cross_file_lock:
        return write_json_file_atomic(
            file_path,
            summary,
        )


def initialize_live_ema_from_history(
    summary: dict,
) -> bool:
    from services.ema_engine import internal_ema_engine

    return internal_ema_engine.initialize_from_history(summary)


def fetch_historical_candle_batches(
    instrument_key: str,
    interval: str,
    from_date: str,
    to_date: str,
    api_version: str = DEFAULT_API_VERSION,
    max_days_per_request: int = DEFAULT_MAX_DAYS_PER_REQUEST,
) -> dict:
    try:
        from_date_obj = datetime.strptime(
            from_date,
            "%Y-%m-%d",
        ).date()
        to_date_obj = datetime.strptime(
            to_date,
            "%Y-%m-%d",
        ).date()
    except ValueError as ex:
        return {
            "status": "failed",
            "candles": [],
            "candles_count": 0,
            "batches": [],
            "errors": ["Invalid date format. Expected YYYY-MM-DD. " f"Error: {ex}"],
        }

    batches = build_date_batches(
        from_date=from_date_obj,
        to_date=to_date_obj,
        max_days_per_request=max_days_per_request,
    )

    api_instance = upstox_client.HistoryApi()
    all_candles = []
    batch_results = []
    errors = []

    for batch in batches:
        batch_from = batch["from_date"]
        batch_to = batch["to_date"]

        logger.info(
            "Historical batch request: instrument_key=%s, "
            "interval=%s, from_date=%s, to_date=%s",
            instrument_key,
            interval,
            batch_from,
            batch_to,
        )

        try:
            api_response = api_instance.get_historical_candle_data1(
                instrument_key,
                interval,
                batch_to,
                batch_from,
                api_version,
            )

            candles = extract_candles_from_response(api_response)

            all_candles.extend(candles)

            batch_results.append(
                {
                    "type": "historical",
                    "from_date": batch_from,
                    "to_date": batch_to,
                    "status": ("success" if candles else "empty"),
                    "candles_count": len(candles),
                }
            )

            logger.info(
                "Historical batch completed: "
                "instrument_key=%s, from_date=%s, "
                "to_date=%s, candles_count=%s",
                instrument_key,
                batch_from,
                batch_to,
                len(candles),
            )
        except ApiException as ex:
            error_body = getattr(
                ex,
                "body",
                str(ex),
            )

            error_message = (
                f"ApiException for {instrument_key}, "
                f"from_date={batch_from}, "
                f"to_date={batch_to}: {error_body}"
            )

            logger.error(error_message)
            errors.append(error_message)

            batch_results.append(
                {
                    "type": "historical",
                    "from_date": batch_from,
                    "to_date": batch_to,
                    "status": "failed",
                    "candles_count": 0,
                    "error": error_body,
                }
            )
        except Exception as ex:
            error_message = (
                f"{type(ex).__name__} for "
                f"{instrument_key}, "
                f"from_date={batch_from}, "
                f"to_date={batch_to}: {ex}"
            )

            logger.error(error_message)
            errors.append(error_message)

            batch_results.append(
                {
                    "type": "historical",
                    "from_date": batch_from,
                    "to_date": batch_to,
                    "status": "failed",
                    "candles_count": 0,
                    "error": error_message,
                }
            )

        if DEFAULT_SLEEP_SECONDS > 0:
            time.sleep(DEFAULT_SLEEP_SECONDS)

    all_candles = normalize_candles(all_candles)

    if errors and not all_candles:
        status = "failed"
    elif all_candles:
        status = "success"
    else:
        status = "empty"

    return {
        "status": status,
        "candles": all_candles,
        "candles_count": len(all_candles),
        "batches": batch_results,
        "errors": errors,
    }


def fetch_intraday_candles_for_instrument(
    instrument_key: str,
    interval: str = DEFAULT_INTERVAL,
) -> dict:
    (
        unit,
        intraday_interval,
    ) = get_intraday_unit_and_interval(interval)

    if not unit or not intraday_interval:
        return {
            "status": "skipped",
            "candles": [],
            "candles_count": 0,
            "unit": unit,
            "interval": intraday_interval,
            "error": (f"Unsupported intraday interval: " f"{interval}"),
        }

    try:
        api_instance = upstox_client.HistoryV3Api()

        logger.info(
            "Intraday candle request: " "instrument_key=%s, unit=%s, interval=%s",
            instrument_key,
            unit,
            intraday_interval,
        )

        api_response = api_instance.get_intra_day_candle_data(
            instrument_key,
            unit,
            intraday_interval,
        )

        candles = normalize_candles(extract_candles_from_response(api_response))

        logger.info(
            "Intraday candle completed: " "instrument_key=%s, candles_count=%s",
            instrument_key,
            len(candles),
        )

        return {
            "status": ("success" if candles else "empty"),
            "candles": candles,
            "candles_count": len(candles),
            "unit": unit,
            "interval": intraday_interval,
            "error": None,
        }
    except ApiException as ex:
        error_body = getattr(
            ex,
            "body",
            str(ex),
        )

        logger.error(
            "ApiException in intraday candle fetch " "for %s: %s",
            instrument_key,
            error_body,
        )

        return {
            "status": "failed",
            "candles": [],
            "candles_count": 0,
            "unit": unit,
            "interval": intraday_interval,
            "error": error_body,
        }
    except Exception as ex:
        error_message = f"{type(ex).__name__}: {ex}"

        logger.error(
            "Exception in intraday candle fetch " "for %s: %s",
            instrument_key,
            error_message,
        )

        return {
            "status": "failed",
            "candles": [],
            "candles_count": 0,
            "unit": unit,
            "interval": intraday_interval,
            "error": error_message,
        }


def fetch_latest_intraday_ema_crosses(
    instrument_key: str,
    interval: str = DEFAULT_INTERVAL,
    history_days: int = DEFAULT_HISTORY_DAYS,
    save_crosses: bool = True,
) -> dict:
    if not instrument_key:
        return {
            "status": "failed",
            "message": "instrument_key is required.",
            "instrument_key": instrument_key,
            "historical_candles_count": 0,
            "intraday_candles_count": 0,
            "combined_candles_count": 0,
            "intraday_crosses_count": 0,
            "last_intraday_cross": None,
            "intraday_crosses": [],
            "errors": ["instrument_key is required."],
        }

    now_market_time = get_now_market_time()
    market_date = now_market_time.date()
    historical_to_date = market_date - timedelta(days=1)
    historical_days = max(
        1,
        int(history_days),
    )
    historical_from_date = historical_to_date - timedelta(days=historical_days - 1)

    from_date = format_date(historical_from_date)
    to_date = format_date(historical_to_date)

    logger.info(
        "Latest intraday EMA cross calculation started. "
        "instrument_key=%s, interval=%s, "
        "historical_from_date=%s, historical_to_date=%s",
        instrument_key,
        interval,
        from_date,
        to_date,
    )

    historical_result = fetch_historical_candle_batches(
        instrument_key=instrument_key,
        interval=interval,
        from_date=from_date,
        to_date=to_date,
        api_version=DEFAULT_API_VERSION,
        max_days_per_request=(DEFAULT_MAX_DAYS_PER_REQUEST),
    )

    intraday_result = fetch_intraday_candles_for_instrument(
        instrument_key=instrument_key,
        interval=interval,
    )

    historical_candles = historical_result.get(
        "candles",
        [],
    )
    intraday_candles = intraday_result.get(
        "candles",
        [],
    )

    combined_candles = normalize_candles(historical_candles + intraday_candles)

    ema_result = calculate_ema_crossovers(
        candles=combined_candles,
        fast_period=DEFAULT_EMA_FAST_PERIOD,
        slow_period=DEFAULT_EMA_SLOW_PERIOD,
    )

    all_crosses = ema_result.get(
        "crossovers",
        [],
    )

    intraday_crosses = [
        crossover
        for crossover in all_crosses
        if is_timestamp_on_market_date(
            crossover.get("timestamp"),
            market_date,
        )
    ]

    contract_info = get_contract_info_by_key(instrument_key)

    save_result = None
    errors = []

    errors.extend(historical_result.get("errors", []))

    if intraday_result.get("error"):
        errors.append(str(intraday_result.get("error")))

    if save_crosses:
        try:
            save_result = save_cross_records(
                instrument_key=instrument_key,
                crossovers=intraday_crosses,
                file_name="intraday_crosses.json",
                source="latest_intraday_api_calculation",
                contract_info=contract_info,
            )
        except Exception as ex:
            save_error = (
                f"Failed saving latest intraday EMA "
                f"crosses: {type(ex).__name__}: {ex}"
            )
            logger.error(save_error)
            errors.append(save_error)

    if (
        historical_result.get("status") == "failed"
        and intraday_result.get("status") == "failed"
    ):
        status = "failed"
    elif ema_result.get("status") in {
        "empty",
        "insufficient_data",
    }:
        status = ema_result.get("status")
    elif errors:
        status = "partial_success"
    else:
        status = "success"

    latest_intraday_candle = intraday_candles[-1] if intraday_candles else None

    logger.info(
        "Latest intraday EMA cross calculation completed. "
        "instrument_key=%s, historical_candles=%s, "
        "intraday_candles=%s, combined_candles=%s, "
        "intraday_crosses=%s, latest_timestamp=%s",
        instrument_key,
        len(historical_candles),
        len(intraday_candles),
        len(combined_candles),
        len(intraday_crosses),
        ema_result.get("latest_timestamp"),
    )

    return {
        "status": status,
        "message": (
            "Latest intraday EMA crosses calculated "
            "using historical EMA context and all "
            "available intraday candles."
        ),
        "instrument_key": instrument_key,
        "contract_info": contract_info,
        "market_date": format_date(market_date),
        "requested_at": now_market_time.isoformat(),
        "interval": interval,
        "history_days": historical_days,
        "historical_from_date": from_date,
        "historical_to_date": to_date,
        "ema_fast_period": DEFAULT_EMA_FAST_PERIOD,
        "ema_slow_period": DEFAULT_EMA_SLOW_PERIOD,
        "historical_candles_count": len(historical_candles),
        "intraday_candles_count": len(intraday_candles),
        "combined_candles_count": len(combined_candles),
        "latest_intraday_candle_timestamp": (
            latest_intraday_candle[0] if latest_intraday_candle else None
        ),
        "latest_intraday_close": (
            safe_float(latest_intraday_candle[4]) if latest_intraday_candle else None
        ),
        "latest_ema_timestamp": ema_result.get("latest_timestamp"),
        "latest_close": ema_result.get("latest_close"),
        "latest_ema_fast": ema_result.get("latest_ema_fast"),
        "latest_ema_slow": ema_result.get("latest_ema_slow"),
        "latest_signal": ema_result.get("latest_signal"),
        "all_crosses_count": ema_result.get(
            "crossovers_count",
            0,
        ),
        "intraday_crosses_count": len(intraday_crosses),
        "last_intraday_cross": (intraday_crosses[-1] if intraday_crosses else None),
        "intraday_crosses": intraday_crosses,
        "save_crosses": save_crosses,
        "save_result": save_result,
        "historical_fetch": {
            "status": historical_result.get("status"),
            "candles_count": historical_result.get(
                "candles_count",
                0,
            ),
            "batches": historical_result.get(
                "batches",
                [],
            ),
        },
        "intraday_fetch": {
            "status": intraday_result.get("status"),
            "candles_count": intraday_result.get(
                "candles_count",
                0,
            ),
            "unit": intraday_result.get("unit"),
            "interval": intraday_result.get("interval"),
        },
        "errors": errors,
    }


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
    logger.info(
        "Fetching candles for instrument_key=%s, "
        "interval=%s, from_date=%s, to_date=%s, "
        "fetch_intraday_today=%s",
        instrument_key,
        interval,
        from_date,
        to_date,
        fetch_intraday_today,
    )

    if not from_date or not to_date:
        (
            default_from,
            default_to,
            default_intraday_required,
        ) = get_default_history_range(DEFAULT_HISTORY_DAYS)

        from_date = from_date or format_date(default_from)
        to_date = to_date or format_date(default_to)

        if fetch_intraday_today is False:
            fetch_intraday_today = default_intraday_required

    historical_result = fetch_historical_candle_batches(
        instrument_key=instrument_key,
        interval=interval,
        from_date=from_date,
        to_date=to_date,
        api_version=api_version,
        max_days_per_request=max_days_per_request,
    )

    all_candles = list(
        historical_result.get(
            "candles",
            [],
        )
    )
    batch_results = list(
        historical_result.get(
            "batches",
            [],
        )
    )
    errors = list(
        historical_result.get(
            "errors",
            [],
        )
    )
    intraday_today_used = False

    if fetch_intraday_today:
        intraday_result = fetch_intraday_candles_for_instrument(
            instrument_key=instrument_key,
            interval=interval,
        )

        intraday_candles = intraday_result.get(
            "candles",
            [],
        )

        all_candles.extend(intraday_candles)

        if intraday_candles:
            intraday_today_used = True

        if intraday_result.get("error"):
            errors.append(
                f"Intraday fetch error for "
                f"{instrument_key}: "
                f"{intraday_result.get('error')}"
            )

        batch_results.append(
            {
                "type": "intraday_today",
                "date": format_date(get_today_date()),
                "status": intraday_result.get("status"),
                "candles_count": (
                    intraday_result.get(
                        "candles_count",
                        0,
                    )
                ),
                "unit": intraday_result.get("unit"),
                "interval": intraday_result.get("interval"),
                "error": intraday_result.get("error"),
            }
        )

        if DEFAULT_SLEEP_SECONDS > 0:
            time.sleep(DEFAULT_SLEEP_SECONDS)

    all_candles = normalize_candles(all_candles)

    ema_result = calculate_ema_crossovers(
        candles=all_candles,
        fast_period=DEFAULT_EMA_FAST_PERIOD,
        slow_period=DEFAULT_EMA_SLOW_PERIOD,
    )

    candles_count = len(all_candles)
    ema_status = ema_result.get("status")
    contract_info = get_contract_info_by_key(instrument_key)
    ema_cross_files = None

    try:
        ema_cross_files = save_instrument_ema_crosses(
            instrument_key=instrument_key,
            ema_result=ema_result,
            contract_info=contract_info,
        )

        logger.info(
            "EMA cross files updated. "
            "instrument_key=%s, directory=%s, "
            "historical_received=%s, "
            "intraday_received=%s",
            instrument_key,
            ema_cross_files.get("directory"),
            ema_cross_files.get(
                "historical",
                {},
            ).get("received_count", 0),
            ema_cross_files.get(
                "intraday",
                {},
            ).get("received_count", 0),
        )
    except Exception as ex:
        save_error = (
            f"Failed saving EMA cross files for "
            f"{instrument_key}: "
            f"{type(ex).__name__}: {ex}"
        )

        logger.error(save_error)
        errors.append(save_error)

    if errors and candles_count == 0:
        status = "failed"
    elif ema_status == "empty":
        status = "empty"
    elif ema_status == "insufficient_data":
        status = "insufficient_data"
    elif errors:
        status = "partial_success"
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
        "ema_cross_files": ema_cross_files,
        "batches": batch_results,
        "errors": errors,
        "contract_info": contract_info,
        "processed_at": (get_now_market_time().isoformat()),
    }


def fetch_historical_candles_for_all_subscribed(
    interval: str = DEFAULT_INTERVAL,
    history_days: int = DEFAULT_HISTORY_DAYS,
    save_data: bool = True,
    max_workers: int = DEFAULT_MAX_WORKERS,
) -> dict:
    if not is_historical_candle_enabled():
        logger.info(
            "Historical candle fetch skipped " "because it is disabled in config."
        )

        return {
            "status": "disabled",
            "message": ("Historical candle fetch is disabled."),
            "total_instruments": 0,
            "success_count": 0,
            "failed_count": 0,
            "empty_count": 0,
            "insufficient_data_count": 0,
            "total_candles": 0,
        }

    subscribed_keys = get_subscribed_instrument_keys()

    if not subscribed_keys:
        logger.warning(
            "Historical candle fetch skipped. " "No subscribed instruments found."
        )

        return {
            "status": "skipped",
            "message": ("No subscribed instruments found."),
            "total_instruments": 0,
            "success_count": 0,
            "failed_count": 0,
            "empty_count": 0,
            "insufficient_data_count": 0,
            "total_candles": 0,
        }

    (
        from_date_obj,
        to_date_obj,
        intraday_today_required,
    ) = get_default_history_range(history_days)

    from_date = format_date(from_date_obj)
    to_date = format_date(to_date_obj)

    try:
        max_workers = int(max_workers)
    except Exception:
        max_workers = DEFAULT_MAX_WORKERS

    max_workers = max(
        1,
        max_workers,
    )

    logger.info("Historical and intraday EMA crossover " "fetch started.")

    logger.info(
        "Fetching candles and calculating EMA "
        "crossovers for %s instruments. interval=%s, "
        "historical_from_date=%s, "
        "historical_to_date=%s, "
        "intraday_today_required=%s, "
        "history_days=%s, max_workers=%s, "
        "ema_fast=%s, ema_slow=%s",
        len(subscribed_keys),
        interval,
        from_date,
        to_date,
        intraday_today_required,
        history_days,
        max_workers,
        DEFAULT_EMA_FAST_PERIOD,
        DEFAULT_EMA_SLOW_PERIOD,
    )

    started_at = get_now_market_time().isoformat()
    results = {}
    errors = {}
    success_count = 0
    failed_count = 0
    empty_count = 0
    insufficient_data_count = 0
    total_candles = 0
    intraday_used_count = 0
    historical_crosses_saved = 0
    intraday_crosses_saved = 0
    completed_count = 0
    total_instruments = len(subscribed_keys)

    def worker(
        instrument_key: str,
    ) -> dict:
        return fetch_historical_candles_for_instrument(
            instrument_key=instrument_key,
            interval=interval,
            from_date=from_date,
            to_date=to_date,
            api_version=DEFAULT_API_VERSION,
            max_days_per_request=(DEFAULT_MAX_DAYS_PER_REQUEST),
            save_data=False,
            fetch_intraday_today=(intraday_today_required),
        )

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_instrument = {
            executor.submit(
                worker,
                instrument_key,
            ): instrument_key
            for instrument_key in subscribed_keys
        }

        for future in as_completed(future_to_instrument):
            instrument_key = future_to_instrument[future]
            completed_count += 1

            logger.info(
                "Historical and intraday EMA progress: " "%s/%s instrument_key=%s",
                completed_count,
                total_instruments,
                instrument_key,
            )

            try:
                result = future.result()
                result_status = result.get("status")
                candles_count = int(
                    result.get(
                        "candles_count",
                        0,
                    )
                    or 0
                )

                total_candles += candles_count

                if result.get("intraday_today_used"):
                    intraday_used_count += 1

                ema_cross_files = result.get("ema_cross_files") or {}

                historical_file_result = ema_cross_files.get("historical") or {}

                intraday_file_result = ema_cross_files.get("intraday") or {}

                historical_crosses_saved += int(
                    historical_file_result.get(
                        "saved_count",
                        0,
                    )
                    or 0
                )

                intraday_crosses_saved += int(
                    intraday_file_result.get(
                        "saved_count",
                        0,
                    )
                    or 0
                )

                results[instrument_key] = {
                    "status": result_status,
                    "candles_count": candles_count,
                    "intraday_today_used": (
                        result.get(
                            "intraday_today_used",
                            False,
                        )
                    ),
                    "ema_fast_period": result.get("ema_fast_period"),
                    "ema_slow_period": result.get("ema_slow_period"),
                    "ema_result": result.get("ema_result"),
                    "ema_cross_files": (ema_cross_files),
                    "batches": result.get(
                        "batches",
                        [],
                    ),
                    "errors": result.get(
                        "errors",
                        [],
                    ),
                    "contract_info": result.get(
                        "contract_info",
                        {},
                    ),
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
                    errors[instrument_key] = result.get(
                        "errors",
                        [],
                    )
            except Exception as ex:
                error_message = f"{type(ex).__name__}: {ex}"

                logger.error(
                    "Historical and intraday EMA "
                    "fetch failed for "
                    "instrument_key=%s: %s",
                    instrument_key,
                    error_message,
                )

                failed_count += 1
                errors[instrument_key] = [error_message]

                results[instrument_key] = {
                    "status": "failed",
                    "candles_count": 0,
                    "intraday_today_used": False,
                    "ema_fast_period": (DEFAULT_EMA_FAST_PERIOD),
                    "ema_slow_period": (DEFAULT_EMA_SLOW_PERIOD),
                    "ema_result": None,
                    "ema_cross_files": None,
                    "batches": [],
                    "errors": [error_message],
                    "contract_info": (get_contract_info_by_key(instrument_key)),
                    "processed_at": (get_now_market_time().isoformat()),
                }

    completed_at = get_now_market_time().isoformat()

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
        "intraday_today_required": (intraday_today_required),
        "intraday_used_count": (intraday_used_count),
        "interval": interval,
        "history_days": history_days,
        "max_days_per_request": (DEFAULT_MAX_DAYS_PER_REQUEST),
        "max_workers": max_workers,
        "raw_candles_saved": False,
        "ema_fast_period": (DEFAULT_EMA_FAST_PERIOD),
        "ema_slow_period": (DEFAULT_EMA_SLOW_PERIOD),
        "ema_crosses_directory": str(Path(DEFAULT_EMA_CROSSES_DIRECTORY)),
        "historical_crosses_saved": (historical_crosses_saved),
        "intraday_crosses_saved": (intraday_crosses_saved),
        "total_instruments": total_instruments,
        "success_count": success_count,
        "failed_count": failed_count,
        "empty_count": empty_count,
        "insufficient_data_count": (insufficient_data_count),
        "total_candles": total_candles,
        "results": results,
        "errors": errors,
    }

    ema_results_file_path = None

    if save_data and is_test_flag_enabled():
        try:
            ema_results_file_path = save_ema_cross_results_to_file(summary)

            summary["ema_results_file_path"] = ema_results_file_path

            logger.info(
                "Saved EMA crossover summary to %s",
                ema_results_file_path,
            )
        except Exception as ex:
            error_message = f"{type(ex).__name__}: {ex}"

            logger.error(
                "Failed saving EMA crossover " "summary: %s",
                error_message,
            )

            summary["ema_results_file_error"] = error_message
    else:
        logger.info(
            "EMA crossover summary file not saved. " "save_data=%s, TEST_FLAG=%s",
            save_data,
            is_test_flag_enabled(),
        )

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
        historical_candles_cache["ema_crosses_directory"] = str(
            Path(DEFAULT_EMA_CROSSES_DIRECTORY)
        )
        historical_candles_cache["historical_crosses_saved"] = historical_crosses_saved
        historical_candles_cache["intraday_crosses_saved"] = intraday_crosses_saved
        historical_candles_cache["live_ema_initialized"] = live_ema_initialized
        historical_candles_cache["data"] = results
        historical_candles_cache["errors"] = errors

    logger.info(
        "Historical and intraday EMA crossover "
        "fetch completed. status=%s, "
        "total_instruments=%s, success=%s, "
        "empty=%s, insufficient_data=%s, "
        "failed=%s, total_candles=%s, "
        "historical_crosses_saved=%s, "
        "intraday_crosses_saved=%s, "
        "live_ema_initialized=%s",
        overall_status,
        total_instruments,
        success_count,
        empty_count,
        insufficient_data_count,
        failed_count,
        total_candles,
        historical_crosses_saved,
        intraday_crosses_saved,
        live_ema_initialized,
    )

    return summary


def get_historical_candles_status() -> dict:
    with _history_cache_lock:
        return {
            "last_run_at": (historical_candles_cache.get("last_run_at")),
            "from_date": (historical_candles_cache.get("from_date")),
            "to_date": (historical_candles_cache.get("to_date")),
            "intraday_today_used": (
                historical_candles_cache.get("intraday_today_used")
            ),
            "interval": (historical_candles_cache.get("interval")),
            "total_instruments": (historical_candles_cache.get("total_instruments")),
            "success_count": (historical_candles_cache.get("success_count")),
            "failed_count": (historical_candles_cache.get("failed_count")),
            "empty_count": (historical_candles_cache.get("empty_count")),
            "insufficient_data_count": (
                historical_candles_cache.get("insufficient_data_count")
            ),
            "total_candles": (historical_candles_cache.get("total_candles")),
            "ema_fast_period": (historical_candles_cache.get("ema_fast_period")),
            "ema_slow_period": (historical_candles_cache.get("ema_slow_period")),
            "ema_results_file_path": (
                historical_candles_cache.get("ema_results_file_path")
            ),
            "ema_crosses_directory": (
                historical_candles_cache.get("ema_crosses_directory")
            ),
            "historical_crosses_saved": (
                historical_candles_cache.get("historical_crosses_saved")
            ),
            "intraday_crosses_saved": (
                historical_candles_cache.get("intraday_crosses_saved")
            ),
            "live_ema_initialized": (
                historical_candles_cache.get("live_ema_initialized")
            ),
            "errors": (
                historical_candles_cache.get(
                    "errors",
                    {},
                )
            ),
        }
