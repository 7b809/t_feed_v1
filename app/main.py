# app/main.py
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from fastapi import Request

from .api import health as health_api
from .api import dashboard_query
from .api import dummy_orders_query
from .api import isolation_incoming, isolation_query, sr_query, supertrend_query, live_feed_query
from .config import load_settings
from .logging_config import setup_logging
from .scheduler.scheduler_service import SchedulerService
from .services.isolation_store import IsolationStore, today_ist_date
from .services.isolation_subscriber import IsolationSubscriber
from .services.live_feed_state_store import LiveFeedStateStore
from .services.live_feed_candle_store import LiveFeedCandleStore
from .services.mongodb_service import MongoDbService
from .services.sr_state_store import SrStateStore
from .services.supertrend_state_store import SupertrendStateStore
from .services.telegram_service import TelegramService
from .services.dummy_order_store import DummyOrderStore
from .services.dummy_order_mongo_store import DummyOrderMongoStore
from .services.dummy_order_engine import DummyOrderEngine
from .services.daily_data_backup_service import DailyDataBackupService
from .services.market_session import market_session_status

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
    live_feed_state_store = LiveFeedStateStore(config)
    live_feed_candle_store = LiveFeedCandleStore(config)
    telegram = TelegramService(config)
    dummy_order_store = DummyOrderStore(config)
    dummy_order_mongo_store = DummyOrderMongoStore(mongo.db["dummy_orders"])
    dummy_order_engine = DummyOrderEngine(
        config, dummy_order_store, live_feed_state_store, telegram,
        mongo_store=dummy_order_mongo_store,
        candle_store=live_feed_candle_store,
    )
    backup_collection = mongo.db[config.daily_data_backup_collection]
    daily_backup_service = DailyDataBackupService(config, telegram, backup_collection)
    subscriber = IsolationSubscriber(
        config, isolation_store, sr_state_store, supertrend_state_store,
        live_feed_state_store, dummy_order_engine, live_feed_candle_store,
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

        try:
            latest = max(
                active_today,
                key=lambda d: d.get("received_at") or "",
            )
            isolation_store.write_mirror(latest)
            logger.info(
                "isolation mirror refreshed from _id=%s → %s/%s",
                latest.get("_id"), config.data_dir, "instrument_isolation.json",
            )
        except Exception:
            logger.exception("failed to refresh isolation mirror on startup")

    scheduler = SchedulerService(config, subscriber, isolation_store, daily_backup_service, telegram)
    scheduler.start()

    try:
        session_status = market_session_status(config)
        states_today = await dummy_order_engine.get_orders()
        states_today = [
            state for state in (states_today or [])
            if str(state.get("trade_date_ist") or state.get("isolation_id", "").split("__", 1)[0])[:10] == today_ist
        ]
        restored_orders = {}
        for state in states_today:
            for order in list(state.get("order_history") or []) + list((state.get("orders") or {}).values()):
                if not isinstance(order, dict):
                    continue
                identity = str(
                    order.get("order_id")
                    or f"{order.get('indicator', 'order')}:{order.get('started_at_ist') or order.get('entry_time_ist')}"
                )
                restored_orders[identity] = order
        open_count = sum(
            1 for order in restored_orders.values()
            if order.get("status") == "DUMMY_PLACED"
        )
        completed_count = sum(
            1 for order in restored_orders.values()
            if str(order.get("status", "")).startswith("COMPLETED")
        )
        logger.info(
            "Dummy-order startup state restored local_or_mongo=%d open=%d completed=%d",
            len(restored_orders), open_count, completed_count,
        )
        await telegram.notify_startup(
            config.app_name,
            session=session_status,
            today_summary={
                "active_isolations": len(active_today),
                "orders": len(restored_orders),
                "open_orders": open_count,
                "completed_orders": completed_count,
            },
        )
    except Exception:
        logger.exception("telegram startup recovery notify failed")

    app.state.config = config
    app.state.mongo = mongo
    app.state.isolation_store = isolation_store
    app.state.sr_state_store = sr_state_store
    app.state.supertrend_state_store = supertrend_state_store
    app.state.live_feed_state_store = live_feed_state_store
    app.state.live_feed_candle_store = live_feed_candle_store
    app.state.subscriber = subscriber
    app.state.scheduler = scheduler
    app.state.telegram = telegram
    app.state.dummy_order_store = dummy_order_store
    app.state.dummy_order_mongo_store = dummy_order_mongo_store
    app.state.dummy_order_engine = dummy_order_engine
    app.state.daily_backup_service = daily_backup_service

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

app.include_router(health_api.router)
app.include_router(isolation_incoming.router, prefix="/api/v1", tags=["isolation"])
app.include_router(isolation_query.router, prefix="/api/v1", tags=["isolation"])
app.include_router(sr_query.router, prefix="/api/v1", tags=["sr"])
app.include_router(supertrend_query.router, prefix="/api/v1", tags=["supertrend"])
app.include_router(live_feed_query.router, prefix="/api/v1", tags=["live-feed"])
app.include_router(dummy_orders_query.router, prefix="/api/v1", tags=["dummy-orders"])
app.include_router(dashboard_query.router, prefix="/api/v1", tags=["dashboard"])

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


@app.get("/", include_in_schema=False)
async def dashboard_home_page(request: Request):
    return TEMPLATES.TemplateResponse(request=request, name="home.html",
        context={"request": request, "active_page": "home", "page_title": "Live Operations"})

@app.get("/isolated", include_in_schema=False)
async def dashboard_isolated_page(request: Request):
    return TEMPLATES.TemplateResponse(request=request, name="isolated.html",
        context={"request": request, "active_page": "isolated", "page_title": "Isolated Instruments"})

@app.get("/orders", include_in_schema=False)
async def dashboard_orders_page(request: Request):
    return TEMPLATES.TemplateResponse(request=request, name="orders.html",
        context={"request": request, "active_page": "orders", "page_title": "Dummy Orders"})
