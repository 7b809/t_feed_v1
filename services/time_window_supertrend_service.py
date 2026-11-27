"""Daily 15:00–15:40 IST Supertrend scan for the active isolated option pair.

Historical candles are cached per date/instrument/interval batch. Intraday candles
are refreshed each cycle. Only completed candles are evaluated and each signal is
persisted with a deterministic ID before Telegram delivery is attempted.
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import tempfile
import threading
import time
from datetime import date, datetime, time as dt_time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import upstox_client

from core.config import (
    TIMEZONE,
    TIME_WINDOW_SUPERTREND_ENABLED,
    TIME_WINDOW_SUPERTREND_START_HOUR,
    TIME_WINDOW_SUPERTREND_START_MINUTE,
    TIME_WINDOW_SUPERTREND_END_HOUR,
    TIME_WINDOW_SUPERTREND_END_MINUTE,
    TIME_WINDOW_SUPERTREND_SCAN_SECOND,
    TIME_WINDOW_SUPERTREND_CANDLE_GRACE_SECONDS,
    TIME_WINDOW_SUPERTREND_HISTORY_DAYS,
    TIME_WINDOW_SUPERTREND_HISTORY_BATCH_DAYS,
    TIME_WINDOW_SUPERTREND_INTERVALS,
    TIME_WINDOW_SUPERTREND_ATR_PERIOD,
    TIME_WINDOW_SUPERTREND_MULTIPLIER,
    TIME_WINDOW_SUPERTREND_USE_ATR,
    TIME_WINDOW_SUPERTREND_MAX_NOTIFICATION_ATTEMPTS,
    TIME_WINDOW_SUPERTREND_DATA_DIR,
)
from core.logger import get_logger
from core.token_service import fetch_access_token
from services.isolated_archive_service import IsolatedArchiveService
from services.isolation_telegram_service import send_isolation_notifications

logger = get_logger(__file__)
IST = ZoneInfo(TIMEZONE)
_RUN_LOCK = threading.Lock()


def _now() -> datetime:
    return datetime.now(IST)


def _safe(value: object) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in str(value or "unknown"))[:100]


def _parse_timestamp(value: object) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = str(value or "").strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=IST)
    return parsed.astimezone(IST)


def _iso(value: datetime) -> str:
    return value.astimezone(IST).isoformat()


def _atomic_json(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(document, handle, indent=2, ensure_ascii=False, default=str)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except (OSError, ValueError):
        logger.exception("Could not read JSON cache: %s", path)
        return default


def _extract_candles(response) -> list:
    if isinstance(response, dict):
        return ((response.get("data") or {}).get("candles") or [])
    data = getattr(response, "data", None)
    return getattr(data, "candles", []) if data is not None else []


def _normalise_candles(rows: list) -> list[dict]:
    result = []
    for row in rows or []:
        try:
            if isinstance(row, (list, tuple)) and len(row) >= 5:
                timestamp, open_, high, low, close = row[:5]
                volume = row[5] if len(row) > 5 else 0
                oi = row[6] if len(row) > 6 else 0
            elif isinstance(row, dict):
                timestamp = row.get("timestamp") or row.get("ts") or row.get("time")
                open_ = row.get("open")
                high = row.get("high")
                low = row.get("low")
                close = row.get("close")
                volume = row.get("volume", 0)
                oi = row.get("oi", row.get("open_interest", 0))
            else:
                continue
            candle = {
                "timestamp": _iso(_parse_timestamp(timestamp)),
                "open": float(open_),
                "high": float(high),
                "low": float(low),
                "close": float(close),
                "volume": float(volume or 0),
                "open_interest": float(oi or 0),
            }
            if candle["high"] < candle["low"] or candle["close"] <= 0:
                continue
            result.append(candle)
        except (TypeError, ValueError, OverflowError):
            logger.warning("Skipping malformed Upstox candle row: %r", row)
    deduped = {item["timestamp"]: item for item in result}
    return [deduped[key] for key in sorted(deduped)]


def calculate_supertrend(
    candles: list[dict],
    period: int = 10,
    multiplier: float = 3.0,
    use_atr: bool = True,
) -> list[int] | None:
    """Pine-style Supertrend trend series, matching the supplied reference script."""
    if len(candles) < period + 2:
        return None
    high = [float(row["high"]) for row in candles]
    low = [float(row["low"]) for row in candles]
    close = [float(row["close"]) for row in candles]
    source = [(high[i] + low[i]) / 2.0 for i in range(len(candles))]

    true_range = [high[0] - low[0]]
    for i in range(1, len(candles)):
        true_range.append(max(
            high[i] - low[i],
            abs(high[i] - close[i - 1]),
            abs(low[i] - close[i - 1]),
        ))

    if use_atr:
        atr = [true_range[0]]
        for i in range(1, len(true_range)):
            atr.append((atr[i - 1] * (period - 1) + true_range[i]) / period)
    else:
        atr = []
        for i in range(len(true_range)):
            if i < period - 1:
                atr.append(true_range[i])
            else:
                atr.append(sum(true_range[i - period + 1:i + 1]) / period)

    basic_up = [source[i] - multiplier * atr[i] for i in range(len(source))]
    basic_down = [source[i] + multiplier * atr[i] for i in range(len(source))]
    final_up = [basic_up[0]]
    final_down = [basic_down[0]]
    trend = [1]

    for i in range(1, len(candles)):
        previous_up = final_up[i - 1]
        previous_down = final_down[i - 1]
        previous_close = close[i - 1]
        previous_trend = trend[i - 1]

        current_up = max(basic_up[i], previous_up) if previous_close > previous_up else basic_up[i]
        current_down = min(basic_down[i], previous_down) if previous_close < previous_down else basic_down[i]

        if previous_trend == -1 and close[i] > previous_down:
            current_trend = 1
        elif previous_trend == 1 and close[i] < previous_up:
            current_trend = -1
        else:
            current_trend = previous_trend

        final_up.append(current_up)
        final_down.append(current_down)
        trend.append(current_trend)
    return trend


def _is_completed(candle: dict, interval_minutes: int, cutoff: datetime) -> bool:
    try:
        candle_close = _parse_timestamp(candle["timestamp"]) + timedelta(minutes=interval_minutes)
        return candle_close <= cutoff
    except (KeyError, TypeError, ValueError):
        return False


def _signal_id(instrument_key: str, interval: str, signal_time: datetime, side: str) -> str:
    raw = "|".join((instrument_key, interval, _iso(signal_time), side))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


class TimeWindowSupertrendService:
    def __init__(self, root: str | None = None):
        self.root = Path(root or TIME_WINDOW_SUPERTREND_DATA_DIR)

    def _day_root(self, trade_date: date) -> Path:
        return self.root / trade_date.strftime("%Y%m%d")

    def _history_batch_path(self, day_root: Path, role: str, key: str, interval: str, start: date, end: date) -> Path:
        return day_root / "history" / (
            f"{role}_{_safe(key)}_{interval}_{start:%Y%m%d}_{end:%Y%m%d}.json"
        )

    def _fetch_history(self, api, day_root: Path, role: str, key: str, interval: str, today: date) -> list[dict]:
        first_day = today - timedelta(days=TIME_WINDOW_SUPERTREND_HISTORY_DAYS)
        current = first_day
        collected = []
        while current < today:
            batch_end = min(current + timedelta(days=TIME_WINDOW_SUPERTREND_HISTORY_BATCH_DAYS), today)
            cache_path = self._history_batch_path(day_root, role, key, interval, current, batch_end)
            cached = _read_json(cache_path, {}) or {}
            if cached.get("complete") and isinstance(cached.get("candles"), list):
                collected.extend(cached["candles"])
            else:
                try:
                    response = api.get_historical_candle_data1(
                        key, "minutes", interval.removesuffix("m"),
                        batch_end.strftime("%Y-%m-%d"), current.strftime("%Y-%m-%d"),
                    )
                    candles = _normalise_candles(_extract_candles(response))
                    candles = [
                        row for row in candles
                        if _parse_timestamp(row["timestamp"]).date() < today
                    ]
                    _atomic_json(cache_path, {
                        "schema_version": 1,
                        "complete": True,
                        "instrument_key": key,
                        "role": role,
                        "interval": interval,
                        "from_date": current.isoformat(),
                        "to_date": batch_end.isoformat(),
                        "fetched_at_ist": _iso(_now()),
                        "candles": candles,
                    })
                    collected.extend(candles)
                    if not candles:
                        logger.warning(
                            "Historical batch returned no candles | role=%s key=%s interval=%s range=%s..%s",
                            role, key, interval, current, batch_end,
                        )
                except Exception:
                    logger.exception(
                        "Historical fetch failed | role=%s key=%s interval=%s range=%s..%s",
                        role, key, interval, current, batch_end,
                    )
            current = batch_end
        merged = {row["timestamp"]: row for row in collected}
        return [merged[key] for key in sorted(merged)]

    def _fetch_intraday(self, api, day_root: Path, role: str, key: str, interval: str) -> list[dict]:
        response = api.get_intra_day_candle_data(
            key, "minutes", interval.removesuffix("m")
        )
        candles = _normalise_candles(_extract_candles(response))
        today = _now().date()
        candles = [row for row in candles if _parse_timestamp(row["timestamp"]).date() == today]
        if candles:
            _atomic_json(
                day_root / "intraday" / f"{role}_{_safe(key)}_{interval}.json",
                {
                    "schema_version": 1,
                    "instrument_key": key,
                    "role": role,
                    "interval": interval,
                    "updated_at_ist": _iso(_now()),
                    "candles": candles,
                },
            )
        return candles

    @staticmethod
    def _active_legs() -> dict:
        archive = IsolatedArchiveService().load_current()
        if not archive or str(archive.get("status", "ACTIVE")).upper() != "ACTIVE":
            return {}
        instruments = archive.get("instruments") or {}
        source = archive.get("source_record") or {}
        isolated = dict(instruments.get("isolated") or {})
        opposite = dict(instruments.get("opposite") or {})
        if not isolated.get("instrument_key"):
            isolated = {
                "instrument_key": source.get("instrument_key"),
                "trading_symbol": source.get("trading_symbol"),
            }
        if not opposite.get("instrument_key"):
            opposite = dict(source.get("opposite_leg") or {})
        return {
            role: leg for role, leg in (("isolated", isolated), ("opposite", opposite))
            if leg.get("instrument_key")
        }

    def _load_state(self, day_root: Path) -> dict:
        state = _read_json(day_root / "signals.json", {})
        if not isinstance(state, dict):
            state = {}
        state.setdefault("schema_version", 1)
        state.setdefault("trade_date_ist", _now().date().isoformat())
        if not isinstance(state.get("signals"), list):
            state["signals"] = []
        state["signals"] = [
            item for item in state["signals"] if isinstance(item, dict) and item.get("signal_id")
        ]
        return state

    def _persist_signal(self, day_root: Path, state: dict, record: dict) -> bool:
        existing = {item.get("signal_id") for item in state["signals"] if isinstance(item, dict)}
        if record["signal_id"] in existing:
            return False
        state["signals"].append(record)
        state["signals"].sort(key=lambda item: (
            item.get("signal_time_ist", ""),
            item.get("interval", ""),
            item.get("instrument_key", ""),
        ))
        _atomic_json(day_root / "signals" / record["interval"] / f"{record['signal_id']}.json", record)
        _atomic_json(day_root / "signals.json", state)
        return True

    def _make_notifications(self, record: dict) -> tuple[str, str]:
        symbol = html.escape(str(record.get("trading_symbol") or record["instrument_key"]))
        role = html.escape(record["leg_role"])
        interval = html.escape(record["interval"])
        side = html.escape(record["signal"])
        signal_time = html.escape(record["signal_time_ist"])
        group_message = (
            "<b>Time-Window Supertrend Signal</b>\n"
            f"Interval: <b>{interval}</b>\nDirection: <b>{side}</b>\n"
            f"Signal time: {signal_time}\nInstrument: <code>{symbol}</code>\n"
            f"Leg: {role}"
        )
        private_message = (
            "<b>Time-Window Supertrend Signal — Details</b>\n"
            f"Instrument: <code>{symbol}</code>\n"
            f"Instrument key: <code>{html.escape(record['instrument_key'])}</code>\n"
            f"Leg role: <b>{role}</b>\nInterval: <b>{interval}</b>\n"
            f"Direction: <b>{side}</b>\nSignal candle: {signal_time}\n"
            f"Candle close: <b>{float(record['close_price']):.2f}</b>\n"
            f"Detected at: {html.escape(record['detected_at_ist'])}\n"
            f"Signal ID: <code>{html.escape(record['signal_id'])}</code>\n"
            f"Supertrend: ATR {TIME_WINDOW_SUPERTREND_ATR_PERIOD}, "
            f"multiplier {TIME_WINDOW_SUPERTREND_MULTIPLIER:g}"
        )
        return group_message, private_message

    def _deliver_pending(self, state: dict) -> int:
        delivered = 0
        for record in state["signals"]:
            notification = record.setdefault("notification", {"status": "pending", "attempts": 0})
            if notification.get("status") == "delivered":
                continue
            if int(notification.get("attempts", 0)) >= TIME_WINDOW_SUPERTREND_MAX_NOTIFICATION_ATTEMPTS:
                continue
            group_message, private_message = self._make_notifications(record)
            notification["attempts"] = int(notification.get("attempts", 0)) + 1
            notification["last_attempt_at_ist"] = _iso(_now())
            try:
                sent = send_isolation_notifications(group_message, private_message)
            except Exception:
                logger.exception("Telegram delivery raised for signal %s", record["signal_id"])
                sent = False
            notification["status"] = "delivered" if sent else "pending"
            if sent:
                delivered += 1
            _atomic_json(self._day_root(_now().date()) / "signals.json", state)
        return delivered

    def run_cycle(self, now: datetime | None = None) -> dict:
        if not TIME_WINDOW_SUPERTREND_ENABLED:
            return {"state": "disabled"}
        now = (now or _now()).astimezone(IST)
        start = dt_time(TIME_WINDOW_SUPERTREND_START_HOUR, TIME_WINDOW_SUPERTREND_START_MINUTE)
        end = dt_time(TIME_WINDOW_SUPERTREND_END_HOUR, TIME_WINDOW_SUPERTREND_END_MINUTE)
        last_tick = (datetime.combine(now.date(), end, tzinfo=IST) +
                     timedelta(seconds=TIME_WINDOW_SUPERTREND_SCAN_SECOND)).time()
        if now.weekday() >= 5 or not (start <= now.timetz().replace(tzinfo=None) <= last_tick):
            return {"state": "outside_window", "at_ist": _iso(now)}

        if not _RUN_LOCK.acquire(blocking=False):
            logger.warning("Time-window Supertrend cycle skipped: previous cycle still running")
            return {"state": "already_running"}
        try:
            legs = self._active_legs()
            if len(legs) != 2:
                logger.warning("Time-window Supertrend scan skipped: today's active isolation pair is unavailable")
                return {"state": "isolation_pair_unavailable"}

            day_root = self._day_root(now.date())
            day_root.mkdir(parents=True, exist_ok=True)
            state = self._load_state(day_root)
            api_config = upstox_client.Configuration()
            api_config.access_token = fetch_access_token()
            api = upstox_client.HistoryV3Api(upstox_client.ApiClient(api_config))
            cutoff = now - timedelta(seconds=TIME_WINDOW_SUPERTREND_CANDLE_GRACE_SECONDS)
            new_signals = 0
            errors = []

            for role in ("isolated", "opposite"):
                leg = legs.get(role) or {}
                key = str(leg.get("instrument_key") or "")
                symbol = str(leg.get("trading_symbol") or key)
                for interval in TIME_WINDOW_SUPERTREND_INTERVALS:
                    try:
                        historical = self._fetch_history(api, day_root, role, key, interval, now.date())
                        intraday = self._fetch_intraday(api, day_root, role, key, interval)
                        combined = {
                            row["timestamp"]: row for row in (historical + intraday)
                        }
                        candles = [
                            combined[stamp] for stamp in sorted(combined)
                            if _is_completed(combined[stamp], int(interval.removesuffix("m")), cutoff)
                        ]
                        trends = calculate_supertrend(
                            candles,
                            period=TIME_WINDOW_SUPERTREND_ATR_PERIOD,
                            multiplier=TIME_WINDOW_SUPERTREND_MULTIPLIER,
                            use_atr=TIME_WINDOW_SUPERTREND_USE_ATR,
                        )
                        if trends is None:
                            logger.info(
                                "Insufficient completed candles | role=%s key=%s interval=%s count=%d",
                                role, key, interval, len(candles),
                            )
                            continue

                        for index in range(1, len(trends)):
                            previous, current = trends[index - 1], trends[index]
                            if previous == current:
                                continue
                            side = "BUY" if previous == -1 and current == 1 else "SELL"
                            candle = candles[index]
                            candle_start = _parse_timestamp(candle["timestamp"])
                            signal_time = candle_start + timedelta(minutes=int(interval.removesuffix("m")))
                            window_start = datetime.combine(now.date(), start, tzinfo=IST)
                            window_end = datetime.combine(now.date(), end, tzinfo=IST)
                            if not (window_start <= signal_time <= window_end):
                                continue
                            record = {
                                "schema_version": 1,
                                "document_type": "time_window_supertrend_signal",
                                "signal_id": _signal_id(key, interval, signal_time, side),
                                "trade_date_ist": now.date().isoformat(),
                                "leg_role": role,
                                "instrument_key": key,
                                "trading_symbol": symbol,
                                "interval": interval,
                                "signal": side,
                                "trend_before": previous,
                                "trend_after": current,
                                "candle_start_ist": _iso(candle_start),
                                "signal_time_ist": _iso(signal_time),
                                "close_price": candle["close"],
                                "detected_at_ist": _iso(now),
                                "calculation": {
                                    "atr_period": TIME_WINDOW_SUPERTREND_ATR_PERIOD,
                                    "multiplier": TIME_WINDOW_SUPERTREND_MULTIPLIER,
                                    "use_atr_rma": TIME_WINDOW_SUPERTREND_USE_ATR,
                                },
                                "notification": {"status": "pending", "attempts": 0},
                            }
                            if self._persist_signal(day_root, state, record):
                                new_signals += 1
                                logger.info(
                                    "Time-window Supertrend signal | %s | %s | %s | %s | close=%.2f",
                                    interval, side, role, symbol, candle["close"],
                                )
                    except Exception as exc:
                        errors.append({"role": role, "instrument_key": key, "interval": interval, "error": str(exc)})
                        logger.exception(
                            "Time-window Supertrend processing failed | role=%s key=%s interval=%s",
                            role, key, interval,
                        )

            notifications_sent = self._deliver_pending(state)
            _atomic_json(day_root / "state.json", {
                "trade_date_ist": now.date().isoformat(),
                "last_cycle_at_ist": _iso(now),
                "intervals": TIME_WINDOW_SUPERTREND_INTERVALS,
                "instrument_count": len(legs),
                "signal_count": len(state["signals"]),
                "new_signals_this_cycle": new_signals,
                "notifications_delivered_this_cycle": notifications_sent,
                "errors": errors,
            })
            logger.info(
                "Time-window Supertrend cycle completed | instruments=%d intervals=%s new_signals=%d notifications=%d errors=%d",
                len(legs), ",".join(TIME_WINDOW_SUPERTREND_INTERVALS),
                new_signals, notifications_sent, len(errors),
            )
            return {
                "state": "completed",
                "new_signals": new_signals,
                "total_signals": len(state["signals"]),
                "notifications_sent": notifications_sent,
                "errors": errors,
            }
        finally:
            _RUN_LOCK.release()

    def status(self) -> dict:
        day_root = self._day_root(_now().date())
        state = _read_json(day_root / "state.json", {})
        return {
            "enabled": TIME_WINDOW_SUPERTREND_ENABLED,
            "intervals": TIME_WINDOW_SUPERTREND_INTERVALS,
            "window_ist": (
                f"{TIME_WINDOW_SUPERTREND_START_HOUR:02d}:{TIME_WINDOW_SUPERTREND_START_MINUTE:02d}-"
                f"{TIME_WINDOW_SUPERTREND_END_HOUR:02d}:{TIME_WINDOW_SUPERTREND_END_MINUTE:02d}"
            ),
            "data_directory": str(day_root),
            "last_cycle": state or None,
        }


_service = TimeWindowSupertrendService()


def run_time_window_supertrend_cycle() -> dict:
    return _service.run_cycle()


def get_time_window_supertrend_status() -> dict:
    return _service.status()
