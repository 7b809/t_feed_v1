"""
core/health.py
Root + health routers. No business logic — just status probes.
"""
from fastapi import APIRouter

from core.config import core_config, mongo_manager
from core.logger import get_logger
from token_tasks import scheduler
from token_tasks.service import token_service

logger = get_logger(__name__)

router = APIRouter(tags=["health"])


@router.get("/")
def root() -> dict:
    logger.info("GET /")
    return {
        "app": core_config.APP_NAME,
        "version": core_config.APP_VERSION,
        "status": "ok",
    }


@router.get("/health")
def health() -> dict:
    logger.info("GET /health")
    return {
        "status": "ok",
        "mongo": mongo_manager.ping(),
        "scheduler_running": scheduler.is_running(),
        "token_cache": token_service.get_cache_status(),
    }