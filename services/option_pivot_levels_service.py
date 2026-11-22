"""Parallel per-option opening-candle pivot calculation."""
from __future__ import annotations

import json
import math
import os
import re
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from core.candle_fetcher import CandleFetchError, CandleFetcher
from core.logger import get_logger
from core.or_levels_calculator import compute_or_levels

logger = get_logger(__file__)
IST = ZoneInfo("Asia/Kolkata")
LEVEL_KEYS = ("R3", "R2", "R1", "AVG", "S1", "S2", "S3")


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


def _valid_existing(path: Path, key: str, trade_date: str) -> bool:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        levels = doc.get("levels") or {}
        source_stamp = _as_ist(doc.get("source_candle_timestamp"))
        return (
            doc.get("trade_date_ist") == trade_date
            and doc.get("instrument_key") == key
            and source_stamp is not None
            and source_stamp.date() == date.fromisoformat(trade_date)
            and source_stamp.hour == 9
            and source_stamp.minute == 15
            and all(levels.get(level) is not None for level in LEVEL_KEYS)
        )
    except (OSError, json.JSONDecodeError, AttributeError):
        return False


class OptionInstrumentPivotLevelsService:
    """Use bounded parallelism with a shared API request-rate limiter."""

    def __init__(self, root="data/option_chain", requests_per_second=None,
                 max_retries=None, retry_backoff_seconds=None, max_workers=None):
        self.root = Path(root)
        self.requests_per_second = float(os.getenv("OPTION_PIVOT_FETCH_REQUESTS_PER_SECOND", "4")) if requests_per_second is None else float(requests_per_second)
        self.max_retries = int(os.getenv("OPTION_PIVOT_FETCH_MAX_RETRIES", "2")) if max_retries is None else int(max_retries)
        self.retry_backoff_seconds = float(os.getenv("OPTION_PIVOT_FETCH_RETRY_BACKOFF_SECONDS", "1")) if retry_backoff_seconds is None else float(retry_backoff_seconds)
        self.max_workers = max(1, int(os.getenv("OPTION_PIVOT_MAX_WORKERS", "8")) if max_workers is None else int(max_workers))
        self._last_request_at = 0.0
        self._rate_lock = threading.Lock()
        self._thread_local = threading.local()

    def process_contracts(self, contracts: list[dict], access_token: str, trade_date: str) -> dict:
        summary = {"state": "completed", "trade_date_ist": str(trade_date), "total": len(contracts or []),
                   "computed": 0, "already_present": 0, "failed": 0, "skipped": 0, "workers": self.max_workers}
        if not contracts:
            return summary
        logger.info("Option pivot parallel batch started | contracts=%d workers=%d rate=%.2f/sec candle=09:15 IST",
                    len(contracts), self.max_workers, self.requests_per_second)
        completed = 0
        with ThreadPoolExecutor(max_workers=self.max_workers, thread_name_prefix="option-pivot") as pool:
            futures = {pool.submit(self._process_one, row, str(trade_date), access_token): row for row in contracts}
            for future in as_completed(futures):
                completed += 1
                row = futures[future] or {}
                try:
                    summary[future.result()] += 1
                except Exception as exc:
                    summary["failed"] += 1
                    if summary["failed"] <= 3:
                        logger.exception("Option pivot failed; continuing | %d/%d | key=%s", completed, len(contracts), row.get("instrument_key"))
                    elif summary["failed"] % 100 == 0:
                        logger.warning("Option pivot failures=%d latest=%s error=%s", summary["failed"], row.get("instrument_key"), exc)
                if completed == 1 or completed % 50 == 0 or completed == len(contracts):
                    logger.info("Option pivot progress %d/%d computed=%d existing=%d failed=%d skipped=%d",
                                completed, len(contracts), summary["computed"], summary["already_present"], summary["failed"], summary["skipped"])
        if summary["failed"]:
            summary["state"] = "partial"
        logger.info("Option pivot parallel batch completed: %s", summary)
        return summary

    def _fetcher(self, token: str):
        client = getattr(self._thread_local, "fetcher", None)
        if client is None:
            client = CandleFetcher(token)
            self._thread_local.fetcher = client
        return client

    def _process_one(self, contract: dict, trade_date: str, token: str) -> str:
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
        target = folder / "pivot_levels.json"
        if _valid_existing(target, key, trade_date):
            return "already_present"

        candles = self._fetch_with_retry(self._fetcher(token), key)
        wanted_date = date.fromisoformat(trade_date)
        # Exact 09:15 start timestamp only: never substitute 09:16 or later.
        opening = []
        for candle in candles:
            stamp = _as_ist(candle.get("timestamp"))
            if stamp and stamp.date() == wanted_date and stamp.hour == 9 and stamp.minute == 15:
                opening.append((stamp, candle))
        opening.sort(key=lambda item: item[0])
        if not opening:
            logger.warning("Option pivot deferred: exact 09:15 candle unavailable | key=%s date=%s", key, trade_date)
            return "failed"
        first = opening[0][1]
        values = compute_or_levels(first)
        levels = {"R3": values["R3"], "R2": values["R2"], "R1": values["R1"],
                  "AVG": values.get("A", values.get("avg")), "S1": values["S1"],
                  "S2": values["S2"], "S3": values["S3"]}
        document = {"schema_version": 1, "trade_date_ist": trade_date,
                    "computed_at_ist": datetime.now(IST).isoformat(), "instrument_key": key,
                    "trading_symbol": contract.get("trading_symbol") or key,
                    "index_instrument_key": contract.get("index_instrument_key"),
                    "index_canonical": contract.get("index_canonical"),
                    "source": "Upstox HistoryV3Api.get_intra_day_candle_data",
                    "source_candle_timestamp": first.get("timestamp"), "levels": levels}
        self._save_atomic(target, document)
        return "computed"

    def _fetch_with_retry(self, fetcher, key: str) -> list[dict]:
        attempts = max(0, self.max_retries) + 1
        for attempt in range(1, attempts + 1):
            self._throttle()
            try:
                return fetcher.fetch_intraday_candles(key, unit="minutes", interval="1")
            except CandleFetchError as exc:
                message = str(exc).lower()
                transient = any(token in message for token in ("429", "500", "502", "503", "504", "timeout", "timed out", "connection", "temporar", "rate limit", "no candles"))
                if not transient or attempt >= attempts:
                    raise
                delay = self.retry_backoff_seconds * (2 ** (attempt - 1))
                logger.warning("Transient candle error; retry %d/%d in %.1fs | key=%s | %s", attempt, attempts - 1, delay, key, exc)
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
