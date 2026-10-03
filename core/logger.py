
"""
core/logger.py

Central logging configuration.

Every module imports:

    from core.logger import get_logger

    logger = get_logger(__name__)

Responsibilities:
    - Configure console logging.
    - Configure rotating file logging.
    - Use the application timezone from core.config.
    - Include filename, line number, and function name.
    - Keep noisy third-party loggers under control.
"""

import logging
import os
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler

from core.config import core_config


# =============================================================
# Module State
# =============================================================

_CONFIGURED = False


# =============================================================
# Timezone-aware Formatter
# =============================================================


class ISTFormatter(logging.Formatter):
    """
    Logging formatter that uses the application's configured
    timezone instead of the operating system timezone.

    The timezone is provided by:

        core_config.TIMEZONE

    Example output:

        2026-10-03 22:47:21 IST | INFO     | main | main.py:40
        | create_app() | Building FastAPI app
    """

    def formatTime(
        self,
        record: logging.LogRecord,
        datefmt: str | None = None,
    ) -> str:
        """
        Convert the log timestamp into the configured application
        timezone.
        """

        dt = datetime.fromtimestamp(
            record.created,
            tz=core_config.TIMEZONE,
        )

        if datefmt:
            timestamp = dt.strftime(datefmt)
        else:
            timestamp = dt.isoformat()

        return f"{timestamp} {core_config.TIMEZONE_ABBR}"


# =============================================================
# Logging Setup
# =============================================================


def setup_logging() -> None:
    """
    Configure root logger with:

        - Console handler
        - Rotating file handler

    The logger uses the application's configured timezone.
    """

    global _CONFIGURED

    if _CONFIGURED:
        return

    # ---------------------------------------------------------
    # Log format
    # ---------------------------------------------------------

    log_format = (
        "%(asctime)s | "
        "%(levelname)-8s | "
        "%(name)s | "
        "%(filename)s:%(lineno)d | "
        "%(funcName)s() | "
        "%(message)s"
    )

    formatter = ISTFormatter(
        log_format,
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # ---------------------------------------------------------
    # Root logger
    # ---------------------------------------------------------

    root = logging.getLogger()

    root.setLevel(
        core_config.LOG_LEVEL.upper()
    )

    # ---------------------------------------------------------
    # Console handler
    # ---------------------------------------------------------

    console_handler = logging.StreamHandler(
        sys.stdout
    )

    console_handler.setFormatter(
        formatter
    )

    root.addHandler(
        console_handler
    )

    # ---------------------------------------------------------
    # File handler
    # ---------------------------------------------------------

    try:
        os.makedirs(
            core_config.LOG_DIR,
            exist_ok=True,
        )

        file_handler = RotatingFileHandler(
            os.path.join(
                core_config.LOG_DIR,
                core_config.LOG_FILE,
            ),
            maxBytes=5 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )

        file_handler.setFormatter(
            formatter
        )

        root.addHandler(
            file_handler
        )

    except Exception as exc:
        root.warning(
            "File logging disabled: %s",
            exc,
        )

    # ---------------------------------------------------------
    # Quiet noisy third-party loggers
    # ---------------------------------------------------------

    logging.getLogger(
        "pymongo"
    ).setLevel(
        logging.WARNING
    )

    logging.getLogger(
        "uvicorn.access"
    ).setLevel(
        logging.WARNING
    )

    # ---------------------------------------------------------
    # Mark logging as configured
    # ---------------------------------------------------------

    _CONFIGURED = True


# =============================================================
# Logger Factory
# =============================================================


def get_logger(
    name: str,
) -> logging.Logger:
    """
    Return a configured logger.

    Safe to call before setup_logging().
    """

    if not _CONFIGURED:
        setup_logging()

    return logging.getLogger(name)