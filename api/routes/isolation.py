# api/routes/isolation.py
"""Isolation API: current, history, manual scan trigger, backfill trigger,
and a synchronous client-notification test endpoint.

Routers
-------
``router``       prefix ``/api/v1/isolation``  (production isolation API)
``test_router``  prefix ``/api``               (``POST /api/isolation-test``)
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from core.config import ISOLATION_DOC_ID, PROJECT_INFO_COLLECTION
from core.logger import get_logger
from core.mongo_client import get_mongo_db
from core.token_service import fetch_access_token
from jobs.daily_refresh import get_feed_service
from jobs.isolation_scan import (
    get_isolation_service, run_startup_backfill,
)
from services.isolation_notification_service import (
    send_isolation_test_notification,
)

logger = get_logger(__file__)

router = APIRouter(prefix="/api/v1/isolation", tags=["isolation"])
test_router = APIRouter(prefix="/api", tags=["isolation-test"])


# --------------------------------------------------------------------------- #
# Production isolation API                                                    #
# --------------------------------------------------------------------------- #
@router.get("/current")
def get_current_isolation():
    try:
        db = get_mongo_db()
        doc = db[PROJECT_INFO_COLLECTION].find_one({"_id": ISOLATION_DOC_ID})
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    if not doc:
        raise HTTPException(status_code=404, detail="No isolation recorded yet")
    doc["_id"] = str(doc["_id"])
    return doc


@router.get("/history")
def get_isolation_history(limit: int = Query(20, ge=1, le=200)):
    from repositories.isolation_repository import IsolationRepository
    try:
        repo = IsolationRepository()
        items = repo.list_history(limit=limit)
        for item in items:
            item["_id"] = str(item["_id"])
        return {"count": len(items), "items": items}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


class TriggerIsolationBody(BaseModel):
    force: bool = False


@router.post("/trigger")
def trigger_isolation(body: TriggerIsolationBody = TriggerIsolationBody()):
    feed = get_feed_service()
    if not feed:
        raise HTTPException(status_code=503, detail="Feed not connected")
    try:
        token = fetch_access_token()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Token fetch failed: {exc}")
    try:
        service = get_isolation_service(token)
        record = service.scan_once(feed)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    if not record:
        return {"status": "no_isolation",
                "message": "No new touch or isolation already exists."}
    record.pop("_id", None)
    return {"status": "ok", "record": record}


@router.post("/backfill")
def trigger_backfill():
    """Manually run the startup backfill (reconstruct missed isolation).

    Idempotent — no-op if today's isolation already exists.
    """
    try:
        run_startup_backfill()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    db = get_mongo_db()
    doc = db[PROJECT_INFO_COLLECTION].find_one({"_id": ISOLATION_DOC_ID})
    if not doc:
        return {"status": "no_isolation",
                "message": "Backfill found no touch, or nothing to reconstruct."}
    doc["_id"] = str(doc["_id"])
    return {
        "status": "ok",
        "message": "Backfill executed",
        "backfilled": bool(doc.get("isolation_backfilled")),
        "record": doc,
    }


# --------------------------------------------------------------------------- #
# POST /api/isolation-test                                                    #
# --------------------------------------------------------------------------- #
class IsolationTestRequest(BaseModel):
    urls: list[str] | None = None
    key: str | None = None
    include_payload: bool = False


@test_router.post("/isolation-test")
def isolation_test(body: IsolationTestRequest = IsolationTestRequest()):
    """Synchronously POST a static dummy isolation document to client URLs.

    Validates end-to-end:
      * network reachability from this service to each receiver
      * shared-secret authentication (``X-Isolation-Key`` by default)
      * the exact JSON shape that a real isolation produces

    The payload is a complete isolation-shaped document built from a
    static fake instrument, tagged ``is_test=true`` and
    ``source="isolation_test_endpoint"`` so receivers can never confuse
    it with a real isolation event. It is **not persisted to Mongo**.

    Request body (all optional)::

        {
          "urls": ["https://client-a.example/api/isolation"],
          "key":  "override-shared-secret",
          "include_payload": true
        }

    - ``urls`` overrides ``ISOLATION_NOTIFY_CLIENT_URLS`` for this call.
    - ``key`` overrides ``ISOLATION_NOTIFY_CLIENT_KEY`` for this call.
      Pass ``""`` to force-send without a key header.
    - ``include_payload`` echoes the full test payload in the response.

    Response::

        {
          "sent": true,
          "key_header": "X-Isolation-Key",
          "key_configured": true,
          "payload_summary": {
            "trading_symbol": "NIFTY 25000 CE 09 NOV 26",
            "standard_execution_mode": "STANDARD",
            "is_test": true,
            "is_dummy": true,
            "source": "isolation_test_endpoint"
          },
          "results": [
            {
              "url": "https://client-a.example/api/isolation",
              "ok": true,
              "http_status": 200,
              "response_body_preview": "{\"ok\":true}",
              "error": null
            }
          ]
        }
    """
    try:
        return send_isolation_test_notification(
            urls=body.urls,
            key=body.key,
            include_payload=body.include_payload,
        )
    except Exception as exc:
        logger.exception("isolation-test failed")
        raise HTTPException(status_code=500, detail=str(exc))