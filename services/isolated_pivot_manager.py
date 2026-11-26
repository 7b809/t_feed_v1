"""Calculate and persist 1m/15m pivot levels for the two isolated option legs.

This is deliberately isolated from option-chain capture and all order/execution
workflows. It only processes the active isolation's CE/PE pair.
"""
from __future__ import annotations

import json
import math
import os
import re
import tempfile
import threading
import time
from datetime import date, datetime, time as dt_time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from core.candle_fetcher import CandleFetchError, CandleFetcher
from core.config import ISOLATED_PIVOT_ENABLED, ISOLATED_PIVOT_INTERVALS
from core.logger import get_logger
from core.or_levels_calculator import compute_or_levels

logger = get_logger(__file__)
IST = ZoneInfo("Asia/Kolkata")
SESSION_OPEN = dt_time(9, 15)
SESSION_CLOSE = dt_time(15, 30)
LEVEL_KEYS = ("R3", "R2", "R1", "AVG", "S1", "S2", "S3")
THRESHOLD_KEYS = ("R3_Threshold", "S3_Threshold")


def _safe(value: Any) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "unknown")).strip("._") or "unknown"


def _as_ist(value: Any) -> datetime | None:
    try:
        raw = str(value).strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        parsed = datetime.fromisoformat(raw)
        return (parsed.replace(tzinfo=IST) if parsed.tzinfo is None else parsed).astimezone(IST)
    except (TypeError, ValueError, OverflowError):
        return None


def parse_pivot_intervals(value: str | None = None) -> list[int]:
    """Return supported isolated pivot intervals; default is 1m and 15m."""
    raw = os.getenv("ISOLATED_PIVOT_INTERVALS", "1,15") if value is None else value
    parsed: list[int] = []
    for item in str(raw).split(","):
        item = item.strip().lower().removesuffix("m")
        if not item:
            continue
        try:
            minutes = int(item)
        except ValueError:
            logger.warning("Ignoring invalid ISOLATED_PIVOT_INTERVALS value %r", item)
            continue
        if minutes not in (1, 15):
            logger.warning("Ignoring unsupported isolated pivot interval %s", minutes)
            continue
        if minutes not in parsed:
            parsed.append(minutes)
    return sorted(parsed or [1, 15])


def latest_completed_candle_start(
    trade_date: str,
    interval_minutes: int,
    now: datetime | None = None,
    grace_seconds: float | None = None,
) -> datetime | None:
    """Get the latest completed candle, aligned to the 09:15 IST session open."""
    current = now or datetime.now(IST)
    current = current.replace(tzinfo=IST) if current.tzinfo is None else current.astimezone(IST)
    session_start = datetime.combine(date.fromisoformat(trade_date), SESSION_OPEN, tzinfo=IST)
    session_end = datetime.combine(date.fromisoformat(trade_date), SESSION_CLOSE, tzinfo=IST)
    grace = (
        float(os.getenv("ISOLATED_PIVOT_CANDLE_CLOSE_GRACE_SECONDS", "5"))
        if grace_seconds is None else max(0.0, float(grace_seconds))
    )
    effective_now = min(current - timedelta(seconds=max(0.0, grace)), session_end)
    interval_seconds = int(interval_minutes) * 60
    completed = math.floor((effective_now - session_start).total_seconds() / interval_seconds)
    max_full_bars = math.floor((session_end - session_start).total_seconds() / interval_seconds)
    completed = min(completed, max_full_bars)
    if completed <= 0:
        return None
    return session_start + timedelta(seconds=(completed - 1) * interval_seconds)


def _leg_rows(record: dict) -> list[dict]:
    isolated = {
        "leg_role": "isolated",
        "instrument_key": record.get("instrument_key"),
        "trading_symbol": record.get("trading_symbol"),
        "strike_price": record.get("strike", record.get("strike_price")),
        "instrument_type": record.get("option_type", record.get("instrument_type")),
        "expiry": record.get("expiry"),
    }
    opposite_source = record.get("opposite_leg") or {}
    opposite = {
        "leg_role": "opposite",
        "instrument_key": opposite_source.get("instrument_key"),
        "trading_symbol": opposite_source.get("trading_symbol"),
        "strike_price": opposite_source.get("strike", opposite_source.get("strike_price")),
        "instrument_type": opposite_source.get("option_type", opposite_source.get("instrument_type")),
        "expiry": opposite_source.get("expiry"),
    }
    return [row for row in (isolated, opposite) if row.get("instrument_key")]


class IsolatedPivotLevelsService:
    """Fetch completed candles and persist independent pivot documents per leg/interval."""

    def __init__(
        self,
        access_token: str,
        root: str | Path | None = None,
        intervals: list[int] | None = None,
        close_grace_seconds: float | None = None,
        requests_per_second: float | None = None,
        max_retries: int | None = None,
        fetcher=None,
    ):
        self.fetcher = fetcher or CandleFetcher(access_token)
        self.root = Path(root or os.getenv("ISOLATED_DATA_DIR", "data/isolated")) / "pivots"
        self.intervals = parse_pivot_intervals(
            ",".join(str(x) for x in intervals)
        ) if intervals is not None else list(ISOLATED_PIVOT_INTERVALS)
        self.close_grace_seconds = (
            max(0.0, float(os.getenv("ISOLATED_PIVOT_CANDLE_CLOSE_GRACE_SECONDS", "5")))
            if close_grace_seconds is None else max(0.0, float(close_grace_seconds))
        )
        self.requests_per_second = (
            max(0.0, float(os.getenv("ISOLATED_PIVOT_FETCH_REQUESTS_PER_SECOND", "4")))
            if requests_per_second is None else max(0.0, float(requests_per_second))
        )
        self.max_retries = (
            max(0, int(os.getenv("ISOLATED_PIVOT_FETCH_MAX_RETRIES", "2")))
            if max_retries is None else max(0, int(max_retries))
        )
        self._last_request_at = 0.0
        self._rate_lock = threading.Lock()

    def process_cycle(self, record: dict, now: datetime | None = None) -> dict:
        trade_date = str(record.get("trade_date_ist") or datetime.now(IST).date().isoformat())
        summary = {
            "state": "completed",
            "trade_date_ist": trade_date,
            "intervals": {},
            "legs": len(_leg_rows(record)),
        }
        legs = _leg_rows(record)
        for interval in self.intervals:
            target_stamp = latest_completed_candle_start(
                trade_date, interval, now=now, grace_seconds=self.close_grace_seconds
            )
            counts = {"computed": 0, "already_present": 0, "failed": 0, "skipped": 0}
            summary["intervals"][f"{interval}m"] = {
                "target_candle_timestamp": target_stamp.isoformat() if target_stamp else None,
                **counts,
            }
            if target_stamp is None:
                continue
            for leg in legs:
                try:
                    result = self._process_leg(leg, trade_date, interval, target_stamp)
                    summary["intervals"][f"{interval}m"][result] += 1
                except Exception:
                    summary["intervals"][f"{interval}m"]["failed"] += 1
                    logger.exception(
                        "Isolated pivot calculation failed | role=%s | interval=%dm | key=%s",
                        leg.get("leg_role"), interval, leg.get("instrument_key"),
                    )
        if any(item["failed"] for item in summary["intervals"].values()):
            summary["state"] = "partial"
        logger.info("Isolated pivot cycle completed: %s", summary)
        return summary

    def _folder(self, leg: dict, trade_date: str) -> Path:
        return (
            self.root / trade_date.replace("-", "") /
            str(leg.get("leg_role") or "unknown") /
            _safe(leg.get("instrument_key"))
        )

    def _process_leg(
        self, leg: dict, trade_date: str, interval: int, target_stamp: datetime
    ) -> str:
        key = str(leg.get("instrument_key") or "").strip()
        if not key:
            return "skipped"
        folder = self._folder(leg, trade_date)
        folder.mkdir(parents=True, exist_ok=True)
        target_path = folder / f"pivot_levels_{interval}m.json"
        opening_stamp = datetime.combine(
            date.fromisoformat(trade_date), SESSION_OPEN, tzinfo=IST
        )
        opening_path = folder / "pivot_levels.json"
        opening_is_saved = True
        if interval == 1:
            opening_is_saved = self._is_current(
                opening_path, key, trade_date, 1, opening_stamp
            )
        if self._is_current(target_path, key, trade_date, interval, target_stamp) and opening_is_saved:
            return "already_present"

        candles = self._fetch_with_retry(key, interval)
        matched = next(
            (row for row in candles if _as_ist(row.get("timestamp")) == target_stamp),
            None,
        )
        if interval == 1 and not opening_is_saved:
            opening = next(
                (row for row in candles if _as_ist(row.get("timestamp")) == opening_stamp),
                None,
            )
            if opening is not None:
                self._save_levels(
                    opening_path, leg, key, trade_date, 1, opening_stamp, opening,
                    document_type="opening_candle",
                )
        if matched is None:
            logger.debug(
                "Isolated pivot candle not published yet | interval=%dm key=%s target=%s",
                interval, key, target_stamp.isoformat(),
            )
            return "failed"
        self._save_levels(
            target_path, leg, key, trade_date, interval, target_stamp, matched,
            document_type="rolling_completed_candle",
        )
        return "computed"

    def _fetch_with_retry(self, key: str, interval: int) -> list[dict]:
        for attempt in range(self.max_retries + 1):
            self._throttle()
            try:
                return self.fetcher.fetch_intraday_candles(
                    key, unit="minutes", interval=str(interval)
                )
            except CandleFetchError as exc:
                message = str(exc).lower()
                transient = any(word in message for word in (
                    "429", "500", "502", "503", "504", "timeout", "connection",
                    "temporar", "rate limit", "no candles",
                ))
                if not transient or attempt >= self.max_retries:
                    raise
                delay = max(0.0, float(os.getenv("ISOLATED_PIVOT_FETCH_RETRY_BACKOFF_SECONDS", "1"))) * (2 ** attempt)
                logger.warning(
                    "Transient isolated pivot candle error; retry in %.1fs | key=%s interval=%dm | %s",
                    delay, key, interval, exc,
                )
                time.sleep(delay)
        return []

    def _throttle(self):
        if self.requests_per_second <= 0:
            return
        with self._rate_lock:
            wait = (1.0 / self.requests_per_second) - (time.monotonic() - self._last_request_at)
            if wait > 0:
                time.sleep(wait)
            self._last_request_at = time.monotonic()

    @staticmethod
    def _is_current(path: Path, key: str, trade_date: str, interval: int, target: datetime) -> bool:
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
            stamp = _as_ist(doc.get("source_candle_timestamp"))
            levels = doc.get("levels") or {}
            return (
                doc.get("trade_date_ist") == trade_date
                and doc.get("instrument_key") == key
                and int(doc.get("interval_minutes", 0)) == interval
                and stamp == target
                and all(levels.get(name) is not None for name in LEVEL_KEYS)
                and all(levels.get(name) is not None for name in THRESHOLD_KEYS)
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError, AttributeError):
            return False

    @staticmethod
    def _save_levels(
        path: Path, leg: dict, key: str, trade_date: str, interval: int,
        candle_stamp: datetime, candle: dict, document_type: str,
    ):
        calculated = compute_or_levels(candle)
        levels = {
            "R3": calculated["R3"], "R2": calculated["R2"], "R1": calculated["R1"],
            "AVG": calculated.get("A", calculated.get("avg")),
            "S1": calculated["S1"], "S2": calculated["S2"], "S3": calculated["S3"],
            "R3_Threshold": calculated["R3_Threshold"],
            "S3_Threshold": calculated["S3_Threshold"],
        }
        if any(value is None or not math.isfinite(float(value)) for value in levels.values()):
            raise ValueError(f"Invalid pivot levels for {key} interval={interval}m")
        document = {
            "schema_version": 1,
            "document_type": document_type,
            "trade_date_ist": trade_date,
            "computed_at_ist": datetime.now(IST).isoformat(),
            "leg_role": leg.get("leg_role"),
            "instrument_key": key,
            "trading_symbol": leg.get("trading_symbol") or key,
            "strike": leg.get("strike_price"),
            "option_type": leg.get("instrument_type"),
            "expiry": leg.get("expiry"),
            "interval_minutes": interval,
            "interval_label": f"{interval}m",
            "source": "Upstox HistoryV3Api.get_intra_day_candle_data",
            "source_candle_timestamp": candle.get("timestamp"),
            "source_candle_end_timestamp": (candle_stamp + timedelta(minutes=interval)).isoformat(),
            "source_candle": candle,
            "levels": levels,
            "thresholds": {
                "R3_Threshold": levels["R3_Threshold"],
                "S3_Threshold": levels["S3_Threshold"],
            },
            "calculation": calculated,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix="pivot_levels_", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(document, handle, indent=2, ensure_ascii=False, allow_nan=False, default=str)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)


class IsolatedPivotCaptureManager:
    """Own one polling worker for the current day's isolated CE/PE pair."""

    def __init__(self):
        self._lock = threading.RLock()
        self._workers: dict[str, tuple[threading.Thread, threading.Event]] = {}
        self._status: dict[str, dict] = {}

    def start(self, record: dict, access_token: str):
        if not ISOLATED_PIVOT_ENABLED or not access_token:
            return
        trade_date = str(record.get("trade_date_ist") or datetime.now(IST).date().isoformat())
        isolation_id = str(record.get("isolation_id") or f"{trade_date}__{record.get('instrument_key')}")
        if not _leg_rows(record):
            logger.warning("Isolated pivot capture skipped: no instrument keys")
            return
        with self._lock:
            existing = self._workers.get(isolation_id)
            if existing and existing[0].is_alive():
                return
            stop_event = threading.Event()
            worker = threading.Thread(
                target=self._run,
                args=(isolation_id, dict(record), access_token, stop_event),
                name="isolated-pivot-capture",
                daemon=True,
            )
            self._workers[isolation_id] = (worker, stop_event)
            self._status[isolation_id] = {
                "state": "starting", "trade_date_ist": trade_date,
                "intervals": [f"{item}m" for item in ISOLATED_PIVOT_INTERVALS],
                "legs": [row["leg_role"] for row in _leg_rows(record)],
            }
            worker.start()
        logger.info(
            "Isolated pivot capture started | isolation=%s | legs=%d | intervals=%s",
            isolation_id, len(_leg_rows(record)), ISOLATED_PIVOT_INTERVALS,
        )

    def _run(self, isolation_id: str, record: dict, token: str, stop_event: threading.Event):
        try:
            service = IsolatedPivotLevelsService(token)
            poll_seconds = max(15, int(os.getenv("ISOLATED_PIVOT_SCHEDULER_INTERVAL_SECONDS", "30")))
            trade_date = str(record.get("trade_date_ist") or datetime.now(IST).date().isoformat())
            final_cycle_done = False
            while not stop_event.is_set():
                now = datetime.now(IST)
                if now.date().isoformat() != trade_date or now.weekday() >= 5:
                    break
                if now.time().replace(tzinfo=None) < SESSION_OPEN:
                    boundary = now.replace(hour=9, minute=15, second=0, microsecond=0)
                    stop_event.wait(max(1.0, (boundary - now).total_seconds()))
                    continue
                close_at = datetime.combine(now.date(), SESSION_CLOSE, tzinfo=IST)
                grace = timedelta(seconds=service.close_grace_seconds)
                if now >= close_at + grace and final_cycle_done:
                    break
                if now >= close_at and now < close_at + grace:
                    # Let the final 15:15–15:30 and 15:29–15:30 candles
                    # publish before fetching them after the configured grace.
                    stop_event.wait(max(0.1, (close_at + grace - now).total_seconds()))
                    continue
                with self._lock:
                    self._status[isolation_id] = {
                        **self._status.get(isolation_id, {}),
                        "state": "running",
                        "last_cycle_started_at_ist": now.isoformat(),
                    }
                try:
                    result = service.process_cycle(record, now=now)
                    with self._lock:
                        self._status[isolation_id] = {
                            **self._status.get(isolation_id, {}),
                            "state": "running", "last_cycle": result,
                            "last_cycle_completed_at_ist": datetime.now(IST).isoformat(),
                        }
                except Exception as exc:
                    logger.exception("Isolated pivot polling cycle failed | isolation=%s", isolation_id)
                    with self._lock:
                        self._status[isolation_id] = {
                            **self._status.get(isolation_id, {}),
                            "state": "error", "last_error": str(exc),
                        }
                after_cycle = datetime.now(IST)
                close_at = datetime.combine(after_cycle.date(), SESSION_CLOSE, tzinfo=IST)
                final_deadline = close_at + timedelta(seconds=service.close_grace_seconds)
                remaining = (final_deadline - after_cycle).total_seconds()
                if remaining <= 0:
                    final_cycle_done = True
                    break
                # Do not sleep past the final completed-candle fetch window.
                stop_event.wait(min(poll_seconds, max(0.1, remaining)))
            with self._lock:
                self._status[isolation_id] = {
                    **self._status.get(isolation_id, {}),
                    "state": "stopped",
                    "stopped_at_ist": datetime.now(IST).isoformat(),
                }
        except Exception as exc:
            logger.exception("Isolated pivot worker failed | isolation=%s", isolation_id)
            with self._lock:
                self._status[isolation_id] = {
                    **self._status.get(isolation_id, {}),
                    "state": "error", "last_error": str(exc),
                }

    def status(self) -> dict:
        with self._lock:
            return {
                "enabled": ISOLATED_PIVOT_ENABLED,
                "intervals": [f"{item}m" for item in ISOLATED_PIVOT_INTERVALS],
                "workers": {key: dict(value) for key, value in self._status.items()},
            }

    def stop(self):
        with self._lock:
            workers = list(self._workers.values())
        for _, event in workers:
            event.set()
        for thread, _ in workers:
            if thread.is_alive():
                thread.join(timeout=3)


_manager = IsolatedPivotCaptureManager()


def start_isolated_pivot_capture(record: dict, access_token: str) -> None:
    _manager.start(record, access_token)


def get_isolated_pivot_status() -> dict:
    return _manager.status()


def stop_isolated_pivot_capture() -> None:
    _manager.stop()
