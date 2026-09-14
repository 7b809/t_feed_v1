import logging
from pathlib import Path
from core.config import get_settings

_LOG_ROOT = Path(__file__).resolve().parents[1] / "logs"

def get_logger(filename: str) -> logging.Logger:
    """Write logs only to logs/<filename>/<filename>.log."""
    safe_name = Path(filename).stem.replace(" ", "_")
    log_dir = _LOG_ROOT / safe_name
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"{safe_name}.log"
    logger = logging.getLogger(f"project_update_service.{safe_name}")
    logger.setLevel(getattr(logging, get_settings().log_level.upper(), logging.INFO))
    logger.propagate = False
    if not logger.handlers:
        handler = logging.FileHandler(log_file, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s"))
        logger.addHandler(handler)
    return logger
