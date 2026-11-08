# main.py
"""FastAPI entry point — runs scheduler, daily refresh and feed under uvicorn.

Run:
    python -m uvicorn main:app --host 0.0.0.0 --port 8001

Routes
------
GET  /health                                liveness probe
GET  /status                                scheduler + job info
GET  /api/v1/isolation/current              persisted daily isolation record
POST /trigger/refresh                       manual daily refresh (testing)

GET  /api/v1/logs                           list all log files
GET  /api/v1/logs/{category}/{filename}     tail of a given log file
                                            ?lines=500    tail size (max 5000)
                                            ?raw=true     include full content
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse, PlainTextResponse

from scheduler import start_scheduler
from jobs.daily_refresh import run_daily_refresh, disconnect_feed
from core.config import PROJECT_INFO_COLLECTION
from core.logger import get_logger
from core.mongo_client import get_mongo_db
from core.telegram_notifier import send_telegram_message
from services.log_file_service import (
    DEFAULT_TAIL_LINES,
    HARD_MAX_TAIL_LINES,
    list_log_files,
    read_log_file,
)

logger = get_logger(__file__)

_scheduler = None  # populated in lifespan


# --------------------------------------------------------------------------- #
# Lifespan: startup + shutdown                                                #
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def lifespan(app: FastAPI):
    global _scheduler

    logger.info("Project starting…")
    send_telegram_message("🚀 Project starting…")

    try:
        run_daily_refresh()
    except Exception:
        logger.exception("Initial daily refresh failed.")

    try:
        _scheduler = start_scheduler()
        logger.info(
            "Scheduler running. Jobs: %s",
            [j.id for j in _scheduler.get_jobs()],
        )
    except Exception:
        logger.exception("Scheduler start failed.")
        _scheduler = None

    yield

    logger.info("Shutting down…")

    try:
        disconnect_feed()
    except Exception:
        logger.exception("Feed disconnect failed during shutdown.")

    if _scheduler is not None:
        try:
            _scheduler.shutdown(wait=False)
        except Exception:
            logger.exception("Scheduler shutdown failed.")

    send_telegram_message("🛑 Project stopped.")


# --------------------------------------------------------------------------- #
# App                                                                         #
# --------------------------------------------------------------------------- #
app = FastAPI(
    title="Instrument Isolation Service",
    description=(
        "Daily refresh, Upstox feed, index-isolation pipeline "
        "and log inspection API."
    ),
    version="0.2.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Health / status                                                             #
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/status")
def status():
    jobs = []
    if _scheduler is not None:
        for job in _scheduler.get_jobs():
            jobs.append(
                {
                    "id": job.id,
                    "name": job.name,
                    "next_run": str(job.next_run_time),
                }
            )
    return {"scheduler_running": _scheduler is not None, "jobs": jobs}


# --------------------------------------------------------------------------- #
# Isolation                                                                   #
# --------------------------------------------------------------------------- #
@app.get("/api/v1/isolation/current")
def current_isolation():
    try:
        db = get_mongo_db()
        doc = db[PROJECT_INFO_COLLECTION].find_one(
            {"_id": "instrument_isolation"}
        )
    except Exception as exc:
        logger.exception("Failed to read isolation record.")
        raise HTTPException(status_code=500, detail=str(exc))

    if not doc:
        raise HTTPException(
            status_code=404,
            detail="No isolation record for the current session yet.",
        )

    doc["_id"] = str(doc["_id"])
    return JSONResponse(content=doc, default=str)


@app.post("/trigger/refresh")
def trigger_refresh():
    try:
        run_daily_refresh()
        return {"status": "ok", "message": "Daily refresh executed."}
    except Exception as exc:
        logger.exception("Manual refresh failed.")
        raise HTTPException(status_code=500, detail=str(exc))


# --------------------------------------------------------------------------- #
# Log inspection                                                              #
# --------------------------------------------------------------------------- #
@app.get("/api/v1/logs")
def api_list_logs():
    """List every file in ``logs/`` and ``logs_text/`` with metadata."""
    try:
        files = list_log_files()
    except Exception as exc:
        logger.exception("Failed to list log files.")
        raise HTTPException(status_code=500, detail=str(exc))
    return {"count": len(files), "files": files}


@app.get("/api/v1/logs/{category}/{filename}")
def api_get_log(
    category: str,
    filename: str,
    lines: int = Query(
        DEFAULT_TAIL_LINES,
        ge=1,
        le=HARD_MAX_TAIL_LINES,
        description="Number of trailing lines to return.",
    ),
    raw: bool = Query(
        False,
        description="Also include the full file content in 'raw_content'.",
    ),
):
    """Return the tail of ``<category>/<filename>``.

    Valid ``category`` values are ``log`` (``logs/``) and ``text``
    (``logs_text/``).
    """
    try:
        return read_log_file(category, filename, lines=lines, raw=raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except FileNotFoundError:
        raise HTTPException(
            status_code=404,
            detail=f"Log file '{category}/{filename}' not found.",
        )
    except OSError as exc:
        logger.exception("Failed to read log file %s/%s", category, filename)
        raise HTTPException(status_code=500, detail=str(exc))


@app.get(
    "/api/v1/logs/{category}/{filename}/download",
    response_class=PlainTextResponse,
)
def api_download_log(category: str, filename: str):
    """Download the raw content of a log file as ``text/plain``."""
    try:
        payload = read_log_file(category, filename, lines=1, raw=True)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except FileNotFoundError:
        raise HTTPException(
            status_code=404,
            detail=f"Log file '{category}/{filename}' not found.",
        )
    except OSError as exc:
        logger.exception("Failed to download log file %s/%s", category, filename)
        raise HTTPException(status_code=500, detail=str(exc))

    return PlainTextResponse(
        payload["raw_content"],
        media_type="text/plain",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"'
        },
    )