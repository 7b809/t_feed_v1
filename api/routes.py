import secrets
from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from core.config import Settings, get_settings
from models.schemas import JobStatus, UpdateAccepted

router = APIRouter(prefix="/api/v1", tags=["project-updates"])

def require_api_key(x_api_key: str = Header(...), settings: Settings = Depends(get_settings)):
    if not secrets.compare_digest(x_api_key, settings.api_key):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")

@router.get("/projects", dependencies=[Depends(require_api_key)])
async def list_projects(settings: Settings = Depends(get_settings)):
    return {"projects": sorted(settings.projects)}

@router.post("/projects/{project}/update", response_model=UpdateAccepted, status_code=202, dependencies=[Depends(require_api_key)])
async def update_project(project: str, request: Request):
    try:
        job = request.app.state.update_manager.submit(project, requested_by="api")
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown project")
    return UpdateAccepted(job_id=job.job_id, project=job.project)

@router.get("/jobs/{job_id}", response_model=JobStatus, dependencies=[Depends(require_api_key)])
async def get_job(job_id: str, request: Request):
    job = request.app.state.update_manager.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Unknown job")
    return job
