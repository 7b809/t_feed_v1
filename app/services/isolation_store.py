# app/services/isolation_store.py
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from pymongo import ReturnDocument

from ..config import Settings
from .mongodb_service import MongoDbService

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")

# Fixed filename for the on-disk mirror. Matches app1's singleton
# ``_id='instrument_isolation'`` convention. Mongo remains the source
# of truth; the mirror is a latest-wins convenience view.
MIRROR_FILENAME = "instrument_isolation.json"


def _now_ist_iso() -> str:
    return datetime.now(IST).isoformat()


def today_ist_date() -> str:
    """Return today's IST date as ``YYYY-MM-DD``."""
    return datetime.now(IST).strftime("%Y-%m-%d")


class IsolationStore:
    """Persistence for isolation_events.

    One document per isolation per day in Mongo
    (``_id = trade_date__instrument_key``). Also mirrors the latest
    document to ``{data_dir}/instrument_isolation.json`` when
    ``DATA_DIR`` is set.
    """

    UPSTREAM_SINGLETON_ID = "instrument_isolation"

    def __init__(self, mongo: MongoDbService, config: Settings) -> None:
        self._mongo = mongo
        self._config = config

        if config.data_dir_enabled:
            try:
                Path(config.data_dir).mkdir(parents=True, exist_ok=True)
            except Exception:
                logger.exception(
                    "could not create DATA_DIR=%s; mirror disabled",
                    config.data_dir,
                )

    def _collection(self):
        return self._mongo.db[self._config.isolation_events_collection]

    # ------------------------------------------------------------------ id
    @staticmethod
    def make_isolation_id(trade_date_ist: str, instrument_key: str) -> str:
        return f"{trade_date_ist}__{instrument_key.replace('|', '_')}"

    # -------------------------------------------------------------- mirror
    def _mirror_path(self) -> Optional[Path]:
        if not self._config.data_dir_enabled:
            return None
        return Path(self._config.data_dir) / MIRROR_FILENAME

    def write_mirror(self, doc: Dict[str, Any]) -> None:
        """Public helper: write ``doc`` to ``{DATA_DIR}/instrument_isolation.json``.

        No-op when ``DATA_DIR`` is unset. Best-effort: exceptions are
        logged and swallowed so callers (startup, POST handler,
        ``set_status``) never fail because of an I/O hiccup.
        """
        path = self._mirror_path()
        if path is None or not doc:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(doc, indent=2, default=str, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception:
            logger.exception("failed to write isolation mirror %s", path)

    # Backwards-compatible alias used internally.
    _write_mirror = write_mirror

    def _delete_mirror(self) -> None:
        path = self._mirror_path()
        if path is None:
            return
        try:
            if path.exists():
                path.unlink()
        except Exception:
            logger.exception("failed to remove isolation mirror %s", path)

    # -------------------------------------------------------------- upsert
    def upsert(
        self,
        payload: Dict[str, Any],
        isolation_id: str,
        overwrite: bool = False,
    ) -> str:
        doc = dict(payload)
        incoming_id = doc.pop("_id", None)
        if incoming_id in (None, ""):
            incoming_id = None

        now = _now_ist_iso()

        if overwrite:
            full_doc: Dict[str, Any] = {
                **doc,
                "_id": isolation_id,
                "received_at": now,
                "isolation_id": isolation_id,
                "consumer_status": "subscribing",
                "subscribed_legs": [],
            }
            if incoming_id is not None:
                full_doc["isolation_source_id"] = incoming_id

            self._collection().replace_one(
                {"_id": isolation_id}, full_doc, upsert=True
            )
            self.write_mirror(full_doc)
            logger.info(
                "isolation_events overwrite id=%s (full replace)", isolation_id
            )
            return isolation_id

        set_doc: Dict[str, Any] = {
            **doc,
            "received_at": now,
            "isolation_id": isolation_id,
        }
        if incoming_id is not None:
            set_doc["isolation_source_id"] = incoming_id

        update: Dict[str, Any] = {"$set": set_doc}
        existing = self._collection().find_one({"_id": isolation_id}, {"_id": 1})
        if existing is None:
            update["$setOnInsert"] = {
                "consumer_status": "subscribing",
                "subscribed_legs": [],
            }

        self._collection().update_one(
            {"_id": isolation_id}, update, upsert=True
        )

        persisted = self._collection().find_one({"_id": isolation_id})
        if persisted:
            self.write_mirror(persisted)

        logger.info("isolation_events upsert id=%s", isolation_id)
        return isolation_id

    # -------------------------------------------------------------- status
    def set_status(
        self,
        isolation_id: str,
        status: str,
        subscribed_legs: Optional[List[str]] = None,
    ) -> None:
        update: Dict[str, Any] = {"$set": {"consumer_status": status}}
        if subscribed_legs is not None:
            update["$set"]["subscribed_legs"] = list(subscribed_legs)
        updated = self._collection().find_one_and_update(
            {"_id": isolation_id},
            update,
            return_document=ReturnDocument.AFTER,
        )
        if updated:
            self.write_mirror(updated)
        logger.info(
            "isolation_events status id=%s status=%s legs=%s",
            isolation_id,
            status,
            subscribed_legs,
        )

    # -------------------------------------------------------------- reads
    def get_current(self) -> Optional[Dict[str, Any]]:
        return self._collection().find_one(sort=[("received_at", -1)])

    def list_history(self, limit: int = 20) -> List[Dict[str, Any]]:
        return list(
            self._collection().find().sort("received_at", -1).limit(int(limit))
        )

    def list_active(self) -> List[Dict[str, Any]]:
        return list(
            self._collection().find(
                {"consumer_status": {"$in": ["live", "subscribing"]}}
            )
        )

    def list_active_today(self, today_ist: str) -> List[Dict[str, Any]]:
        return list(
            self._collection().find(
                {
                    "consumer_status": {"$in": ["live", "subscribing"]},
                    "trade_date_ist": today_ist,
                }
            )
        )

    def find_today_isolations(self, today_ist: str) -> List[Dict[str, Any]]:
        """Return today's isolations to re-subscribe on startup.

        Merges two sources (deduped by the derived isolation_id):

          1. Consumer-owned docs (``consumer_status IN ("live","subscribing")``)

          2. app1's singleton document (``_id == "instrument_isolation"``)
             if it is for *today* and ``status == "ACTIVE"``. Has no
             ``consumer_status`` field, so it must be adopted separately.

        Source #1 wins when both exist for the same instrument.
        """
        results: Dict[str, Dict[str, Any]] = {}

        try:
            consumer_docs = self._collection().find(
                {
                    "consumer_status": {"$in": ["live", "subscribing"]},
                    "trade_date_ist": today_ist,
                }
            )
            for doc in consumer_docs:
                td = doc.get("trade_date_ist")
                ik = doc.get("instrument_key")
                if not (td and ik):
                    continue
                results[self.make_isolation_id(td, ik)] = doc
        except Exception:
            logger.exception("find_today_isolations: consumer query failed")

        try:
            upstream_doc = self._collection().find_one(
                {
                    "_id": self.UPSTREAM_SINGLETON_ID,
                    "trade_date_ist": today_ist,
                    "status": "ACTIVE",
                }
            )
            if upstream_doc:
                td = upstream_doc.get("trade_date_ist")
                ik = upstream_doc.get("instrument_key")
                if td and ik:
                    derived = self.make_isolation_id(td, ik)
                    if derived not in results:
                        results[derived] = upstream_doc
        except Exception:
            logger.exception("find_today_isolations: upstream query failed")

        if results:
            logger.info(
                "find_today_isolations today=%s matched=%d (consumer=%d, "
                "upstream_singleton_adopted=%d)",
                today_ist,
                len(results),
                sum(
                    1
                    for d in results.values()
                    if d.get("consumer_status") in ("live", "subscribing")
                ),
                sum(
                    1
                    for d in results.values()
                    if d.get("_id") == self.UPSTREAM_SINGLETON_ID
                ),
            )
        return list(results.values())

    def get(self, isolation_id: str) -> Optional[Dict[str, Any]]:
        return self._collection().find_one({"_id": isolation_id})