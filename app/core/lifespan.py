from contextlib import asynccontextmanager
from fastapi import FastAPI
from app.core.config import get_settings
from app.core.logger import configure_logging
from app.services.supervisor import Supervisor
@asynccontextmanager
async def lifespan(app: FastAPI):
    settings=get_settings(); configure_logging(settings)
    settings.files_dir.mkdir(parents=True, exist_ok=True)
    app.state.supervisor=Supervisor(settings); await app.state.supervisor.start()
    try: yield
    finally: await app.state.supervisor.stop()
