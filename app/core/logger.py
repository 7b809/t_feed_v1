"""Centralized logging configuration."""
import logging
from logging.handlers import TimedRotatingFileHandler
from app.core.config import settings

def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)

def configure_logging() -> None:
    settings.LOG_DIR.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    if getattr(root, "_order_logging_configured", False):
        return
    root.setLevel(getattr(logging, settings.LOG_LEVEL, logging.INFO))
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    file_handler = TimedRotatingFileHandler(
        settings.LOG_DIR / "orders.log", when="midnight", backupCount=14, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    root.addHandler(console)
    root.addHandler(file_handler)
    root._order_logging_configured = True
