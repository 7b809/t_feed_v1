from __future__ import annotations

import json
import logging
from typing import Any
from bson import ObjectId
from pymongo import MongoClient
from pymongo.errors import PyMongoError
from app.config import Settings

logger = logging.getLogger(__name__)


class SourceDocumentError(RuntimeError):
    pass


class MongoDocumentService:
    def __init__(self, config: Settings):
        if not config.mongo_url:
            raise ValueError("MONGO_URL is required")
        self.config = config
        self.client = MongoClient(
            config.mongo_url,
            serverSelectionTimeoutMS=config.mongo_timeout_ms,
            connectTimeoutMS=config.mongo_timeout_ms,
            socketTimeoutMS=config.mongo_timeout_ms,
        )
        self.collection = self.client[config.mongo_database][config.mongo_collection]

    def ping(self) -> bool:
        self.client.admin.command("ping")
        return True

    def get_source_document(self) -> dict[str, Any]:
        try:
            if self.config.mongo_document_id:
                raw = self.config.mongo_document_id
                kind = self.config.mongo_document_id_type
                if kind == "objectid":
                    try:
                        selector_id = ObjectId(raw)
                    except Exception as exc:
                        raise SourceDocumentError("MONGO_DOCUMENT_ID is not a valid ObjectId") from exc
                elif kind == "int":
                    try:
                        selector_id = int(raw)
                    except ValueError as exc:
                        raise SourceDocumentError("MONGO_DOCUMENT_ID is not a valid integer") from exc
                else:
                    selector_id = raw
                query = {"_id": selector_id}
            else:
                query = json.loads(self.config.mongo_query_json)
                if not isinstance(query, dict) or not query:
                    raise SourceDocumentError("MongoDB selector must be a non-empty JSON object")
            matches = list(self.collection.find(query).limit(2))
        except SourceDocumentError:
            raise
        except (PyMongoError, ValueError) as exc:
            logger.exception("MongoDB source lookup failed")
            raise SourceDocumentError("MongoDB source lookup failed") from exc

        if not matches:
            raise SourceDocumentError("No MongoDB document matched the configured selector")
        if len(matches) > 1:
            raise SourceDocumentError("MongoDB selector matched multiple documents; configure a unique selector")
        return matches[0]

    def close(self) -> None:
        self.client.close()
