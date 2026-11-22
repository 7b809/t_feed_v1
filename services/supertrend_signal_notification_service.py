"""Policy-controlled Telegram delivery for isolated-instrument Supertrend signals."""
from __future__ import annotations

import hashlib
import html
import json
import os
import threading
from datetime import datetime, time as dt_time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from core.logger import get_logger
from services.isolation_telegram_service import send_group_html

logger = get_logger(__file__)
IST = ZoneInfo("Asia/Kolkata")
_LOCK = threading.RLock()\n_VOLATILE_STATE = {}


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
            if raw.replace(".", "", 1).isdigit():
                stamp = float(raw)
                if stamp > 10_000_000_000:
                    stamp /= 1000.0
                parsed = datetime.fromtimestamp(stamp, tz=IST)
            else:
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
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


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
        return html.escape(str(value), quote=True)


def _fmt_time(value):
    return value.strftime("%d %b %Y, %I:%M:%S %p IST")


def _format_duration(seconds):
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} second{'s' if seconds != 1 else ''}"
    minutes, remainder = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} min {remainder} sec" if remainder else f"{minutes} min"
    hours, remainder_minutes = divmod(minutes, 60)
    return f"{hours} hr {remainder_minutes} min" if remainder_minutes else f"{hours} hr"


def _format_signal_age(signal_time, now=None):
    now = _parse_time(now, datetime.now(IST))
    return _format_duration((now - signal_time).total_seconds()) + " ago"


def _format_signal_isolation_gap(signal_time, record):
    isolation_time = _parse_time(
        record.get("isolation_touch_time") or record.get("isolation_created_at"),
        None,
    )
    if isolation_time is None:
        return "Not available"
    gap = abs((isolation_time - signal_time).total_seconds())
    relation = "before isolation" if signal_time < isolation_time else "after isolation"
    return f"{_format_duration(gap)} {relation}"


def _message(record, direction, signal_time, prices, payload, now=None):
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
        f"Signal age: <b>{_safe_text(_format_signal_age(signal_time, now))}</b>",
        f"Signal-to-isolation gap: <b>{_safe_text(_format_signal_isolation_gap(signal_time, record))}</b>",
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


def _notify_isolated_supertrend_signal(record, payload, received_at=None, role="isolated",
                                       allow_current_minute_pre_isolation=False, notification_now=None):
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
    now = _parse_time(notification_now, received)
    current_minute = now.replace(second=0, microsecond=0)
    same_minute_catchup = (
        allow_current_minute_pre_isolation
        and signal_time < isolation_time
        and signal_time >= current_minute
        and isolation_time >= current_minute
        and signal_time <= now
    )
    if signal_time < isolation_time and not same_minute_catchup:
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
        volatile_entry = _VOLATILE_STATE.get(isolation_id)
        if isinstance(volatile_entry, dict):
            entry["sent_signal_keys"] = list(dict.fromkeys(
                list(entry.get("sent_signal_keys") or [])
                + list(volatile_entry.get("sent_signal_keys") or [])
            ))[-500:]
            entry["initial_window_success_count"] = max(
                int(entry.get("initial_window_success_count", 0)),
                int(volatile_entry.get("initial_window_success_count", 0)),
            )
            entry["fallback_sent"] = bool(entry.get("fallback_sent") or volatile_entry.get("fallback_sent"))
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
        message = _message(record, direction, signal_time, prices, payload, now=now)
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
        try:
            _save_state(path, state)
            _VOLATILE_STATE.pop(isolation_id, None)
        except Exception:
            # Telegram already accepted the message. Keep the successful
            # delivery in process memory and do not fail the feed callback.
            _VOLATILE_STATE[isolation_id] = {
                **entry,
                "sent_signal_keys": list(entry.get("sent_signal_keys") or []),
            }
            logger.exception(
                "Telegram accepted Supertrend signal but delivery state could not be persisted; "
                "in-process deduplication remains active | isolation=%s",
                isolation_id,
            )
        logger.info(
            "Supertrend Telegram signal delivered | kind=%s | direction=%s | symbol=%s | signal_time=%s",
            delivery_kind, direction, record.get("trading_symbol"), signal_time.isoformat(),
        )
        return True



def notify_isolated_supertrend_signal(record, payload, received_at=None, role="isolated",
                                      allow_current_minute_pre_isolation=False,
                                      notification_now=None):
    """Exception-safe public notifier; notification faults never escape to feed callers."""
    try:
        return _notify_isolated_supertrend_signal(
            record, payload, received_at, role,
            allow_current_minute_pre_isolation=allow_current_minute_pre_isolation,
            notification_now=notification_now,
        )
    except Exception:
        logger.exception(
            "Unexpected Supertrend Telegram notification failure; feed processing will continue | key=%s",
            (record or {}).get("instrument_key") if isinstance(record, dict) else "unknown",
        )
        return False



def dispatch_isolated_supertrend_signal(record, payload, received_at=None, role="isolated"):
    """Queue one best-effort notification on a daemon thread; never wait in feed loops."""
    try:
        worker = threading.Thread(
            target=notify_isolated_supertrend_signal,
            args=(dict(record or {}), dict(payload or {}), received_at, role),
            name="isolated-supertrend-telegram",
            daemon=True,
        )
        worker.start()
        return True
    except Exception:
        logger.exception("Could not schedule isolated Supertrend Telegram notification")
        return False

def _read_recent_text_lines(path, max_bytes=None):
    """Read a bounded tail of an append-only feed file, never its full history."""
    limit = max_bytes or max(64 * 1024, int(os.getenv("ISOLATION_SUPERTREND_CATCHUP_MAX_BYTES", str(2 * 1024 * 1024))))
    with Path(path).open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(max(0, size - limit))
        if size > limit:
            handle.readline()  # discard a possibly partial first line
        data = handle.read()
    return data.decode("utf-8", errors="replace").splitlines()


def notify_current_minute_supertrend_signal(record, now=None):
    """Send the newest isolated-leg signal already archived in the current IST minute.

    This catches a signal emitted moments before isolation but in the same minute.
    Archive inspection and delivery are deliberately best-effort and exception-safe.
    """
    try:
        if not isinstance(record, dict):
            return False
        now = _parse_time(now, datetime.now(IST))
        minute_start = now.replace(second=0, microsecond=0)
        instrument_key = str(record.get("instrument_key") or "")
        trade_date = str(record.get("trade_date_ist") or now.date().isoformat())

        sources = []
        try:
            from services.option_chain_history import _find_contract_folder
            folder = _find_contract_folder(instrument_key, trade_date)
            if folder is not None:
                source = Path(folder) / "supertrend_feed.jsonl"
                if source.is_file():
                    sources.append(source)
        except Exception:
            logger.exception("Could not locate option-chain Supertrend archive | key=%s", instrument_key)

        # Also inspect the isolation-owned archive. This covers deployments
        # where the option-chain socket was not active when isolation began.
        safe_key = "".join(c if c.isalnum() or c in "-_." else "_" for c in instrument_key)[:100]
        isolated_source = (
            Path(os.getenv("ISOLATED_DATA_DIR", "data/isolated"))
            / "supertrend" / f"{trade_date}_isolated_{safe_key}.jsonl"
        )
        if isolated_source.is_file():
            sources.append(isolated_source)

        if not sources:
            logger.info("No Supertrend feed file available for current-minute catch-up | key=%s", instrument_key)
            return False

        candidates = []
        for source in sources:
            for line in _read_recent_text_lines(source):
                raw = line.strip()
                if not raw:
                    continue
                try:
                    row = json.loads(raw)
                except json.JSONDecodeError:
                    # A concurrently written trailing row is ignored and can be
                    # picked up by the live WebSocket path if it arrives later.
                    continue
                if not isinstance(row, dict):
                    continue
                if row.get("instrument_key") not in (None, "", instrument_key):
                    continue
                payload = row.get("payload") if isinstance(row.get("payload"), dict) else row
                if payload.get("event") != "supertrend_signal":
                    continue
                signal_time = _signal_timestamp(payload, row.get("received_at_ist") or now.isoformat())
                if minute_start <= signal_time <= now and _signal_direction(payload):
                    candidates.append((signal_time, payload))

        if not candidates:
            logger.info("No Supertrend signal found in current minute archive | key=%s minute=%s",
                        instrument_key, minute_start.isoformat())
            return False

        # Only the newest current-minute event is replayed. The persistent
        # event key prevents the same signal being sent again by the live socket.
        signal_time, payload = max(candidates, key=lambda item: item[0])
        return notify_isolated_supertrend_signal(
            record, payload, received_at=now.isoformat(), role="isolated",
            allow_current_minute_pre_isolation=True, notification_now=now,
        )
    except Exception:
        logger.exception(
            "Current-minute Supertrend archive catch-up failed; isolation continues | key=%s",
            (record or {}).get("instrument_key") if isinstance(record, dict) else "unknown",
        )
        return False
