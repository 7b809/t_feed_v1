import logging
from pathlib import Path

from core.config import get_settings


# ================================================================
# Log Root Directory
# ================================================================

_LOG_ROOT = Path(__file__).resolve().parents[1] / "logs"


# ================================================================
# Logger Factory
# ================================================================

def get_logger(filename: str) -> logging.Logger:
    """
    Create a logger that writes to both:

        logs/<filename>/<filename>.log

    and the terminal/console.

    Example:

        get_logger("api")

    creates:

        logs/api/api.log
    """

    # ------------------------------------------------------------
    # Safe logger/file name
    # ------------------------------------------------------------

    safe_name = Path(filename).stem.replace(" ", "_")

    # ------------------------------------------------------------
    # Log directory
    # ------------------------------------------------------------

    log_dir = _LOG_ROOT / safe_name
    log_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------
    # Log file
    # ------------------------------------------------------------

    log_file = log_dir / f"{safe_name}.log"

    # ------------------------------------------------------------
    # Logger
    # ------------------------------------------------------------

    logger = logging.getLogger(
        f"project_update_service.{safe_name}"
    )

    # ------------------------------------------------------------
    # Log level
    # ------------------------------------------------------------

    configured_level = get_settings().log_level.upper()

    logger.setLevel(
        getattr(
            logging,
            configured_level,
            logging.INFO,
        )
    )

    # ------------------------------------------------------------
    # Prevent messages from being handled by the root logger
    # ------------------------------------------------------------

    logger.propagate = False

    # ------------------------------------------------------------
    # Do not add duplicate handlers
    # ------------------------------------------------------------

    if not logger.handlers:

        # --------------------------------------------------------
        # Formatter
        # --------------------------------------------------------

        formatter = logging.Formatter(
            "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
        )

        # --------------------------------------------------------
        # File handler
        # --------------------------------------------------------

        file_handler = logging.FileHandler(
            log_file,
            encoding="utf-8",
        )

        file_handler.setFormatter(formatter)

        logger.addHandler(file_handler)

        # --------------------------------------------------------
        # Console / terminal handler
        # --------------------------------------------------------

        console_handler = logging.StreamHandler()

        console_handler.setFormatter(formatter)

        logger.addHandler(console_handler)

    # ------------------------------------------------------------
    # Return logger
    # ------------------------------------------------------------

    return logger