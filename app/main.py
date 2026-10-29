# app/main.py

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

from app.api.ema import router as ema_router
from app.api.health import router as health_router
from app.api.levels import router as levels_router
from app.api.mega_archive import router as mega_router
from app.api.ws_catalog import router as ws_catalog_router
from app.config import PROJECT_ROOT, settings
from app.logging_config import configure_logging
from app.scheduler.scheduler_service import SchedulerService
from app.services.cleanup_service import clean_directories
from app.services.ema_feed_service import EmaFeedService
from app.services.ema_state_store import EmaStateStore
from app.services.feed_broadcaster import FeedBroadcaster
from app.services.instrument_store import InstrumentStore
from app.services.levels_service import LevelsService
from app.services.levels_state_store import LevelsStateStore
from app.services.mega_archive_service import MegaArchiveService
from app.services.mongodb_service import MongoDocumentService
from app.services.project_info_service import ProjectInfoService
from app.services.telegram_service import ProjectNotifier
from app.services.upstox_client_service import UpstoxService

configure_logging(
    settings.logs_path,
    cycle_log_every=settings.ema_cycle_log_every,
)
logger = logging.getLogger(__name__)

TEMPLATES_DIR = PROJECT_ROOT / "templates"


@asynccontextmanager
async def lifespan(app: FastAPI):
    mongo = None
    scheduler = None
    ema_service = None
    levels_service = None
    notifier = ProjectNotifier(settings)
    app.state.notifier = notifier

    try:
        # ------------------------------------------------------------------
        # 0. Cleanup BEFORE anything else.
        # ------------------------------------------------------------------
        try:
            removed = clean_directories(settings, trigger="startup")
            if removed:
                logger.info("Startup cleanup removed %d path(s)", len(removed))
        except Exception as exc:  # noqa: BLE001
            logger.exception("Startup cleanup failed; continuing anyway")
            await asyncio.to_thread(
                notifier.notify_exception,
                where="lifespan.cleanup",
                exc=exc,
            )

        # ------------------------------------------------------------------
        # 1. Ensure required directories exist (recreated after cleanup).
        # ------------------------------------------------------------------
        settings.download_dir.mkdir(parents=True, exist_ok=True)
        settings.destination_dir.parent.mkdir(parents=True, exist_ok=True)
        settings.ema_state_dir.mkdir(parents=True, exist_ok=True)

        # ---------------- Job 1 services ----------------
        mongo = MongoDocumentService(settings)
        app.state.mongo_service = mongo
        archive_service = MegaArchiveService(settings, mongo, notifier=notifier)
        app.state.mega_archive_service = archive_service

        # ---------------- Shared MongoDB writer (project_info) ----------------
        project_info = ProjectInfoService(settings, mongo.client)
        app.state.project_info_service = project_info

        # ------------------------------------------------------------------
        # 2. Startup refresh — SEQUENTIAL, must complete before EMA bootstrap.
        # ------------------------------------------------------------------
        if settings.refresh_on_startup:
            logger.info("Startup refresh running (blocking) before EMA bootstrap")
            try:
                await asyncio.to_thread(_startup_refresh, archive_service)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Startup refresh raised unexpectedly")
                await asyncio.to_thread(
                    notifier.notify_exception,
                    where="lifespan.startup_refresh",
                    exc=exc,
                )

        # ------------------------------------------------------------------
        # 3. Shared Upstox client + Job 2 (EMA) + Job 3 (Levels).
        # ------------------------------------------------------------------
        instruments = InstrumentStore(settings.instruments_file)
        upstox = UpstoxService(settings)

        # -------- Job 2 (EMA) --------
        state_store = EmaStateStore(settings)
        broadcaster = FeedBroadcaster()
        ema_service = EmaFeedService(
            settings,
            broadcaster,
            instruments,
            upstox,
            state_store,
            notifier=notifier,
        )
        app.state.ema_feed_service = ema_service
        app.state.feed_broadcaster = broadcaster

        # -------- Job 3 (Levels) --------
        levels_store = LevelsStateStore(settings)
        levels_service = LevelsService(
            settings,
            upstox,
            levels_store,
            notifier=notifier,
            project_info=project_info,
        )
        app.state.levels_service = levels_service

        # ------------------------------------------------------------------
        # 4. EMA bootstrap.
        # ------------------------------------------------------------------
        try:
            await ema_service.bootstrap()
        except Exception as exc:  # noqa: BLE001
            logger.exception("EMA bootstrap failed; service will keep running")
            await asyncio.to_thread(
                notifier.notify_exception,
                where="lifespan.ema_bootstrap",
                exc=exc,
            )

        # ------------------------------------------------------------------
        # 5. Levels bootstrap.
        # ------------------------------------------------------------------
        try:
            await levels_service.bootstrap()
        except Exception as exc:  # noqa: BLE001
            logger.exception("Levels bootstrap failed; service will keep running")
            await asyncio.to_thread(
                notifier.notify_exception,
                where="lifespan.levels_bootstrap",
                exc=exc,
            )

        # ------------------------------------------------------------------
        # 6. Scheduler last.
        # ------------------------------------------------------------------
        scheduler = SchedulerService(
            settings,
            archive_service,
            ema_service,
            levels_service,
        )
        scheduler.set_event_loop(asyncio.get_running_loop())
        app.state.scheduler_service = scheduler
        scheduler.start()

        await asyncio.to_thread(
            notifier.notify_startup,
            host=settings.app_host,
            port=settings.app_port,
            scheduler_enabled=settings.scheduler_enabled,
            refresh_on_startup=settings.refresh_on_startup,
            ema_instruments=settings.index_instrument_keys,
            pid=os.getpid(),
        )

        yield
    except Exception as exc:
        logger.exception("Lifespan failed")
        try:
            await asyncio.to_thread(
                notifier.notify_exception,
                where="lifespan",
                exc=exc,
            )
        except Exception:  # noqa: BLE001
            pass
        raise
    finally:
        try:
            await asyncio.to_thread(notifier.notify_shutdown, reason="lifespan exit")
        except Exception:  # noqa: BLE001
            pass

        if ema_service is not None:
            await ema_service.shutdown()
        if scheduler is not None:
            scheduler.shutdown()
        if mongo is not None:
            mongo.close()
        notifier.close()


def _startup_refresh(service: MegaArchiveService) -> None:
    try:
        service.refresh("startup")
    except Exception:
        logger.exception("Startup refresh failed")


app = FastAPI(title=settings.app_name, version="1.5.0", lifespan=lifespan)
app.include_router(health_router)
app.include_router(mega_router)
app.include_router(ema_router)
app.include_router(levels_router)
app.include_router(ws_catalog_router)


@app.get("/", include_in_schema=False)
def dashboard():
    html_path = TEMPLATES_DIR / "index.html"
    if not html_path.exists():
        return JSONResponse(
            status_code=500,
            content={
                "error": "dashboard template missing",
                "path": str(html_path),
            },
        )
    return FileResponse(
        html_path,
        media_type="text/html",
        content_disposition_type="inline",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/v1/info")
def info():
    return {
        "app": settings.app_name,
        "scope": (
            "Job 1: MongoDB -> MEGA ZIP -> safe extraction | "
            "Job 2: EMA crosses live feed | "
            "Job 3: First-candle pivot levels (Telegram + MongoDB persistence)"
        ),
        "docs": "/docs",
        "health": "/health",
        "dashboard": "/",
        "ema_status": "/api/v1/ema/status",
        "ema_channels": "/api/v1/ema/channels",
        "ws_catalog": "/api/v1/ws/catalog",
        "ws_catalog_lookup": "/api/v1/ws/catalog/{instrument_key}",
        "levels_all": "/api/v1/levels",
        "levels_db": "/api/v1/levels/db",
        "levels_refresh": "/api/v1/levels/refresh",
    }


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
    notifier: ProjectNotifier | None = getattr(request.app.state, "notifier", None)
    if notifier is not None:
        try:
            await asyncio.to_thread(
                notifier.notify_exception,
                where=f"{request.method} {request.url.path}",
                exc=exc,
                context=str(request.client.host) if request.client else None,
            )
        except Exception:  # noqa: BLE001
            pass
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
    )