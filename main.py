"""
main.py
Application entrypoint. Composition + uvicorn only.

Everything else lives in core/, token_tasks/, upstox_app/, api/, or web/:
    - startup / shutdown wiring ......... core/lifespan.py
    - health + root endpoints ........... core/health.py
    - MongoDB singleton ................. core/config.py
    - logging setup ..................... core/logger.py
    - token cache / save / refresh ...... token_tasks/service.py
    - token HTTP API .................... token_tasks/router.py
    - 30-min refresh loop ............... token_tasks/scheduler.py
    - Upstox validation ................. upstox_app/get_profile_status.py
    - Upstox streamers HTTP API ......... upstox_app/router.py
    - Downstream WebSocket .............. upstox_app/ws_router.py
    - Option contracts .................. upstox_app/option_router.py
                                          + option_service.py
                                          + option_storage.py
    - Instruments API ................... api/instruments_api.py
    - Instrument Search API ............. upstox_app/instruments_search/
    - Web UI (templates + APIs + WS) .... web/page_router.py
                                          web/api_router.py
                                          web/ws_router.py
"""
from fastapi import FastAPI

from core.config import core_config
from core.health import router as health_router
from core.lifespan import app_lifespan
from core.logger import get_logger, setup_logging
from token_tasks.router import router as token_router
from upstox_app.option.option_router import router as options_router
from upstox_app.api.router import router as upstox_router
from upstox_app.streamer.ws_router import router as upstox_ws_router
from upstox_app.candle.candle_router import router as candle_router
from upstox_app.market_quote.quote_router import router as quote_router

# ---- Instrument Search (separate service) ----------------------------------
from upstox_app.instruments_search.router import router as instruments_search_router

from api import api_router

# ---- Web UI routers (templates + JSON APIs + WebSockets) -------------------
from web.page_router import router as web_page_router
from web.api_router import router as web_api_router
from web.ws_router import router as web_ws_router


# Configure logging before anything else
setup_logging()
logger = get_logger(__name__)


def create_app() -> FastAPI:
    """Build and return the FastAPI application."""
    logger.info(
        "Building FastAPI app | name=%s | version=%s",
        core_config.APP_NAME,
        core_config.APP_VERSION,
    )

    app = FastAPI(
        title=core_config.APP_NAME,
        version=core_config.APP_VERSION,
        lifespan=app_lifespan,
    )

    # ---- Web UI pages FIRST so GET / serves index.html --------------------
    app.include_router(web_page_router)

    # ---- Existing routers -------------------------------------------------
    app.include_router(health_router)
    app.include_router(token_router)
    app.include_router(upstox_router)
    app.include_router(upstox_ws_router)
    app.include_router(options_router)
    app.include_router(candle_router)
    app.include_router(quote_router)
    app.include_router(api_router)

    # ---- Instrument Search API --------------------------------------------
    app.include_router(instruments_search_router)

    # ---- Web UI JSON APIs + WebSockets ------------------------------------
    app.include_router(web_api_router)
    app.include_router(web_ws_router)

    logger.info(
        "Routers registered | web-pages + health + token + upstox + "
        "upstox-ws + options + candles + quotes + api + instruments-search + "
        "web(api/ws)"
    )
    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    logger.info(
        "Starting uvicorn | host=%s | port=%s | reload=%s",
        core_config.APP_HOST,
        core_config.APP_PORT,
        core_config.DEBUG,
    )
    uvicorn.run(
        "main:app",
        host=core_config.APP_HOST,
        port=core_config.APP_PORT,
        reload=core_config.DEBUG,
    )