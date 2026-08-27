import json
from datetime import date, datetime
from pathlib import Path
from threading import Lock
from typing import Any

import upstox_client
from upstox_client.rest import ApiException

from core import config
from core.logger import get_logger
from services.token_service import token_service

logger = get_logger(__file__)


# ============================================================
# Runtime Cache
# ============================================================

_cache_lock = Lock()

options_cache = {
    "nearest_expiry": None,
    "total_contracts": 0,
    "subscribed_keys": [],
    "data": [],
    "contracts_by_key": {},
    "contracts_by_strike_type": {},
}


# ============================================================
# Feed Constants
# ============================================================

NIFTY_INDEX_FEED = {
    "instrument_key": getattr(
        config,
        "MAIN_NIFTY_SECURITY",
        "NSE_INDEX|Nifty 50",
    ),
    "instrument_type": "INDEX",
    "strike_price": None,
    "expiry": None,
    "trading_symbol": "NIFTY 50",
    "underlying_type": "INDEX",
    "underlying_symbol": "NIFTY 50",
}

NIFTY_SUPPORTED_INTERVALS = [0]

OPTION_SUPPORTED_INTERVALS = [
    0,
    1,
    3,
    5,
]

SUPPORTED_INTERVALS = OPTION_SUPPORTED_INTERVALS


# ============================================================
# Value Helpers
# ============================================================


def safe_float(
    value: Any,
    default: float = 0.0,
) -> float:
    try:
        if value is None:
            return default

        return float(value)
    except (
        TypeError,
        ValueError,
        OverflowError,
    ):
        return default


def safe_optional_float(
    value: Any,
) -> float | None:
    try:
        if value is None:
            return None

        return float(value)
    except (
        TypeError,
        ValueError,
        OverflowError,
    ):
        return None


def safe_int(
    value: Any,
    default: int = 0,
) -> int:
    try:
        if value is None:
            return default

        return int(value)
    except (
        TypeError,
        ValueError,
        OverflowError,
    ):
        return default


def normalize_option_type(
    option_type: str | None,
) -> str | None:
    if not option_type:
        return None

    value = str(option_type).strip().upper()

    if value in {
        "CE",
        "CALL",
    }:
        return "CE"

    if value in {
        "PE",
        "PUT",
    }:
        return "PE"

    return None


def get_opposite_option_type(
    option_type: str | None,
) -> str | None:
    normalized_type = normalize_option_type(option_type)

    if normalized_type == "CE":
        return "PE"

    if normalized_type == "PE":
        return "CE"

    return None


def parse_expiry_date(
    expiry_value: Any,
) -> date | None:
    if not expiry_value:
        return None

    if isinstance(expiry_value, datetime):
        return expiry_value.date()

    if isinstance(expiry_value, date):
        return expiry_value

    if isinstance(expiry_value, str):
        try:
            date_part = expiry_value.split()[0]

            return datetime.strptime(
                date_part,
                "%Y-%m-%d",
            ).date()
        except ValueError as ex:
            logger.warning(
                "Failed to parse expiry date. value=%s, error=%s",
                expiry_value,
                ex,
            )

    return None


def clean_contract_data(
    item: dict,
) -> dict:
    expiry_date = parse_expiry_date(item.get("expiry"))

    expiry_text = (
        expiry_date.strftime("%Y-%m-%d")
        if expiry_date
        else str(item.get("expiry") or "")
    )

    instrument_type = normalize_option_type(
        item.get("instrument_type") or item.get("option_type")
    )

    return {
        "instrument_key": item.get("instrument_key"),
        "instrument_type": (instrument_type or item.get("instrument_type")),
        "option_type": instrument_type,
        "strike_price": item.get("strike_price"),
        "expiry": expiry_text,
        "trading_symbol": item.get("trading_symbol"),
        "underlying_type": item.get("underlying_type"),
        "underlying_symbol": item.get("underlying_symbol"),
    }


def build_strike_type_key(
    strike_price: Any,
    instrument_type: str,
) -> str | None:
    option_type = normalize_option_type(instrument_type)

    if strike_price is None or not option_type:
        return None

    try:
        return f"{float(strike_price)}_" f"{option_type}"
    except (
        TypeError,
        ValueError,
        OverflowError,
    ):
        return None


def round_to_nearest_strike(
    value: Any,
    strike_step: int | None = None,
) -> int:
    step = safe_int(
        strike_step,
        safe_int(
            getattr(
                config,
                "EMA_ALERT_STRIKE_STEP",
                50,
            ),
            50,
        ),
    )

    if step <= 0:
        step = 50

    spot = safe_float(value)

    return int(round(spot / step) * step)


def clamp_strike_to_filter_range(
    strike_value: float,
) -> float:
    strike_from = safe_float(
        getattr(
            config,
            "STRIKE_FROM",
            strike_value,
        )
    )

    strike_to = safe_float(
        getattr(
            config,
            "STRIKE_TO",
            strike_value,
        )
    )

    return max(
        strike_from,
        min(
            float(strike_value),
            strike_to,
        ),
    )


def is_strike_inside_filter_range(
    strike_value: Any,
) -> bool:
    if strike_value is None:
        return False

    strike = safe_float(strike_value)

    strike_from = safe_float(
        getattr(
            config,
            "STRIKE_FROM",
            0.0,
        )
    )

    strike_to = safe_float(
        getattr(
            config,
            "STRIKE_TO",
            0.0,
        )
    )

    return strike_from <= strike <= strike_to


def is_strike_inside_window(
    strike_value: Any,
    lower_limit: Any,
    upper_limit: Any,
) -> bool:
    if strike_value is None:
        return False

    strike = safe_float(strike_value)
    lower = safe_float(lower_limit)
    upper = safe_float(upper_limit)

    return lower <= strike <= upper


# ============================================================
# Contract Filtering
# ============================================================


def get_nearest_expiry_contracts(
    contracts: list,
) -> tuple[str | None, list]:
    if not contracts:
        return None, []

    today = datetime.now().date()
    valid_expiries = set()

    for item in contracts:
        if not isinstance(item, dict):
            continue

        expiry_date = parse_expiry_date(item.get("expiry"))

        if expiry_date and expiry_date >= today:
            valid_expiries.add(expiry_date)

    if not valid_expiries:
        logger.warning("No valid future expiry dates found.")

        return None, []

    nearest_date = min(valid_expiries)

    nearest_date_text = nearest_date.strftime("%Y-%m-%d")

    matching_contracts = [
        clean_contract_data(item)
        for item in contracts
        if (
            isinstance(item, dict)
            and parse_expiry_date(item.get("expiry")) == nearest_date
        )
    ]

    strike_from = safe_float(
        getattr(
            config,
            "STRIKE_FROM",
            0.0,
        )
    )

    strike_to = safe_float(
        getattr(
            config,
            "STRIKE_TO",
            0.0,
        )
    )

    matching_contracts = [
        item
        for item in matching_contracts
        if (
            item.get("strike_price") is not None
            and strike_from <= safe_float(item.get("strike_price")) <= strike_to
        )
    ]

    return (
        nearest_date_text,
        matching_contracts,
    )


def _build_cache_indexes(
    data: list,
) -> tuple[dict, dict]:
    contracts_by_key = {}
    contracts_by_strike_type = {}

    for item in data or []:
        if not isinstance(item, dict):
            continue

        instrument_key = item.get("instrument_key")

        if instrument_key:
            contracts_by_key[str(instrument_key)] = item.copy()

        strike_type_key = build_strike_type_key(
            item.get("strike_price"),
            item.get("instrument_type"),
        )

        if strike_type_key:
            contracts_by_strike_type[strike_type_key] = item.copy()

    return (
        contracts_by_key,
        contracts_by_strike_type,
    )


def _refresh_cache_indexes_locked() -> None:
    data = options_cache.get(
        "data",
        [],
    )

    (
        contracts_by_key,
        contracts_by_strike_type,
    ) = _build_cache_indexes(data)

    options_cache["contracts_by_key"] = contracts_by_key

    options_cache["contracts_by_strike_type"] = contracts_by_strike_type


# ============================================================
# Cache Access
# ============================================================


def get_subscribed_instrument_keys() -> list:
    with _cache_lock:
        return list(
            options_cache.get(
                "subscribed_keys",
                [],
            )
        )


def get_cached_option_contracts() -> list:
    with _cache_lock:
        return [
            item.copy()
            for item in options_cache.get(
                "data",
                [],
            )
            if isinstance(item, dict)
        ]


def get_all_cached_instruments() -> list:
    return [
        NIFTY_INDEX_FEED.copy(),
        *get_cached_option_contracts(),
    ]


def get_contract_info_by_instrument_key(
    instrument_key: str,
) -> dict | None:
    main_key = getattr(
        config,
        "MAIN_NIFTY_SECURITY",
        "NSE_INDEX|Nifty 50",
    )

    if instrument_key == main_key:
        return NIFTY_INDEX_FEED.copy()

    normalized_key = str(instrument_key or "").strip()

    if not normalized_key:
        return None

    with _cache_lock:
        item = options_cache.get(
            "contracts_by_key",
            {},
        ).get(normalized_key)

        if item:
            return item.copy()

        cached_data = list(
            options_cache.get(
                "data",
                [],
            )
        )

    for item in cached_data:
        if not isinstance(item, dict):
            continue

        if str(item.get("instrument_key") or "").strip() == normalized_key:
            return item.copy()

    return None


def get_contract_info_by_strike_type(
    strike_price: float,
    instrument_type: str,
) -> dict | None:
    option_type = normalize_option_type(instrument_type)

    if strike_price is None or not option_type:
        return None

    strike_type_key = build_strike_type_key(
        strike_price,
        option_type,
    )

    if not strike_type_key:
        return None

    with _cache_lock:
        item = options_cache.get(
            "contracts_by_strike_type",
            {},
        ).get(strike_type_key)

        if item:
            return item.copy()

        cached_data = list(
            options_cache.get(
                "data",
                [],
            )
        )

    target_strike = safe_float(strike_price)

    for contract in cached_data:
        if not isinstance(contract, dict):
            continue

        contract_strike = safe_float(contract.get("strike_price"))

        contract_type = normalize_option_type(
            contract.get("instrument_type") or contract.get("option_type")
        )

        if contract_strike == target_strike and contract_type == option_type:
            return contract.copy()

    return None


def get_option_contracts_in_strike_window(
    lower_limit: float,
    upper_limit: float,
    option_types: list[str] | None = None,
) -> list:
    normalized_types = None

    if option_types:
        normalized_types = {
            normalized_type
            for item in option_types
            if (normalized_type := normalize_option_type(item))
        }

    lower = safe_float(lower_limit)
    upper = safe_float(upper_limit)

    cached_data = get_cached_option_contracts()

    output = []

    for item in cached_data:
        strike = item.get("strike_price")

        option_type = normalize_option_type(
            item.get("instrument_type") or item.get("option_type")
        )

        if strike is None or not option_type:
            continue

        if normalized_types and option_type not in normalized_types:
            continue

        if is_strike_inside_window(
            strike,
            lower,
            upper,
        ):
            output.append(item)

    return output


def get_option_contracts_in_average_window(
    average_value: float,
    window_points: float | None = None,
    option_types: list[str] | None = None,
) -> dict:
    window = safe_float(
        window_points,
        safe_float(
            getattr(
                config,
                "OPENING_RANGE_ISOLATION_AVERAGE_WINDOW_POINTS",
                500.0,
            ),
            500.0,
        ),
    )

    average = safe_float(average_value)

    configured_from = safe_float(
        getattr(
            config,
            "STRIKE_FROM",
            0.0,
        )
    )

    configured_to = safe_float(
        getattr(
            config,
            "STRIKE_TO",
            0.0,
        )
    )

    raw_lower = average - window
    raw_upper = average + window

    final_lower = max(
        configured_from,
        raw_lower,
    )

    final_upper = min(
        configured_to,
        raw_upper,
    )

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
    if not contracts:
        return None

    average = safe_float(average_value)

    valid_contracts = [
        item
        for item in contracts
        if (isinstance(item, dict) and item.get("strike_price") is not None)
    ]

    if not valid_contracts:
        return None

    nearest_contract = min(
        valid_contracts,
        key=lambda item: abs(safe_float(item.get("strike_price")) - average),
    )

    return nearest_contract.copy()


# ============================================================
# EMA Order Side
# ============================================================


def get_order_option_type_for_ema_cross(
    cross_type: str,
) -> str | None:
    cross_text = str(cross_type or "").strip().lower()

    if "bullish" in cross_text:
        return normalize_option_type(
            getattr(
                config,
                "EMA_ALERT_BULLISH_OPTION_TYPE",
                "CE",
            )
        )

    if "bearish" in cross_text:
        return normalize_option_type(
            getattr(
                config,
                "EMA_ALERT_BEARISH_OPTION_TYPE",
                "PE",
            )
        )

    return None


def get_order_option_type_for_isolated_ema_cross(
    cross_type: str,
    isolated_instrument_type: str | None,
) -> str | None:
    isolated_type = normalize_option_type(isolated_instrument_type)

    if not isolated_type:
        return None

    cross_text = str(cross_type or "").strip().lower()

    if "bullish" in cross_text:
        return isolated_type

    if "bearish" in cross_text:
        return get_opposite_option_type(isolated_type)

    return None


# ============================================================
# EMA Nearest Instruments
# ============================================================


def build_nearest_order_strikes(
    current_nifty_ltp: float,
    strike_step: int | None = None,
    offsets: list[int] | None = None,
    clamp_to_filter_range: bool | None = None,
) -> list[int]:
    step = safe_int(strike_step, safe_int(config, "EMA_ALERT_STRIKE_STEP", 50), 50)

    if step <= 0:
        step = 50

    selected_offsets = offsets

    if selected_offsets is None:
        selected_offsets = list(
            getattr(
                config,
                "EMA_ALERT_NEAREST_STRIKE_OFFSETS",
                [-50, 0, 50],
            )
        )

    should_clamp = (
        bool(clamp_to_filter_range)
        if clamp_to_filter_range is not None
        else bool(
            getattr(
                config,
                "EMA_ALERT_ORDER_STRIKES_CLAMP_TO_FILTER_RANGE",
                True,
            )
        )
    )

    nearest_strike = round_to_nearest_strike(
        current_nifty_ltp,
        step,
    )

    output: list[int] = []

    for offset in selected_offsets:
        strike = nearest_strike + safe_int(offset)

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
    if isolated_instrument_type:
        option_type = get_order_option_type_for_isolated_ema_cross(
            cross_type=cross_type,
            isolated_instrument_type=(isolated_instrument_type),
        )
    else:
        option_type = get_order_option_type_for_ema_cross(cross_type)

    if not option_type:
        return []

    strikes = build_nearest_order_strikes(current_nifty_ltp)

    output = []

    for strike in strikes:
        contract = get_contract_info_by_strike_type(
            strike,
            option_type,
        )

        if not contract:
            output.append(
                {
                    "strike_price": float(strike),
                    "instrument_type": option_type,
                    "option_type": option_type,
                    "instrument_key": None,
                    "trading_symbol": (f"NIFTY {strike} " f"{option_type}"),
                    "available": False,
                    "live_ltp": None,
                }
            )

            continue

        output.append(
            {
                **contract,
                "instrument_type": option_type,
                "option_type": option_type,
                "available": True,
                "live_ltp": None,
            }
        )

    max_items = safe_int(
        getattr(
            config,
            "EMA_ALERT_MAX_ORDER_INSTRUMENTS",
            3,
        ),
        3,
    )

    return output[:max_items]


# ============================================================
# EMA Budget Instruments
# ============================================================


def get_budget_range_order_instruments(
    option_type: str,
    ltp_by_instrument: dict[str, Any],
    current_nifty_ltp: float | None = None,
    minimum_price: float | None = None,
    maximum_price: float | None = None,
    maximum_instruments: int | None = None,
    subscribed_only: bool | None = None,
    sort_mode: str | None = None,
    inclusive: bool | None = None,
) -> list:
    if not bool(
        getattr(
            config,
            "EMA_ALERT_BUDGET_RANGE_ENABLED",
            True,
        )
    ):
        return []

    normalized_option_type = normalize_option_type(option_type)

    if not normalized_option_type:
        return []

    if not isinstance(
        ltp_by_instrument,
        dict,
    ):
        return []

    min_price = (
        safe_float(minimum_price)
        if minimum_price is not None
        else safe_float(
            getattr(
                config,
                "EMA_ALERT_BUDGET_MIN_PRICE",
                20.0,
            ),
            20.0,
        )
    )

    max_price = (
        safe_float(maximum_price)
        if maximum_price is not None
        else safe_float(
            getattr(
                config,
                "EMA_ALERT_BUDGET_MAX_PRICE",
                30.0,
            ),
            30.0,
        )
    )

    max_items = (
        safe_int(maximum_instruments)
        if maximum_instruments is not None
        else safe_int(
            getattr(
                config,
                "EMA_ALERT_BUDGET_MAX_INSTRUMENTS",
                2,
            ),
            2,
        )
    )

    use_subscribed_only = (
        bool(subscribed_only)
        if subscribed_only is not None
        else bool(
            getattr(
                config,
                "EMA_ALERT_BUDGET_SUBSCRIBED_ONLY",
                True,
            )
        )
    )

    selected_sort_mode = (
        str(
            sort_mode
            if sort_mode is not None
            else getattr(
                config,
                "EMA_ALERT_BUDGET_SORT_MODE",
                "nearest_to_budget_midpoint",
            )
        )
        .strip()
        .lower()
    )

    range_inclusive = (
        bool(inclusive)
        if inclusive is not None
        else bool(
            getattr(
                config,
                "EMA_ALERT_BUDGET_RANGE_INCLUSIVE",
                True,
            )
        )
    )

    budget_midpoint = (min_price + max_price) / 2

    nifty_ltp = safe_optional_float(current_nifty_ltp)

    with _cache_lock:
        contracts = [
            item.copy()
            for item in options_cache.get(
                "data",
                [],
            )
            if isinstance(item, dict)
        ]

        subscribed_keys = set(
            options_cache.get(
                "subscribed_keys",
                [],
            )
        )

    candidates = []

    for contract in contracts:
        instrument_key = str(contract.get("instrument_key") or "").strip()

        if not instrument_key:
            continue

        if use_subscribed_only and instrument_key not in subscribed_keys:
            continue

        contract_option_type = normalize_option_type(
            contract.get("instrument_type") or contract.get("option_type")
        )

        if contract_option_type != normalized_option_type:
            continue

        raw_ltp = ltp_by_instrument.get(instrument_key)

        if isinstance(raw_ltp, dict):
            raw_ltp = raw_ltp.get("ltp")

        live_ltp = safe_optional_float(raw_ltp)

        if live_ltp is None:
            continue

        if range_inclusive:
            within_range = min_price <= live_ltp <= max_price
        else:
            within_range = min_price < live_ltp < max_price

        if not within_range:
            continue

        strike_price = safe_optional_float(contract.get("strike_price"))

        distance_from_nifty = None

        if nifty_ltp is not None and strike_price is not None:
            distance_from_nifty = abs(strike_price - nifty_ltp)

        candidates.append(
            {
                **contract,
                "instrument_type": (contract_option_type),
                "option_type": (contract_option_type),
                "live_ltp": live_ltp,
                "within_budget": True,
                "minimum_budget_price": min_price,
                "maximum_budget_price": max_price,
                "distance_from_budget_midpoint": abs(live_ltp - budget_midpoint),
                "distance_from_nifty": (distance_from_nifty),
            }
        )

    if selected_sort_mode == "nearest_to_nifty":
        candidates.sort(
            key=lambda item: (
                (
                    item.get("distance_from_nifty")
                    if item.get("distance_from_nifty") is not None
                    else float("inf")
                ),
                item.get(
                    "distance_from_budget_midpoint",
                    float("inf"),
                ),
                (
                    item.get("strike_price")
                    if item.get("strike_price") is not None
                    else float("inf")
                ),
            )
        )

    elif selected_sort_mode == "price_ascending":
        candidates.sort(
            key=lambda item: (
                item.get(
                    "live_ltp",
                    float("inf"),
                ),
                (
                    item.get("strike_price")
                    if item.get("strike_price") is not None
                    else float("inf")
                ),
            )
        )

    elif selected_sort_mode == "price_descending":
        candidates.sort(
            key=lambda item: (
                -item.get(
                    "live_ltp",
                    0.0,
                ),
                (
                    item.get("strike_price")
                    if item.get("strike_price") is not None
                    else float("inf")
                ),
            )
        )

    else:
        candidates.sort(
            key=lambda item: (
                item.get(
                    "distance_from_budget_midpoint",
                    float("inf"),
                ),
                (
                    item.get("distance_from_nifty")
                    if item.get("distance_from_nifty") is not None
                    else float("inf")
                ),
                (
                    item.get("strike_price")
                    if item.get("strike_price") is not None
                    else float("inf")
                ),
            )
        )

    return candidates[:max_items]


# ============================================================
# Feed Discovery
# ============================================================


def get_available_feeds() -> list:
    feeds = [
        {
            **NIFTY_INDEX_FEED,
            "supported_intervals": (NIFTY_SUPPORTED_INTERVALS),
        }
    ]

    for item in get_cached_option_contracts():
        feeds.append(
            {
                **item,
                "supported_intervals": (OPTION_SUPPORTED_INTERVALS),
            }
        )

    return feeds


def get_feed_by_instrument_key(
    instrument_key: str,
) -> dict | None:
    main_key = getattr(
        config,
        "MAIN_NIFTY_SECURITY",
        "NSE_INDEX|Nifty 50",
    )

    if instrument_key == main_key:
        return {
            **NIFTY_INDEX_FEED,
            "supported_intervals": (NIFTY_SUPPORTED_INTERVALS),
        }

    contract = get_contract_info_by_instrument_key(instrument_key)

    if not contract:
        return None

    return {
        **contract,
        "supported_intervals": (OPTION_SUPPORTED_INTERVALS),
    }


def is_nifty_index_feed(
    instrument_key: str,
) -> bool:
    main_key = getattr(
        config,
        "MAIN_NIFTY_SECURITY",
        "NSE_INDEX|Nifty 50",
    )

    return instrument_key == main_key


def is_valid_feed_interval(
    instrument_key: str,
    interval: int,
) -> bool:
    if is_nifty_index_feed(instrument_key):
        return interval in NIFTY_SUPPORTED_INTERVALS

    return interval in OPTION_SUPPORTED_INTERVALS


# ============================================================
# Upstox Options Fetch
# ============================================================


def get_options_contracts(
    instrument_key: str | None = None,
    expiry_date: str | None = None,
    output_filename: str = ("data/nearest_nifty_option_contracts.json"),
    filter_nearest: bool = True,
    save_data: bool = False,
) -> dict | None:
    if not instrument_key:
        instrument_key = getattr(
            config,
            "MAIN_NIFTY_SECURITY",
            "NSE_INDEX|Nifty 50",
        )

    access_token = token_service.get_access_token()

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

        response_dict = (
            api_response.to_dict()
            if hasattr(
                api_response,
                "to_dict",
            )
            else api_response
        )

        if not isinstance(response_dict, dict):
            response_dict = {}

        all_contracts = response_dict.get(
            "data",
            [],
        )

        if filter_nearest and not expiry_date:
            (
                nearest_expiry,
                filtered_contracts,
            ) = get_nearest_expiry_contracts(all_contracts)

            final_output = {
                "status": response_dict.get(
                    "status",
                    "success",
                ),
                "nearest_expiry": (nearest_expiry),
                "total_contracts": len(filtered_contracts),
                "data": filtered_contracts,
            }
        else:
            cleaned_contracts = [
                clean_contract_data(item)
                for item in all_contracts
                if isinstance(item, dict)
            ]

            final_output = {
                "status": response_dict.get(
                    "status",
                    "success",
                ),
                "nearest_expiry": expiry_date,
                "total_contracts": len(cleaned_contracts),
                "data": cleaned_contracts,
            }

        option_keys = [
            item.get("instrument_key")
            for item in final_output.get(
                "data",
                [],
            )
            if item.get("instrument_key")
        ]

        keys_to_subscribe = list(
            dict.fromkeys(
                [
                    instrument_key,
                    *option_keys,
                ]
            )
        )

        (
            contracts_by_key,
            contracts_by_strike_type,
        ) = _build_cache_indexes(
            final_output.get(
                "data",
                [],
            )
        )

        with _cache_lock:
            options_cache["nearest_expiry"] = final_output.get("nearest_expiry")

            options_cache["total_contracts"] = final_output.get(
                "total_contracts",
                0,
            )

            options_cache["subscribed_keys"] = keys_to_subscribe

            options_cache["data"] = final_output.get(
                "data",
                [],
            )

            options_cache["contracts_by_key"] = contracts_by_key

            options_cache["contracts_by_strike_type"] = contracts_by_strike_type

        logger.info(
            "Updated options cache. "
            "subscribed_keys=%s, "
            "contracts_by_key=%s, "
            "contracts_by_strike_type=%s",
            len(keys_to_subscribe),
            len(contracts_by_key),
            len(contracts_by_strike_type),
        )

        if save_data:
            output_path = Path(output_filename)

            output_path.parent.mkdir(
                parents=True,
                exist_ok=True,
            )

            with output_path.open(
                "w",
                encoding="utf-8",
            ) as file:
                json.dump(
                    final_output,
                    file,
                    indent=4,
                    default=str,
                )

            logger.info(
                "Saved options response to %s",
                output_path.resolve(),
            )

        return final_output

    except ApiException as ex:
        logger.error(
            "Upstox option contracts request failed: %s",
            getattr(
                ex,
                "body",
                str(ex),
            ),
        )

        return None

    except Exception as ex:
        logger.error(
            "Unexpected option contracts error: %s: %s",
            type(ex).__name__,
            ex,
        )

        return None


# ============================================================
# Cache Summary
# ============================================================


def get_options_cache_summary() -> dict:
    with _cache_lock:
        cached_data = options_cache.get(
            "data",
            [],
        )

        return {
            "nearest_expiry": (options_cache.get("nearest_expiry")),
            "total_contracts": (options_cache.get("total_contracts")),
            "subscribed_keys_count": len(
                options_cache.get(
                    "subscribed_keys",
                    [],
                )
            ),
            "contracts_by_key_count": len(
                options_cache.get(
                    "contracts_by_key",
                    {},
                )
            ),
            "contracts_by_strike_type_count": len(
                options_cache.get(
                    "contracts_by_strike_type",
                    {},
                )
            ),
            "sample_subscribed_keys": (
                options_cache.get(
                    "subscribed_keys",
                    [],
                )[:5]
            ),
            "sample_contract": (cached_data[0] if cached_data else None),
        }


# ============================================================
# Public API
# ============================================================

__all__ = [
    "options_cache",
    "NIFTY_INDEX_FEED",
    "NIFTY_SUPPORTED_INTERVALS",
    "OPTION_SUPPORTED_INTERVALS",
    "SUPPORTED_INTERVALS",
    "safe_float",
    "safe_optional_float",
    "safe_int",
    "normalize_option_type",
    "get_opposite_option_type",
    "parse_expiry_date",
    "clean_contract_data",
    "build_strike_type_key",
    "round_to_nearest_strike",
    "clamp_strike_to_filter_range",
    "is_strike_inside_filter_range",
    "is_strike_inside_window",
    "get_nearest_expiry_contracts",
    "get_subscribed_instrument_keys",
    "get_cached_option_contracts",
    "get_all_cached_instruments",
    "get_contract_info_by_instrument_key",
    "get_contract_info_by_strike_type",
    "get_option_contracts_in_strike_window",
    "get_option_contracts_in_average_window",
    "get_nearest_contract_to_average",
    "get_order_option_type_for_ema_cross",
    "get_order_option_type_for_isolated_ema_cross",
    "build_nearest_order_strikes",
    "get_nearest_order_instruments_for_ema_cross",
    "get_budget_range_order_instruments",
    "get_available_feeds",
    "get_feed_by_instrument_key",
    "is_nifty_index_feed",
    "is_valid_feed_interval",
    "get_options_contracts",
    "get_options_cache_summary",
]


# ============================================================
# Manual Test
# ============================================================

# if __name__ == "__main__":
#     logger.info(
#         "Executing option contracts fetch and filtering..."
#     )
#
#     result = get_options_contracts(
#         instrument_key="NSE_INDEX|Nifty 50",
#         output_filename=(
#             "data/nearest_nifty_option_contracts.json"
#         ),
#         filter_nearest=True,
#         save_data=True,
#     )
#
#     if result:
#         print("\n--- Processed Options Contracts ---")
#         print(
#             "Target Expiry Date:",
#             result.get("nearest_expiry"),
#         )
#         print(
#             "Total Contracts:",
#             result.get("total_contracts"),
#         )
#
#         if result.get("data"):
#             print("Sample Contract Format:")
#             print(
#                 json.dumps(
#                     result["data"][0],
#                     indent=4,
#                 )
#             )
