from threading import Thread

import uvicorn
from fastapi import FastAPI

from api.refresh_routes import configure_refresh_routes, router as refresh_router
from core import config
from core.logger import get_logger
from core.token_service import token_service
from services.runtime import EmaRuntime
from services.scheduler import Scheduler
from services.telegram_service import telegram_service

logger = get_logger(__file__)

app = FastAPI(
    title="NIFTY EMA Crossover Service",
    version="1.0.0",
)


def main() -> None:
    runtime = EmaRuntime(
        token_service,
        telegram_service,
    )

    scheduler = Scheduler(
        token_service,
        runtime,
        telegram_service,
    )

    scheduler_thread = Thread(
        target=scheduler.run,
        name="ema-scheduler",
        daemon=True,
    )

    configure_refresh_routes(scheduler)
    app.include_router(refresh_router)

    scheduler_thread.start()

    try:
        logger.info(
            "NIFTY EMA API starting on %s:%s",
            config.API_HOST,
            config.API_PORT,
        )

        uvicorn.run(
            app,
            host=config.API_HOST,
            port=config.API_PORT,
            log_level=config.LOG_LEVEL.lower(),
        )

    finally:
        scheduler.stop()
        scheduler_thread.join(timeout=10)

        runtime.close()
        token_service.close()

        telegram_service.send(
            "NIFTY EMA crossover service stopped"
        )

        logger.info(
            "NIFTY EMA crossover service stopped"
        )


if __name__ == "__main__":
    main()