# repositories/isolation_repository.py
"""MongoDB persistence for the isolation document.

Primary doc:   project_info._id = "instrument_isolation"   (single, overwritten)
Audit history: isolation_events.*                          (one per isolation)
"""

from __future__ import annotations

from typing import Optional

from core.config import (
    PROJECT_INFO_COLLECTION,
    ISOLATION_DOC_ID,
    ISOLATION_EVENTS_COLLECTION,
)
from core.logger import get_logger
from core.mongo_client import get_mongo_db

logger = get_logger(__file__)


class IsolationRepository:
    def __init__(self):
        self.db = get_mongo_db()

    # ------------------------------------------------------------------ #
    def get_current(self) -> Optional[dict]:
        return self.db[PROJECT_INFO_COLLECTION].find_one({"_id": ISOLATION_DOC_ID})

    def upsert_current(self, record: dict) -> None:
        self.db[PROJECT_INFO_COLLECTION].replace_one(
            {"_id": record["_id"]}, record, upsert=True
        )

    def append_history(self, record: dict) -> None:
        history = dict(record)
        history.pop("_id", None)
        history["_id"] = (
            f"{record['trade_date_ist']}__{record['trigger_index']}__"
            f"{record['trigger_side']}__"
            f"{record['instrument_key'].replace('|', '_')}"
        )
        try:
            self.db[ISOLATION_EVENTS_COLLECTION].replace_one(
                {"_id": history["_id"]}, history, upsert=True
            )
        except Exception:
            logger.exception("History write failed (non-fatal)")

    def list_history(self, limit: int = 20) -> list[dict]:
        cursor = (
            self.db[ISOLATION_EVENTS_COLLECTION]
            .find({})
            .sort("trade_date_ist", -1)
            .limit(limit)
        )
        return list(cursor)