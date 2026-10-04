"""
Build the rich isolated EMA alert payload.

Mirrors the shape consumed by downstream systems (Telegram, algo app,
dashboard) but is our own, self-contained implementation.

Payload blocks:
    schema_version, event_id, event_type, source, market, timezone,
    created_at
    instrument        - key, symbol, strike, option_type, expiry, LTP
    opening_range     - levels, selected level, trigger, touch time
    market_snapshot   - index LTP, isolated LTP, snapshot time
    ema               - cross_type, direction, fast/slow, candle
    order_suggestion  - rule, side, nearest instruments, budget filter
    duplicate_control - minute_alert_key, direction
    delivery          - telegram, orders
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from ema_app.isolation.state import (
    IndexIsolationState,
    IsolatedInstrument,
    TouchEvent,
    format_ist,
    now_ist,
)

IST = timezone(timedelta(hours=5, minutes=30))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _safe_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _normalize_option_type(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip().upper()
    if text in ("CE", "CALL"):
        return "CE"
    if text in ("PE", "PUT"):
        return "PE"
    return None


def _opposite(option_type: str) -> Optional[str]:
    if option_type == "CE":
        return "PE"
    if option_type == "PE":
        return "CE"
    return None


def _normalize_direction(value: Any) -> str:
    text = str(value or "").strip().lower()
    if "bullish" in text or text in ("buy", "long", "up"):
        return "bullish"
    if "bearish" in text or text in ("sell", "short", "down"):
        return "bearish"
    return "unknown"


def _make_event_id(instrument_key: str, direction: str, ts: int) -> str:
    normalized_key = str(instrument_key or "unknown").replace("|", "-").replace(" ", "-")
    stamp = datetime.fromtimestamp(ts, tz=IST).strftime("%Y%m%dT%H%M%S")
    return f"EMA-ISO-{normalized_key}-{stamp}-{direction}-{uuid.uuid4().hex[:8]}"


def _make_minute_alert_key(instrument_key: str, ts: int, direction: str) -> str:
    minute_ts = (int(ts) // 60) * 60
    stamp = datetime.fromtimestamp(minute_ts, tz=IST).strftime("%Y-%m-%d::%H_%M")
    return f"{stamp}::{instrument_key}::{direction}"


def _suggest_order_side(
    isolated_type: Optional[str], direction: str
) -> Optional[str]:
    """bullish_same_side_bearish_opposite_side"""
    normalized = _normalize_option_type(isolated_type)
    if not normalized:
        return None
    if direction == "bullish":
        return normalized
    if direction == "bearish":
        return _opposite(normalized)
    return None


# ---------------------------------------------------------------------------
# Payload builder
# ---------------------------------------------------------------------------
def build_payload(
    *,
    state: IndexIsolationState,
    isolated: IsolatedInstrument,
    cross_record: Dict[str, Any],
    index_ltp: Optional[float],
    budget_instruments: Optional[List[Dict[str, Any]]] = None,
    nearest_instruments: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    Build the alert payload for a single EMA cross on the isolated
    instrument. `cross_record` is the record emitted by `ema_service`
    (has cross_time, cross_type, close, ema_fast, ema_slow, etc.).

    `state` is the bucket that owns the winner. Under global scope its
    `index_name` attribute is the sentinel `__GLOBAL__`, so we prefer
    `isolated.underlying` for any index-level labelling.
    """
    cross_time = int(cross_record.get("cross_time") or cross_record.get("time") or 0)
    direction = _normalize_direction(
        cross_record.get("cross_type") or cross_record.get("direction")
    )
    isolated_type = _normalize_option_type(isolated.option_type)
    suggested_side = _suggest_order_side(isolated_type, direction)

    event_id = _make_event_id(isolated.instrument_key, direction, cross_time)
    minute_alert_key = _make_minute_alert_key(
        isolated.instrument_key, cross_time, direction
    )

    close = _safe_float(cross_record.get("close"))
    ema_fast = _safe_float(cross_record.get("ema_fast"))
    ema_slow = _safe_float(cross_record.get("ema_slow"))

    levels = state.opening_range or {}

    # Prefer the winner's own underlying for any index-level field.
    # Under per-index scope this equals `state.index_name`; under global
    # scope it corrects the sentinel back to the winner's real index.
    effective_index = (
        str(isolated.underlying or "").upper()
        or str(state.index_name or "").upper()
    )

    payload: Dict[str, Any] = {
        "schema_version": "1.0",
        "event_id": event_id,
        "event_type": "isolated_instrument_ema_alert",
        "source": "ema_app_isolation",
        "market": "NSE",
        "timezone": "Asia/Kolkata",
        "created_at": now_ist().isoformat(),

        # ------------------------------------------------------------------
        # Instrument
        # ------------------------------------------------------------------
        "instrument": {
            "instrument_key": isolated.instrument_key,
            "trading_symbol": isolated.trading_symbol,
            "underlying_symbol": isolated.underlying,
            "option_type": isolated_type,
            "instrument_type": isolated_type,
            "strike_price": isolated.strike,
            "expiry": isolated.expiry,
            "lot_size": isolated.lot_size,
            "isolated": True,
            "live_ltp": close,
        },

        # ------------------------------------------------------------------
        # Opening range
        # ------------------------------------------------------------------
        "opening_range": {
            "available": bool(levels),
            "selected_level": isolated.selected_level,
            "selected_level_value": isolated.level_value,
            "trigger_field": isolated.trigger_field,
            "trigger_price": isolated.trigger_price,
            "touch_time": isolated.touch_time,
            "touch_time_iso": (
                format_ist(isolated.touch_time) if isolated.touch_time else None
            ),
            "reference_average": isolated.reference_average,
            "selection_reason": isolated.selection_reason,
            "selection_priority": isolated.selection_priority,
            "levels": {
                "r1": _safe_float(levels.get("r1")),
                "r2": _safe_float(levels.get("r2")),
                "r3": _safe_float(levels.get("r3")),
                "s1": _safe_float(levels.get("s1")),
                "s2": _safe_float(levels.get("s2")),
                "s3": _safe_float(levels.get("s3")),
            },
        },

        # ------------------------------------------------------------------
        # Market snapshot
        # ------------------------------------------------------------------
        "market_snapshot": {
            "index_name": effective_index,
            "index_ltp": _safe_float(index_ltp),
            "isolated_instrument_ltp": close,
            "snapshot_at": now_ist().isoformat(),
        },

        # ------------------------------------------------------------------
        # EMA
        # ------------------------------------------------------------------
        "ema": {
            "cross_type": cross_record.get("cross_type"),
            "direction": direction,
            "calculation_mode": cross_record.get("ema_calculation_mode") or "candle_close",
            "fast_period": 9,
            "slow_period": 21,
            "fast_value": ema_fast,
            "slow_value": ema_slow,
            "price": close,
            "timestamp": format_ist(cross_time) if cross_time else None,
            "candle": {
                "time": cross_time,
                "open": _safe_float(cross_record.get("open")),
                "high": _safe_float(cross_record.get("high")),
                "low": _safe_float(cross_record.get("low")),
                "close": close,
            },
        },

        # ------------------------------------------------------------------
        # Order suggestion
        # ------------------------------------------------------------------
        "order_suggestion": {
            "rule": "bullish_same_side_bearish_opposite_side",
            "isolated_instrument_type": isolated_type,
            "suggested_order_side": suggested_side,
            "nearest_instruments": list(nearest_instruments or []),
            "budget_filter": {
                "enabled": True,
                "minimum_price": 20.0,
                "maximum_price": 30.0,
                "maximum_instruments": 2,
                "sort_mode": "nearest_to_budget_midpoint",
                "range_inclusive": True,
                "matched_count": len(budget_instruments or []),
                "instruments": list(budget_instruments or []),
            },
        },

        # ------------------------------------------------------------------
        # Duplicate control
        # ------------------------------------------------------------------
        "duplicate_control": {
            "minute_alert_key": minute_alert_key,
            "direction": direction,
        },

        # ------------------------------------------------------------------
        # Delivery (filled by the caller: telegram + orders)
        # ------------------------------------------------------------------
        "delivery": {
            "telegram": {"enabled": False, "attempted": False, "success": False},
            "orders": {
                "enabled": False,
                "attempted": False,
                "success": False,
                "orders_placed": 0,
                "mode": None,
                "skipped_reason": None,
            },
        },
    }

    return payload


__all__ = ["build_payload"]