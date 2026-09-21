from datetime import datetime
from threading import Lock, Thread
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, status

from core import config
from core.logger import get_logger
from utils.json_store import read_json

logger = get_logger(__file__)

router = APIRouter(
    prefix="/api",
    tags=["Refresh"],
)


class ApiController:
    def __init__(self) -> None:
        self.scheduler = None
        self.jobs: dict[str, dict] = {}
        self.lock = Lock()

    def set_scheduler(self, scheduler) -> None:
        self.scheduler = scheduler

    def start_hard_refresh(self) -> dict:
        if self.scheduler is None:
            logger.error("Hard refresh failed because scheduler is not configured")
            raise RuntimeError("Scheduler is not configured")

        job_id = str(uuid4())

        with self.lock:
            if any(job.get("status") == "running" for job in self.jobs.values()):
                logger.warning(
                    "Hard refresh request rejected because another job is running"
                )
                raise RuntimeError("A hard refresh is already running")

            job = {
                "job_id": job_id,
                "status": "running",
                "started_at": datetime.now(config.MARKET_TIMEZONE).isoformat(),
            }

            self.jobs[job_id] = job

        logger.info("Starting API hard refresh job=%s", job_id)

        Thread(
            target=self._run,
            args=(job_id,),
            name=f"hard-refresh-{job_id}",
            daemon=True,
        ).start()

        return job.copy()

    def _run(self, job_id: str) -> None:
        try:
            logger.info(
                "API hard refresh execution started job=%s",
                job_id,
            )

            result = self.scheduler.refresh_day(
                datetime.now(config.MARKET_TIMEZONE),
                force=True,
                trigger="api_hard_refresh",
            )

            update = {
                "status": "success",
                "result": result,
            }

            logger.info(
                "API hard refresh completed successfully job=%s",
                job_id,
            )

        except Exception as ex:
            logger.exception(
                "API hard refresh failed job=%s",
                job_id,
            )

            update = {
                "status": "failed",
                "error": str(ex),
            }

        with self.lock:
            job = self.jobs.get(job_id)

            if job is None:
                logger.error(
                    "Hard refresh job disappeared before completion job=%s",
                    job_id,
                )
                return

            job.update(update)
            job["completed_at"] = datetime.now(config.MARKET_TIMEZONE).isoformat()

    def get_job(self, job_id: str) -> dict | None:
        with self.lock:
            job = self.jobs.get(job_id)
            return job.copy() if job else None


controller = ApiController()


def configure_refresh_routes(scheduler) -> None:
    controller.set_scheduler(scheduler)
    logger.info("Refresh API routes configured with scheduler")


def verify_api_key(
    x_api_key: str = Header(default=""),
) -> None:
    if not config.API_AUTH_ENABLED:
        return

    if x_api_key != config.ADMIN_API_KEY:
        logger.warning("API request rejected due to invalid API key")

        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )


@router.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "time": datetime.now(config.MARKET_TIMEZONE).isoformat(),
    }


@router.get(
    "/state",
    dependencies=[Depends(verify_api_key)],
)
def state_endpoint() -> dict:
    logger.debug("Service state requested")

    return read_json(
        config.RUNTIME_ROOT / "service_state.json",
        {},
    )


@router.post(
    "/hard-refresh",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(verify_api_key)],
)
def hard_refresh() -> dict:
    logger.info("Hard refresh API request received")

    try:
        return controller.start_hard_refresh()

    except RuntimeError as ex:
        logger.warning(
            "Hard refresh API request failed error=%s",
            ex,
        )

        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(ex),
        ) from ex


@router.get(
    "/hard-refresh/{job_id}",
    dependencies=[Depends(verify_api_key)],
)
def hard_refresh_status(job_id: str) -> dict:
    logger.debug(
        "Hard refresh status requested job=%s",
        job_id,
    )

    job = controller.get_job(job_id)

    if not job:
        logger.warning(
            "Hard refresh job not found job=%s",
            job_id,
        )

        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Job not found",
        )

    return job
