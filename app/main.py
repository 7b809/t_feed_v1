import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from time import perf_counter
from typing import Any, AsyncIterator, Callable
from urllib.parse import quote

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
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates

from app.candles import CandleAggregator, VALID_INTERVALS
from app.config import settings
from app.database import Database
from app.logger import get_logger
from app.models import SubscriptionRequest
from app.upstox_service import UpstoxMarketService
from app.websockets import WebSocketHub

logger = get_logger(__file__)

database = Database()
hub = WebSocketHub()
candle_aggregator = CandleAggregator()
market = UpstoxMarketService(database, hub, candle_aggregator)

scheduler = AsyncIOScheduler(timezone=settings.token_refresh_timezone)

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# ============================================================
# PUBLIC CONNECT URL HELPERS
# ------------------------------------------------------------
# Builds the client-facing WebSocket URL for a given
# instrument key, e.g.
#
#   wss://feed.novag7.in/ws/market?instrument_key=BSE_FO%7C1101001
#
# Resolution order for the base URL:
#   1. settings.public_ws_base_url  (if configured)
#   2. X-Forwarded-Proto / X-Forwarded-Host / Host headers
#      (so it works correctly behind nginx / a reverse proxy)
#   3. request.url scheme + netloc as a last resort
# ============================================================

WS_MARKET_PATH = "/ws/market"


def resolve_ws_base_url(request: Request) -> str:
    configured = getattr(settings, "public_ws_base_url", None)

    if configured:
        return str(configured).rstrip("/")

    forwarded_proto = request.headers.get("x-forwarded-proto")
    scheme = (
        forwarded_proto.split(",")[0].strip()
        if forwarded_proto
        else request.url.scheme
    )

    ws_scheme = "wss" if scheme in ("https", "wss") else "ws"

    forwarded_host = request.headers.get("x-forwarded-host")
    host = (
        forwarded_host.split(",")[0].strip()
        if forwarded_host
        else request.headers.get("host")
    ) or request.url.netloc

    return f"{ws_scheme}://{host}"


def build_connect_url(base_url: str, instrument_key: str) -> str:
    return (
        f"{base_url}{WS_MARKET_PATH}"
        f"?instrument_key={quote(instrument_key, safe='')}"
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

        if settings.token_refresh_enabled:
            scheduler.add_job(
                market.refresh_token_and_reconnect,
                CronTrigger(
                    day_of_week=settings.token_refresh_day_of_week,
                    hour=settings.token_refresh_hour,
                    minute=settings.token_refresh_minute,
                    timezone=settings.token_refresh_timezone,
                ),
                id="token_refresh",
                replace_existing=True,
                coalesce=True,
                max_instances=1,
            )

            logger.info(
                "Scheduler configured | token_refresh_schedule=%s %02d:%02d %s",
                settings.token_refresh_day_of_week,
                settings.token_refresh_hour,
                settings.token_refresh_minute,
                settings.token_refresh_timezone,
            )
        else:
            logger.info(
                "Token refresh schedule disabled via TOKEN_REFRESH_ENABLED"
            )

        scheduler.start()
        scheduler_started = True

        logger.info("Scheduler started")
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
                logger.exception("Failed to stop Upstox market service cleanly")

        try:
            database.close()
            logger.info("Database connection closed")
        except Exception:
            logger.exception("Failed to close database connection cleanly")

        logger.info("Application shutdown completed")


app = FastAPI(
    title="Upstox Market Stream Gateway",
    version="1.0.0",
    lifespan=lifespan,
)

# ============================================================
# CORS — DUMMY / TESTING STAGE: ALLOW EVERYTHING
# ------------------------------------------------------------
# NOTE:
#   * allow_origins=["*"] MUST NOT be combined with
#     allow_credentials=True — per the CORS spec, browsers
#     silently reject the response. We therefore keep
#     allow_credentials=False so the wildcard actually works.
#   * This is intentionally permissive and is ONLY safe for a
#     dummy / staging environment. Tighten allow_origins to an
#     explicit list before going to production.
#   * CORSMiddleware does NOT apply to WebSocket connections.
#     Browsers do not enforce CORS on WS handshakes, so the
#     /ws/market, /all-feeds, /ws/ema-crossover, etc. endpoints
#     will work cross-origin without extra configuration.
# ============================================================
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
    max_age=600,
)


@app.middleware("http")
async def request_logging_middleware(
    request: Request,
    call_next: Callable[..., Any],
) -> Response:
    start_time = perf_counter()
    client_host = request.client.host if request.client else "unknown"

    logger.info(
        "HTTP request started | method=%s | path=%s | client=%s",
        request.method,
        request.url.path,
        client_host,
    )

    try:
        response = await call_next(request)

        duration_ms = (perf_counter() - start_time) * 1000

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
        duration_ms = (perf_counter() - start_time) * 1000

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
            "HTTP exception | method=%s | path=%s | " "status=%s | detail=%s",
            request.method,
            request.url.path,
            exc.status_code,
            exc.detail,
        )
    else:
        logger.warning(
            "HTTP exception | method=%s | path=%s | " "status=%s | detail=%s",
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
            "active_subscriptions": len(active_subscriptions),
            "websocket_clients": hub.client_counts(),
            "valid_candle_intervals": sorted(VALID_INTERVALS),
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
        "Instrument search requested | query=%s | " "page=%s | records=%s",
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
        result = await market.search_instruments(search_parameters)

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


# ============================================================
# Intraday candles
# ------------------------------------------------------------
# Two ways to call this endpoint:
#
#   1. Direct instrument key:
#      /api/candles/intraday?instrument_key=NSE_FO%7C47142&interval=5
#
#   2. Resolved option contract:
#      /api/candles/intraday
#          ?underlying=NSE_INDEX%7CNifty%2050
#          &strike=22000
#          &strike_type=CE
#          &expiry=2026-10-30
#          &interval=5
#
# Response body:
#
#   {
#     "instrument_key": "NSE_FO|47142",
#     "interval": 5,
#     "candles": [
#       ["2026-10-14T14:35:00+05:30", o, h, l, c, v, oi],
#       ...
#     ]
#   }
#
# The `candles` rows are returned in DESCENDING order (newest first),
# exactly as delivered by the Upstox V3 intraday endpoint.
# ============================================================
@app.get("/api/candles/intraday")
async def get_intraday_candles(
    instrument_key: str | None = Query(
        None,
        description=(
            "Direct instrument key, e.g. NSE_FO|47142. "
            "If provided, the option-resolution params are ignored."
        ),
    ),
    underlying: str | None = Query(
        None,
        description=(
            "Underlying instrument key for option lookup, "
            "e.g. NSE_INDEX|Nifty 50 or BSE_INDEX|SENSEX."
        ),
    ),
    strike: float | None = Query(
        None,
        description="Option strike price. Required when resolving by option.",
    ),
    strike_type: str | None = Query(
        None,
        description="Option type: CE or PE.",
    ),
    expiry: str | None = Query(
        None,
        description=(
            "Option expiry date (YYYY-MM-DD). Optional for option "
            "resolution; when omitted the API returns the first match."
        ),
    ),
    interval: int = Query(
        1,
        description=(
            f"Candle interval in minutes. Allowed: {sorted(VALID_INTERVALS)}"
        ),
    ),
) -> dict[str, Any]:
    if interval not in VALID_INTERVALS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unsupported interval {interval}; "
                f"allowed: {sorted(VALID_INTERVALS)}"
            ),
        )

    resolved_key = (instrument_key or "").strip()

    if not resolved_key:
        # Resolve via underlying + strike + strike_type (+ optional expiry)
        if not underlying or strike is None or not strike_type:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Provide either 'instrument_key' directly, or "
                    "'underlying' + 'strike' + 'strike_type' "
                    "(+ optional 'expiry') to resolve an option contract."
                ),
            )

        try:
            resolved_key = await asyncio.to_thread(
                market.resolve_option_instrument,
                underlying.strip(),
                float(strike),
                strike_type.strip(),
                expiry.strip() if expiry else None,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=404,
                detail=str(exc),
            ) from exc
        except Exception as exc:
            logger.exception(
                "Option resolution failed | underlying=%s | strike=%s | "
                "strike_type=%s | expiry=%s",
                underlying,
                strike,
                strike_type,
                expiry,
            )
            raise HTTPException(
                status_code=502,
                detail="Option resolution service failed",
            ) from exc

    logger.info(
        "Intraday candles requested | instrument_key=%s | interval=%s",
        resolved_key,
        interval,
    )

    try:
        candles = await asyncio.to_thread(
            market.fetch_intraday_candles,
            resolved_key,
            interval,
        )
    except Exception as exc:
        logger.exception(
            "Failed to fetch intraday candles | instrument_key=%s | "
            "interval=%s",
            resolved_key,
            interval,
        )
        raise HTTPException(
            status_code=502,
            detail="Failed to fetch intraday candles",
        ) from exc

    return {
        "instrument_key": resolved_key,
        "interval": interval,
        "candles": candles,
    }


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
        result = market.unsubscribe(body.instrument_keys)

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
        "Subscription mode change requested | " "instrument_count=%s | mode=%s",
        len(body.instrument_keys),
        body.mode,
    )

    try:
        result = market.change_mode(
            body.instrument_keys,
            body.mode,
        )

        logger.info(
            "Subscription mode changed | " "instrument_count=%s | mode=%s",
            len(body.instrument_keys),
            body.mode,
        )

        return result

    except Exception as exc:
        logger.exception(
            "Subscription mode change failed | " "instrument_count=%s | mode=%s",
            len(body.instrument_keys),
            body.mode,
        )

        raise HTTPException(
            status_code=502,
            detail="Failed to change subscription mode",
        ) from exc


@app.get("/api/subscriptions")
def list_subscriptions(request: Request) -> dict[str, Any]:
    try:
        active_subscriptions = database.list_active()
        recent_events = database.recent_events()

        ws_base_url = resolve_ws_base_url(request)

        enriched_active = [
            {
                **subscription,
                "connect_url": build_connect_url(
                    ws_base_url,
                    subscription["instrument_key"],
                ),
                "candle_intervals": sorted(
                    candle_aggregator.active_intervals(
                        subscription["instrument_key"]
                    )
                ),
            }
            for subscription in active_subscriptions
        ]

        logger.info(
            "Subscriptions listed | active_count=%s | ws_base_url=%s",
            len(enriched_active),
            ws_base_url,
        )

        return {
            "ws_base_url": ws_base_url,
            "valid_candle_intervals": sorted(VALID_INTERVALS),
            "active": enriched_active,
            "last_7_days": recent_events,
        }

    except Exception as exc:
        logger.exception("Failed to list subscriptions")

        raise HTTPException(
            status_code=502,
            detail="Failed to retrieve subscriptions",
        ) from exc


@app.post("/api/token/hard-refresh")
def hard_refresh() -> Any:
    logger.warning("Manual token hard refresh requested")

    try:
        result = market.refresh_token_and_reconnect()

        logger.info("Manual token hard refresh completed")

        return result

    except Exception as exc:
        logger.exception("Manual token hard refresh failed")

        raise HTTPException(
            status_code=502,
            detail="Token refresh failed",
        ) from exc


@app.websocket("/ws/market")
async def instrument_feed(
    websocket: WebSocket,
    instrument_key: str = Query(...),
    interval: str | None = Query(None),
) -> None:
    """
    Per-instrument WebSocket endpoint.

    Query params:
        instrument_key  (required)  e.g. NSE_FO|47142
        interval        (optional)  one of 1, 3, 5, 15, 30, 60

    Behaviour:
        * interval absent → the client receives the raw full-feed ticks
          (identical to the previous behaviour of this endpoint).
        * interval present → the client receives candle payloads in
          the form:
              [timestamp_ist, open, high, low, close, volume, oi]

    Multiple clients may connect to the same instrument with different
    intervals simultaneously; each (instrument_key, interval) pair is
    tracked independently by the CandleAggregator.
    """
    client_host = websocket.client.host if websocket.client else "unknown"

    # -------- Parse & validate the optional interval -----------------
    parsed_interval: int | None = None

    if interval is not None and interval.strip():
        try:
            parsed_interval = int(interval.strip())
        except ValueError:
            logger.warning(
                "WebSocket connection rejected — non-integer interval | "
                "instrument_key=%s | interval=%r | client=%s",
                instrument_key,
                interval,
                client_host,
            )
            try:
                await websocket.close(
                    code=1008,
                    reason="interval must be an integer",
                )
            except Exception:
                pass
            return

        if parsed_interval not in VALID_INTERVALS:
            logger.warning(
                "WebSocket connection rejected — unsupported interval | "
                "instrument_key=%s | interval=%s | allowed=%s | client=%s",
                instrument_key,
                parsed_interval,
                sorted(VALID_INTERVALS),
                client_host,
            )
            try:
                await websocket.close(
                    code=1008,
                    reason=(
                        f"unsupported interval {parsed_interval}; "
                        f"allowed: {sorted(VALID_INTERVALS)}"
                    ),
                )
            except Exception:
                pass
            return

    is_candle = parsed_interval is not None
    channel = (
        instrument_key
        if not is_candle
        else f"{instrument_key}|interval={parsed_interval}"
    )

    logger.info(
        "WebSocket connection requested | instrument_key=%s | "
        "interval=%s | channel=%s | client=%s",
        instrument_key,
        parsed_interval if is_candle else "raw",
        channel,
        client_host,
    )

    # -------- Candle-specific setup ----------------------------------
    candle_registered = False

    if is_candle:
        try:
            market.ensure_full_subscription(instrument_key)
        except Exception:
            logger.exception(
                "Failed to ensure upstream subscription for candle feed | "
                "instrument_key=%s",
                instrument_key,
            )

        try:
            candle_aggregator.register(instrument_key, parsed_interval)
            candle_registered = True
        except Exception:
            logger.exception(
                "Failed to register candle subscription | "
                "instrument_key=%s | interval=%s",
                instrument_key,
                parsed_interval,
            )

    # -------- Connect ------------------------------------------------
    connected = False

    try:
        await hub.connect(channel, websocket)
        connected = True

        logger.info(
            "WebSocket connected | instrument_key=%s | interval=%s | "
            "channel=%s | client=%s",
            instrument_key,
            parsed_interval if is_candle else "raw",
            channel,
            client_host,
        )

        while True:
            await websocket.receive_text()

    except WebSocketDisconnect:
        logger.info(
            "WebSocket disconnected | instrument_key=%s | interval=%s | "
            "client=%s",
            instrument_key,
            parsed_interval if is_candle else "raw",
            client_host,
        )

    except asyncio.CancelledError:
        logger.warning(
            "WebSocket task cancelled | instrument_key=%s | interval=%s | "
            "client=%s",
            instrument_key,
            parsed_interval if is_candle else "raw",
            client_host,
        )
        raise

    except Exception:
        logger.exception(
            "Unexpected WebSocket failure | instrument_key=%s | "
            "interval=%s | client=%s",
            instrument_key,
            parsed_interval if is_candle else "raw",
            client_host,
        )

        try:
            await websocket.close(
                code=1011,
                reason="Internal server error",
            )
        except Exception:
            logger.debug(
                "WebSocket was already closed | instrument_key=%s",
                instrument_key,
                exc_info=True,
            )

    finally:
        if connected:
            try:
                await hub.disconnect(channel, websocket)

                logger.info(
                    "WebSocket cleanup completed | instrument_key=%s | "
                    "interval=%s | client=%s",
                    instrument_key,
                    parsed_interval if is_candle else "raw",
                    client_host,
                )

            except asyncio.CancelledError:
                logger.warning(
                    "WebSocket cleanup cancelled | instrument_key=%s | "
                    "interval=%s | client=%s",
                    instrument_key,
                    parsed_interval if is_candle else "raw",
                    client_host,
                )
                raise

            except Exception:
                logger.exception(
                    "WebSocket cleanup failed | instrument_key=%s | "
                    "interval=%s | client=%s",
                    instrument_key,
                    parsed_interval if is_candle else "raw",
                    client_host,
                )

        if candle_registered:
            try:
                candle_aggregator.unregister(instrument_key, parsed_interval)
            except Exception:
                logger.exception(
                    "Failed to unregister candle subscription | "
                    "instrument_key=%s | interval=%s",
                    instrument_key,
                    parsed_interval,
                )