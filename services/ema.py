from typing import Any

from core import config
from utils.common import safe_float


def normalize_candle(raw: Any, source: str) -> dict | None:
    from utils.common import parse_timestamp, safe_int
    if isinstance(raw, (list, tuple)) and len(raw) >= 5:
        ts, op, hi, lo, close = raw[:5]
        volume = raw[5] if len(raw) > 5 else 0
        oi = raw[6] if len(raw) > 6 else 0
    elif isinstance(raw, dict):
        ts = raw.get("timestamp") or raw.get("time") or raw.get("ts") or raw.get("start_time")
        op, hi, lo, close = raw.get("open"), raw.get("high"), raw.get("low"), raw.get("close")
        volume = raw.get("volume", raw.get("vol", 0))
        oi = raw.get("open_interest", raw.get("oi", 0))
    else:
        return None
    stamp = parse_timestamp(ts)
    values = [safe_float(value) for value in (op, hi, lo, close)]
    if stamp is None or any(value is None for value in values):
        return None
    return {"timestamp": stamp.isoformat(), "date": stamp.date().isoformat(), "open": values[0], "high": values[1],
            "low": values[2], "close": values[3], "volume": safe_int(volume), "open_interest": safe_int(oi), "source": source}


def calculate_sequence(candles: list[dict]) -> tuple[list[dict], dict | None]:
    state = None
    output = []
    for candle in sorted({c["timestamp"]: c for c in candles}.values(), key=lambda row: row["timestamp"]):
        enriched, state = update_state(state, candle)
        output.append(enriched)
    return output, state


def update_state(state: dict | None, candle: dict) -> tuple[dict, dict]:
    close = safe_float(candle.get("close"))
    if close is None:
        raise ValueError("Candle close is invalid")
    count = int((state or {}).get("valid_candle_count", 0)) + 1
    old_fast = (state or {}).get("ema_9")
    old_slow = (state or {}).get("ema_21")
    old_diff = (state or {}).get("ema_difference")
    fast = close if old_fast is None else ((close - old_fast) * (2 / (config.EMA_FAST_PERIOD + 1))) + old_fast
    slow = close if old_slow is None else ((close - old_slow) * (2 / (config.EMA_SLOW_PERIOD + 1))) + old_slow
    diff = fast - slow
    cross = None
    if count >= config.EMA_SLOW_PERIOD and old_diff is not None:
        if old_diff <= 0 < diff:
            cross = "bullish"
        elif old_diff >= 0 > diff:
            cross = "bearish"
    enriched = {**candle, "ema_9": round(fast, 6), "ema_21": round(slow, 6), "ema_difference": round(diff, 6),
                "previous_ema_difference": round(old_diff, 6) if old_diff is not None else None,
                "cross_type": cross, "is_bullish_cross": cross == "bullish", "is_bearish_cross": cross == "bearish"}
    new_state = {"ema_9": fast, "ema_21": slow, "ema_difference": diff, "valid_candle_count": count,
                 "last_processed_timestamp": candle["timestamp"]}
    return enriched, new_state
