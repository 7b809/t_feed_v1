from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from core import config
from core.logger import get_logger

logger = get_logger(__file__)


def safe_float(
    value: Any,
    default: float | None = None,
) -> float | None:
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
    option_type: Any,
) -> str | None:
    if option_type is None:
        return None

    normalized = str(option_type).strip().upper()

    if normalized in {
        "CE",
        "CALL",
    }:
        return "CE"

    if normalized in {
        "PE",
        "PUT",
    }:
        return "PE"

    return None


def get_opposite_option_type(
    option_type: Any,
) -> str | None:
    normalized = normalize_option_type(option_type)

    if normalized == "CE":
        return "PE"

    if normalized == "PE":
        return "CE"

    return None


def normalize_cross_direction(
    cross_type: Any,
    current_signal: Any = None,
) -> str:
    cross_text = str(cross_type or "").strip().lower()

    signal_text = str(current_signal or "").strip().lower()

    bullish_values = {
        "bullish",
        "bullish_cross",
        "buy",
        "long",
        "up",
    }

    bearish_values = {
        "bearish",
        "bearish_cross",
        "sell",
        "short",
        "down",
    }

    if (
        "bullish" in cross_text
        or cross_text in bullish_values
        or signal_text in bullish_values
    ):
        return "bullish"

    if (
        "bearish" in cross_text
        or cross_text in bearish_values
        or signal_text in bearish_values
    ):
        return "bearish"

    return "unknown"


def get_suggested_order_side(
    cross_type: Any,
    isolated_instrument_type: Any,
) -> str | None:
    isolated_type = normalize_option_type(isolated_instrument_type)

    cross_direction = normalize_cross_direction(cross_type)

    if isolated_type:
        if cross_direction == "bullish":
            return isolated_type

        if cross_direction == "bearish":
            return get_opposite_option_type(isolated_type)

    if cross_direction == "bullish":
        return normalize_option_type(
            getattr(
                config,
                "EMA_ALERT_BULLISH_OPTION_TYPE",
                "CE",
            )
        )

    if cross_direction == "bearish":
        return normalize_option_type(
            getattr(
                config,
                "EMA_ALERT_BEARISH_OPTION_TYPE",
                "PE",
            )
        )

    return None


def get_market_timezone() -> ZoneInfo:
    timezone_name = str(
        getattr(
            config,
            "MARKET_TIMEZONE",
            "Asia/Kolkata",
        )
        or "Asia/Kolkata"
    ).strip()

    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        return ZoneInfo("Asia/Kolkata")


def get_market_datetime() -> datetime:
    return datetime.now(get_market_timezone())


def normalize_timestamp(
    value: Any,
) -> str | None:
    if value is None:
        return None

    market_timezone = get_market_timezone()

    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=market_timezone)
        else:
            value = value.astimezone(market_timezone)

        return value.isoformat()

    if isinstance(value, date):
        return datetime.combine(
            value,
            datetime.min.time(),
            tzinfo=market_timezone,
        ).isoformat()

    if isinstance(
        value,
        (int, float),
    ):
        numeric_value = float(value)

        if numeric_value > 1_000_000_000_000:
            numeric_value /= 1000.0

        try:
            return datetime.fromtimestamp(
                numeric_value,
                tz=market_timezone,
            ).isoformat()
        except (
            TypeError,
            ValueError,
            OverflowError,
            OSError,
        ):
            return str(value)

    text = str(value).strip()

    if not text:
        return None

    normalized_text = text.replace(
        "Z",
        "+00:00",
    )

    try:
        parsed = datetime.fromisoformat(normalized_text)

        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=market_timezone)
        else:
            parsed = parsed.astimezone(market_timezone)

        return parsed.isoformat()
    except ValueError:
        return text


def make_json_safe(
    value: Any,
) -> Any:
    if value is None:
        return None

    if isinstance(
        value,
        (str, int, float, bool),
    ):
        return value

    if isinstance(value, Decimal):
        return float(value)

    if isinstance(value, datetime):
        return normalize_timestamp(value)

    if isinstance(value, date):
        return value.isoformat()

    if isinstance(value, dict):
        return {str(key): make_json_safe(item) for key, item in value.items()}

    if isinstance(
        value,
        (list, tuple, set),
    ):
        return [make_json_safe(item) for item in value]

    if hasattr(value, "to_dict"):
        try:
            return make_json_safe(value.to_dict())
        except Exception:
            return str(value)

    return str(value)


def clean_optional_values(
    value: Any,
) -> Any:
    if isinstance(value, dict):
        return {
            key: clean_optional_values(item)
            for key, item in value.items()
            if item is not None
        }

    if isinstance(value, list):
        return [clean_optional_values(item) for item in value]

    return value


def get_live_ema_calculation_mode() -> str:
    return (
        "tick_ltp"
        if bool(
            getattr(
                config,
                "LIVE_EMA_CALCULATION_MODE",
                False,
            )
        )
        else "candle_close"
    )


def build_event_id(
    instrument_key: str,
    direction: str,
    event_timestamp: Any = None,
) -> str:
    normalized_instrument_key = (
        str(instrument_key or "unknown-instrument")
        .strip()
        .replace("|", "-")
        .replace(" ", "-")
    )

    normalized_direction = str(direction or "unknown").strip().lower()

    normalized_timestamp = normalize_timestamp(event_timestamp)

    if normalized_timestamp:
        timestamp_text = (
            normalized_timestamp.replace("-", "")
            .replace(":", "")
            .replace("+", "")
            .replace(".", "")
        )

        timestamp_text = timestamp_text[:20]
    else:
        timestamp_text = get_market_datetime().strftime("%Y%m%dT%H%M%S")

    return (
        f"EMA-{normalized_instrument_key}-"
        f"{timestamp_text}-"
        f"{normalized_direction}-"
        f"{uuid4().hex[:8]}"
    )


def normalize_candle_payload(
    candle: Any,
    fallback_event: dict | None = None,
) -> dict | None:
    fallback_event = fallback_event if isinstance(fallback_event, dict) else {}

    if not isinstance(candle, dict):
        candle = {}

    if not candle and not fallback_event:
        return None

    open_price = safe_float(
        candle.get(
            "open",
            fallback_event.get("open"),
        )
    )

    high_price = safe_float(
        candle.get(
            "high",
            fallback_event.get("high"),
        )
    )

    low_price = safe_float(
        candle.get(
            "low",
            fallback_event.get("low"),
        )
    )

    close_price = safe_float(
        candle.get(
            "close",
            fallback_event.get(
                "close",
                fallback_event.get("ltp"),
            ),
        )
    )

    volume = safe_float(
        candle.get(
            "volume",
            candle.get(
                "vol",
                fallback_event.get("volume"),
            ),
        )
    )

    timestamp = (
        candle.get("timestamp")
        or candle.get("time")
        or candle.get("ts")
        or candle.get("start_time")
        or fallback_event.get("timestamp")
        or fallback_event.get("ts")
    )

    close_minus_low = safe_float(candle.get("close_minus_low_points"))

    if close_minus_low is None and close_price is not None and low_price is not None:
        close_minus_low = round(
            close_price - low_price,
            4,
        )

    high_minus_low = safe_float(candle.get("high_minus_low_points"))

    if high_minus_low is None and high_price is not None and low_price is not None:
        high_minus_low = round(
            high_price - low_price,
            4,
        )

    normalized = {
        "timestamp": normalize_timestamp(timestamp),
        "open": open_price,
        "high": high_price,
        "low": low_price,
        "close": close_price,
        "volume": volume,
        "close_minus_low_points": (close_minus_low),
        "high_minus_low_points": (high_minus_low),
    }

    if not any(value is not None for value in normalized.values()):
        return None

    return normalized


def extract_ema_candle(
    ema_event: dict,
) -> dict:
    if not isinstance(ema_event, dict):
        ema_event = {}

    candle = ema_event.get("candle") or {}

    if not isinstance(candle, dict):
        candle = {}

    market_ohlc = ema_event.get("market_ohlc") or ema_event.get("marketOHLC") or {}

    if not isinstance(market_ohlc, dict):
        market_ohlc = {}

    merged_fallback = {
        **market_ohlc,
        **ema_event,
    }

    normalized = normalize_candle_payload(
        candle=candle,
        fallback_event=merged_fallback,
    )

    return normalized or {
        "timestamp": None,
        "open": None,
        "high": None,
        "low": None,
        "close": None,
        "volume": None,
        "close_minus_low_points": None,
        "high_minus_low_points": None,
    }


def build_instrument_payload(
    instrument_key: str,
    contract_info: dict,
    isolated_instrument_type: str | None,
    ema_candle: dict,
) -> dict:
    if not isinstance(contract_info, dict):
        contract_info = {}

    if not isinstance(ema_candle, dict):
        ema_candle = {}

    option_type = normalize_option_type(
        isolated_instrument_type
    ) or normalize_option_type(
        contract_info.get("instrument_type") or contract_info.get("option_type")
    )

    return {
        "instrument_key": instrument_key,
        "trading_symbol": (contract_info.get("trading_symbol")),
        "underlying_symbol": (contract_info.get("underlying_symbol") or "NIFTY"),
        "underlying_type": (contract_info.get("underlying_type")),
        "instrument_type": option_type,
        "option_type": option_type,
        "strike_price": safe_float(contract_info.get("strike_price")),
        "expiry": contract_info.get("expiry"),
        "lot_size": safe_int(
            contract_info.get("lot_size"),
            default=0,
        ),
        "isolated": True,
        "live_ltp": safe_float(ema_candle.get("close")),
    }


def build_opening_range_payload(
    selected_state: dict,
    ema_event: dict,
) -> dict:
    if not isinstance(selected_state, dict):
        selected_state = {}

    if not isinstance(ema_event, dict):
        ema_event = {}

    event_opening_range = ema_event.get("opening_range") or {}

    if not isinstance(
        event_opening_range,
        dict,
    ):
        event_opening_range = {}

    selected_levels = selected_state.get("levels") or {}

    if not isinstance(selected_levels, dict):
        selected_levels = {}

    event_levels = (
        event_opening_range.get("levels")
        if isinstance(
            event_opening_range.get("levels"),
            dict,
        )
        else event_opening_range
    )

    levels = {
        **selected_levels,
        **event_levels,
    }

    return {
        "available": bool(levels),
        "selected_level": (
            selected_state.get("selected_level") or ema_event.get("selected_level")
        ),
        "selected_level_value": safe_float(
            selected_state.get("level_value")
            or selected_state.get("selected_level_value")
            or ema_event.get("selected_level_value")
        ),
        "trigger_price": safe_float(selected_state.get("trigger_price")),
        "trigger_field": (selected_state.get("trigger_field")),
        "touch_time": normalize_timestamp(selected_state.get("touch_time")),
        "touch_source": (
            selected_state.get("touch_source") or selected_state.get("selection_source")
        ),
        "selected_at": normalize_timestamp(selected_state.get("selected_at")),
        "selection_priority": (selected_state.get("selection_priority")),
        "selection_reason": (selected_state.get("selection_reason")),
        "reference_average": safe_float(selected_state.get("reference_average")),
        "average_window": deepcopy(selected_state.get("average_window")),
        "range": deepcopy(selected_state.get("range")),
        "levels": {
            "r1": safe_float(levels.get("r1")),
            "s1": safe_float(levels.get("s1")),
            "r2": safe_float(levels.get("r2")),
            "s2": safe_float(levels.get("s2")),
            "r3": safe_float(levels.get("r3")),
            "s3": safe_float(levels.get("s3")),
            "sub_resistance": safe_float(levels.get("sub_resistance")),
            "sub_support": safe_float(levels.get("sub_support")),
        },
        "touch_status": deepcopy(ema_event.get("touch_status") or {}),
        "processed_at": normalize_timestamp(ema_event.get("processed_at")),
    }


def build_ema_payload(
    ema_event: dict,
    ema_candle: dict,
    alert_direction: str,
) -> dict:
    if not isinstance(ema_event, dict):
        ema_event = {}

    if not isinstance(ema_candle, dict):
        ema_candle = {}

    calculation_mode = (
        str(
            ema_event.get(
                "ema_calculation_mode",
                get_live_ema_calculation_mode(),
            )
            or get_live_ema_calculation_mode()
        )
        .strip()
        .lower()
    )

    fast_period = safe_int(
        ema_event.get(
            "ema_fast_period",
            getattr(
                config,
                "LIVE_EMA_FAST_PERIOD",
                9,
            ),
        ),
        default=getattr(
            config,
            "LIVE_EMA_FAST_PERIOD",
            9,
        ),
    )

    slow_period = safe_int(
        ema_event.get(
            "ema_slow_period",
            getattr(
                config,
                "LIVE_EMA_SLOW_PERIOD",
                21,
            ),
        ),
        default=getattr(
            config,
            "LIVE_EMA_SLOW_PERIOD",
            21,
        ),
    )

    return {
        "cross_type": ema_event.get("cross_type"),
        "direction": alert_direction,
        "signal": (ema_event.get("current_signal") or alert_direction),
        "previous_signal": (ema_event.get("previous_signal")),
        "current_signal": (ema_event.get("current_signal")),
        "calculation_mode": calculation_mode,
        "fast_period": fast_period,
        "slow_period": slow_period,
        "fast_value": safe_float(ema_event.get("ema_fast")),
        "slow_value": safe_float(ema_event.get("ema_slow")),
        "previous_fast_value": safe_float(ema_event.get("previous_ema_fast")),
        "previous_slow_value": safe_float(ema_event.get("previous_ema_slow")),
        "price": safe_float(
            ema_event.get(
                "close",
                ema_event.get("ltp"),
            )
        ),
        "source": (ema_event.get("source") or "live_feed"),
        "timestamp": (
            ema_candle.get("timestamp")
            or normalize_timestamp(ema_event.get("timestamp"))
        ),
        "candle": deepcopy(ema_candle),
    }


def normalize_order_instrument(
    instrument: dict,
) -> dict:
    if not isinstance(instrument, dict):
        return {}

    instrument_key = str(instrument.get("instrument_key") or "").strip()

    option_type = normalize_option_type(
        instrument.get("instrument_type") or instrument.get("option_type")
    )

    candle = normalize_candle_payload(
        instrument.get("candle")
        or instrument.get("latest_candle")
        or instrument.get("completed_candle")
    )

    live_ltp = safe_float(
        instrument.get("live_ltp"),
        default=safe_float(instrument.get("ltp")),
    )

    if live_ltp is None and isinstance(candle, dict):
        live_ltp = safe_float(candle.get("close"))

    return {
        "instrument_key": (instrument_key or None),
        "trading_symbol": (instrument.get("trading_symbol")),
        "underlying_symbol": (instrument.get("underlying_symbol")),
        "underlying_type": (instrument.get("underlying_type")),
        "instrument_type": option_type,
        "option_type": option_type,
        "strike_price": safe_float(instrument.get("strike_price")),
        "expiry": instrument.get("expiry"),
        "lot_size": safe_int(
            instrument.get("lot_size"),
            default=0,
        ),
        "available": bool(
            instrument.get(
                "available",
                bool(instrument_key),
            )
        ),
        # ==========================================
        # Option Chain Data
        # ==========================================
        "ltp": safe_float(instrument.get("ltp")),
        "live_ltp": live_ltp,
        "close_price": safe_float(instrument.get("close_price")),
        "pcr": safe_float(instrument.get("pcr")),
        "underlying_spot_price": safe_float(instrument.get("underlying_spot_price")),
        "market_data": deepcopy(instrument.get("market_data") or {}),
        "option_greeks": deepcopy(instrument.get("option_greeks") or {}),
        "data_source": instrument.get("data_source"),
        # ==========================================
        # Existing Fields
        # ==========================================
        "live_ltp_updated_at": (
            normalize_timestamp(
                instrument.get("live_ltp_updated_at") or instrument.get("updated_at")
            )
        ),
        "candle": deepcopy(candle),
        "is_isolated_instrument": bool(instrument.get("is_isolated_instrument")),
        "ema_candle_close": safe_float(
            instrument.get("ema_candle_close"),
            default=(
                safe_float(candle.get("close")) if isinstance(candle, dict) else None
            ),
        ),
        "ema_candle_low": safe_float(
            instrument.get("ema_candle_low"),
            default=(
                safe_float(candle.get("low")) if isinstance(candle, dict) else None
            ),
        ),
        "close_minus_low_points": safe_float(
            instrument.get("close_minus_low_points"),
            default=(
                safe_float(candle.get("close_minus_low_points"))
                if isinstance(candle, dict)
                else None
            ),
        ),
        "within_budget": bool(
            instrument.get(
                "within_budget",
                False,
            )
        ),
        "minimum_budget_price": safe_float(instrument.get("minimum_budget_price")),
        "maximum_budget_price": safe_float(instrument.get("maximum_budget_price")),
        "distance_from_budget_midpoint": safe_float(
            instrument.get("distance_from_budget_midpoint")
        ),
        "distance_from_nifty": safe_float(instrument.get("distance_from_nifty")),
    }


def normalize_order_instruments(
    instruments: list | tuple | None,
) -> list:
    if not isinstance(
        instruments,
        (list, tuple),
    ):
        return []

    output = []

    for item in instruments:
        normalized = normalize_order_instrument(item)

        if normalized:
            output.append(normalized)

    return output


def build_order_suggestion_payload(
    isolated_instrument_type: str | None,
    suggested_order_option_type: str | None,
    suggested_instruments: list,
    budget_instruments: list,
) -> dict:
    isolated_type = normalize_option_type(isolated_instrument_type)

    suggested_side = normalize_option_type(suggested_order_option_type)

    nearest_instruments = normalize_order_instruments(suggested_instruments)

    normalized_budget_instruments = normalize_order_instruments(budget_instruments)

    return {
        "rule": ("bullish_same_side_" "bearish_opposite_side"),
        "isolated_instrument_type": (isolated_type),
        "suggested_order_side": (suggested_side),
        "nearest_instruments": (nearest_instruments),
        "budget_filter": {
            "enabled": bool(
                getattr(
                    config,
                    "EMA_ALERT_BUDGET_RANGE_ENABLED",
                    True,
                )
            ),
            "minimum_price": safe_float(
                getattr(
                    config,
                    "EMA_ALERT_BUDGET_MIN_PRICE",
                    20.0,
                )
            ),
            "maximum_price": safe_float(
                getattr(
                    config,
                    "EMA_ALERT_BUDGET_MAX_PRICE",
                    30.0,
                )
            ),
            "maximum_instruments": safe_int(
                getattr(
                    config,
                    "EMA_ALERT_BUDGET_MAX_INSTRUMENTS",
                    2,
                ),
                default=2,
            ),
            "sort_mode": str(
                getattr(
                    config,
                    "EMA_ALERT_BUDGET_SORT_MODE",
                    "nearest_to_budget_midpoint",
                )
            )
            .strip()
            .lower(),
            "range_inclusive": bool(
                getattr(
                    config,
                    "EMA_ALERT_BUDGET_RANGE_INCLUSIVE",
                    True,
                )
            ),
            "matched_count": len(normalized_budget_instruments),
            "instruments": (normalized_budget_instruments),
        },
    }


def build_delivery_metadata() -> dict:
    return {
        "telegram": {
            "enabled": bool(
                getattr(
                    config,
                    "EMA_ISOLATED_INSTRUMENT_TELEGRAM_ENABLED",
                    True,
                )
            ),
            "attempted": False,
            "success": False,
        },
        "algo_app": {
            "enabled": bool(
                getattr(
                    config,
                    "ALGO_APP_ENABLED",
                    False,
                )
            ),
            "attempted": False,
            "dispatched": False,
            "success": False,
        },
    }


def build_isolated_ema_alert_payload(
    ema_event: dict,
    selected_state: dict,
    contract_info: dict | None = None,
    isolated_instrument_type: str | None = None,
    suggested_order_option_type: str | None = None,
    suggested_instruments: list | None = None,
    budget_instruments: list | None = None,
    ema_candle: dict | None = None,
    minute_alert_key: str | None = None,
    alert_direction: str | None = None,
    nifty_ltp: float | None = None,
) -> dict:
    if not isinstance(ema_event, dict):
        ema_event = {}

    if not isinstance(selected_state, dict):
        selected_state = {}

    selected_contract_info = (
        contract_info
        or selected_state.get("contract_info")
        or ema_event.get("contract_info")
        or {}
    )

    if not isinstance(
        selected_contract_info,
        dict,
    ):
        selected_contract_info = {}

    instrument_key = str(
        ema_event.get("instrument_key")
        or selected_state.get("instrument_key")
        or selected_contract_info.get("instrument_key")
        or ""
    ).strip()

    resolved_isolated_type = normalize_option_type(
        isolated_instrument_type
    ) or normalize_option_type(
        selected_contract_info.get("instrument_type")
        or selected_contract_info.get("option_type")
    )

    direction = (
        str(alert_direction).strip().lower()
        if alert_direction
        else normalize_cross_direction(
            ema_event.get("cross_type"),
            ema_event.get("current_signal"),
        )
    )

    resolved_order_side = normalize_option_type(
        suggested_order_option_type
    ) or get_suggested_order_side(
        ema_event.get("cross_type"),
        resolved_isolated_type,
    )

    if isinstance(ema_candle, dict):
        resolved_candle = normalize_candle_payload(
            ema_candle,
            fallback_event=ema_event,
        )
    else:
        resolved_candle = extract_ema_candle(ema_event)

    if not resolved_candle:
        resolved_candle = extract_ema_candle(ema_event)

    event_timestamp = resolved_candle.get("timestamp") or ema_event.get("timestamp")

    event_id = build_event_id(
        instrument_key=instrument_key,
        direction=direction,
        event_timestamp=event_timestamp,
    )

    resolved_nifty_ltp = safe_float(
        nifty_ltp,
        default=safe_float(
            ema_event.get("latest_main_index_ltp"),
            default=safe_float(selected_state.get("latest_main_index_ltp")),
        ),
    )

    order_suggestion = build_order_suggestion_payload(
        isolated_instrument_type=(resolved_isolated_type),
        suggested_order_option_type=(resolved_order_side),
        suggested_instruments=(suggested_instruments or []),
        budget_instruments=(budget_instruments or []),
    )

    now_market = get_market_datetime()

    payload = {
        "schema_version": str(
            getattr(
                config,
                "ALGO_APP_PAYLOAD_SCHEMA_VERSION",
                "1.0",
            )
        ),
        "event_id": (
            event_id
            if bool(
                getattr(
                    config,
                    "ALGO_APP_INCLUDE_EVENT_ID",
                    True,
                )
            )
            else None
        ),
        "event_type": ("isolated_instrument_ema_alert"),
        "source": str(
            getattr(
                config,
                "ALGO_APP_SOURCE_NAME",
                "option_feed_engine",
            )
        ),
        "market": "NSE",
        "timezone": str(
            getattr(
                config,
                "MARKET_TIMEZONE",
                "Asia/Kolkata",
            )
        ),
        "created_at": now_market.isoformat(),
        "instrument": build_instrument_payload(
            instrument_key=instrument_key,
            contract_info=(selected_contract_info),
            isolated_instrument_type=(resolved_isolated_type),
            ema_candle=resolved_candle,
        ),
        "opening_range": (
            build_opening_range_payload(
                selected_state=selected_state,
                ema_event=ema_event,
            )
        ),
        "market_snapshot": {
            "nifty_ltp": resolved_nifty_ltp,
            "isolated_instrument_ltp": (resolved_candle.get("close")),
            "latest_intraday_close": (
                safe_float(ema_event.get("latest_intraday_close"))
            ),
            "snapshot_at": (now_market.isoformat()),
        },
        "ema": build_ema_payload(
            ema_event=ema_event,
            ema_candle=resolved_candle,
            alert_direction=direction,
        ),
        "order_suggestion": order_suggestion,
        "duplicate_control": {
            "minute_alert_key": (minute_alert_key),
            "direction": direction,
        },
        "delivery": (build_delivery_metadata()),
        "raw_ema_event": deepcopy(ema_event),
    }

    if not bool(
        getattr(
            config,
            "EMA_ALGO_PAYLOAD_INCLUDE_OPENING_RANGE",
            True,
        )
    ):
        payload.pop(
            "opening_range",
            None,
        )

    if not bool(
        getattr(
            config,
            "EMA_ALGO_PAYLOAD_INCLUDE_EMA_VALUES",
            True,
        )
    ):
        ema_payload = payload.get(
            "ema",
            {},
        )

        for field_name in (
            "fast_period",
            "slow_period",
            "fast_value",
            "slow_value",
            "previous_fast_value",
            "previous_slow_value",
        ):
            ema_payload.pop(
                field_name,
                None,
            )

    if not bool(
        getattr(
            config,
            "EMA_ALGO_PAYLOAD_INCLUDE_CANDLE",
            True,
        )
    ):
        payload.get(
            "ema",
            {},
        ).pop(
            "candle",
            None,
        )

    if not bool(
        getattr(
            config,
            "EMA_ALGO_PAYLOAD_INCLUDE_NEAREST_INSTRUMENTS",
            True,
        )
    ):
        payload.get(
            "order_suggestion",
            {},
        )["nearest_instruments"] = []

    if not bool(
        getattr(
            config,
            "EMA_ALGO_PAYLOAD_INCLUDE_NEAREST_INSTRUMENT_CANDLES",
            True,
        )
    ):
        nearest_instruments = payload.get(
            "order_suggestion",
            {},
        ).get(
            "nearest_instruments",
            [],
        )

        for instrument in nearest_instruments:
            if isinstance(instrument, dict):
                instrument.pop(
                    "candle",
                    None,
                )

    if not bool(
        getattr(
            config,
            "EMA_ALGO_PAYLOAD_INCLUDE_BUDGET_INSTRUMENTS",
            True,
        )
    ):
        budget_filter = payload.get(
            "order_suggestion",
            {},
        ).get(
            "budget_filter",
            {},
        )

        budget_filter["matched_count"] = 0

        budget_filter["instruments"] = []

    if not bool(
        getattr(
            config,
            "EMA_ALGO_PAYLOAD_INCLUDE_BUDGET_INSTRUMENT_CANDLES",
            True,
        )
    ):
        budget_instruments_payload = (
            payload.get(
                "order_suggestion",
                {},
            )
            .get(
                "budget_filter",
                {},
            )
            .get(
                "instruments",
                [],
            )
        )

        for instrument in budget_instruments_payload:
            if isinstance(instrument, dict):
                instrument.pop(
                    "candle",
                    None,
                )

    if not bool(
        getattr(
            config,
            "EMA_ALGO_PAYLOAD_INCLUDE_DELIVERY_METADATA",
            False,
        )
    ):
        payload.pop(
            "delivery",
            None,
        )

    if not bool(
        getattr(
            config,
            "EMA_ALGO_PAYLOAD_INCLUDE_RAW_EMA_EVENT",
            True,
        )
    ):
        payload.pop(
            "raw_ema_event",
            None,
        )

    return make_json_safe(clean_optional_values(payload))


class EmaAlertPayloadService:
    def build_isolated_ema_alert_payload(
        self,
        ema_event: dict,
        selected_state: dict,
        contract_info: dict | None = None,
        isolated_instrument_type: str | None = None,
        suggested_order_option_type: str | None = None,
        suggested_instruments: list | None = None,
        budget_instruments: list | None = None,
        ema_candle: dict | None = None,
        minute_alert_key: str | None = None,
        alert_direction: str | None = None,
        nifty_ltp: float | None = None,
    ) -> dict:
        return build_isolated_ema_alert_payload(
            ema_event=ema_event,
            selected_state=selected_state,
            contract_info=contract_info,
            isolated_instrument_type=(isolated_instrument_type),
            suggested_order_option_type=(suggested_order_option_type),
            suggested_instruments=(suggested_instruments),
            budget_instruments=(budget_instruments),
            ema_candle=ema_candle,
            minute_alert_key=(minute_alert_key),
            alert_direction=(alert_direction),
            nifty_ltp=nifty_ltp,
        )

    def extract_ema_candle(
        self,
        ema_event: dict,
    ) -> dict:
        return extract_ema_candle(ema_event)

    def normalize_order_instrument(
        self,
        instrument: dict,
    ) -> dict:
        return normalize_order_instrument(instrument)

    def get_suggested_order_side(
        self,
        cross_type: Any,
        isolated_instrument_type: Any,
    ) -> str | None:
        return get_suggested_order_side(
            cross_type=cross_type,
            isolated_instrument_type=(isolated_instrument_type),
        )

    def make_json_safe(
        self,
        value: Any,
    ) -> Any:
        return make_json_safe(value)


ema_alert_payload_service = EmaAlertPayloadService()


__all__ = [
    "safe_float",
    "safe_int",
    "normalize_option_type",
    "get_opposite_option_type",
    "normalize_cross_direction",
    "get_suggested_order_side",
    "get_market_timezone",
    "get_market_datetime",
    "normalize_timestamp",
    "make_json_safe",
    "clean_optional_values",
    "get_live_ema_calculation_mode",
    "build_event_id",
    "normalize_candle_payload",
    "extract_ema_candle",
    "build_instrument_payload",
    "build_opening_range_payload",
    "build_ema_payload",
    "normalize_order_instrument",
    "normalize_order_instruments",
    "build_order_suggestion_payload",
    "build_delivery_metadata",
    "build_isolated_ema_alert_payload",
    "EmaAlertPayloadService",
    "ema_alert_payload_service",
]
