# main.py
"""FastAPI entry point — scheduler, daily refresh, feed, isolation API.

Startup sequence (lifespan)
---------------------------
``TEST_FLAG`` and the deferred-startup cutoff control whether the
process runs the startup sequence immediately or goes idle:

    TEST_FLAG=true                 → run now (dev/testing)
    TEST_FLAG=false, now >= cutoff → run now
    TEST_FLAG=false, now <  cutoff → DEFER: stay idle, register a one-shot
                                      `deferred_startup` job at the cutoff,
                                      which then runs daily refresh
                                      → backfill in order.

Routes
------
GET  /health                                liveness probe
GET  /status                                scheduler + job info
GET  /startup-state                         deferred-startup diagnostics
POST /trigger/refresh?force=false|true      manual daily refresh

GET  /api/v1/isolation/current              persisted daily isolation record
GET  /api/v1/isolation/history              recent isolation history
POST /api/v1/isolation/trigger              manual isolation scan
POST /api/v1/isolation/backfill             manual startup backfill

POST /api/isolation-test                    send a static dummy isolation
                                            payload to each client URL and
                                            return per-URL HTTP result

GET  /api/v1/search/instrument              first match at a given strike
GET  /api/v1/search/instrument/all          all matches at a given strike
GET  /api/v1/search/instrument/atm          ATM lookup from NIFTY LTP
GET  /api/v1/search/instrument/raw          raw Upstox response

GET  /api/v1/logs                           list all log files
GET  /api/v1/logs/{category}/{filename}     tail of a given log file
GET  /api/v1/logs/{category}/{filename}/download
                                            raw file as text/plain attachment
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import PlainTextResponse

from api.routes.isolation import (
    router as isolation_router,
    test_router as isolation_test_router,
)
from api.routes.search import router as search_router
from api.routes.market_feed import router as market_feed_router, shutdown_websocket_feeds
from scheduler import start_scheduler
from jobs.daily_refresh import (
    run_daily_refresh,
    disconnect_feed,
    get_last_daily_refresh_date,
)
from jobs.isolation_scan import run_startup_backfill
from core.config import (
    TIMEZONE,
    TEST_FLAG,
    DEFERRED_STARTUP_HOUR,
    DEFERRED_STARTUP_MINUTE,
)
from core.logger import get_logger
from core.telegram_notifier import send_telegram_message
from services.isolated_archive_service import IsolatedArchiveService
from services.isolated_feed_manager import (
    start_isolated_feeds, get_isolated_feed_status, stop_isolated_feeds,
)
from services.isolation_telegram_service import notify_runtime_event
from services.isolated_live_feed_capture_service import (
    start_isolated_live_feed, get_isolated_live_feed_status, stop_isolated_live_feed,
)
from services.log_file_service import (
    DEFAULT_TAIL_LINES,
    HARD_MAX_TAIL_LINES,
    list_log_files,
    read_log_file,
)

logger = get_logger(__file__)


def _get_option_chain_capture_status_safe() -> dict:
    """Optional feature health must never make the API unavailable."""
    try:
        from services.option_chain_capture_service import get_option_chain_capture_status
        return get_option_chain_capture_status()
    except Exception:
        logger.exception("Optional option-chain status unavailable")
        return {"enabled": True, "state": "unavailable"}


def _stop_option_chain_capture_safe() -> None:
    """Shut down optional capture without affecting core shutdown."""
    try:
        from services.option_chain_capture_service import stop_option_chain_capture
        stop_option_chain_capture()
    except Exception:
        logger.exception("Optional option-chain capture shutdown failed")


_scheduler = None                       # populated in lifespan
_startup_mode: str = "unknown"          # "immediate" | "deferred" | "test"
_startup_cutoff_iso: str | None = None  # populated in deferred mode

# --------------------------------------------------------------------------- #
# Lifespan                                                                    #
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def lifespan(app: FastAPI):
    global _scheduler, _startup_mode, _startup_cutoff_iso

    logger.info("Project starting…")

    # Mandatory runtime cleanup runs before daily refresh/backfill. This is
    # deliberately independent of the deferred market-startup gate.
    try:
        from services.daily_startup_cleanup_service import run_daily_startup_cleanup
        cleanup_result = run_daily_startup_cleanup()
        logger.info("Startup cleanup state: %s", cleanup_result.get("state"))
    except Exception:
        logger.exception("Daily startup cleanup failed; continuing with isolation startup")

    # ------------------------------------------------------------------ #
    # TEST_FLAG visibility line — printed exactly once, on every startup.
    # Grep for [TEST-FLAG] to instantly classify a run as test or prod.
    # ------------------------------------------------------------------ #
    if TEST_FLAG:
        logger.info(
            "[TEST-FLAG] TEST_FLAG=TRUE  → test mode is ON "
            "(startup will ignore the deferred cutoff and run immediately)"
        )
    else:
        logger.info(
            "[TEST-FLAG] TEST_FLAG=FALSE → production mode "
            "(deferred startup gate is active if now < cutoff)"
        )

    notify_runtime_event("ISOLATION SERVICE STARTING", "Environment initialising · Asia/Kolkata", "🚀")

    now = datetime.now(ZoneInfo(TIMEZONE))
    cutoff = now.replace(
        hour=DEFERRED_STARTUP_HOUR,
        minute=DEFERRED_STARTUP_MINUTE,
        second=0,
        microsecond=0,
    )
    should_defer = (not TEST_FLAG) and (now < cutoff)

    # ------------------------------------------------------------------ #
    # BRANCH A — Deferred startup
    # ------------------------------------------------------------------ #
    if should_defer:
        _startup_mode = "deferred"
        _startup_cutoff_iso = cutoff.isoformat()

        logger.warning(
            "[STARTUP-GATE] TEST_FLAG=false and now=%s < cutoff=%s. "
            "Going idle until cutoff. Startup sequence will run at %s.",
            now.strftime("%A %H:%M:%S"),
            cutoff.strftime("%H:%M:%S"),
            cutoff.strftime("%H:%M:%S"),
        )
        send_telegram_message(
            f"⏸ Deferred startup — idle until "
            f"{cutoff.strftime('%H:%M')} IST"
        )

        try:
            _scheduler = start_scheduler(deferred_until=cutoff)
            logger.info(
                "Scheduler running (deferred). Jobs: %s",
                [j.id for j in _scheduler.get_jobs()],
            )
        except Exception:
            logger.exception("Scheduler start failed.")
            _scheduler = None

    # ------------------------------------------------------------------ #
    # BRANCH B — Immediate startup
    # ------------------------------------------------------------------ #
    else:
        _startup_mode = "test" if TEST_FLAG else "immediate"

        if TEST_FLAG:
            logger.info(
                "[STARTUP-GATE] TEST_FLAG=true → running startup sequence "
                "immediately (now=%s).",
                now.strftime("%A %H:%M:%S"),
            )
        else:
            logger.info(
                "[STARTUP-GATE] now=%s >= cutoff=%s → running startup "
                "sequence immediately.",
                now.strftime("%A %H:%M:%S"),
                cutoff.strftime("%H:%M:%S"),
            )

        try:
            run_daily_refresh()
        except Exception:
            logger.exception("Initial daily refresh failed.")

        try:
            run_startup_backfill()
        except Exception:
            logger.exception("Startup backfill failed.")

        _restore_isolation_capture()

        try:
            from services.option_chain_capture_service import ensure_option_chain_capture
            logger.info("Startup option-chain capture: %s", ensure_option_chain_capture())
        except Exception:
            logger.exception("Startup option-chain capture kickoff failed")

        try:
            from services.live_gtt_execution_service import start_live_gtt_execution_service
            logger.info("Startup live GTT execution service: %s", start_live_gtt_execution_service())
        except Exception:
            logger.exception("Live GTT execution service startup failed")

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

    # ------------------------------------------------------------------ #
    # Shutdown
    # ------------------------------------------------------------------ #
    logger.info("Shutting down…")

    try:
        from services.live_gtt_execution_service import stop_live_gtt_execution_service
        stop_live_gtt_execution_service()
    except Exception:
        logger.exception("Live GTT execution service shutdown failed")

    _stop_option_chain_capture_safe()

    try:
        stop_isolated_live_feed()
    except Exception:
        logger.exception("Isolated live-feed capture shutdown failed")

    try:
        stop_isolated_feeds()
    except Exception:
        logger.exception("Isolated SR/Supertrend feed shutdown failed.")

    try:
        shutdown_websocket_feeds()
    except Exception:
        logger.exception("External WebSocket feed shutdown failed.")

    try:
        disconnect_feed()
    except Exception:
        logger.exception("Feed disconnect failed during shutdown.")

    if _scheduler is not None:
        try:
            _scheduler.shutdown(wait=False)
        except Exception:
            logger.exception("Scheduler shutdown failed.")

    notify_runtime_event("ISOLATION SERVICE STOPPED", "Application shutdown completed.", "🛑")

# --------------------------------------------------------------------------- #
# App                                                                         #
# --------------------------------------------------------------------------- #
app = FastAPI(
    title="Instrument Isolation Service",
    description=(
        "OR-level auto-compute, Upstox feed, index-isolation pipeline "
        "with NIFTY-trend bias, deferred startup gate, startup backfill, "
        "isolation API, instrument search API, log inspection API, and "
        "synchronous client-notification test endpoint."
    ),
    version="0.7.1",
    lifespan=lifespan,
)

app.include_router(isolation_router)
app.include_router(isolation_test_router)
app.include_router(search_router)
app.include_router(market_feed_router)

# --------------------------------------------------------------------------- #
# Health / status                                                             #
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    return {
        "status": "ok",
        "isolated_feeds": get_isolated_feed_status(),
        "isolated_live_feed": get_isolated_live_feed_status(),
        "option_chain_capture": _get_option_chain_capture_status_safe(),
    }


def _restore_isolation_capture() -> None:
    """Resume indicator capture for today's persisted isolation after restart."""
    try:
        archive = IsolatedArchiveService().load_current()
        if archive:
            record = archive.get("source_record") or {}
        else:
            from core.config import ISOLATION_DOC_ID, PROJECT_INFO_COLLECTION
            from core.mongo_client import get_mongo_db
            record = get_mongo_db()[PROJECT_INFO_COLLECTION].find_one({"_id": ISOLATION_DOC_ID}) or {}
        today = datetime.now(ZoneInfo(TIMEZONE)).date().isoformat()
        if (record and record.get("trade_date_ist") == today
                and record.get("status", "ACTIVE") == "ACTIVE"
                and record.get("instrument_key")):
            if not archive:
                IsolatedArchiveService().save_isolation(record)
            start_isolated_live_feed(record)
            start_isolated_feeds(record)
            notify_runtime_event(
                "ISOLATION CAPTURE RESTORED",
                f"{record.get('trading_symbol') or record.get('instrument_key')} · live feed, SR and Supertrend recovery requested.",
                "🔄",
            )
    except Exception:
        logger.exception("Could not restore today's isolation feed capture")

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
    return {"scheduler_running": _scheduler is not None, "jobs": jobs,
            "isolated_feeds": get_isolated_feed_status(),
            "isolated_live_feed": get_isolated_live_feed_status(),
            "option_chain_capture": _get_option_chain_capture_status_safe()}

@app.get("/startup-state")
def startup_state():
    """Report startup gate state — useful during deferred mode."""
    return {
        "startup_mode": _startup_mode,   # "immediate" | "deferred" | "test"
        "test_flag": TEST_FLAG,
        "deferred_cutoff_hour": DEFERRED_STARTUP_HOUR,
        "deferred_cutoff_minute": DEFERRED_STARTUP_MINUTE,
        "deferred_cutoff_iso": _startup_cutoff_iso,
        "last_daily_refresh_date": get_last_daily_refresh_date(),
    }

# --------------------------------------------------------------------------- #
# Manual daily refresh                                                        #
# --------------------------------------------------------------------------- #
@app.post("/trigger/refresh")
def trigger_refresh(
    force: bool = Query(
        False,
        description=(
            "When true, bypass the per-day idempotency guard. Useful for "
            "testing even after today's scheduled refresh already ran."
        ),
    ),
):
    try:
        run_daily_refresh(force=force)
        return {
            "status": "ok",
            "message": "Daily refresh executed.",
            "forced": force,
        }
    except Exception as exc:
        logger.exception("Manual refresh failed.")
        raise HTTPException(status_code=500, detail=str(exc))

# --------------------------------------------------------------------------- #
# Log inspection                                                              #
# --------------------------------------------------------------------------- #
@app.get("/api/v1/logs")
def api_list_logs():
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
    lines: int = Query(DEFAULT_TAIL_LINES, ge=1, le=HARD_MAX_TAIL_LINES),
    raw: bool = Query(False),
):
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