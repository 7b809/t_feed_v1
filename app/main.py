# app/main.py
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse

from .api import health as health_api
from .api import isolation_incoming, isolation_query, sr_query, supertrend_query
from .config import load_settings
from .logging_config import setup_logging
from .scheduler.scheduler_service import SchedulerService
from .services.isolation_store import IsolationStore, today_ist_date
from .services.isolation_subscriber import IsolationSubscriber
from .services.mongodb_service import MongoDbService
from .services.sr_state_store import SrStateStore
from .services.supertrend_state_store import SupertrendStateStore
from .services.telegram_service import TelegramService

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    config = load_settings()
    setup_logging(config)
    logger.info("starting %s (env=%s)", config.app_name, config.app_env)

    mongo = MongoDbService(config)
    mongo.connect()

    isolation_store = IsolationStore(mongo, config)
    sr_state_store = SrStateStore(config)
    supertrend_state_store = SupertrendStateStore(config)
    subscriber = IsolationSubscriber(
        config, isolation_store, sr_state_store, supertrend_state_store
    )

    # -------- startup: today-only re-subscription
    today_ist = today_ist_date()
    try:
        active_today = await asyncio.to_thread(
            isolation_store.find_today_isolations, today_ist
        )
    except Exception:
        logger.exception(
            "failed to load today's isolations (%s); will wait for API",
            today_ist,
        )
        active_today = []

    if not active_today:
        logger.info(
            "no active isolations for today (%s); waiting for API POST",
            today_ist,
        )
    else:
        logger.info(
            "found %d active isolation(s) for today (%s); resubscribing",
            len(active_today),
            today_ist,
        )
        for doc in active_today:
            td = doc.get("trade_date_ist")
            ik = doc.get("instrument_key")
            if not (td and ik):
                logger.warning(
                    "skipping doc without trade_date_ist/instrument_key: _id=%s",
                    doc.get("_id"),
                )
                continue

            derived_id = IsolationStore.make_isolation_id(td, ik)
            raw_id = doc.get("_id")

            # Adopt app1's singleton into our own tracking doc first, so the
            # consumer has a proper record (with consumer_status) on the next
            # restart. app1's doc itself is never modified.
            if raw_id != derived_id:
                logger.info(
                    "adopting upstream isolation _id=%s → %s",
                    raw_id,
                    derived_id,
                )
                try:
                    await asyncio.to_thread(
                        isolation_store.upsert, dict(doc), derived_id, False
                    )
                except Exception:
                    logger.exception(
                        "adopt upsert failed for %s (from %s)",
                        derived_id,
                        raw_id,
                    )
                    continue

            try:
                logger.info("resubscribing isolation_id=%s", derived_id)
                await subscriber.subscribe_isolation(
                    doc, derived_id, overwrite=False
                )
            except Exception:
                logger.exception("resubscribe failed for %s", derived_id)

        # ---- refresh on-disk mirror (data/instrument_isolation.json)
        # ``run.bat`` wipes ``data/`` on every start. When the doc was
        # already a consumer-owned one, the adoption branch above is
        # skipped and ``upsert()`` is never called — so nothing would
        # write the mirror otherwise. Mirror reflects the most recently
        # received isolation for today (latest-wins).
        try:
            latest = max(
                active_today,
                key=lambda d: d.get("received_at") or "",
            )
            isolation_store.write_mirror(latest)
            logger.info(
                "isolation mirror refreshed from _id=%s → %s/%s",
                latest.get("_id"),
                config.data_dir,
                "instrument_isolation.json",
            )
        except Exception:
            logger.exception("failed to refresh isolation mirror on startup")

    # -------- scheduler
    scheduler = SchedulerService(config, subscriber, isolation_store)
    scheduler.start()

    # -------- telegram (best-effort)
    telegram = TelegramService(config)
    try:
        await telegram.notify_startup(config.app_name)
    except Exception:
        logger.exception("telegram startup notify failed")

    # -------- expose on app.state
    app.state.config = config
    app.state.mongo = mongo
    app.state.isolation_store = isolation_store
    app.state.sr_state_store = sr_state_store
    app.state.supertrend_state_store = supertrend_state_store
    app.state.subscriber = subscriber
    app.state.scheduler = scheduler
    app.state.telegram = telegram

    logger.info("startup complete")

    try:
        yield
    finally:
        logger.info("shutting down")
        try:
            scheduler.shutdown()
        except Exception:
            logger.exception("scheduler shutdown error")
        try:
            await subscriber.stop_all()
        except Exception:
            logger.exception("subscriber stop_all error")
        try:
            await telegram.notify_shutdown(config.app_name)
        except Exception:
            logger.exception("telegram shutdown notify failed")
        try:
            mongo.close()
        except Exception:
            logger.exception("mongo close error")
        logger.info("shutdown complete")


app = FastAPI(
    title="isolation_consumer_service",
    version="0.3.2",
    lifespan=lifespan,
)

# ---- routers
app.include_router(health_api.router)
app.include_router(isolation_incoming.router, prefix="/api/v1", tags=["isolation"])
app.include_router(isolation_query.router, prefix="/api/v1", tags=["isolation"])
app.include_router(sr_query.router, prefix="/api/v1", tags=["sr"])
app.include_router(supertrend_query.router, prefix="/api/v1", tags=["supertrend"])


# ---- optional dashboard
@app.get("/", include_in_schema=False)
async def dashboard():
    path = Path(__file__).resolve().parent.parent / "templates" / "index.html"
    if path.exists():
        return FileResponse(path, media_type="text/html")
    return JSONResponse({"message": "isolation_consumer_service is running"})