"""Per-option pivot levels for each configured completed candle interval.

Each interval is persisted independently beside its option contract metadata:
1m keeps the legacy pivot_levels.json name; other intervals use
pivot_levels_<N>m.json. Only completed candles aligned to the 09:15 IST
session open are eligible.
"""
from __future__ import annotations

import json
import math
import os
import re
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, time as dt_time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from core.candle_fetcher import CandleFetchError, CandleFetcher
from core.logger import get_logger
from core.or_levels_calculator import compute_or_levels

logger = get_logger(__file__)
IST = ZoneInfo("Asia/Kolkata")
SESSION_OPEN = dt_time(9, 15)
SESSION_CLOSE = dt_time(15, 30)
LEVEL_KEYS = ("R3", "R2", "R1", "AVG", "S1", "S2", "S3")
SUPPORTED_INTERVALS = {1, 2, 3, 5, 10, 15, 30}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        logger.warning("Invalid %s; using default %s", name, default)
        return default


def _env_float(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
        if not math.isfinite(value):
            raise ValueError("non-finite value")
        return value
    except (TypeError, ValueError):
        logger.warning("Invalid %s; using default %s", name, default)
        return default


def _safe(value: Any) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "unknown")).strip("._") or "unknown"


def _contract_folder(root: Path, contract: dict, date_key: str) -> Path:
    strike = float(contract.get("strike_price") or 0)
    side = str(contract.get("instrument_type") or "unknown").lower()
    name = f"{strike:g}_{side}_{_safe(contract.get('instrument_key'))}"
    return root / str(contract.get("index_folder") or "unknown") / date_key / name


def _as_ist(timestamp: Any) -> datetime | None:
    try:
        raw = str(timestamp).strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        parsed = datetime.fromisoformat(raw)
        return (parsed.replace(tzinfo=IST) if parsed.tzinfo is None else parsed).astimezone(IST)
    except (TypeError, ValueError, OverflowError):
        return None


def parse_intervals(value: str | None = None) -> list[int]:
    """Parse, validate, and de-duplicate configured Upstox minute intervals."""
    raw = os.getenv("OPTION_PIVOT_INTERVALS", "1,15") if value is None else value
    intervals: list[int] = []
    for item in str(raw).split(","):
        item = item.strip().lower().removesuffix("m")
        if not item:
            continue
        try:
            interval = int(item)
        except ValueError:
            logger.warning("Ignoring invalid OPTION_PIVOT_INTERVALS item: %r", item)
            continue
        if interval not in SUPPORTED_INTERVALS:
            logger.warning("Ignoring unsupported option-pivot interval: %s", interval)
            continue
        if interval not in intervals:
            intervals.append(interval)
    if not intervals:
        logger.warning("No valid OPTION_PIVOT_INTERVALS configured; using [1, 15]")
        return [1, 15]
    return sorted(intervals)


def latest_completed_candle_start(
    trade_date: str,
    interval_minutes: int,
    now: datetime | None = None,
    grace_seconds: float | None = None,
) -> datetime | None:
    """Return the latest session-aligned candle start known to be complete.

    The publication grace avoids requesting a candle at the exact exchange
    boundary. A 15-minute candle is aligned from 09:15 (09:15-09:30, etc.).
    """
    current = now or datetime.now(IST)
    current = current.replace(tzinfo=IST) if current.tzinfo is None else current.astimezone(IST)
    session_start = datetime.combine(date.fromisoformat(trade_date), SESSION_OPEN, tzinfo=IST)
    session_end = datetime.combine(date.fromisoformat(trade_date), SESSION_CLOSE, tzinfo=IST)
    grace = _env_float("OPTION_PIVOT_CANDLE_CLOSE_GRACE_SECONDS", 5.0) if grace_seconds is None else max(0.0, float(grace_seconds))
    effective_now = min(current - timedelta(seconds=max(0.0, grace)), session_end)
    elapsed = (effective_now - session_start).total_seconds()
    interval_seconds = int(interval_minutes) * 60
    completed_count = math.floor(elapsed / interval_seconds)
    # Do not treat a shortened final session fragment as a full interval bar.
    max_full_bars = math.floor((session_end - session_start).total_seconds() / interval_seconds)
    completed_count = min(completed_count, max_full_bars)
    if completed_count <= 0:
        return None
    return session_start + timedelta(seconds=(completed_count - 1) * interval_seconds)


def _interval_path(folder: Path, interval: int) -> Path:
    return folder / f"pivot_levels_{interval}m.json"


def _valid_existing(
    path: Path, key: str, trade_date: str, interval: int, target_stamp: datetime
) -> bool:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        levels = doc.get("levels") or {}
        source_stamp = _as_ist(doc.get("source_candle_timestamp"))
        return (
            doc.get("trade_date_ist") == trade_date
            and doc.get("instrument_key") == key
            and int(doc.get("interval_minutes", 1)) == interval
            and source_stamp is not None
            and source_stamp == target_stamp
            and all(levels.get(level) is not None for level in LEVEL_KEYS)
        )
    except (OSError, json.JSONDecodeError, AttributeError, TypeError, ValueError):
        return False


class OptionInstrumentPivotLevelsService:
    """Bounded parallel fetches with one shared request-rate limiter."""

    def __init__(
        self, root="data/option_chain", requests_per_second=None,
        max_retries=None, retry_backoff_seconds=None, max_workers=None,
        intervals=None, close_grace_seconds=None,
    ):
        self.root = Path(root)
        self.requests_per_second = _env_float("OPTION_PIVOT_FETCH_REQUESTS_PER_SECOND", 4.0) if requests_per_second is None else float(requests_per_second)
        self.max_retries = max(0, _env_int("OPTION_PIVOT_FETCH_MAX_RETRIES", 2)) if max_retries is None else max(0, int(max_retries))
        self.retry_backoff_seconds = max(0.0, _env_float("OPTION_PIVOT_FETCH_RETRY_BACKOFF_SECONDS", 1.0)) if retry_backoff_seconds is None else max(0.0, float(retry_backoff_seconds))
        self.max_workers = max(1, _env_int("OPTION_PIVOT_MAX_WORKERS", 8) if max_workers is None else int(max_workers))
        self.intervals = parse_intervals() if intervals is None else parse_intervals(",".join(str(x) for x in intervals))
        self.close_grace_seconds = max(0.0, _env_float("OPTION_PIVOT_CANDLE_CLOSE_GRACE_SECONDS", 5.0)) if close_grace_seconds is None else max(0.0, float(close_grace_seconds))
        self._last_request_at = 0.0
        self._rate_lock = threading.Lock()
        self._thread_local = threading.local()

    def process_contracts(
        self, contracts: list[dict], access_token: str, trade_date: str,
        intervals: list[int] | None = None, now: datetime | None = None,
    ) -> dict:
        selected_intervals = self.intervals if intervals is None else parse_intervals(",".join(str(x) for x in intervals))
        summary = {
            "state": "completed", "trade_date_ist": str(trade_date),
            "total_contracts": len(contracts or []), "workers": self.max_workers,
            "intervals": {},
        }
        if not contracts:
            return summary

        due = {}
        for interval in selected_intervals:
            target = latest_completed_candle_start(
                trade_date, interval, now=now, grace_seconds=self.close_grace_seconds
            )
            if target is not None:
                due[interval] = target
            summary["intervals"][f"{interval}m"] = {
                "target_candle_timestamp": target.isoformat() if target else None,
                "computed": 0, "already_present": 0, "failed": 0, "skipped": 0,
            }
        if not due:
            summary["state"] = "waiting_for_completed_candle"
            return summary

        logger.info(
            "Option pivot cycle started | contracts=%d intervals=%s workers=%d rate=%.2f/sec",
            len(contracts), ",".join(f"{n}m" for n in due), self.max_workers,
            self.requests_per_second,
        )
        for interval, target in due.items():
            counts = summary["intervals"][f"{interval}m"]
            completed = 0
            with ThreadPoolExecutor(max_workers=self.max_workers, thread_name_prefix=f"option-pivot-{interval}m") as pool:
                futures = {
                    pool.submit(self._process_one, row, str(trade_date), access_token, interval, target): row
                    for row in contracts
                }
                for future in as_completed(futures):
                    completed += 1
                    row = futures[future] or {}
                    try:
                        counts[future.result()] += 1
                    except Exception as exc:
                        counts["failed"] += 1
                        if counts["failed"] <= 3:
                            logger.exception(
                                "Option pivot failed; continuing | interval=%dm | %d/%d | key=%s",
                                interval, completed, len(contracts), row.get("instrument_key"),
                            )
                        elif counts["failed"] % 100 == 0:
                            logger.warning(
                                "Option pivot failures=%d interval=%dm latest=%s error=%s",
                                counts["failed"], interval, row.get("instrument_key"), exc,
                            )
                    if completed == 1 or completed % 50 == 0 or completed == len(contracts):
                        logger.info(
                            "Option pivot progress interval=%dm %d/%d computed=%d existing=%d failed=%d skipped=%d",
                            interval, completed, len(contracts), counts["computed"],
                            counts["already_present"], counts["failed"], counts["skipped"],
                        )
            if counts["failed"]:
                summary["state"] = "partial"
            logger.info("Option pivot interval completed | interval=%dm | %s", interval, counts)
        logger.info("Option pivot cycle completed: %s", summary)
        return summary

    def _fetcher(self, token: str):
        client = getattr(self._thread_local, "fetcher", None)
        if client is None:
            client = CandleFetcher(token)
            self._thread_local.fetcher = client
        return client

    def _process_one(
        self, contract: dict, trade_date: str, token: str,
        interval: int, target_stamp: datetime,
    ) -> str:
        key = str((contract or {}).get("instrument_key") or "").strip()
        if not key:
            return "skipped"
        try:
            strike = float(contract.get("strike_price"))
            if not math.isfinite(strike) or strike <= 0:
                return "skipped"
        except (TypeError, ValueError):
            return "skipped"

        folder = _contract_folder(self.root, contract, trade_date.replace("-", ""))
        folder.mkdir(parents=True, exist_ok=True)
        target = _interval_path(folder, interval)
        target_is_current = _valid_existing(target, key, trade_date, interval, target_stamp)
        opening_is_saved = True
        if interval == 1:
            opening_stamp = datetime.combine(
                date.fromisoformat(trade_date), SESSION_OPEN, tzinfo=IST
            )
            opening_is_saved = _valid_existing(
                folder / "pivot_levels.json", key, trade_date, 1, opening_stamp
            )
        if target_is_current and opening_is_saved:
            return "already_present"

        candles = self._fetch_with_retry(self._fetcher(token), key, interval)
        match = None
        for candle in candles:
            stamp = _as_ist(candle.get("timestamp"))
            if stamp == target_stamp:
                match = candle
                break
        # Preserve the original opening-minute snapshot for downstream code
        # that historically reads pivot_levels.json. The rolling 1m series is
        # stored separately in pivot_levels_1m.json.
        if interval == 1:
            opening_stamp = datetime.combine(
                date.fromisoformat(trade_date), SESSION_OPEN, tzinfo=IST
            )
            opening_path = folder / "pivot_levels.json"
            if not _valid_existing(opening_path, key, trade_date, 1, opening_stamp):
                opening_match = next(
                    (
                        candle for candle in candles
                        if _as_ist(candle.get("timestamp")) == opening_stamp
                    ),
                    None,
                )
                if opening_match is not None:
                    self._save_candle_levels(
                        opening_path, contract, key, trade_date, 1, opening_stamp, opening_match
                    )

        if match is None:
            logger.debug(
                "Option pivot deferred: completed candle not yet published | interval=%dm key=%s target=%s",
                interval, key, target_stamp.isoformat(),
            )
            return "failed"

        self._save_candle_levels(target, contract, key, trade_date, interval, target_stamp, match)
        return "computed"

    def _save_candle_levels(
        self, path: Path, contract: dict, key: str, trade_date: str,
        interval: int, candle_stamp: datetime, candle: dict,
    ) -> None:
        values = compute_or_levels(candle)
        levels = {
            "R3": values["R3"], "R2": values["R2"], "R1": values["R1"],
            "AVG": values.get("A", values.get("avg")),
            "S1": values["S1"], "S2": values["S2"], "S3": values["S3"],
        }
        if any(levels.get(name) is None or not math.isfinite(float(levels[name])) for name in LEVEL_KEYS):
            raise ValueError(f"Invalid pivot values returned for {key} interval={interval}m")
        candle_end = candle_stamp + timedelta(minutes=interval)
        document = {
            "schema_version": 2,
            "trade_date_ist": trade_date,
            "computed_at_ist": datetime.now(IST).isoformat(),
            "instrument_key": key,
            "trading_symbol": contract.get("trading_symbol") or key,
            "index_instrument_key": contract.get("index_instrument_key"),
            "index_canonical": contract.get("index_canonical"),
            "interval_minutes": interval,
            "interval_label": f"{interval}m",
            "source": "Upstox HistoryV3Api.get_intra_day_candle_data",
            "source_candle_timestamp": candle.get("timestamp"),
            "source_candle_end_timestamp": candle_end.isoformat(),
            "levels": levels,
        }
        self._save_atomic(path, document)

    def _fetch_with_retry(self, fetcher, key: str, interval: int) -> list[dict]:
        attempts = max(0, self.max_retries) + 1
        for attempt in range(1, attempts + 1):
            self._throttle()
            try:
                return fetcher.fetch_intraday_candles(
                    key, unit="minutes", interval=str(interval)
                )
            except CandleFetchError as exc:
                message = str(exc).lower()
                transient = any(token in message for token in (
                    "429", "500", "502", "503", "504", "timeout", "timed out",
                    "connection", "temporar", "rate limit", "no candles",
                ))
                if not transient or attempt >= attempts:
                    raise
                delay = self.retry_backoff_seconds * (2 ** (attempt - 1))
                logger.warning(
                    "Transient candle error; retry %d/%d in %.1fs | interval=%dm key=%s | %s",
                    attempt, attempts - 1, delay, interval, key, exc,
                )
                time.sleep(max(0.0, delay))
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
    def _save_atomic(path: Path, document: dict):
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix="pivot_levels_", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(document, handle, indent=2, ensure_ascii=False, allow_nan=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, path)
        finally:
            if os.path.exists(name):
                os.unlink(name)
