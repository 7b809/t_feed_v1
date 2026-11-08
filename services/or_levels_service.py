# services/or_levels_service.py
"""Opening Range levels orchestration.

Flow:
  1. Read `pivot_levels` from Mongo. If `trade_date_ist == today` →
     save locally, return it.
  2. Else fetch 1-min intraday candles for the 4 indices.
  3. Compute OR levels for each (via core.or_levels_calculator).
  4. Build the same shape as the upstream `pivot_levels` doc.
  5. Save to Mongo (replace) + `data/or_levels.json`.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime
from zoneinfo import ZoneInfo

from core.candle_fetcher import CandleFetcher, CandleFetchError
from core.config import (
    INDEX_METADATA,
    INSTRUMENT_KEYS,
    OR_LEVELS_LOCAL_PATH,
    PIVOT_DOC_ID,
    PROJECT_INFO_COLLECTION,
    TIMEZONE,
)
from core.logger import get_logger
from core.mongo_client import get_mongo_db
from core.or_levels_calculator import compute_or_levels, pick_first_candle

logger = get_logger(__file__)


class ORLevelsService:
    def __init__(self, access_token: str):
        self.access_token = access_token
        self.fetcher = CandleFetcher(access_token)

    # ------------------------------------------------------------------ #
    def ensure_or_levels(self) -> dict:
        """Return today's OR levels (loading/computing as needed)."""
        today = date.today().isoformat()

        existing = self._load_from_mongo()
        if existing and existing.get("trade_date_ist") == today:
            logger.info(
                "OR levels present in Mongo for %s; using existing", today
            )
            self._save_local(existing)
            return existing

        if existing:
            logger.warning(
                "pivot_levels trade_date_ist=%s != today=%s; recomputing",
                existing.get("trade_date_ist"), today,
            )
        else:
            logger.warning("pivot_levels not found; computing from candles")

        doc = self._compute_fresh(today)
        self._save_mongo(doc)
        self._save_local(doc)
        logger.info("OR levels computed and persisted for %s", today)
        return doc

    # ------------------------------------------------------------------ #
    def _compute_fresh(self, today: str) -> dict:
        now = datetime.now(ZoneInfo(TIMEZONE)).isoformat()
        indexes: dict = {}

        for key in INSTRUMENT_KEYS:
            meta = INDEX_METADATA.get(key, {})
            try:
                candles = self.fetcher.fetch_intraday_candles(key)
            except CandleFetchError:
                logger.exception("OR: candle fetch failed for %s", key)
                continue

            first = pick_first_candle(candles)
            if not first:
                logger.error("OR: no first candle for %s", key)
                continue

            try:
                levels = compute_or_levels(first)
            except Exception:
                logger.exception("OR: compute failed for %s", key)
                continue

            indexes[key] = {
                "canonical": meta.get("canonical"),
                "folder": meta.get("folder"),
                "underlying": meta.get("underlying"),
                "trading_symbol": meta.get("trading_symbol"),
                "priority": meta.get("priority"),
                "first_candle": {
                    "timestamp": first.get("timestamp"),
                    "open": first.get("open"),
                    "high": first.get("high"),
                    "low": first.get("low"),
                    "close": first.get("close"),
                    "volume": first.get("volume", 0),
                },
                "levels": levels,
            }
            logger.info(
                "OR %s: R3=%.2f (thr=%.2f) S3=%.2f (thr=%.2f)",
                meta.get("canonical"),
                levels["R3"], levels["R3_Threshold"],
                levels["S3"], levels["S3_Threshold"],
            )

        if not indexes:
            raise RuntimeError("OR: no indexes computed")

        return {
            "_id": PIVOT_DOC_ID,
            "computed_at": now,
            "indexes": indexes,
            "job": "pivot_levels",
            "trade_date_ist": today,
            "trigger": "local_startup_backfill",
            "source": "or_levels_service",
            "updated_at": now,
        }

    # ------------------------------------------------------------------ #
    def _load_from_mongo(self) -> dict | None:
        try:
            db = get_mongo_db()
            return db[PROJECT_INFO_COLLECTION].find_one({"_id": PIVOT_DOC_ID})
        except Exception:
            logger.exception("OR: Mongo read failed")
            return None

    def _save_mongo(self, doc: dict) -> None:
        try:
            db = get_mongo_db()
            db[PROJECT_INFO_COLLECTION].replace_one(
                {"_id": PIVOT_DOC_ID}, doc, upsert=True
            )
        except Exception:
            logger.exception("OR: Mongo save failed")

    def _save_local(self, doc: dict) -> None:
        try:
            path = OR_LEVELS_LOCAL_PATH
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(doc, f, indent=2, default=str)
            logger.info("OR levels saved locally to %s", path)
        except Exception:
            logger.exception("OR: local save failed")