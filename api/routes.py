import secrets

from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Request,
    status,
)

from core.config import Settings, get_settings
from core.logger import get_logger
from models.schemas import JobStatus, UpdateAccepted


# ============================================================
# Logger
# ============================================================

logger = get_logger("api")


# ============================================================
# Router
# ============================================================

router = APIRouter(
    prefix="/api/v1",
    tags=["project-updates"],
)


# ============================================================
# API Key Authentication
# ============================================================

def require_api_key(
    x_api_key: str = Header(...),
    settings: Settings = Depends(get_settings),
):
    """
    Validate the API key supplied through X-API-Key header.

    The actual API key is NEVER written to the logs.
    """

    if not secrets.compare_digest(
        x_api_key,
        settings.api_key,
    ):
        logger.warning(
            "API authentication failed | invalid API key"
        )

        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )

    logger.info(
        "API authentication successful"
    )


# ============================================================
# List Projects
# ============================================================

@router.get(
    "/projects",
    dependencies=[Depends(require_api_key)],
)
async def list_projects(
    settings: Settings = Depends(get_settings),
):
    """
    Return all configured projects.
    """

    projects = sorted(settings.projects)

    logger.info(
        "List projects | count=%d | projects=%s",
        len(projects),
        projects,
    )

    return {
        "projects": projects
    }


# ============================================================
# Queue Project Update
# ============================================================

@router.post(
    "/projects/{project}/update",
    response_model=UpdateAccepted,
    status_code=202,
    dependencies=[Depends(require_api_key)],
)
async def update_project(
    project: str,
    request: Request,
):
    """
    Queue an update for the selected project.
    """

    logger.info(
        "Update request received | project=%s | requested_by=api",
        project,
    )

    try:
        job = request.app.state.update_manager.submit(
            project,
            requested_by="api",
        )

    except KeyError:
        logger.warning(
            "Update request rejected | unknown project=%s",
            project,
        )

        raise HTTPException(
            status_code=404,
            detail="Unknown project",
        )

    except Exception:
        logger.exception(
            "Update request failed unexpectedly | project=%s",
            project,
        )

        raise HTTPException(
            status_code=500,
            detail="Failed to queue project update",
        )

    logger.info(
        "Update request accepted | project=%s | job_id=%s",
        job.project,
        job.job_id,
    )

    return UpdateAccepted(
        job_id=job.job_id,
        project=job.project,
    )


# ============================================================
# Get Job Status
# ============================================================

@router.get(
    "/jobs/{job_id}",
    response_model=JobStatus,
    dependencies=[Depends(require_api_key)],
)
async def get_job(
    job_id: str,
    request: Request,
):
    """
    Return the current status of a job.
    """

    logger.info(
        "Job status request | job_id=%s",
        job_id,
    )

    job = request.app.state.update_manager.get(
        job_id
    )

    if not job:
        logger.warning(
            "Job status request failed | unknown job_id=%s",
            job_id,
        )

        raise HTTPException(
            status_code=404,
            detail="Unknown job",
        )

    logger.info(
        "Job status returned | job_id=%s | project=%s | status=%s",
        job.job_id,
        job.project,
        job.status,
    )

    return job