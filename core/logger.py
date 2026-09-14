import logging
from pathlib import Path

from core.config import get_settings


_LOG_ROOT = Path(__file__).resolve().parents[1] / "logs"


def get_logger(filename: str) -> logging.Logger:
    """
    Write logs to:
        logs/<filename>/<filename>.log

    Also print every log message to the terminal/console.
    """

    safe_name = Path(filename).stem.replace(" ", "_")

    log_dir = _LOG_ROOT / safe_name
    log_dir.mkdir(parents=True, exist_ok=True)

    log_file = log_dir / f"{safe_name}.log"

    logger = logging.getLogger(
        f"project_update_service.{safe_name}"
    )

    logger.setLevel(
        getattr(
            get_settings().log_level.upper(),
            logging.INFO,
        )
    )

    logger.propagate = False

    if not logger.handlers:

        # ========================================================
        # Common formatter
        # ========================================================

        formatter = logging.Formatter(
            "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
        )

        # ========================================================
        # File handler
        # ========================================================

        file_handler = logging.FileHandler(
            log_file,
            encoding="utf-8",
        )

        file_handler.setFormatter(formatter)

        logger.addHandler(file_handler)

        # ========================================================
        # Console / Terminal handler
        # ========================================================

        console_handler = logging.StreamHandler()

        console_handler.setFormatter(formatter)

        logger.addHandler(console_handler)

    return logger
