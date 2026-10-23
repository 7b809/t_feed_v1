import hmac
from fastapi import APIRouter, Header, HTTPException, Request, status
from app.config import settings
from app.models.job_status import job_state
from app.services.mega_archive_service import RefreshConflict, RefreshError

router = APIRouter(prefix="/api/v1/jobs/mega-archive", tags=["mega-archive"])


def _authorize(x_api_key: str | None) -> None:
    if settings.api_key:
        if not x_api_key or not hmac.compare_digest(x_api_key, settings.api_key):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or missing API key")
    else:
        # Explicitly allow local/dev use, but deployment must restrict network access.
        return


@router.get("/status")
def get_status():
    return job_state.snapshot()


@router.post("/hard-refresh")
def hard_refresh(request: Request, x_api_key: str | None = Header(default=None, alias="X-API-Key")):
    _authorize(x_api_key)
    service = getattr(request.app.state, "mega_archive_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="Job service is unavailable")
    try:
        return service.refresh("manual")
    except RefreshConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RefreshError as exc:
        raise HTTPException(status_code=503, detail={"message": "Refresh failed", "error": str(exc)}) from exc
