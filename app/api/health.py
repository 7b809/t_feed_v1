from fastapi import APIRouter, Request
from app.models.job_status import job_state

router = APIRouter()


@router.get("/health")
def health(request: Request) -> dict:
    mongo_ok = False
    mongo_error = None
    mongo = getattr(request.app.state, "mongo_service", None)
    if mongo is not None:
        try:
            mongo_ok = mongo.ping()
        except Exception:
            mongo_error = "MongoDB unavailable"
    return {
        "status": "ok" if mongo_ok else "degraded",
        "app": request.app.title,
        "dependencies": {"mongodb": {"ok": mongo_ok, "error": mongo_error}},
        "job": job_state.snapshot(),
    }
