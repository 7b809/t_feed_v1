# api/refresh_routes.py

from __future__ import annotations

import asyncio
import inspect
from datetime import datetime
from threading import Lock, Thread
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Header, HTTPException, status

from core import config
from core.logger import get_logger
from utils.json_store import read_json

logger = get_logger(__file__)


# ---------------------------------------------------------------------------
# Debug Configuration
# ---------------------------------------------------------------------------

DEBUG_MODE = False


def _debug(message: str, *args: Any) -> None:
    """
    Print debug information only when DEBUG_MODE is enabled.
    """
    if not DEBUG_MODE:
        return

    if args:
        message = message.format(*args)

    print(f"[REFRESH API DEBUG] {message}")


# ---------------------------------------------------------------------------
# Router configuration
# ---------------------------------------------------------------------------

router = APIRouter(
    prefix="/api",
    tags=["Refresh"],
)


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def _get_timezone() -> ZoneInfo:
    """
    Resolve the configured market timezone.

    Supports:
        config.MARKET_TIMEZONE = "Asia/Kolkata"
        config.MARKET_TIMEZONE = ZoneInfo("Asia/Kolkata")
    """

    configured_timezone = getattr(
        config,
        "MARKET_TIMEZONE",
        "Asia/Kolkata",
    )

    if isinstance(configured_timezone, ZoneInfo):
        return configured_timezone

    if hasattr(configured_timezone, "utcoffset"):
        return configured_timezone

    try:
        return ZoneInfo(str(configured_timezone))

    except Exception:
        logger.warning(
            "Invalid MARKET_TIMEZONE=%s. " "Falling back to Asia/Kolkata",
            configured_timezone,
        )

        return ZoneInfo("Asia/Kolkata")


def _now() -> datetime:
    """
    Return the current time in the configured market timezone.
    """

    return datetime.now(_get_timezone())


def _iso_now() -> str:
    """
    Return the current market time in ISO format.
    """

    return _now().isoformat()


def _json_safe(value: Any) -> Any:
    """
    Convert values into JSON-compatible data.
    """

    if value is None:
        return None

    if isinstance(value, (str, int, float, bool)):
        return value

    if isinstance(value, datetime):
        return value.isoformat()

    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}

    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]

    if hasattr(value, "model_dump"):
        return _json_safe(value.model_dump())

    if hasattr(value, "dict"):
        return _json_safe(value.dict())

    if hasattr(value, "to_dict"):
        return _json_safe(value.to_dict())

    return str(value)


def _get_runtime_state_path():
    """
    Resolve the runtime state file path.
    """

    runtime_root = getattr(
        config,
        "RUNTIME_ROOT",
        None,
    )

    if runtime_root is None:
        raise RuntimeError("RUNTIME_ROOT is not configured")

    state_path = runtime_root / "service_state.json"

    _debug("Resolved runtime state path: {}", state_path)

    return state_path


def _run_scheduler_refresh(
    scheduler: Any,
    refresh_time: datetime,
) -> Any:
    """
    Execute scheduler.refresh_day().

    Supports both synchronous and asynchronous
    refresh_day implementations.

    The refresh is normally executed in a dedicated
    background thread.
    """

    refresh_method = getattr(
        scheduler,
        "refresh_day",
        None,
    )

    if not callable(refresh_method):
        raise RuntimeError("Scheduler does not implement refresh_day()")

    _debug(
        "Invoking scheduler.refresh_day at {}",
        refresh_time.isoformat(),
    )

    result = refresh_method(
        refresh_time,
        force=True,
        trigger="api_hard_refresh",
    )

    if inspect.isawaitable(result):
        _debug("refresh_day returned an awaitable; running to completion")

        try:
            asyncio.get_running_loop()

        except RuntimeError:
            return asyncio.run(result)

        # A running event loop should not normally exist
        # inside the dedicated refresh thread.
        loop = asyncio.new_event_loop()

        try:
            asyncio.set_event_loop(loop)

            return loop.run_until_complete(result)

        finally:
            loop.close()
            asyncio.set_event_loop(None)

    return result


# ---------------------------------------------------------------------------
# API Controller
# ---------------------------------------------------------------------------


class ApiController:
    """
    Controls asynchronous hard-refresh jobs.

    Only one hard-refresh job is allowed to run at a time.
    """

    def __init__(self) -> None:
        self.scheduler: Any = None

        self.jobs: dict[
            str,
            dict[str, Any],
        ] = {}

        self.lock = Lock()

    def set_scheduler(
        self,
        scheduler: Any,
    ) -> None:
        """
        Configure the scheduler used by hard refresh.
        """

        if scheduler is None:
            raise ValueError("Scheduler cannot be None")

        self.scheduler = scheduler

        _debug(
            "Scheduler configured: {}",
            type(scheduler).__name__,
        )

        logger.info("Refresh controller scheduler configured")

    def is_refresh_running(self) -> bool:
        """
        Check whether a refresh job is currently running.
        """

        with self.lock:
            return any(job.get("status") == "running" for job in self.jobs.values())

    def _running_job_id(self) -> str | None:
        """
        Return the currently running job ID, if any.
        """

        with self.lock:
            for job_id, job in self.jobs.items():
                if job.get("status") == "running":
                    return job_id

        return None

    def start_hard_refresh(self) -> dict[str, Any]:
        """
        Create and start a hard-refresh background job.
        """

        if self.scheduler is None:
            logger.error("Hard refresh rejected: " "scheduler is not configured")

            raise RuntimeError("Scheduler is not configured")

        job_id = str(uuid4())

        with self.lock:
            running_job_id = self._running_job_id()

            if running_job_id is not None:
                logger.warning(
                    "Hard refresh rejected because " "another job is running job=%s",
                    running_job_id,
                )

                raise RuntimeError("A hard refresh is already running")

            job = {
                "job_id": job_id,
                "status": "running",
                "trigger": "api_hard_refresh",
                "started_at": _iso_now(),
                "completed_at": None,
                "result": None,
                "error": None,
            }

            self.jobs[job_id] = job

        _debug(
            "Starting hard refresh job_id={} total_jobs={}",
            job_id,
            len(self.jobs),
        )

        logger.info(
            "Starting API hard refresh job=%s",
            job_id,
        )

        refresh_thread = Thread(
            target=self._run,
            args=(job_id,),
            name=f"hard-refresh-{job_id}",
            daemon=True,
        )

        refresh_thread.start()

        return job.copy()

    def _run(
        self,
        job_id: str,
    ) -> None:
        """
        Execute the refresh job in the background.
        """

        _debug("Background refresh thread started job_id={}", job_id)

        logger.info(
            "API hard refresh execution started job=%s",
            job_id,
        )

        try:
            refresh_time = _now()

            result = _run_scheduler_refresh(
                self.scheduler,
                refresh_time,
            )

            update = {
                "status": "success",
                "result": _json_safe(result),
                "error": None,
            }

            _debug(
                "Refresh job completed job_id={} result_type={}",
                job_id,
                type(result).__name__,
            )

            logger.info(
                "API hard refresh completed successfully " "job=%s",
                job_id,
            )

        except Exception as ex:
            logger.exception(
                "API hard refresh failed job=%s",
                job_id,
            )

            _debug(
                "Refresh job failed job_id={} error={}",
                job_id,
                ex,
            )

            update = {
                "status": "failed",
                "result": None,
                "error": str(ex),
            }

        with self.lock:
            job = self.jobs.get(job_id)

            if job is None:
                logger.error(
                    "Hard refresh job disappeared " "before completion job=%s",
                    job_id,
                )
                return

            job.update(update)

            job["completed_at"] = _iso_now()

    def get_job(
        self,
        job_id: str,
    ) -> dict[str, Any] | None:
        """
        Return a copy of one job.
        """

        with self.lock:
            job = self.jobs.get(job_id)

            if job is None:
                return None

            return _json_safe(job.copy())

    def get_jobs(
        self,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """
        Return the most recent refresh jobs.
        """

        safe_limit = max(
            1,
            min(limit, 100),
        )

        with self.lock:
            jobs = list(self.jobs.values())

        jobs.reverse()

        return [_json_safe(job.copy()) for job in jobs[:safe_limit]]

    def cleanup_completed_jobs(
        self,
        keep_latest: int = 50,
    ) -> None:
        """
        Keep only the latest completed jobs.

        Running jobs are never removed.
        """

        keep_latest = max(
            1,
            keep_latest,
        )

        with self.lock:
            completed_jobs = [
                (job_id, job)
                for job_id, job in self.jobs.items()
                if job.get("status") != "running"
            ]

            if len(completed_jobs) <= keep_latest:
                return

            completed_jobs.sort(
                key=lambda item: item[1].get(
                    "completed_at",
                    "",
                )
            )

            jobs_to_remove = completed_jobs[:-keep_latest]

            for job_id, _ in jobs_to_remove:
                self.jobs.pop(
                    job_id,
                    None,
                )

        _debug(
            "Cleaned up {} completed refresh jobs",
            len(jobs_to_remove),
        )

        logger.debug(
            "Completed refresh jobs cleaned up count=%s",
            len(jobs_to_remove),
        )


# ---------------------------------------------------------------------------
# Controller configuration
# ---------------------------------------------------------------------------


controller = ApiController()


def configure_refresh_routes(
    scheduler: Any,
) -> None:
    """
    Configure the scheduler used by the refresh API.

    Call this during application startup.
    """

    controller.set_scheduler(scheduler)

    _debug("Refresh routes configured")

    logger.info("Refresh API routes configured with scheduler")


def get_refresh_controller() -> ApiController:
    """
    Return the global refresh controller.
    """

    return controller


# ---------------------------------------------------------------------------
# API authentication
# ---------------------------------------------------------------------------


def verify_api_key(
    x_api_key: str = Header(
        default="",
        alias="X-API-Key",
    ),
) -> None:
    """
    Validate the API key when authentication is enabled.
    """

    api_auth_enabled = bool(
        getattr(
            config,
            "API_AUTH_ENABLED",
            False,
        )
    )

    _debug("API auth enabled: {}", api_auth_enabled)

    if not api_auth_enabled:
        return

    configured_api_key = str(
        getattr(
            config,
            "ADMIN_API_KEY",
            "",
        )
    ).strip()

    if not configured_api_key:
        logger.error(
            "API authentication is enabled, " "but ADMIN_API_KEY is not configured"
        )

        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="API authentication is not configured",
        )

    if not x_api_key or x_api_key != configured_api_key:
        logger.warning("API request rejected due to invalid API key")

        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )


# ---------------------------------------------------------------------------
# Health endpoint
# ---------------------------------------------------------------------------


@router.get(
    "/health",
    summary="Service health check",
)
def health() -> dict[str, Any]:
    """
    Return service health and refresh controller status.
    """

    scheduler_configured = controller.scheduler is not None

    running_job_id = controller._running_job_id()

    _debug(
        "Health check: scheduler_configured={} running_job_id={}",
        scheduler_configured,
        running_job_id,
    )

    return {
        "status": "ok",
        "time": _iso_now(),
        "scheduler_configured": scheduler_configured,
        "refresh_running": running_job_id is not None,
        "running_job_id": running_job_id,
    }


# ---------------------------------------------------------------------------
# Runtime state endpoint
# ---------------------------------------------------------------------------


@router.get(
    "/state",
    dependencies=[Depends(verify_api_key)],
    summary="Get service runtime state",
)
def state_endpoint() -> dict[str, Any]:
    """
    Return the persisted service state.
    """

    _debug("Service state requested")

    logger.debug("Service state requested")

    try:
        state_path = _get_runtime_state_path()

        state = read_json(
            state_path,
            {},
        )

        return _json_safe(state)

    except Exception as ex:
        logger.exception("Failed to load service state")

        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Unable to read service state: {ex}",
        ) from ex


# ---------------------------------------------------------------------------
# Refresh status endpoint
# ---------------------------------------------------------------------------


@router.get(
    "/hard-refresh/{job_id}",
    dependencies=[Depends(verify_api_key)],
    summary="Get hard-refresh job status",
)
def hard_refresh_status(
    job_id: str,
) -> dict[str, Any]:
    """
    Return the status of a specific hard-refresh job.
    """

    job_id = job_id.strip()

    if not job_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Job ID is required",
        )

    _debug("Hard refresh status requested job_id={}", job_id)

    logger.debug(
        "Hard refresh status requested job=%s",
        job_id,
    )

    job = controller.get_job(job_id)

    if job is None:
        logger.warning(
            "Hard refresh job not found job=%s",
            job_id,
        )

        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Job not found",
        )

    return job


# ---------------------------------------------------------------------------
# Refresh job list endpoint
# ---------------------------------------------------------------------------


@router.get(
    "/hard-refresh",
    dependencies=[Depends(verify_api_key)],
    summary="List hard-refresh jobs",
)
def hard_refresh_jobs(
    limit: int = 20,
) -> dict[str, Any]:
    """
    Return recent hard-refresh jobs.
    """

    _debug("Hard refresh job list requested limit={}", limit)

    jobs = controller.get_jobs(limit=limit)

    return {
        "status": "success",
        "count": len(jobs),
        "jobs": jobs,
    }


# ---------------------------------------------------------------------------
# Start hard refresh endpoint
# ---------------------------------------------------------------------------


@router.post(
    "/hard-refresh",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(verify_api_key)],
    summary="Start hard refresh",
)
def hard_refresh() -> dict[str, Any]:
    """
    Start a background hard-refresh job.

    Returns HTTP 202 when the job is accepted.
    Returns HTTP 409 when another refresh is running.
    """

    _debug("Hard refresh API request received")

    logger.info("Hard refresh API request received")

    try:
        job = controller.start_hard_refresh()

        return {
            "status": "accepted",
            "message": "Hard refresh started",
            "job": job,
        }

    except RuntimeError as ex:
        logger.warning(
            "Hard refresh API request failed error=%s",
            ex,
        )

        error_message = str(ex)

        _debug("Hard refresh rejected: {}", error_message)

        if "already running" in error_message.lower():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=error_message,
            ) from ex

        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=error_message,
        ) from ex

    except Exception as ex:
        logger.exception("Unexpected hard refresh API error")

        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unable to start hard refresh",
        ) from ex


# ---------------------------------------------------------------------------
# Optional controller cleanup endpoint
# ---------------------------------------------------------------------------


@router.delete(
    "/hard-refresh/cleanup",
    dependencies=[Depends(verify_api_key)],
    summary="Clean completed refresh jobs",
)
def cleanup_hard_refresh_jobs(
    keep_latest: int = 50,
) -> dict[str, Any]:
    """
    Remove old completed refresh jobs from memory.
    """

    _debug(
        "Cleanup requested keep_latest={}",
        keep_latest,
    )

    controller.cleanup_completed_jobs(keep_latest=keep_latest)

    return {
        "status": "success",
        "message": "Completed refresh jobs cleaned up",
        "remaining_jobs": len(controller.jobs),
    }


# ---------------------------------------------------------------------------
# Public exports
# ---------------------------------------------------------------------------


__all__ = [
    "router",
    "controller",
    "ApiController",
    "configure_refresh_routes",
    "get_refresh_controller",
    "verify_api_key",
]
