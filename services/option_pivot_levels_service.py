"""Per-option-instrument opening-range pivot levels from Upstox intraday candles.

Each selected option contract gets a small, atomic pivot_levels.json beside
its existing option-chain files. Only the seven requested levels and minimal
provenance metadata are persisted; raw candle history is not copied here.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from core.candle_fetcher import CandleFetchError, CandleFetcher
from core.logger import get_logger
from core.or_levels_calculator import compute_or_levels, pick_first_candle

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


def _candle_date(timestamp: Any) -> date | None:
    if timestamp in (None, ""):
        return None
    try:
        raw = str(timestamp).strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        parsed = datetime.fromisoformat(raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=IST)
        return parsed.astimezone(IST).date()
    except (TypeError, ValueError, OverflowError):
        return None


def _valid_existing(path: Path, instrument_key: str, trade_date: str) -> bool:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        levels = doc.get("levels") or {}
        return (
            doc.get("trade_date_ist") == trade_date
            and doc.get("instrument_key") == instrument_key
            and all(levels.get(key) is not None for key in LEVEL_KEYS)
        )
    except (OSError, json.JSONDecodeError, AttributeError):
        return False


class OptionInstrumentPivotLevelsService:
    """Compute opening-candle pivots for option contracts without blocking feeds."""

    def __init__(
        self,
        root: str | Path = "data/option_chain",
        requests_per_second: float | None = None,
        max_retries: int | None = None,
        retry_backoff_seconds: float | None = None,
    ):
        self.root = Path(root)
        self.requests_per_second = (
            float(os.getenv("OPTION_PIVOT_FETCH_REQUESTS_PER_SECOND", "4"))
            if requests_per_second is None else float(requests_per_second)
        )
        self.max_retries = (
            int(os.getenv("OPTION_PIVOT_FETCH_MAX_RETRIES", "2"))
            if max_retries is None else int(max_retries)
        )
        self.retry_backoff_seconds = (
            float(os.getenv("OPTION_PIVOT_FETCH_RETRY_BACKOFF_SECONDS", "1"))
            if retry_backoff_seconds is None else float(retry_backoff_seconds)
        )
        self._last_request_at = 0.0
        self._rate_lock = threading.Lock()

    def process_contracts(
        self,
        contracts: list[dict],
        access_token: str,
        trade_date: str,
    ) -> dict:
        """Process all contracts independently; one failure never stops the batch."""
        summary = {
            "state": "completed",
            "trade_date_ist": str(trade_date),
            "total": len(contracts or []),
            "computed": 0,
            "already_present": 0,
            "failed": 0,
            "skipped": 0,
        }
        if not contracts:
            return summary
        try:
            fetcher = CandleFetcher(access_token)
        except Exception:
            logger.exception("Could not initialise HistoryV3Api for option pivots")
            summary["state"] = "initialization_failed"
            summary["failed"] = len(contracts)
            return summary

        logger.info(
            "Option pivot calculation started | contracts=%d | date=%s | source=HistoryV3Api",
            len(contracts), trade_date,
        )
        for index, contract in enumerate(contracts, start=1):
            try:
                result = self._process_one(fetcher, contract, str(trade_date))
                summary[result] += 1
            except Exception:
                summary["failed"] += 1
                logger.exception(
                    "Option pivot calculation failed; continuing | progress=%d/%d | key=%s",
                    index, len(contracts), (contract or {}).get("instrument_key"),
                )
            if index == 1 or index % 50 == 0 or index == len(contracts):
                logger.info(
                    "Option pivot progress %d/%d | computed=%d existing=%d failed=%d",
                    index, len(contracts), summary["computed"],
                    summary["already_present"], summary["failed"],
                )

        if summary["failed"]:
            summary["state"] = "partial"
        logger.info("Option pivot calculation completed: %s", summary)
        return summary

    def _process_one(self, fetcher: CandleFetcher, contract: dict, trade_date: str) -> str:
        instrument_key = str((contract or {}).get("instrument_key") or "").strip()
        if not instrument_key:
            logger.warning("Skipping option pivot: contract has no instrument_key")
            return "skipped"

        folder = _contract_folder(self.root, contract, trade_date.replace("-", ""))
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / "pivot_levels.json"
        if _valid_existing(target, instrument_key, trade_date):
            return "already_present"

        candles = self._fetch_with_retry(fetcher, instrument_key)
        first = pick_first_candle(candles)
        if not first:
            logger.warning("Option pivot deferred: no intraday candle | key=%s", instrument_key)
            return "failed"

        candle_date = _candle_date(first.get("timestamp"))
        if candle_date is None or candle_date.isoformat() != trade_date:
            logger.warning(
                "Option pivot rejected non-today candle | key=%s | candle_time=%s | expected_date=%s",
                instrument_key, first.get("timestamp"), trade_date,
            )
            return "failed"

        calculated = compute_or_levels(first)
        levels = {
            "R3": calculated["R3"],
            "R2": calculated["R2"],
            "R1": calculated["R1"],
            "AVG": calculated.get("A", calculated.get("avg")),
            "S1": calculated["S1"],
            "S2": calculated["S2"],
            "S3": calculated["S3"],
        }
        document = {
            "schema_version": 1,
            "trade_date_ist": trade_date,
            "computed_at_ist": datetime.now(IST).isoformat(),
            "instrument_key": instrument_key,
            "trading_symbol": contract.get("trading_symbol") or instrument_key,
            "index_instrument_key": contract.get("index_instrument_key"),
            "index_canonical": contract.get("index_canonical"),
            "source": "Upstox HistoryV3Api.get_intra_day_candle_data",
            "source_candle_timestamp": first.get("timestamp"),
            "levels": levels,
        }
        self._save_atomic(target, document)
        logger.debug(
            "Option pivots saved | key=%s | symbol=%s | path=%s",
            instrument_key, document["trading_symbol"], target,
        )
        return "computed"

    def _fetch_with_retry(self, fetcher: CandleFetcher, instrument_key: str) -> list[dict]:
        attempts = max(0, self.max_retries) + 1
        for attempt in range(1, attempts + 1):
            self._throttle()
            try:
                return fetcher.fetch_intraday_candles(
                    instrument_key, unit="minutes", interval="1"
                )
            except CandleFetchError as exc:
                message = str(exc).lower()
                transient = any(token in message for token in (
                    "429", "500", "502", "503", "504", "timeout",
                    "timed out", "connection", "temporar", "rate limit",
                ))
                if not transient or attempt >= attempts:
                    raise
                delay = self.retry_backoff_seconds * (2 ** (attempt - 1))
                logger.warning(
                    "Transient option candle fetch error; retry %d/%d in %.1fs | key=%s | error=%s",
                    attempt, attempts - 1, delay, instrument_key, exc,
                )
                time.sleep(max(0.0, delay))
        return []

    def _throttle(self) -> None:
        if self.requests_per_second <= 0:
            return
        minimum_interval = 1.0 / self.requests_per_second
        with self._rate_lock:
            now = time.monotonic()
            wait = minimum_interval - (now - self._last_request_at)
            if wait > 0:
                time.sleep(wait)
            self._last_request_at = time.monotonic()

    @staticmethod
    def _save_atomic(path: Path, document: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(
            prefix="pivot_levels_", suffix=".tmp", dir=str(path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(document, handle, indent=2, ensure_ascii=False, allow_nan=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, path)
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)
