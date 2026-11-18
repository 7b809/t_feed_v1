"""Central console, rotating-file and plain-text logging."""
from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

from core.config import LOG_LEVEL, LOGS_DIR, LOGS_TEXT_DIR

_LEVEL = getattr(logging, LOG_LEVEL, logging.INFO)
_FORMAT = logging.Formatter(
    "%(asctime)s [%(levelname)s] [%(name)s] [%(filename)s:%(lineno)d] "
    "[%(funcName)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


def get_logger(name: str) -> logging.Logger:
    """Return a module logger with a console and two file destinations."""
    logger_name = Path(name).stem
    logger = logging.getLogger(logger_name)
    logger.setLevel(_LEVEL)
    logger.propagate = False
    if logger.handlers:
        return logger

    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_TEXT_DIR.mkdir(parents=True, exist_ok=True)

    console = logging.StreamHandler()
    console.setLevel(_LEVEL)
    console.setFormatter(_FORMAT)
    logger.addHandler(console)

    rotating = logging.handlers.RotatingFileHandler(
        LOGS_DIR / f"{logger_name}.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    rotating.setLevel(_LEVEL)
    rotating.setFormatter(_FORMAT)
    logger.addHandler(rotating)

    text_file = logging.FileHandler(
        LOGS_TEXT_DIR / f"{logger_name}.txt", mode="a", encoding="utf-8"
    )
    text_file.setLevel(_LEVEL)
    text_file.setFormatter(_FORMAT)
    logger.addHandler(text_file)
    return logger
