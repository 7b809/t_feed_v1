# app/logging_config.py
#
# Central logging setup.
#
# Three sinks, all installed on the root logger:
#   1. Console (stderr)              — every record, unthrottled.
#   2. Combined file                 — logs/mega_archive_service.log
#   3. Per-module file               — logs/<logger_name>.log (dots → _)
#
# Throttling the "Cycle N complete" line:
#   Job 2 emits one ``Cycle N complete`` message per cycle. That is useful
#   in the per-module log and in the console (it shows liveness in real
#   time), but at 1 cycle per minute the combined log file grows without
#   bound for no diagnostic value.
#
#   ``CycleCompleteThrottleFilter`` is attached ONLY to the combined file
#   handler. It suppresses the cycle-complete record unless the cycle
#   number is a multiple of ``EMA_CYCLE_LOG_EVERY`` (default 100). The
#   per-module and console handlers receive every cycle-complete line
#   unthrottled.
#
#   Cycle-complete records are identified by ``extra={"cycle_complete": True,
#   "cycle_number": N}`` on the log call site (see ema_feed_service.py).
#   All other records pass through unchanged.
#
# Sensitive data redaction:
#   Telegram bot tokens (and any other bearer-token-shaped secret) must
#   never reach the log files. ``httpx`` logs every request URL at INFO
#   level, and the Telegram Bot API embeds the token in the URL path — so
#   without intervention, every Telegram send leaks the token.
#
#   Three independent layers prevent this:
#
#     * ``httpx`` / ``httpcore`` / ``h11`` loggers are pinned to WARNING,
#       so the primary "HTTP Request: POST ..." INFO line is never
#       emitted at all. This is the most direct fix.
#
#     * ``SensitiveDataRedactionFilter`` rewrites ``record.msg`` in place
#       before every handler formats it. This catches any string that
#       still happens to reach a handler (e.g. an explicit
#       ``logger.warning("... %s", exc)`` whose exception message embeds
#       the URL).
#
#     * ``RedactingFormatter`` runs the same regex over the FULL rendered
#       output (message + traceback). This covers ``logger.exception()``
#       and any formatting the previous layer might have missed.
#
#   Together, layers 2 and 3 guarantee the token never appears even when
#   ``LOG_LEVEL=DEBUG`` is set by a future deployer.
#
# The redaction pattern ``\d{6,12}:[A-Za-z0-9_-]{25,}`` matches the
# Telegram bot token shape (e.g. ``123456789:ABC-DEF...``). It is
# intentionally narrow so it will not false-positive on ordinary log
# content such as timestamps or ISO offsets.

from __future__ import annotations

import logging
import os
import re
import threading
from pathlib import Path

# ---------------------------------------------------------------------------
# Sensitive-data pattern
# ---------------------------------------------------------------------------
# Telegram bot tokens are ``<bot_id>:<base64ish>``, e.g.
#   123456789:ABC-DEF1234ghIkl-zyx57W2v1u123ew11
# The bot_id is 6–12 digits; the secret half is 25+ chars of [A-Za-z0-9_-].
_BOT_TOKEN_RE = re.compile(r"\d{6,12}:[A-Za-z0-9_-]{25,}")
_REDACTED = "<REDACTED>"

# ---------------------------------------------------------------------------
# Filter: MEGA SDK byte-progress → percentage
# ---------------------------------------------------------------------------
class MegaDownloadPercentFilter(logging.Filter):
    """Rewrite ``N of M downloaded`` log lines into percentage form."""

    _PATTERN = re.compile(r"(\d+)\s+of\s+(\d+)\s+downloaded", re.IGNORECASE)

    def filter(self, record: logging.LogRecord) -> bool:
        if not record.name.startswith("mega_sdk"):
            return True
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001
            return True
        match = self._PATTERN.search(message)
        if not match:
            return True
        current = int(match.group(1))
        total = int(match.group(2))
        percent = (current * 100.0 / total) if total else 0.0
        record.msg = "MEGA download progress: %6.2f%% (%d/%d bytes)" % (
            percent,
            current,
            total,
        )
        record.args = ()
        return True

# ---------------------------------------------------------------------------
# Filter: throttle "Cycle N complete" on the combined file only
# ---------------------------------------------------------------------------
class CycleCompleteThrottleFilter(logging.Filter):
    """
    Suppress the ``Cycle N complete`` line unless N is a multiple of ``every``.

    The record is identified via ``extra={"cycle_complete": True,
    "cycle_number": N}``. Any other record passes through untouched.
    """

    def __init__(self, every: int) -> None:
        super().__init__()
        self.every = max(1, int(every))

    def filter(self, record: logging.LogRecord) -> bool:
        if not getattr(record, "cycle_complete", False):
            return True
        cycle = getattr(record, "cycle_number", None)
        if cycle is None:
            return True
        return int(cycle) % self.every == 0

# ---------------------------------------------------------------------------
# Filter: redact Telegram bot tokens from record.msg
# ---------------------------------------------------------------------------
class SensitiveDataRedactionFilter(logging.Filter):
    """
    Redact bearer-token-shaped secrets (Telegram bot token) from the
    record message before any handler renders it.

    Mutates ``record.msg`` in place and clears ``record.args`` so every
    downstream handler (console, combined file, per-module file) sees the
    already-redacted message.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001
            return True
        redacted = _BOT_TOKEN_RE.sub(_REDACTED, message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True

# ---------------------------------------------------------------------------
# Formatter: redact the FULL rendered output (message + traceback)
# ---------------------------------------------------------------------------
class RedactingFormatter(logging.Formatter):
    """
    A ``logging.Formatter`` that redacts secrets after formatting.

    This is the last line of defence: even a traceback that embeds the
    full Telegram URL (from an ``httpx`` request exception propagated
    through ``logger.exception``) is sanitised before being written.
    """

    def format(self, record: logging.LogRecord) -> str:
        try:
            rendered = super().format(record)
        except Exception:  # noqa: BLE001
            # Never crash logging; fall back to a minimal line.
            rendered = f"{record.levelname} {record.name} {record.msg}"
        return _BOT_TOKEN_RE.sub(_REDACTED, rendered)

# ---------------------------------------------------------------------------
# Handler: one file per logger name
# ---------------------------------------------------------------------------
class PerLoggerFileHandler(logging.Handler):
    """Writes each record to logs/<logger_name>.log, one handle per logger."""

    def __init__(self, directory: Path):
        super().__init__()
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self._handles: dict[str, object] = {}
        self._lock = threading.Lock()

    def _handle_for(self, name: str):
        safe = name.replace(".", "_") or "root"
        handle = self._handles.get(safe)
        if handle is None:
            path = self.directory / f"{safe}.log"
            handle = path.open("a", encoding="utf-8")
            self._handles[safe] = handle
        return handle

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
            with self._lock:
                handle = self._handle_for(record.name)
                handle.write(message + "\n")
                handle.flush()
        except Exception:  # noqa: BLE001
            self.handleError(record)

# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def configure_logging(
    logs_dir: Path | None = None,
    cycle_log_every: int = 100,
) -> None:
    """
    Install console, combined-file, and per-module-file handlers on root.

    ``cycle_log_every`` controls how often the ``Cycle N complete`` line is
    written to the COMBINED file. Per-module files and the console always
    see every cycle. See module docstring for details.

    Every handler has both ``SensitiveDataRedactionFilter`` and
    ``RedactingFormatter`` applied, so a secret can never reach any sink.
    """
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    if logs_dir is None:
        project_root = Path(__file__).resolve().parent.parent
        logs_dir = project_root / os.getenv("LOGS_DIR", "logs")
    logs_dir.mkdir(parents=True, exist_ok=True)

    formatter = RedactingFormatter(
        fmt="%(asctime)s %(levelname)s %(name)s %(message)s"
    )

    percent_filter = MegaDownloadPercentFilter()
    cycle_throttle = CycleCompleteThrottleFilter(every=cycle_log_every)
    redaction_filter = SensitiveDataRedactionFilter()

    # Console — every record.
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    stream_handler.addFilter(percent_filter)
    stream_handler.addFilter(redaction_filter)

    # Combined project log — cycle-complete throttled to every Nth cycle.
    combined_handler = logging.FileHandler(
        logs_dir / "mega_archive_service.log", encoding="utf-8"
    )
    combined_handler.setFormatter(formatter)
    combined_handler.addFilter(percent_filter)
    combined_handler.addFilter(cycle_throttle)
    combined_handler.addFilter(redaction_filter)

    # Per-module log files — every record, unthrottled.
    per_logger_handler = PerLoggerFileHandler(logs_dir)
    per_logger_handler.setFormatter(formatter)
    per_logger_handler.addFilter(percent_filter)
    per_logger_handler.addFilter(redaction_filter)

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    root.setLevel(level)
    root.addHandler(stream_handler)
    root.addHandler(combined_handler)
    root.addHandler(per_logger_handler)

    logging.getLogger("apscheduler").setLevel(max(level, logging.INFO))
    logging.getLogger("uvicorn.access").setLevel(max(level, logging.INFO))

    # Silence HTTP client libraries at their default level. httpx logs
    # every request URL at INFO — this is the primary path by which the
    # Telegram bot token used to leak into the log files. WARNING keeps
    # httpx / httpcore / h11 out of the log while still surfacing real
    # transport errors. Layers 2 and 3 above ensure the token remains
    # redacted even if some future deployer raises these loggers back
    # to INFO/DEBUG.
    for noisy in ("httpx", "httpcore", "h11"):
        logging.getLogger(noisy).setLevel(logging.WARNING)