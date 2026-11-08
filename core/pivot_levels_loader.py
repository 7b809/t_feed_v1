# core/pivot_levels_loader.py
"""Check and load pivot levels from MongoDB ``project_info``."""

import json
import os
from datetime import datetime, date

from core.config import PROJECT_INFO_COLLECTION
from core.mongo_client import get_mongo_db
from core.logger import get_logger

logger = get_logger(__file__)

PIVOT_DOC_ID = "pivot_levels"
LOCAL_CACHE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "data", "pivot_levels.json"
)


def is_pivot_levels_updated_today() -> bool:
    """Return ``True`` if the ``pivot_levels`` document was updated today (IST)."""
    db = get_mongo_db()
    doc = db[PROJECT_INFO_COLLECTION].find_one({"_id": PIVOT_DOC_ID})
    if not doc:
        logger.error("pivot_levels document not found.")
        return False

    # Preferred check: trade_date_ist
    trade_date = doc.get("trade_date_ist")
    if trade_date:
        today_str = date.today().isoformat()
        if trade_date == today_str:
            logger.info("pivot_levels updated for today: %s", trade_date)
            return True
        logger.warning(
            "pivot_levels trade_date_ist=%s does not match today=%s",
            trade_date,
            today_str,
        )
        return False

    # Fallback: computed_at
    computed_at = doc.get("computed_at")
    if computed_at:
        try:
            dt = datetime.fromisoformat(computed_at)
            if dt.date() == date.today():
                logger.info("pivot_levels computed_at is today: %s", computed_at)
                return True
        except Exception:
            pass

    logger.warning("pivot_levels not updated today.")
    return False


def load_pivot_levels_to_local() -> dict:
    """Load the ``pivot_levels`` document and save it to a local JSON cache."""
    db = get_mongo_db()
    doc = db[PROJECT_INFO_COLLECTION].find_one({"_id": PIVOT_DOC_ID})
    if not doc:
        raise RuntimeError("pivot_levels document not found.")

    os.makedirs(os.path.dirname(LOCAL_CACHE_PATH), exist_ok=True)
    with open(LOCAL_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2, default=str)

    logger.info("Pivot levels saved to %s", LOCAL_CACHE_PATH)
    return doc