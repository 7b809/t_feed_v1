"""Build a diagnostic isolation document from a searched option contract.

This builder is used only by the explicit search API diagnostic endpoint.
It mirrors the persisted isolation document's public shape but clearly
marks the record as dummy/test data. It does not place or manage orders.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from core.config import ISOLATION_DOC_ID, TIMEZONE
from services.execution_mode_service import pin_execution_mode


def _as_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def build_dummy_isolation_record(
    *,
    instrument: dict,
    strike: int,
    bias: str,
    nifty_ltp: float,
    option_ltp: float,
    touch_time_iso: str,
    nifty_ltp_source: str = "dummy_param",
) -> dict:
    """Create an isolation-shaped diagnostic record without live detection."""
    if not isinstance(instrument, dict):
        raise TypeError("instrument must be a dictionary")

    instrument_key = instrument.get("instrument_key")
    if not instrument_key:
        raise ValueError("instrument must contain instrument_key")

    bias = str(bias or "").strip().upper()
    if bias not in ("CE", "PE"):
        raise ValueError("bias must be CE or PE")

    touch_dt = datetime.fromisoformat(str(touch_time_iso).replace("Z", "+00:00"))
    if touch_dt.tzinfo is None:
        touch_dt = touch_dt.replace(tzinfo=ZoneInfo(TIMEZONE))
    touch_dt = touch_dt.astimezone(ZoneInfo(TIMEZONE))
    touch_time = touch_dt.isoformat()

    now = datetime.now(ZoneInfo(TIMEZONE))
    nifty_price = _as_float(nifty_ltp)
    option_price = _as_float(option_ltp)
    mode_payload = pin_execution_mode(touch_time, option_price)

    record = {
        "_id": ISOLATION_DOC_ID,
        "trade_date_ist": touch_dt.date().isoformat(),
        "instrument_key": str(instrument_key),
        "exchange_token": instrument.get("exchange_token"),
        "trading_symbol": instrument.get("trading_symbol"),
        "short_name": instrument.get("short_name"),
        "name": instrument.get("name"),
        "strike": instrument.get("strike_price", strike),
        "option_type": instrument.get("instrument_type") or bias,
        "expiry": instrument.get("expiry"),
        "lot_size": instrument.get("lot_size"),
        "tick_size": instrument.get("tick_size"),
        "segment": instrument.get("segment") or "FO",
        "exchange": instrument.get("exchange") or "NSE",
        "underlying": instrument.get("underlying_symbol") or "NIFTY",
        "bias": bias,
        "strike_computed": int(strike),
        "nifty_ltp_at_isolation": nifty_price,
        "nifty_ltp_source": nifty_ltp_source,
        "trigger_index": "DUMMY",
        "trigger_index_canonical": "DUMMY",
        "trigger_side": "DUMMY",
        "trigger_level": None,
        "trigger_level_name": "DUMMY_DIAGNOSTIC",
        "trigger_value": None,
        "trigger_priority": None,
        "simultaneous_touches": [],
        "level_ref": None,
        "routed_index": "NIFTY",
        "routed_ltp": nifty_price,
        "isolation_touch_time": touch_time,
        "isolation_created_at": now.isoformat(),
        "isolated_option_ltp": option_price,
        "isolated_option_ltp_source": "dummy_param",
        **mode_payload,
        "status": "ACTIVE",
        "source": "dummy_isolation_builder",
        "is_dummy": True,
        "isolation_backfilled": False,
        "opposite_leg": None,
    }
    record["isolation_id"] = (
        f"{record['trade_date_ist']}__{record['instrument_key']}"
    )
    record["dummy_metadata"] = {
        "is_test_record": True,
        "warning": (
            "This record was created by the diagnostic search endpoint, "
            "not by the live isolation detector."
        ),
        "created_at_ist": now.isoformat(),
    }
    record["explanation"] = {
        "summary": (
            f"Dummy diagnostic for {record.get('trading_symbol') or instrument_key}; "
            f"bias={bias}; strike={strike}; NIFTY={nifty_price:.2f}; "
            f"option LTP={option_price:.2f}."
        ),
        "backfilled": False,
        "or_levels_source": {"first_candle": None, "computed_levels": {}},
        "trigger_context": {
            "index_key": "DUMMY",
            "canonical": "DUMMY",
            "side": "DUMMY",
            "level_name": "DUMMY_DIAGNOSTIC",
            "level_value": None,
            "index_ltp_at_touch": None,
            "detected_at": touch_time,
            "priority": None,
            "simultaneous_touches": [],
        },
        "nifty_routing": {
            "nifty_ltp": nifty_price,
            "source": nifty_ltp_source,
            "bias_source": "dummy_diagnostic",
        },
        "trend": {
            "trend": "DUMMY",
            "bias": bias,
            "reason": "Bias was supplied to the diagnostic endpoint.",
        },
        "strike_generation": {
            "step": None,
            "nifty_ltp": nifty_price,
            "formula": "Explicit diagnostic strike supplied by request.",
            "strike": int(strike),
            "option_type": bias,
        },
        "option_snapshot": {
            "instrument_key": str(instrument_key),
            "trading_symbol": record.get("trading_symbol"),
            "option_ltp": option_price,
            "option_ltp_source": "dummy_param",
        },
        "mode_decision": {
            "standard_execution_mode": mode_payload["standard_execution_mode"],
            "early_extreme_reversal": mode_payload["early_extreme_reversal"],
            "reason": mode_payload.get("execution_mode_reason"),
        },
    }
    return record
