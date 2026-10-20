"""ProChart alerts persistence service.

Storage layout
--------------
Every received alert is persisted to TWO places:

1. MongoDB
   Database  : UPSTOX_APP_V2
   Collection: prochart_alerts
   Doc _id   : <instrument_key>
   Shape::
       {
         "_id":         "NSE_INDEX|Nifty 50",
         "day":         "2026-10-20",        <- today's date
         "count":       3,
         "alerts": [
           {
             "time_hhmm":       "09_23",
             "time_iso":        "2026-10-20T09:23:00+05:30",
             "alert_type":      "ema_cross",
             "cross_type":      "bullish",
             "cross_price":     123.45,
             "trigger_config":  "after_r3_or_s3_touch",
             "trigger_context": {
                "level_touched":    "S3",
                "level_value":      155.5,
                "level_touch_time": "2026-10-20T09:15:00+05:30"
             }
           },
           ...
         ],
         "updated_at": "2026-10-20T10:30:14Z"
       }

2. Data folder
   Path: data/prochart_alerts/<safe_instrument_key>.json
   Contains exactly the same document body as the MongoDB doc,
   pretty-printed for offline inspection.

Day-reset rule
--------------
When a new alert arrives and the stored `day` no longer matches the
cross's own day (i.e. the clock has rolled past midnight IST), the
`alerts` list is reset to empty before appending — so the document
always contains **today's** EMA-cross payloads only, as required.

Deduplication
-------------
An alert whose `time_hhmm` AND `cross_type` already exist in the
list is skipped (prevents duplicate payloads when the browser
re-renders during the same minute).

Concurrency
-----------
A single service-level `threading.Lock` serialises the read-modify-
write cycle for both the Mongo doc and the data-folder file. Multiple
browsers / instruments can save concurrently; each instrument key is
still isolated by `_id`/filename.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pymongo import MongoClient

from core import config
from core.logger import get_logger

logger = get_logger(__file__)

# --------------------------------------------------------------------------- #
# Constants                                                                    #
# --------------------------------------------------------------------------- #
DB_NAME = "UPSTOX_APP_V2"
COLLECTION_NAME = "prochart_alerts"
DATA_SUBDIR = "prochart_alerts"

# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #
def _safe_folder_name(key: str) -> str:
    text = str(key or "unknown").strip()
    for ch in ("|", "/", "\\", ":", "*", "?", '"', "<", ">", " "):
        text = text.replace(ch, "_")
    return text.strip("._") or "unknown"

def _parse_cross_dt(cross_time: str) -> tuple[str, str]:
    """Return (day_iso, time_hhmm_underscore) from an ISO timestamp."""
    try:
        dt = datetime.fromisoformat(str(cross_time).replace("Z", "+00:00"))
        return dt.date().isoformat(), dt.strftime("%H_%M")
    except Exception:
        now = datetime.now(timezone.utc)
        return now.date().isoformat(), now.strftime("%H_%M")

# --------------------------------------------------------------------------- #
# Service                                                                      #
# --------------------------------------------------------------------------- #
class ProChartAlertsService:
    def __init__(self) -> None:
        self._client: MongoClient | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # Mongo / paths                                                      #
    # ------------------------------------------------------------------ #
    def _mongo(self) -> MongoClient:
        if self._client is None:
            if not config.MONGO_URI:
                raise RuntimeError(
                    "MONGO_URL must be set for ProChart alerts"
                )
            self._client = MongoClient(config.MONGO_URI)
        return self._client

    def _collection(self):
        return self._mongo()[DB_NAME][COLLECTION_NAME]

    def _data_path(self, instrument_key: str) -> Path:
        safe = _safe_folder_name(instrument_key)
        return Path(config.LIVE_DATA_DIR) / DATA_SUBDIR / f"{safe}.json"

    # ------------------------------------------------------------------ #
    # Public API                                                         #
    # ------------------------------------------------------------------ #
    def save_alert(self, payload: dict[str, Any]) -> dict[str, Any]:
        instrument_key = str(payload.get("instrument_key") or "").strip()
        if not instrument_key:
            return {"status": "error", "error": "instrument_key_required"}

        cross_time = str(payload.get("cross_time") or "").strip()
        alert_day, time_hhmm = _parse_cross_dt(cross_time)

        record = {
            "time_hhmm": time_hhmm,
            "time_iso": cross_time,
            "alert_type": payload.get("alert_type", "ema_cross"),
            "cross_type": payload.get("cross_type"),
            "cross_price": payload.get("cross_price"),
            "trigger_config": payload.get("trigger_config"),
            "trigger_context": payload.get("trigger_context") or {},
        }

        with self._lock:
            doc = self._merge_and_write(instrument_key, alert_day, record)

        return {
            "status": "ok",
            "instrument_key": instrument_key,
            "day": doc["day"],
            "count": doc["count"],
            "data_path": str(self._data_path(instrument_key)),
        }

    def get_alerts(self, instrument_key: str) -> dict[str, Any]:
        instrument_key = str(instrument_key or "").strip()
        if not instrument_key:
            return {"status": "error", "error": "instrument_key_required"}
        try:
            doc = self._collection().find_one({"_id": instrument_key})
            if not doc:
                return {
                    "status": "ok",
                    "instrument_key": instrument_key,
                    "count": 0,
                    "alerts": [],
                }
            return {
                "status": "ok",
                "instrument_key": instrument_key,
                "day": doc.get("day"),
                "count": doc.get("count", 0),
                "alerts": doc.get("alerts", []),
                "updated_at": doc.get("updated_at"),
            }
        except Exception:
            logger.exception("[prochart-alerts] mongo read failed")
            return {"status": "error", "error": "mongo_read_failed"}

    # ------------------------------------------------------------------ #
    # Internal merge + write                                             #
    # ------------------------------------------------------------------ #
    def _merge_and_write(
        self,
        instrument_key: str,
        alert_day: str,
        record: dict[str, Any],
    ) -> dict[str, Any]:
        existing: dict[str, Any] | None = None
        try:
            existing = self._collection().find_one({"_id": instrument_key})
        except Exception:
            logger.exception("[prochart-alerts] mongo read failed")

        if not existing or str(existing.get("day")) != alert_day:
            alerts_list: list[dict[str, Any]] = []
        else:
            alerts_list = list(existing.get("alerts") or [])

        # Dedupe by (time_hhmm, cross_type)
        for prev in alerts_list:
            if (
                prev.get("time_hhmm") == record["time_hhmm"]
                and prev.get("cross_type") == record["cross_type"]
            ):
                return {
                    "_id": instrument_key,
                    "day": alert_day,
                    "count": len(alerts_list),
                    "alerts": alerts_list,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }

        alerts_list.append(record)
        alerts_list.sort(key=lambda a: str(a.get("time_iso") or ""))

        doc = {
            "_id": instrument_key,
            "day": alert_day,
            "count": len(alerts_list),
            "alerts": alerts_list,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

        # 1) Write to MongoDB
        try:
            self._collection().replace_one(
                {"_id": instrument_key}, doc, upsert=True
            )
        except Exception:
            logger.exception("[prochart-alerts] mongo write failed")

        # 2) Write to data folder
        try:
            path = self._data_path(instrument_key)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(doc, indent=2, default=str), encoding="utf-8"
            )
            tmp.replace(path)
        except Exception:
            logger.exception("[prochart-alerts] data folder write failed")

        logger.info(
            "[prochart-alerts] saved alert — instrument=%s day=%s "
            "time=%s cross=%s count=%d",
            instrument_key,
            alert_day,
            record["time_hhmm"],
            record["cross_type"],
            doc["count"],
        )
        return doc

prochart_alerts_service = ProChartAlertsService()