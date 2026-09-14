from contextlib import asynccontextmanager
from fastapi import FastAPI
from api.routes import router
from core.config import get_settings
from core.logger import get_logger
from services.notifier import TelegramNotifier
from services.update_runner import UpdateManager
from telegram_bot.bot import ProjectUpdateBot

settings = get_settings()
logger = get_logger("app")
notifier = TelegramNotifier(settings)
manager = UpdateManager(settings, notifier)
bot = ProjectUpdateBot(settings, manager)

@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.update_manager = manager
    await bot.start()
    logger.info("Service started")
    yield
    await bot.stop()
    logger.info("Service stopped")

app = FastAPI(title=settings.app_name, version="1.0.0", lifespan=lifespan)
app.include_router(router)

@app.get("/health")
async def health():
    return {"status": "ok"}
