import os
import logging
from logging.handlers import RotatingFileHandler

# Directory where log files will be saved
LOGS_DIR = os.path.join(os.getcwd(), "logs")

# Ensure the logs directory exists
os.makedirs(LOGS_DIR, exist_ok=True)


def get_file_logger(file_path: str) -> logging.Logger:
    """
    Creates and returns a Logger instance that writes logs to a dedicated file in the `logs/` folder.

    Usage in any file:
        from app.utils.logger import get_file_logger
        logger = get_file_logger(__file__)

    If passed from `app/services/live_market_feed_service.py`,
    it will create a log file at `logs/live_market_feed_service.log`.
    """
    # Extract filename without extension (e.g., 'live_market_feed_service')
    base_name = os.path.splitext(os.path.basename(file_path))[0]

    logger = logging.getLogger(base_name)

    # Avoid adding multiple handlers if logger is already configured
    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)

    # Define log message format
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 1. Rotating File Handler (Saves to logs/<filename>.log)
    log_file_path = os.path.join(LOGS_DIR, f"{base_name}.log")
    file_handler = RotatingFileHandler(
        filename=log_file_path,
        maxBytes=10 * 1024 * 1024,  # 10 MB per file
        backupCount=5,  # Keep up to 5 backup files
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.INFO)
    logger.addHandler(file_handler)

    # 2. Console Handler (Prints logs to terminal as well)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.setLevel(logging.INFO)
    logger.addHandler(console_handler)

    return logger
