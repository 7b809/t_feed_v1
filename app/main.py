import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from .config import (
    BASE_DIR,
    IST,
    LOG_DIR,
    SUBSCRIPTIONS,
)
from .socket_manager import SocketManager
from .scheduler import session_scheduler, in_session

LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_DIR / "app.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

manager = SocketManager()
scheduler_task = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global scheduler_task
    logger.info(
        "Starting NovaG7 socket dashboard; IST=%s",
        datetime.now(IST).isoformat(),
    )
    scheduler_task = asyncio.create_task(
        session_scheduler(manager), name="session-scheduler"
    )
    yield
    if scheduler_task:
        scheduler_task.cancel()
        try:
            await scheduler_task
        except asyncio.CancelledError:
            pass
    await manager.stop_all()
    logger.info("Dashboard shutdown complete")


app = FastAPI(
    title="NovaG7 Socket Dashboard", version="2.0.0", lifespan=lifespan
)
app.mount(
    "/static",
    StaticFiles(directory=str(BASE_DIR / "app" / "static")),
    name="static",
)


# ------------------------------------------------------------------ #
# UI
# ------------------------------------------------------------------ #
@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return (
        BASE_DIR / "app" / "templates" / "index.html"
    ).read_text(encoding="utf-8")


# ------------------------------------------------------------------ #
# Status / introspection
# ------------------------------------------------------------------ #
@app.get("/api/status")
async def status():
    inside = in_session()
    return {
        **manager.snapshot(),
        "ist_now": datetime.now(IST).isoformat(),
        "inside_scheduled_session": inside,
        "auto_connect_active": inside,
        "schedule": "Monday-Friday, 09:00-15:40 Asia/Kolkata",
        "feeds": {
            feed: {
                "label": cfg["label"],
                "api_url": cfg["api_url"],
                "dir": str(cfg["dir"].relative_to(BASE_DIR)),
                "wanted": cfg.get("wanted") or [],
            }
            for feed, cfg in SUBSCRIPTIONS.items()
        },
    }


@app.get("/api/subscriptions")
async def subscriptions():
    """
    Last raw snapshot fetched from each feed's subscriptions API, plus
    the current worker list derived from it.
    """
    return {
        "last_refresh_at": manager.last_refresh_at,
        "last_refresh_summary": manager.last_refresh_summary,
        "raw": manager.snapshots,
        "workers": {
            k: {
                "feed": c["feed"],
                "instrument_key": c["instrument_key"],
                "url": c["url"],
                "file": str(c["file"].relative_to(BASE_DIR)),
            }
            for k, c in manager.worker_configs.items()
        },
    }


# ------------------------------------------------------------------ #
# Refresh
# ------------------------------------------------------------------ #
@app.post("/api/refresh")
async def refresh():
    summary = await manager.refresh_subscriptions(auto_start=in_session())
    return {"ok": True, "summary": summary}


# ------------------------------------------------------------------ #
# Per-worker control
# ------------------------------------------------------------------ #
@app.post("/api/sockets/{key}/start")
async def start_socket(key: str):
    if key not in manager.worker_configs:
        raise HTTPException(404, "Unknown socket key")
    await manager.start(key)
    warning = (
        None
        if in_session()
        else "Outside market hours — the scheduler will stop this worker shortly."
    )
    return {"ok": True, "socket": key, "warning": warning}


@app.post("/api/sockets/{key}/stop")
async def stop_socket(key: str):
    if key not in manager.worker_configs:
        raise HTTPException(404, "Unknown socket key")
    await manager.stop(key)
    return {"ok": True, "socket": key}


# ------------------------------------------------------------------ #
# Per-feed control
# ------------------------------------------------------------------ #
@app.post("/api/feeds/{feed}/start")
async def start_feed(feed: str):
    if feed not in SUBSCRIPTIONS:
        raise HTTPException(404, "Unknown feed")
    await manager.start_feed(feed)
    warning = (
        None
        if in_session()
        else "Outside market hours — the scheduler will stop these workers shortly."
    )
    return {"ok": True, "feed": feed, "warning": warning}


@app.post("/api/feeds/{feed}/stop")
async def stop_feed(feed: str):
    if feed not in SUBSCRIPTIONS:
        raise HTTPException(404, "Unknown feed")
    await manager.stop_feed(feed)
    return {"ok": True, "feed": feed}


# ------------------------------------------------------------------ #
# Global control
# ------------------------------------------------------------------ #
@app.post("/api/sockets/start-all")
async def start_all():
    await manager.start_all()
    warning = (
        None
        if in_session()
        else "Outside market hours — the scheduler will stop these workers shortly."
    )
    return {"ok": True, "warning": warning}


@app.post("/api/sockets/stop-all")
async def stop_all():
    await manager.stop_all()
    return {"ok": True}


# ------------------------------------------------------------------ #
# Dashboard websocket
# ------------------------------------------------------------------ #
@app.websocket("/ws/dashboard")
async def dashboard_ws(websocket: WebSocket):
    await websocket.accept()
    queue = await manager.subscribe()
    try:
        await websocket.send_json(
            {"type": "snapshot", "data": manager.snapshot()}
        )
        while True:
            event = await queue.get()
            await websocket.send_json(event)
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("Dashboard websocket client error")
    finally:
        manager.unsubscribe(queue)