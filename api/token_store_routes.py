from fastapi import APIRouter

from token_tasks.token_monitor import get_token_status_snapshot

router = APIRouter(prefix="/token", tags=["Token management"])


@router.get("/status")
def token_status():
    """Return non-secret token lifecycle metadata."""
    result = get_token_status_snapshot()
    document = dict(result.get("token_document") or {})
    visible_fields = {
        "token_available",
        "created_at",
        "updated_at",
        "source",
        "last_validation_status",
        "last_validation_status_text",
        "last_validated_at",
    }
    result["token_document"] = {
        key: value for key, value in document.items() if key in visible_fields
    }
    return result
