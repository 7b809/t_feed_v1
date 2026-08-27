from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from core import config
from core.logger import get_logger

logger = get_logger(__file__)


# ============================================================
# Value Helpers
# ============================================================


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


# ============================================================
# Time Helpers
# ============================================================


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


# ============================================================
# JSON Helpers
# ============================================================


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


# ============================================================
# Event Helpers
# ============================================================


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


# ============================================================
# Candle Payload
# ============================================================


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

    open_price = safe_float(
        candle.get(
            "open",
            ema_event.get("open"),
        )
    )

    high_price = safe_float(
        candle.get(
            "high",
            ema_event.get("high"),
        )
    )

    low_price = safe_float(
        candle.get(
            "low",
            ema_event.get("low"),
        )
    )

    close_price = safe_float(
        candle.get(
            "close",
            ema_event.get(
                "close",
                ema_event.get("ltp"),
            ),
        )
    )

    volume = safe_float(
        candle.get(
            "volume",
            ema_event.get("volume"),
        )
    )

    candle_timestamp = (
        candle.get("timestamp")
        or candle.get("ts")
        or ema_event.get("timestamp")
        or ema_event.get("ts")
    )

    if open_price is None:
        open_price = safe_float(market_ohlc.get("open"))

    if high_price is None:
        high_price = safe_float(market_ohlc.get("high"))

    if low_price is None:
        low_price = safe_float(market_ohlc.get("low"))

    if close_price is None:
        close_price = safe_float(market_ohlc.get("close"))

    if volume is None:
        volume = safe_float(market_ohlc.get("volume"))

    if candle_timestamp is None:
        candle_timestamp = market_ohlc.get("timestamp") or market_ohlc.get("ts")

    close_minus_low = None

    if close_price is not None and low_price is not None:
        close_minus_low = round(
            close_price - low_price,
            4,
        )

    high_minus_low = None

    if high_price is not None and low_price is not None:
        high_minus_low = round(
            high_price - low_price,
            4,
        )

    return {
        "timestamp": normalize_timestamp(candle_timestamp),
        "open": open_price,
        "high": high_price,
        "low": low_price,
        "close": close_price,
        "volume": volume,
        "close_minus_low_points": (close_minus_low),
        "high_minus_low_points": (high_minus_low),
    }


# ============================================================
# Instrument Payload
# ============================================================


def build_instrument_payload(
    instrument_key: str,
    contract_info: dict,
    isolated_instrument_type: str | None,
    ema_candle: dict,
) -> dict:
    if not isinstance(contract_info, dict):
        contract_info = {}

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
        "expiry": (contract_info.get("expiry")),
        "lot_size": safe_int(
            contract_info.get("lot_size"),
            default=0,
        ),
        "isolated": True,
        "live_ltp": ema_candle.get("close"),
    }


# ============================================================
# Opening Range Payload
# ============================================================


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

    levels = {
        **selected_levels,
        **event_opening_range,
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


# ============================================================
# EMA Payload
# ============================================================


def build_ema_payload(
    ema_event: dict,
    ema_candle: dict,
    alert_direction: str,
) -> dict:
    if not isinstance(ema_event, dict):
        ema_event = {}

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
        "cross_type": (ema_event.get("cross_type")),
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


# ============================================================
# Order Instrument Payload
# ============================================================


def normalize_order_instrument(
    instrument: dict,
) -> dict:
    if not isinstance(instrument, dict):
        return {}

    option_type = normalize_option_type(
        instrument.get("instrument_type") or instrument.get("option_type")
    )

    return {
        "instrument_key": (instrument.get("instrument_key")),
        "trading_symbol": (instrument.get("trading_symbol")),
        "instrument_type": option_type,
        "option_type": option_type,
        "strike_price": safe_float(instrument.get("strike_price")),
        "expiry": instrument.get("expiry"),
        "available": bool(
            instrument.get(
                "available",
                bool(instrument.get("instrument_key")),
            )
        ),
        "live_ltp": safe_float(instrument.get("live_ltp")),
        "live_ltp_updated_at": (
            normalize_timestamp(instrument.get("live_ltp_updated_at"))
        ),
        "is_isolated_instrument": bool(instrument.get("is_isolated_instrument")),
        "ema_candle_close": safe_float(instrument.get("ema_candle_close")),
        "ema_candle_low": safe_float(instrument.get("ema_candle_low")),
        "close_minus_low_points": safe_float(instrument.get("close_minus_low_points")),
        "within_budget": bool(
            instrument.get(
                "within_budget",
                False,
            )
        ),
        "minimum_budget_price": safe_float(instrument.get("minimum_budget_price")),
        "maximum_budget_price": safe_float(instrument.get("maximum_budget_price")),
        "distance_from_budget_midpoint": (
            safe_float(instrument.get("distance_from_budget_midpoint"))
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


# ============================================================
# Delivery Metadata
# ============================================================


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


# ============================================================
# Canonical Payload
# ============================================================


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

    resolved_candle = (
        deepcopy(ema_candle)
        if isinstance(ema_candle, dict)
        else extract_ema_candle(ema_event)
    )

    resolved_candle = {
        **extract_ema_candle(
            {
                **ema_event,
                "candle": resolved_candle,
            }
        ),
        **resolved_candle,
    }

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
        "created_at": (get_market_datetime().isoformat()),
        "instrument": (
            build_instrument_payload(
                instrument_key=instrument_key,
                contract_info=(selected_contract_info),
                isolated_instrument_type=(resolved_isolated_type),
                ema_candle=resolved_candle,
            )
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
            "latest_intraday_close": safe_float(ema_event.get("latest_intraday_close")),
            "snapshot_at": (get_market_datetime().isoformat()),
        },
        "ema": build_ema_payload(
            ema_event=ema_event,
            ema_candle=resolved_candle,
            alert_direction=direction,
        ),
        "order_suggestion": (
            build_order_suggestion_payload(
                isolated_instrument_type=(resolved_isolated_type),
                suggested_order_option_type=(resolved_order_side),
                suggested_instruments=(suggested_instruments or []),
                budget_instruments=(budget_instruments or []),
            )
        ),
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

        ema_payload.pop(
            "fast_period",
            None,
        )

        ema_payload.pop(
            "slow_period",
            None,
        )

        ema_payload.pop(
            "fast_value",
            None,
        )

        ema_payload.pop(
            "slow_value",
            None,
        )

        ema_payload.pop(
            "previous_fast_value",
            None,
        )

        ema_payload.pop(
            "previous_slow_value",
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
            "EMA_ALGO_PAYLOAD_INCLUDE_DELIVERY_METADATA",
            False,
        )
    ):
        payload.pop(
            "delivery",
            None,
        )

    return make_json_safe(clean_optional_values(payload))


# ============================================================
# Payload Service
# ============================================================


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
            alert_direction=alert_direction,
            nifty_ltp=nifty_ltp,
        )

    def extract_ema_candle(
        self,
        ema_event: dict,
    ) -> dict:
        return extract_ema_candle(ema_event)

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
