"""ProChart settings service.

One MongoDB document — _id = "prochart_settings" — in
UPSTOX_APP_V2.app_tokens holds the ENTIRE ProChart state:

    {
      "_id":                  "prochart_settings",
      "chart_instrument_key": "NSE_INDEX|Nifty 50",
      "alerts_enabled":       false,
      "alert_config": {
         "trigger": "after_r3_or_s3_touch"
      },
      "created_at": "…ISO…",
      "updated_at": "…ISO…"
    }

Supported triggers
------------------
    any_ema_cross          — fire on any EMA 9/21 cross
    after_s3_touch         — fire on EMA cross AFTER S3 touch
    after_r3_touch         — fire on EMA cross AFTER R3 touch
    after_r3_or_s3_touch   — fire on EMA cross AFTER whichever of
                              R3 or S3 touches FIRST today (default)

Future fields (e.g. sound, webhook URL) must be added as keys in the
SAME document — no new docs are ever created for this feature.

Never raises; always returns a normalised dict.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pymongo import MongoClient

from core import config
from core.logger import get_logger

logger = get_logger(__file__)

# --------------------------------------------------------------------------- #
# Constants                                                                    #
# --------------------------------------------------------------------------- #
DB_NAME = "UPSTOX_APP_V2"
COLLECTION_NAME = "app_tokens"
SETTINGS_DOC_ID = "prochart_settings"

DEFAULT_INSTRUMENT_KEY = "NSE_INDEX|Nifty 50"

VALID_TRIGGERS = {
    "any_ema_cross",
    "after_s3_touch",
    "after_r3_touch",
    "after_r3_or_s3_touch",   # NEW — default
}

# Default trigger when nothing is saved and no URL override is present.
DEFAULT_TRIGGER = "after_r3_or_s3_touch"

# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def _coerce_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default

def _normalise(settings: dict[str, Any] | None) -> dict[str, Any]:
    """Return a fully-populated settings dict with defaults applied."""
    src = settings or {}

    instrument_key = str(src.get("chart_instrument_key") or "").strip()
    if not instrument_key:
        instrument_key = DEFAULT_INSTRUMENT_KEY

    trigger = str((src.get("alert_config") or {}).get("trigger") or "").strip()
    if trigger not in VALID_TRIGGERS:
        trigger = DEFAULT_TRIGGER

    return {
        "chart_instrument_key": instrument_key,
        "alerts_enabled": _coerce_bool(src.get("alerts_enabled"), False),
        "alert_config": {"trigger": trigger},
    }

# --------------------------------------------------------------------------- #
# Service                                                                      #
# --------------------------------------------------------------------------- #
class ProChartSettingsService:
    def __init__(self) -> None:
        self._client: MongoClient | None = None

    def _mongo(self) -> MongoClient:
        if self._client is None:
            if not config.MONGO_URI:
                raise RuntimeError("MONGO_URL must be set for ProChart settings")
            self._client = MongoClient(config.MONGO_URI)
        return self._client

    def _collection(self):
        return self._mongo()[DB_NAME][COLLECTION_NAME]

    def get_settings(self) -> dict[str, Any]:
        """Return the current ProChart settings (with defaults)."""
        try:
            doc = self._collection().find_one({"_id": SETTINGS_DOC_ID}) or {}
        except Exception:
            logger.exception("[prochart] failed reading settings — using defaults")
            doc = {}

        normalised = _normalise(doc)
        normalised["created_at"] = doc.get("created_at")
        normalised["updated_at"] = doc.get("updated_at")
        return normalised

    def save_settings(self, incoming: dict[str, Any]) -> dict[str, Any]:
        """Upsert the single settings document with a merged payload."""
        try:
            existing = self._collection().find_one({"_id": SETTINGS_DOC_ID}) or {}
        except Exception:
            logger.exception("[prochart] failed reading settings before save")
            existing = {}

        merged_src = dict(existing)
        if "chart_instrument_key" in incoming:
            merged_src["chart_instrument_key"] = incoming["chart_instrument_key"]
        if "alerts_enabled" in incoming:
            merged_src["alerts_enabled"] = incoming["alerts_enabled"]
        if "alert_config" in incoming and isinstance(incoming["alert_config"], dict):
            merged_alert = dict(merged_src.get("alert_config") or {})
            merged_alert.update(
                {k: v for k, v in incoming["alert_config"].items() if k in {"trigger"}}
            )
            merged_src["alert_config"] = merged_alert

        normalised = _normalise(merged_src)

        payload = {
            "_id": SETTINGS_DOC_ID,
            **normalised,
            "created_at": existing.get("created_at") or _now_iso(),
            "updated_at": _now_iso(),
        }

        try:
            self._collection().replace_one(
                {"_id": SETTINGS_DOC_ID}, payload, upsert=True
            )
            logger.info(
                "[prochart] settings saved — instrument=%s alerts=%s trigger=%s",
                normalised["chart_instrument_key"],
                normalised["alerts_enabled"],
                normalised["alert_config"]["trigger"],
            )
        except Exception:
            logger.exception("[prochart] failed saving settings")
            return {**normalised, "persisted": False}

        return {
            **normalised,
            "created_at": payload["created_at"],
            "updated_at": payload["updated_at"],
            "persisted": True,
        }

prochart_settings_service = ProChartSettingsService()