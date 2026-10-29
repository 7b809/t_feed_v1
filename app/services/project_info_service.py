# app/services/project_info_service.py
#
# Persists project-wide info to MongoDB for downstream consumers.
#
# Currently the only writer is Job 3 (pivot levels), which upserts a
# single fixed-_id document (``LEVELS_PROJECT_INFO_DOCUMENT_ID``, default
# "pivot_levels") in the ``LEVELS_PROJECT_INFO_COLLECTION`` collection
# (default "project_info").
#
# Design:
#   * ONE document per logical dataset (not one per day). Each daily run
#     overwrites the same _id, so the collection never grows unbounded.
#   * The _id is a plain string (not ObjectId) so external consumers can
#     fetch it with a stable lookup key.
#   * All writes use ``update_one(..., upsert=True)`` — the very first
#     run creates the doc; every subsequent run replaces its content
#     while preserving _id.
#   * All I/O errors bubble up as ProjectInfoError; callers decide
#     whether to swallow (Job 3 does — persistence is best-effort).

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from pymongo.errors import PyMongoError

from app.config import Settings

logger = logging.getLogger(__name__)


class ProjectInfoError(RuntimeError):
    """Raised when a project_info read or write fails."""


class ProjectInfoService:
    def __init__(self, config: Settings, mongo_client: Any) -> None:
        self.config = config
        self.client = mongo_client
        self.collection = self.client[
            config.mongo_database
        ][config.levels_project_info_collection]

    # ------------------------------------------------------------------ #
    # Write: pivot levels
    # ------------------------------------------------------------------ #
    def upsert_levels(self, result: dict[str, Any]) -> dict[str, Any]:
        """
        Upsert today's pivot levels into the fixed ``_id`` document.

        ``result`` is the dict returned by ``LevelsService.compute_all``:
            {
              "trigger": "scheduled",
              "computed_at": "<iso>",
              "results": {
                 "<instrument_key>": {"status": "ok", "canonical": "...",
                                       "levels": {...}, "first_candle": {...},
                                       ...},
                 ...,
              }
            }

        Only ``status == "ok"`` entries are persisted.
        """
        if not isinstance(result, dict):
            raise ProjectInfoError("result must be a dict")

        results_by_key = result.get("results") or {}
        indexes: dict[str, dict[str, Any]] = {}

        for key, item in results_by_key.items():
            if not isinstance(item, dict):
                continue
            if item.get("status") != "ok":
                continue
            indexes[key] = {
                "canonical": item.get("canonical"),
                "folder": item.get("folder"),
                "underlying": item.get("underlying"),
                "trading_symbol": item.get("trading_symbol"),
                "priority": item.get("priority"),
                "first_candle": item.get("first_candle"),
                "levels": item.get("levels"),
            }

        computed_at = (
            result.get("computed_at")
            or datetime.now(timezone.utc).isoformat()
        )
        trade_date_ist = (
            computed_at[:10] if isinstance(computed_at, str) and len(computed_at) >= 10
            else None
        )

        doc_payload = {
            "job": "pivot_levels",
            "trade_date_ist": trade_date_ist,
            "computed_at": computed_at,
            "trigger": result.get("trigger"),
            "indexes": indexes,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

        try:
            self.collection.update_one(
                {"_id": self.config.levels_project_info_document_id},
                {"$set": doc_payload},
                upsert=True,
            )
        except PyMongoError as exc:
            logger.error("MongoDB upsert failed for levels: %s", exc)
            raise ProjectInfoError(f"MongoDB upsert failed: {exc}") from exc

        logger.info(
            "Levels persisted to MongoDB db=%s collection=%s _id=%s indexes=%d",
            self.config.mongo_database,
            self.config.levels_project_info_collection,
            self.config.levels_project_info_document_id,
            len(indexes),
        )
        return {
            "collection": self.config.levels_project_info_collection,
            "document_id": self.config.levels_project_info_document_id,
            "trade_date_ist": trade_date_ist,
            "indexes": len(indexes),
        }

    # ------------------------------------------------------------------ #
    # Read: pivot levels (used by GET /api/v1/levels/db for verification)
    # ------------------------------------------------------------------ #
    def get_levels(self) -> dict[str, Any] | None:
        try:
            doc = self.collection.find_one(
                {"_id": self.config.levels_project_info_document_id}
            )
        except PyMongoError as exc:
            logger.error("MongoDB find failed for levels: %s", exc)
            raise ProjectInfoError(f"MongoDB find failed: {exc}") from exc

        if doc is None:
            return None
        if "_id" in doc and not isinstance(doc["_id"], str):
            doc["_id"] = str(doc["_id"])
        return doc