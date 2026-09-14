from contextlib import asynccontextmanager

from fastapi import FastAPI

from api.routes import router
from core.config import get_settings
from core.logger import get_logger
from services.notifier import TelegramNotifier
from services.update_runner import UpdateManager
from telegram_bot.bot import ProjectUpdateBot


# ============================================================
# Configuration
# ============================================================

settings = get_settings()


# ============================================================
# Loggers
# ============================================================

logger = get_logger("app")


# ============================================================
# Services
# ============================================================

notifier = TelegramNotifier(settings)

manager = UpdateManager(
    settings,
    notifier,
)

bot = ProjectUpdateBot(
    settings,
    manager,
)


# ============================================================
# Application Lifecycle
# ============================================================

@asynccontextmanager
async def lifespan(app: FastAPI):

    # ========================================================
    # STARTUP
    # ========================================================

    logger.info("==========================================")
    logger.info("FastAPI service starting")
    logger.info("Application: %s", settings.app_name)
    logger.info("Host: %s", settings.host)
    logger.info("Port: %s", settings.port)
    logger.info("Projects: %s", list(settings.projects.keys()))
    logger.info("==========================================")

    app.state.update_manager = manager

    # --------------------------------------------------------
    # Start Telegram bot
    # --------------------------------------------------------

    try:

        await bot.start()

        logger.info(
            "Telegram bot started"
        )

    except Exception:

        logger.exception(
            "Failed to start Telegram bot"
        )

    # --------------------------------------------------------
    # Send startup notification
    # --------------------------------------------------------

    try:

        projects = "\n".join(
            f"• {name}"
            for name in sorted(settings.projects)
        )

        startup_message = (
            f"🚀 {settings.app_name} Started\n\n"
            f"Host: {settings.host}\n"
            f"Port: {settings.port}\n\n"
            f"Projects:\n"
            f"{projects}"
        )

        await notifier.send(
            startup_message
        )

        logger.info(
            "Startup Telegram notification sent"
        )

    except Exception:

        logger.exception(
            "Failed to send startup Telegram notification"
        )

    logger.info(
        "Service started successfully"
    )

    # ========================================================
    # APPLICATION RUNNING
    # ========================================================

    yield

    # ========================================================
    # SHUTDOWN
    # ========================================================

    logger.info(
        "Service shutdown initiated"
    )

    # --------------------------------------------------------
    # Send shutdown notification BEFORE stopping Telegram
    # --------------------------------------------------------

    try:

        shutdown_message = (
            f"🛑 {settings.app_name} Stopped\n\n"
            f"Host: {settings.host}\n"
            f"Port: {settings.port}"
        )

        await notifier.send(
            shutdown_message
        )

        logger.info(
            "Shutdown Telegram notification sent"
        )

    except Exception:

        logger.exception(
            "Failed to send shutdown Telegram notification"
        )

    # --------------------------------------------------------
    # Stop Telegram bot
    # --------------------------------------------------------

    try:

        await bot.stop()

        logger.info(
            "Telegram bot stopped"
        )

    except Exception:

        logger.exception(
            "Failed to stop Telegram bot"
        )

    logger.info(
        "Service stopped"
    )

    logger.info(
        "=========================================="
    )


# ============================================================
# FastAPI Application
# ============================================================

app = FastAPI(
    title=settings.app_name,
    version="1.0.0",
    lifespan=lifespan,
)


# ============================================================
# API Routes
# ============================================================

app.include_router(router)


# ============================================================
# Health Check
# ============================================================

@app.get("/health")
async def health():
    return {
        "status": "ok"
    }