import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from time import perf_counter
from typing import Any, AsyncIterator, Callable

from apscheduler.events import (
    EVENT_JOB_ERROR,
    EVENT_JOB_EXECUTED,
    JobExecutionEvent,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import (
    FastAPI,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates

from app.database import Database
from app.logger import get_logger
from app.models import SubscriptionRequest
from app.upstox_service import UpstoxMarketService
from app.websockets import WebSocketHub


logger = get_logger(__file__)

database = Database()
hub = WebSocketHub()
market = UpstoxMarketService(database, hub)

scheduler = AsyncIOScheduler(timezone="Asia/Kolkata")

templates = Jinja2Templates(
    directory=str(Path(__file__).parent / "templates")
)


def scheduler_event_listener(event: JobExecutionEvent) -> None:
    if event.exception:
        logger.error(
            "Scheduled job failed | job_id=%s | exception=%s",
            event.job_id,
            event.exception,
            exc_info=(
                type(event.exception),
                event.exception,
                event.exception.__traceback__,
            ),
        )
    else:
        logger.info(
            "Scheduled job completed successfully | job_id=%s",
            event.job_id,
        )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator:
    logger.info("Application startup initiated")

    scheduler_started = False
    market_started = False

    try:
        logger.info("Checking MongoDB connectivity")
        database.ping()
        logger.info("MongoDB connection verified")

        logger.info("Ensuring MongoDB indexes")
        database.ensure_indexes()
        logger.info("MongoDB indexes verified")

        logger.info("Starting Upstox market service")
        market.start(asyncio.get_running_loop())
        market_started = True
        logger.info("Upstox market service started")

        scheduler.add_listener(
            scheduler_event_listener,
            EVENT_JOB_EXECUTED | EVENT_JOB_ERROR,
        )

        scheduler.add_job(
            market.refresh_token_and_reconnect,
            CronTrigger(
                day_of_week="mon-fri",
                hour=9,
                minute=0,
                timezone="Asia/Kolkata",
            ),
            id="weekday_token_refresh",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )

        scheduler.start()
        scheduler_started = True

        logger.info(
            "Scheduler started | "
            "token_refresh_schedule=Monday-Friday 09:00 Asia/Kolkata"
        )
        logger.info("Application startup completed")

        yield

    except Exception:
        logger.exception("Application startup or runtime failure")
        raise

    finally:
        logger.info("Application shutdown initiated")

        if scheduler_started:
            try:
                scheduler.shutdown(wait=False)
                logger.info("Scheduler stopped")
            except Exception:
                logger.exception("Failed to stop scheduler cleanly")

        if market_started:
            try:
                market.stop()
                logger.info("Upstox market service stopped")
            except Exception:
                logger.exception(
                    "Failed to stop Upstox market service cleanly"
                )

        try:
            database.close()
            logger.info("Database connection closed")
        except Exception:
            logger.exception(
                "Failed to close database connection cleanly"
            )

        logger.info("Application shutdown completed")


app = FastAPI(
    title="Upstox Market Stream Gateway",
    version="1.0.0",
    lifespan=lifespan,
)


@app.middleware("http")
async def request_logging_middleware(
    request: Request,
    call_next: Callable[..., Any],
) -> Response:
    start_time = perf_counter()
    client_host = (
        request.client.host
        if request.client
        else "unknown"
    )

    logger.info(
        "HTTP request started | method=%s | path=%s | client=%s",
        request.method,
        request.url.path,
        client_host,
    )

    try:
        response = await call_next(request)

        duration_ms = (
            perf_counter() - start_time
        ) * 1000

        logger.info(
            "HTTP request completed | method=%s | path=%s | "
            "status=%s | duration_ms=%.2f",
            request.method,
            request.url.path,
            response.status_code,
            duration_ms,
        )

        return response

    except Exception:
        duration_ms = (
            perf_counter() - start_time
        ) * 1000

        logger.exception(
            "Unhandled HTTP request exception | method=%s | "
            "path=%s | client=%s | duration_ms=%.2f",
            request.method,
            request.url.path,
            client_host,
            duration_ms,
        )

        return JSONResponse(
            status_code=500,
            content={
                "detail": "Internal server error",
            },
        )


@app.exception_handler(HTTPException)
async def http_exception_handler(
    request: Request,
    exc: HTTPException,
) -> JSONResponse:
    if exc.status_code >= 500:
        logger.error(
            "HTTP exception | method=%s | path=%s | "
            "status=%s | detail=%s",
            request.method,
            request.url.path,
            exc.status_code,
            exc.detail,
        )
    else:
        logger.warning(
            "HTTP exception | method=%s | path=%s | "
            "status=%s | detail=%s",
            request.method,
            request.url.path,
            exc.status_code,
            exc.detail,
        )

    return JSONResponse(
        status_code=exc.status_code,
        content={
            "detail": exc.detail,
        },
        headers=exc.headers,
    )


@app.get(
    "/",
    response_class=HTMLResponse,
)
async def index(request: Request) -> HTMLResponse:
    try:
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={},
        )

    except Exception as exc:
        logger.exception("Failed to render index page")

        raise HTTPException(
            status_code=500,
            detail="Failed to render index page",
        ) from exc


@app.get("/api/health")
def health() -> dict[str, Any]:
    try:
        active_subscriptions = database.list_active()

        health_status = {
            "status": "ok",
            "upstox_connected": market.connected,
            "active_subscriptions": len(
                active_subscriptions
            ),
            "websocket_clients": hub.client_counts(),
        }

        logger.debug(
            "Health check completed | status=%s",
            health_status,
        )

        return health_status

    except Exception as exc:
        logger.exception("Health check failed")

        raise HTTPException(
            status_code=503,
            detail="Health check failed",
        ) from exc


@app.get("/api/instruments/search")
async def search_instruments(
    query: str = Query(
        ...,
        min_length=2,
        max_length=50,
    ),
    exchanges: str | None = None,
    segments: str | None = None,
    instrument_types: str | None = None,
    expiry: str | None = None,
    atm_offset: int | None = None,
    page_number: int = Query(
        1,
        ge=1,
    ),
    records: int = Query(
        10,
        ge=1,
        le=30,
    ),
) -> Any:
    logger.info(
        "Instrument search requested | query=%s | "
        "page=%s | records=%s",
        query,
        page_number,
        records,
    )

    search_parameters = {
        "query": query,
        "exchanges": exchanges,
        "segments": segments,
        "instrument_types": instrument_types,
        "expiry": expiry,
        "atm_offset": atm_offset,
        "page_number": page_number,
        "records": records,
    }

    try:
        result = await market.search_instruments(
            search_parameters
        )

        logger.info(
            "Instrument search completed | query=%s",
            query,
        )

        return result

    except Exception as exc:
        logger.exception(
            "Instrument search failed | query=%s",
            query,
        )

        raise HTTPException(
            status_code=502,
            detail="Instrument search service failed",
        ) from exc


@app.post("/api/subscriptions")
def subscribe(
    body: SubscriptionRequest,
) -> Any:
    logger.info(
        "Subscription requested | instrument_count=%s | mode=%s",
        len(body.instrument_keys),
        body.mode,
    )

    try:
        result = market.subscribe(
            body.instrument_keys,
            body.mode,
        )

        logger.info(
            "Subscription completed | instrument_count=%s | mode=%s",
            len(body.instrument_keys),
            body.mode,
        )

        return result

    except Exception as exc:
        logger.exception(
            "Subscription failed | instrument_count=%s | mode=%s",
            len(body.instrument_keys),
            body.mode,
        )

        raise HTTPException(
            status_code=502,
            detail="Failed to create subscriptions",
        ) from exc


@app.delete("/api/subscriptions")
def unsubscribe(
    body: SubscriptionRequest,
) -> Any:
    logger.info(
        "Unsubscribe requested | instrument_count=%s",
        len(body.instrument_keys),
    )

    try:
        result = market.unsubscribe(
            body.instrument_keys
        )

        logger.info(
            "Unsubscribe completed | instrument_count=%s",
            len(body.instrument_keys),
        )

        return result

    except Exception as exc:
        logger.exception(
            "Unsubscribe failed | instrument_count=%s",
            len(body.instrument_keys),
        )

        raise HTTPException(
            status_code=502,
            detail="Failed to remove subscriptions",
        ) from exc


@app.patch("/api/subscriptions/mode")
def change_mode(
    body: SubscriptionRequest,
) -> Any:
    logger.info(
        "Subscription mode change requested | "
        "instrument_count=%s | mode=%s",
        len(body.instrument_keys),
        body.mode,
    )

    try:
        result = market.change_mode(
            body.instrument_keys,
            body.mode,
        )

        logger.info(
            "Subscription mode changed | "
            "instrument_count=%s | mode=%s",
            len(body.instrument_keys),
            body.mode,
        )

        return result

    except Exception as exc:
        logger.exception(
            "Subscription mode change failed | "
            "instrument_count=%s | mode=%s",
            len(body.instrument_keys),
            body.mode,
        )

        raise HTTPException(
            status_code=502,
            detail="Failed to change subscription mode",
        ) from exc


@app.get("/api/subscriptions")
def list_subscriptions() -> dict[str, Any]:
    try:
        active_subscriptions = database.list_active()
        recent_events = database.recent_events()

        logger.info(
            "Subscriptions listed | active_count=%s",
            len(active_subscriptions),
        )

        return {
            "active": active_subscriptions,
            "last_7_days": recent_events,
        }

    except Exception as exc:
        logger.exception(
            "Failed to list subscriptions"
        )

        raise HTTPException(
            status_code=502,
            detail="Failed to retrieve subscriptions",
        ) from exc


@app.post("/api/token/hard-refresh")
def hard_refresh() -> Any:
    logger.warning(
        "Manual token hard refresh requested"
    )

    try:
        result = (
            market.refresh_token_and_reconnect()
        )

        logger.info(
            "Manual token hard refresh completed"
        )

        return result

    except Exception as exc:
        logger.exception(
            "Manual token hard refresh failed"
        )

        raise HTTPException(
            status_code=502,
            detail="Token refresh failed",
        ) from exc


@app.websocket("/ws/market")
async def instrument_feed(
    websocket: WebSocket,
    instrument_key: str = Query(...),
) -> None:
    client_host = (
        websocket.client.host
        if websocket.client
        else "unknown"
    )

    logger.info(
        "WebSocket connection requested | "
        "instrument_key=%s | client=%s",
        instrument_key,
        client_host,
    )

    connected = False

    try:
        await hub.connect(
            instrument_key,
            websocket,
        )
        connected = True

        logger.info(
            "WebSocket connected | "
            "instrument_key=%s | client=%s",
            instrument_key,
            client_host,
        )

        while True:
            await websocket.receive_text()

    except WebSocketDisconnect:
        logger.info(
            "WebSocket disconnected | "
            "instrument_key=%s | client=%s",
            instrument_key,
            client_host,
        )

    except asyncio.CancelledError:
        logger.warning(
            "WebSocket task cancelled | "
            "instrument_key=%s | client=%s",
            instrument_key,
            client_host,
        )
        raise

    except Exception:
        logger.exception(
            "Unexpected WebSocket failure | "
            "instrument_key=%s | client=%s",
            instrument_key,
            client_host,
        )

        try:
            await websocket.close(
                code=1011,
                reason="Internal server error",
            )
        except Exception:
            logger.debug(
                "WebSocket was already closed | "
                "instrument_key=%s",
                instrument_key,
                exc_info=True,
            )

    finally:
        if connected:
            try:
                await hub.disconnect(
                    instrument_key,
                    websocket,
                )

                logger.info(
                    "WebSocket cleanup completed | "
                    "instrument_key=%s | client=%s",
                    instrument_key,
                    client_host,
                )

            except asyncio.CancelledError:
                logger.warning(
                    "WebSocket cleanup cancelled | "
                    "instrument_key=%s | client=%s",
                    instrument_key,
                    client_host,
                )
                raise

            except Exception:
                logger.exception(
                    "WebSocket cleanup failed | "
                    "instrument_key=%s | client=%s",
                    instrument_key,
                    client_host,
                )