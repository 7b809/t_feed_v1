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

from __future__ import annotations

import logging
import os
import re
import threading
from pathlib import Path


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
    """
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    if logs_dir is None:
        project_root = Path(__file__).resolve().parent.parent
        logs_dir = project_root / os.getenv("LOGS_DIR", "logs")
    logs_dir.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)s %(name)s %(message)s"
    )

    percent_filter = MegaDownloadPercentFilter()
    cycle_throttle = CycleCompleteThrottleFilter(every=cycle_log_every)

    # Console — every record.
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    stream_handler.addFilter(percent_filter)

    # Combined project log — cycle-complete throttled to every Nth cycle.
    combined_handler = logging.FileHandler(
        logs_dir / "mega_archive_service.log", encoding="utf-8"
    )
    combined_handler.setFormatter(formatter)
    combined_handler.addFilter(percent_filter)
    combined_handler.addFilter(cycle_throttle)

    # Per-module log files — every record, unthrottled.
    per_logger_handler = PerLoggerFileHandler(logs_dir)
    per_logger_handler.setFormatter(formatter)
    per_logger_handler.addFilter(percent_filter)

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    root.setLevel(level)
    root.addHandler(stream_handler)
    root.addHandler(combined_handler)
    root.addHandler(per_logger_handler)

    logging.getLogger("apscheduler").setLevel(max(level, logging.INFO))
    logging.getLogger("uvicorn.access").setLevel(max(level, logging.INFO))