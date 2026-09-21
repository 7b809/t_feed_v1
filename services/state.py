from datetime import date, datetime
from pathlib import Path

from core import config
from utils.json_store import read_json, write_json_atomic


def slug(contract: dict) -> str:
    strike = float(contract["strike_price"])
    strike_text = str(int(strike)) if strike.is_integer() else str(strike)
    return f"{strike_text}_{contract['option_type']}"


def state_path(contract: dict) -> Path:
    return config.EMA_STATE_ROOT / f"{slug(contract)}.json"


def load_state(contract: dict, trading_date: date) -> dict | None:
    payload = read_json(state_path(contract), {})
    if not isinstance(payload, dict):
        return None
    instrument = payload.get("instrument", {})
    if payload.get("trading_date") != trading_date.isoformat() or instrument.get("instrument_key") != contract.get("instrument_key"):
        return None
    if payload.get("ema_fast_period") != config.EMA_FAST_PERIOD or payload.get("ema_slow_period") != config.EMA_SLOW_PERIOD:
        return None
    return payload.get("state") if isinstance(payload.get("state"), dict) else None


def save_state(contract: dict, trading_date: date, state: dict, warmup_complete: bool = True) -> None:
    write_json_atomic(state_path(contract), {
        "status": "ready", "trading_date": trading_date.isoformat(),
        "updated_at": datetime.now(config.MARKET_TIMEZONE).isoformat(), "instrument": contract,
        "ema_fast_period": config.EMA_FAST_PERIOD, "ema_slow_period": config.EMA_SLOW_PERIOD,
        "historical_warmup_complete": warmup_complete, "state": state,
    })


def append_crossover(contract: dict, trading_date: date, candle: dict) -> None:
    path = config.CROSSOVER_ROOT / trading_date.isoformat() / f"{slug(contract)}.json"
    payload = read_json(path, {})
    rows = payload.get("crossovers", []) if isinstance(payload, dict) else []
    if any(row.get("timestamp") == candle.get("timestamp") for row in rows if isinstance(row, dict)):
        return
    rows.append(candle)
    write_json_atomic(path, {
        "status": "success", "generated_at": datetime.now(config.MARKET_TIMEZONE).isoformat(),
        "instrument": contract, "ema_fast_period": config.EMA_FAST_PERIOD,
        "ema_slow_period": config.EMA_SLOW_PERIOD,
        "bullish_cross_count": sum(row.get("is_bullish_cross") is True for row in rows),
        "bearish_cross_count": sum(row.get("is_bearish_cross") is True for row in rows),
        "latest_cross": rows[-1] if rows else None, "crossovers": rows,
    })


def append_completed_candle(contract: dict, trading_date: date, candle: dict) -> None:
    if not config.SAVE_ALL_COMPLETED_CANDLES:
        return
    path = config.CANDLE_ROOT / trading_date.isoformat() / f"{slug(contract)}.json"
    payload = read_json(path, {})
    rows = payload.get("candles", []) if isinstance(payload, dict) else []
    rows = [row for row in rows if row.get("timestamp") != candle.get("timestamp")]
    rows.append(candle)
    rows.sort(key=lambda row: row["timestamp"])
    write_json_atomic(path, {"instrument": contract, "trading_date": trading_date.isoformat(), "candles": rows})
