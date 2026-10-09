import logging
from logging.handlers import RotatingFileHandler

from app.core.config import settings


def configure_logging() -> None:
    settings.log_file.parent.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
    )

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    file_handler = RotatingFileHandler(
        settings.log_file,
        maxBytes=5_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.setLevel(settings.log_level)

    # Avoid duplicate handlers during development reloads/imports.
    if not any(getattr(h, "name", None) == "app-console" for h in root_logger.handlers):
        console_handler.name = "app-console"
        root_logger.addHandler(console_handler)

    if not any(getattr(h, "name", None) == "app-file" for h in root_logger.handlers):
        file_handler.name = "app-file"
        root_logger.addHandler(file_handler)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
