"""Logging configuration.

Two layers of file logging:

1. Combined project log:
   Every record from every module is written to settings.log_file
   (default logs/app.log). The same records also go to the console.

2. Per-module logs:
   Each module gets its own rotating log file inside
   settings.module_log_dir (default logs/modules/).

   Example:
       app.services.ema_cross_store
       -> logs/modules/ema_cross_store.log

3. JSON logging:
   - json_log() renders objects as JSON.
   - Optional character limits prevent bulk log output.
   - Large JSON values are truncated with an explicit marker.
   - json_log_preview() provides a compact preview for lists/dicts.

4. Rotation:
   Each log file has a configurable maximum size and backup count.

Records are emitted to the module file and propagate to the root logger,
so they also appear in the combined project log.

Disable per-module files by setting MODULE_LOGS_ENABLED=false.
"""

from __future__ import annotations

import json
import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from app.core.config import settings


# ---------------------------------------------------------------------------
# Shared formatting
# ---------------------------------------------------------------------------

_LOG_FORMAT = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
_FORMATTER = logging.Formatter(_LOG_FORMAT)

_MAX_BYTES = 5_000_000
_BACKUP_COUNT = 3

# Maximum characters emitted by json_log().
# Set to 0 to disable truncation.
JSON_LOG_MAX_CHARS = 3000

# Maximum number of items displayed by json_log_preview().
# Set to 0 to display no list items.
JSON_LOG_PREVIEW_LIMIT = 3

_TRUNCATION_MARKER = "... [TRUNCATED: remaining output omitted]"

# Track which module loggers already have a per-module file handler.
_MODULE_HANDLERS_ATTACHED: set[str] = set()


# ---------------------------------------------------------------------------
# JSON logging helpers
# ---------------------------------------------------------------------------

def json_log(
    value: Any,
    max_chars: int = JSON_LOG_MAX_CHARS,
) -> str:
    """Render a value as compact JSON with an optional output limit.

    Args:
        value: Object to serialize.
        max_chars: Maximum output characters. Zero disables truncation.

    Uses default=str for datetime, Path and other unsupported values.
    Falls back to repr if serialization fails.
    """

    try:
        formatted = json.dumps(
            value,
            default=str,
            ensure_ascii=False,
        )
    except Exception:
        formatted = repr(value)

    if max_chars and max_chars > 0 and len(formatted) > max_chars:
        marker = (
            f"... [TRUNCATED: "
            f"{len(formatted) - max_chars} characters omitted]"
        )

        available_chars = max(
            0,
            max_chars - len(marker),
        )

        return formatted[:available_chars] + marker

    return formatted


def json_log_pretty(
    value: Any,
    max_chars: int = JSON_LOG_MAX_CHARS,
) -> str:
    """Render a value as indented JSON with an optional output limit."""

    try:
        formatted = json.dumps(
            value,
            indent=2,
            default=str,
            ensure_ascii=False,
        )
    except Exception:
        formatted = repr(value)

    if max_chars and max_chars > 0 and len(formatted) > max_chars:
        marker = (
            f"\n... [TRUNCATED: "
            f"{len(formatted) - max_chars} characters omitted]"
        )

        available_chars = max(
            0,
            max_chars - len(marker),
        )

        return formatted[:available_chars] + marker

    return formatted


def json_log_preview(
    value: Any,
    limit: int = JSON_LOG_PREVIEW_LIMIT,
    max_chars: int = JSON_LOG_MAX_CHARS,
) -> str:
    """Render a compact summary of a large list or dictionary.

    Lists:
        Shows total count and the first `limit` items.

    Dictionaries:
        Shows the original keys and summarizes large list values.

    Other values:
        Uses json_log() directly.

    This function does not modify the original object.
    """

    if isinstance(value, list):
        preview = {
            "total_items": len(value),
            "preview_limit": max(0, limit),
            "items": value[:max(0, limit)],
            "additional_items_omitted": max(
                0,
                len(value) - max(0, limit),
            ),
        }

        return json_log(
            preview,
            max_chars=max_chars,
        )

    if isinstance(value, dict):
        preview = {}

        for key, item in value.items():
            if isinstance(item, list):
                preview[key] = {
                    "total_items": len(item),
                    "preview_limit": max(0, limit),
                    "items": item[:max(0, limit)],
                    "additional_items_omitted": max(
                        0,
                        len(item) - max(0, limit),
                    ),
                }
            else:
                preview[key] = item

        return json_log(
            preview,
            max_chars=max_chars,
        )

    return json_log(
        value,
        max_chars=max_chars,
    )


# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------

def _safe_module_filename(name: str) -> str:
    """Convert a dotted logger name to a filesystem-safe filename.

    Example:
        app.services.ema_cross_store -> ema_cross_store
    """

    last = name.rsplit(".", 1)[-1] or "root"

    return re.sub(
        r"[^A-Za-z0-9_.-]",
        "_",
        last,
    ) or "root"


def _build_file_handler(
    path: Path,
    handler_name: str,
) -> RotatingFileHandler:
    """Create a rotating file handler with the shared formatter."""

    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    handler = RotatingFileHandler(
        path,
        maxBytes=_MAX_BYTES,
        backupCount=_BACKUP_COUNT,
        encoding="utf-8",
    )

    handler.setFormatter(_FORMATTER)
    handler.name = handler_name

    return handler


# ---------------------------------------------------------------------------
# Root / combined logging configuration
# ---------------------------------------------------------------------------

def configure_logging() -> None:
    """Configure console logging and the combined project log."""

    settings.log_file.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    if settings.module_logs_enabled:
        settings.module_log_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

    root_logger = logging.getLogger()
    root_logger.setLevel(settings.log_level)

    # Console handler.
    if not any(
        getattr(handler, "name", None) == "app-console"
        for handler in root_logger.handlers
    ):
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(_FORMATTER)
        console_handler.name = "app-console"

        root_logger.addHandler(console_handler)

    # Combined project log handler.
    if not any(
        getattr(handler, "name", None) == "app-file"
        for handler in root_logger.handlers
    ):
        combined_handler = _build_file_handler(
            settings.log_file,
            "app-file",
        )

        root_logger.addHandler(combined_handler)

    logging.captureWarnings(True)

    root_logger.debug(
        "Logging configured | level=%s | combined_file=%s | "
        "module_logs_enabled=%s | module_log_dir=%s",
        settings.log_level,
        settings.log_file,
        settings.module_logs_enabled,
        settings.module_log_dir,
    )


# ---------------------------------------------------------------------------
# Per-module file handlers
# ---------------------------------------------------------------------------

def _attach_module_handler(
    logger: logging.Logger,
    name: str,
) -> None:
    """Attach a rotating file handler to a module logger."""

    handler_name = f"module-file:{name}"

    # Avoid duplicate handlers during repeated imports/reloads.
    if any(
        getattr(handler, "name", None) == handler_name
        for handler in logger.handlers
    ):
        _MODULE_HANDLERS_ATTACHED.add(name)
        return

    if name in _MODULE_HANDLERS_ATTACHED:
        return

    filename = f"{_safe_module_filename(name)}.log"
    path = settings.module_log_dir / filename

    handler = _build_file_handler(
        path,
        handler_name,
    )

    logger.addHandler(handler)

    # Do not disable propagation: records should also reach the root
    # logger and appear in the combined project log.
    logger.propagate = True

    _MODULE_HANDLERS_ATTACHED.add(name)


# ---------------------------------------------------------------------------
# Public logging API
# ---------------------------------------------------------------------------

def get_logger(name: str) -> logging.Logger:
    """Return a configured logger with an optional module log file."""

    logger = logging.getLogger(name)

    if (
        settings.module_logs_enabled
        and name
        and name != "root"
    ):
        _attach_module_handler(
            logger,
            name,
        )

    return logger
