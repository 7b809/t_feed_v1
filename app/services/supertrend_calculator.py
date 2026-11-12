"""TradingView-compatible Supertrend calculation and confirmed signal extraction."""
from __future__ import annotations
from typing import Any, Iterable

class SupertrendCalculationError(ValueError):
    pass

def _true_ranges(candles: list[dict[str, Any]]) -> list[float]:
    result, previous_close = [], None
    for candle in candles:
        high, low, close = float(candle["high"]), float(candle["low"]), float(candle["close"])
        tr = high - low if previous_close is None else max(high-low, abs(high-previous_close), abs(low-previous_close))
        result.append(max(0.0, tr))
        previous_close = close
    return result

def _atr_values(trs: list[float], period: int, change_atr: bool) -> list[float | None]:
    out: list[float | None] = [None] * len(trs)
    if change_atr:
        seed, previous = [], None
        for i, tr in enumerate(trs):
            if previous is None:
                seed.append(tr)
                if len(seed) == period:
                    previous = sum(seed) / period
                    out[i] = previous
            else:
                previous = (previous * (period - 1) + tr) / period
                out[i] = previous
    else:
        window = []
        for i, tr in enumerate(trs):
            window.append(tr)
            if len(window) > period:
                window.pop(0)
            if len(window) == period:
                out[i] = sum(window) / period
    return out

def calculate_supertrend(candles: Iterable[dict[str, Any]], period: int = 10,
                         multiplier: float = 3.0, change_atr: bool = True
                         ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Pine v5 Supertrend: hl2 source, Wilder ATR or SMA(TR), confirmed flips."""
    rows = sorted([dict(c) for c in candles if c.get("timestamp") is not None],
                  key=lambda c: str(c.get("timestamp")))
    if period < 1:
        raise SupertrendCalculationError("ATR period must be >= 1")
    if multiplier <= 0:
        raise SupertrendCalculationError("ATR multiplier must be > 0")
    if not rows:
        return [], {"trend": 1, "supertrend": None, "atr": None, "last_ts": None, "last_close": None}
    try:
        for c in rows:
            c["high"], c["low"], c["close"] = float(c["high"]), float(c["low"]), float(c["close"])
    except (KeyError, TypeError, ValueError) as exc:
        raise SupertrendCalculationError("Candles require numeric high, low and close") from exc

    atrs = _atr_values(_true_ranges(rows), period, change_atr)
    prev_up = prev_dn = prev_close = None
    trend, last_line, last_atr = 1, None, None
    signals: list[dict[str, Any]] = []
    for i, candle in enumerate(rows):
        atr = atrs[i]
        close = candle["close"]
        if atr is None:
            prev_close = close
            continue
        hl2 = (candle["high"] + candle["low"]) / 2.0
        basic_up, basic_dn = hl2 - multiplier*atr, hl2 + multiplier*atr
        up1 = prev_up if prev_up is not None else basic_up
        dn1 = prev_dn if prev_dn is not None else basic_dn
        final_up = max(basic_up, up1) if prev_close is not None and prev_close > up1 else basic_up
        final_dn = min(basic_dn, dn1) if prev_close is not None and prev_close < dn1 else basic_dn
        old_trend = trend
        if trend == -1 and close > dn1:
            trend = 1
        elif trend == 1 and close < up1:
            trend = -1
        if trend != old_trend:
            signal = "buy" if trend == 1 else "sell"
            band = final_up if signal == "buy" else final_dn
            signals.append({
                "signal": signal,
                "price": round(float(band), 4),
                "confirmed_time": candle.get("timestamp"),
                "confirmed_time_price": round(float(close), 4),
            })
        last_line, last_atr = (final_up if trend == 1 else final_dn), atr
        prev_up, prev_dn, prev_close = final_up, final_dn, close
    return signals, {
        "trend": trend,
        "supertrend": round(last_line, 4) if last_line is not None else None,
        "atr": round(last_atr, 6) if last_atr is not None else None,
        "last_ts": rows[-1].get("timestamp"),
        "last_close": round(float(rows[-1]["close"]), 4),
    }

def signal_identity(signal: dict[str, Any]) -> tuple[str, str]:
    return str(signal.get("signal", "")).lower(), str(signal.get("confirmed_time", ""))
