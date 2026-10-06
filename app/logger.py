import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path
from threading import Lock


LOG_DIRECTORY = Path("logs")
LOG_DIRECTORY.mkdir(parents=True, exist_ok=True)

LOG_FORMAT = (
    "%(asctime)s | %(levelname)-8s | %(name)s | "
    "%(filename)s:%(lineno)d | %(message)s"
)

DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

MAX_LOG_SIZE = 10 * 1024 * 1024
BACKUP_COUNT = 5

_formatter = logging.Formatter(
    fmt=LOG_FORMAT,
    datefmt=DATE_FORMAT,
)

_configuration_lock = Lock()
_project_logger_configured = False


def _sanitize_filename(filename: str) -> str:
    """
    Converts a Python filename or module path into a safe log filename.

    Examples:
        /project/app/main.py -> main
        app.upstox_service   -> app_upstox_service
    """
    path = Path(filename)

    if path.suffix:
        name = path.stem
    else:
        name = filename

    name = name.replace("\\", "_").replace("/", "_").replace(".", "_")
    name = re.sub(r"[^a-zA-Z0-9_-]", "_", name)

    return name or "application"


def _create_file_handler(log_path: Path) -> RotatingFileHandler:
    handler = RotatingFileHandler(
        filename=log_path,
        maxBytes=MAX_LOG_SIZE,
        backupCount=BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(_formatter)
    return handler


def _configure_project_logger() -> None:
    """
    Configures the root logger.

    Every child logger propagates its messages to this logger, so all
    application logs are written to logs/project.log in chronological order.
    """
    global _project_logger_configured

    if _project_logger_configured:
        return

    with _configuration_lock:
        if _project_logger_configured:
            return

        root_logger = logging.getLogger()
        root_logger.setLevel(logging.DEBUG)

        project_handler = _create_file_handler(
            LOG_DIRECTORY / "project.log"
        )
        project_handler.set_name("project_file_handler")
        root_logger.addHandler(project_handler)

        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.INFO)
        console_handler.setFormatter(_formatter)
        console_handler.set_name("project_console_handler")
        root_logger.addHandler(console_handler)

        _project_logger_configured = True


def get_logger(filename: str) -> logging.Logger:
    """
    Creates and returns a logger for a specific Python file.

    Logs are written to:
        logs/project.log
        logs/{filename}.log

    Usage:
        logger = get_logger(__file__)
    """
    _configure_project_logger()

    logger_name = _sanitize_filename(filename)
    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.DEBUG)

    module_handler_name = f"{logger_name}_file_handler"

    has_module_handler = any(
        handler.get_name() == module_handler_name
        for handler in logger.handlers
    )

    if not has_module_handler:
        with _configuration_lock:
            has_module_handler = any(
                handler.get_name() == module_handler_name
                for handler in logger.handlers
            )

            if not has_module_handler:
                module_handler = _create_file_handler(
                    LOG_DIRECTORY / f"{logger_name}.log"
                )
                module_handler.set_name(module_handler_name)
                logger.addHandler(module_handler)

    # Propagation sends the same log to the root logger,
    # which writes it to project.log and the console.
    logger.propagate = True

    return logger