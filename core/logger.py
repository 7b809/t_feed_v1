import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from core.config import settings

LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)


class PrintHandler(logging.Handler):
    """Print every log message using print()."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
            print(message, flush=True)
        except Exception:
            self.handleError(record)


def get_logger(log_filename: str) -> logging.Logger:
    """
    Return a logger that writes to logs/{log_filename}.

    When PRINT_FLAG=true, every log is also sent through print().
    """

    safe_name = Path(log_filename).name

    if not safe_name.endswith(".log"):
        safe_name += ".log"

    logger_name = f"upstox_order_receiver.{safe_name}"

    logger = logging.getLogger(logger_name)

    logger.setLevel(
        getattr(
            logging,
            settings.log_level.upper(),
            logging.INFO,
        )
    )

    logger.propagate = False

    if logger.handlers:
        return logger

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # ------------------------------------------------------------
    # File logging
    # ------------------------------------------------------------

    file_handler = RotatingFileHandler(
        LOG_DIR / safe_name,
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )

    file_handler.setFormatter(formatter)

    logger.addHandler(file_handler)

    # ------------------------------------------------------------
    # Console / print logging
    # ------------------------------------------------------------

    if settings.print_flag:
        print_handler = PrintHandler()
        print_handler.setFormatter(formatter)
        logger.addHandler(print_handler)

    return logger
