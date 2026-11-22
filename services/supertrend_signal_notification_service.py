"""Policy-controlled Telegram delivery for isolated-instrument Supertrend signals."""
from __future__ import annotations

import hashlib
import html
import json
import os
import threading
from datetime import datetime, time as dt_time
from pathlib import Path
from zoneinfo import ZoneInfo

from core.logger import get_logger
from services.isolation_telegram_service import send_group_html

logger = get_logger(__file__)
IST = ZoneInfo("Asia/Kolkata")
_LOCK = threading.RLock()


def _parse_time(value, fallback=None):
    if value in (None, ""):
        return fallback
    try:
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, (int, float)):
            stamp = float(value)
            if stamp > 10_000_000_000:
                stamp /= 1000.0
            parsed = datetime.fromtimestamp(stamp, tz=IST)
        else:
            raw = str(value).strip()
            if raw.endswith("Z"):
                raw = raw[:-1] + "+00:00"
            try:
                parsed = datetime.fromisoformat(raw)
            except ValueError:
                parsed = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=IST)
        return parsed.astimezone(IST)
    except (TypeError, ValueError, OverflowError, OSError):
        return fallback


def _find_value(payload, names):
    if not isinstance(payload, dict):
        return None
    for name in names:
        value = payload.get(name)
        if value not in (None, ""):
            return value
    for key in ("data", "result", "signal_data", "indicator", "metadata", "supertrend", "details", "payload"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            found = _find_value(nested, names)
            if found not in (None, ""):
                return found
    return None


def _signal_direction(payload):
    value = _find_value(payload, ("signal", "direction", "trend", "side", "signal_type", "signal_direction", "supertrend_direction", "action"))
    if isinstance(value, dict):
        value = _find_value(value, ("direction", "trend", "type", "value"))
    normalized = str(value or "").strip().upper().replace("-", "_").replace(" ", "_")
    if any(token in normalized for token in ("BULL", "BUY", "UPTREND", "UP", "LONG")):
        return "BULLISH"
    if any(token in normalized for token in ("BEAR", "SELL", "DOWNTREND", "DOWN", "SHORT")):
        return "BEARISH"
    return None


def _signal_timestamp(payload, received_at):
    value = _find_value(
        payload,
        ("signal_time", "signal_timestamp", "signal_generated_at", "generated_at", "candle_timestamp", "bar_time", "timestamp", "time", "created_at", "datetime", "ts"),
    )
    return _parse_time(value, _parse_time(received_at, datetime.now(IST)))


def _cutoff():
    hour = int(os.getenv("ISOLATION_SUPERTREND_SIGNAL_CUTOFF_HOUR", "9"))
    minute = int(os.getenv("ISOLATION_SUPERTREND_SIGNAL_CUTOFF_MINUTE", "40"))
    return dt_time(hour, minute)


def _state_path():
    root = Path(os.getenv("ISOLATED_DATA_DIR", "data/isolated")).expanduser()
    if not root.is_absolute():
        root = Path.cwd() / root
    root.mkdir(parents=True, exist_ok=True)
    return root / ".supertrend_signal_notifications.json"


def _load_state(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError):
        logger.exception("Could not read Supertrend Telegram delivery state; starting a fresh state")
        return {}


def _save_state(path, state):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


def _latest_prices(record, payload):
    isolated_key = str(record.get("instrument_key") or "")
    opposite = record.get("opposite_leg") or {}
    opposite_key = str(opposite.get("instrument_key") or "")
    prices = {"isolated": None, "opposite": None}
    try:
        from jobs.daily_refresh import get_feed_service
        feed = get_feed_service()
        if feed is not None:
            if isolated_key:
                prices["isolated"] = feed.get_ltp(isolated_key)
            if opposite_key:
                prices["opposite"] = feed.get_ltp(opposite_key)
    except Exception:
        logger.exception("Could not read latest option prices for Supertrend Telegram message")

    if prices["isolated"] is None:
        prices["isolated"] = _find_value(payload, ("isolated_ltp", "ltp", "last_price", "price"))
    if prices["opposite"] is None:
        prices["opposite"] = next(
            (opposite.get(k) for k in ("ltp", "last_price", "ltp_at_isolation") if opposite.get(k) not in (None, "")),
            None,
        )
    if prices["isolated"] is None:
        prices["isolated"] = record.get("isolated_option_ltp")
    return prices


def _fmt_price(value):
    if value in (None, ""):
        return "Not available"
    try:
        return f"{float(value):,.2f}"
    except (TypeError, ValueError):
        return str(value)


def _fmt_time(value):
    return value.strftime("%d %b %Y, %I:%M:%S %p IST")


def _message(record, direction, signal_time, prices, payload):
    opposite = record.get("opposite_leg") or {}
    isolated_symbol = html.escape(str(record.get("trading_symbol") or record.get("instrument_key") or "—"), quote=True)
    opposite_symbol = html.escape(str(opposite.get("trading_symbol") or opposite.get("instrument_key") or "—"), quote=True)
    instrument_key = html.escape(str(record.get("instrument_key") or "—"), quote=True)
    emoji = "🟢" if direction == "BULLISH" else "🔴"
    lines = [
        f"⚡ <b>SUPERTREND SIGNAL · {emoji} {direction}</b>",
        f"Trading symbol: <b>{isolated_symbol}</b>",
        f"Signal: <b>{direction}</b>",
        f"Signal time: <b>{_fmt_time(signal_time)}</b>",
        f"Isolated instrument price: <b>{_fmt_price(prices['isolated'])}</b>",
        f"Opposite instrument price: <b>{_fmt_price(prices['opposite'])}</b>",
        f"Opposite symbol: <code>{opposite_symbol}</code>",
        f"Instrument key: <code>{instrument_key}</code>",
    ]
    timeframe = _find_value(payload, ("timeframe", "interval", "candle_interval"))
    if timeframe not in (None, ""):
        lines.append(f"Supertrend timeframe: {_safe_text(timeframe)}")
    return "\n".join(lines)


def _safe_text(value):
    return html.escape(str(value), quote=True)


def notify_isolated_supertrend_signal(record, payload, received_at=None, role="isolated"):
    """Send signals in [isolation, 09:40]; otherwise send one first post-cutoff signal.

    Successful deliveries are persisted per isolation so restarts do not resend
    already delivered events. Only the isolated leg is eligible for Telegram.
    """
    if role != "isolated" or not isinstance(record, dict) or not isinstance(payload, dict):
        return False
    if payload.get("event") != "supertrend_signal":
        return False

    direction = _signal_direction(payload)
    if not direction:
        logger.warning("Ignoring Supertrend signal with unrecognized direction: %s", payload)
        return False

    received = _parse_time(received_at, datetime.now(IST))
    signal_time = _signal_timestamp(payload, received.isoformat())
    isolation_time = _parse_time(
        record.get("isolation_touch_time") or record.get("isolation_created_at"),
        None,
    )
    if isolation_time is None:
        logger.warning("Skipping Supertrend Telegram signal: isolation timestamp is missing")
        return False
    if signal_time < isolation_time:
        logger.debug("Ignoring Supertrend signal generated before isolation: %s", signal_time)
        return False

    cutoff = datetime.combine(signal_time.date(), _cutoff(), tzinfo=IST)
    is_initial_window = signal_time <= cutoff
    isolation_id = str(
        record.get("isolation_id")
        or f"{record.get('trade_date_ist') or isolation_time.date().isoformat()}__{record.get('instrument_key') or 'unknown'}"
    )
    # Prefer the provider's event timestamp. If it is absent, hash the
    # stable event body so duplicate fan-out/reconnect frames do not become
    # distinct merely because their local receive times differ.
    provider_time = _find_value(
        payload,
        ("signal_time", "signal_timestamp", "signal_generated_at", "generated_at",
         "candle_timestamp", "bar_time", "timestamp", "time", "created_at", "datetime", "ts"),
    )
    event_identity = (
        signal_time.isoformat()
        if provider_time not in (None, "")
        else hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
        ).hexdigest()
    )
    signal_key = "|".join((
        str(record.get("instrument_key") or ""),
        event_identity,
        direction,
    ))

    with _LOCK:
        path = _state_path()
        state = _load_state(path)
        entry = state.get(isolation_id)
        if not isinstance(entry, dict):
            entry = {"sent_signal_keys": [], "initial_window_success_count": 0, "fallback_sent": False}
        if signal_key in entry.get("sent_signal_keys", []):
            return False

        if is_initial_window:
            should_send = True
            delivery_kind = "initial_window"
        else:
            should_send = (
                int(entry.get("initial_window_success_count", 0)) == 0
                and not bool(entry.get("fallback_sent"))
            )
            delivery_kind = "first_after_cutoff"

        if not should_send:
            return False

        prices = _latest_prices(record, payload)
        message = _message(record, direction, signal_time, prices, payload)
        if not send_group_html(message):
            logger.error(
                "Supertrend Telegram delivery failed | kind=%s | symbol=%s | signal_time=%s",
                delivery_kind, record.get("trading_symbol"), signal_time.isoformat(),
            )
            return False

        sent_keys = list(entry.get("sent_signal_keys") or [])
        sent_keys.append(signal_key)
        entry["sent_signal_keys"] = sent_keys[-500:]
        if is_initial_window:
            entry["initial_window_success_count"] = int(entry.get("initial_window_success_count", 0)) + 1
        else:
            entry["fallback_sent"] = True
        entry["last_success_at_ist"] = datetime.now(IST).isoformat()
        state[isolation_id] = entry
        _save_state(path, state)
        logger.info(
            "Supertrend Telegram signal delivered | kind=%s | direction=%s | symbol=%s | signal_time=%s",
            delivery_kind, direction, record.get("trading_symbol"), signal_time.isoformat(),
        )
        return True
