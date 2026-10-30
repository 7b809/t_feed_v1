# core/logger.py
"""Project-wide logging configuration.

Layout
------
Every module that calls ``get_logger(__file__)`` writes to **three**
destinations:

    console                                    (stderr, with colours optionally)
    logs/<module>.log                          (rotating by line count)
    logs_text/<module>.txt                     (plain append-only mirror)

and, **on Windows only**, in addition, every record is
**fanned out** into a single shared project-wide file pair:

    logs/<PROJECT_LOG_NAME>.log                (rotating by line count)
    logs_text/<PROJECT_LOG_NAME>.txt           (plain append-only mirror)

``PROJECT_LOG_NAME`` defaults to ``"local_project"`` (settable via the
``PROJECT_LOG_NAME`` environment variable) so the combined file is
called e.g. ``logs/local_project.log`` by default.

Why the combined file is gated to Windows only
----------------------------------------------
On Linux / macOS the project can be run under a supervisor that opens
many long-lived workers, and ``os.replace`` inside ``doRollover()``
interacts badly with editors / tailers that hold the shared file open
across rotations: on Linux the second handler keeps writing to the
unlinked inode (silently losing records after rotation); on macOS the
rename succeeds but the tail may be pointed at the wrong inode.

Windows is the platform this project runs on in production, and there
we **want** a single combined file so operators can grep one log
across every module. Rotation on Windows can hit ``PermissionError``
when the file is held open by an editor / antivirus / another Python
process, so we:

  * enable the combined pair on ``os.name == "nt"`` **only**;
  * add defensive handling in ``LineCountRotatingHandler.emit()`` so a
    failed rotation is logged once, retried on the next emit, and the
    record is never lost.

Per-module files are still written on every platform, since each
module owns a distinct path and therefore has no contention.

Why the combined file is a *single shared handler pair*
------------------------------------------------------
Rotating a log file is inherently racy if two ``logging.Handler``
instances own the same path: on ``doRollover()`` one handler renames
``app.log -> app.log.1`` while another still holds an open write
handle on the old inode. On Linux the second handler keeps writing
to the unlinked inode (silently losing records after rotation); on
Windows the rename fails outright with ``PermissionError``.

The old shape of ``get_logger()`` — "one handler per module per file" —
is fine for per-module files (each module owns a distinct path), but
would break for a shared combined path. So this module creates the
combined handler pair **once per process** and caches it in module
level globals. Every logger produced by ``get_logger()`` then attaches
those same two handler instances. ``logging.Handler.handle()`` is
thread-safe (it holds ``self.lock``), so multiple loggers writing
through the same handler is the standard, supported case.

The file is opened in append mode, so a restart appends to the same
combined file. Rotation is still per-handler: once the combined file
hits ``COMBINED_MAX_LINES``, it rotates to ``.1`` … ``.N`` and starts
a fresh ``local_project.log``. The per-module files have their own
independent counters and rotation schedules.

Changing the format of one destination
--------------------------------------
``_build_formatter()`` is shared by every handler so every line looks
the same everywhere. If you ever want a different format for the
combined file (e.g. no ``[%(filename)s:%(lineno)d]`` because you're
grepping across modules), add a second formatter function and pass it
to the combined handler constructor — nothing else in the pipeline
has to change.

Startup diagnostics
-------------------
When this module is first imported, it prints a single one-line summary
of the detected OS (``os.name``, ``sys.platform``, ``platform.system()``,
``platform.release()``) and whether the combined project-wide log is
ENABLED or DISABLED (and, when disabled, that it is because the host is
NOT Windows). A second print fires the first time the combined handler
pair is actually created, showing the concrete file paths.

Both prints use ``print(..., flush=True)`` — not the logger — because
they execute at module-import time, before any ``get_logger()`` call
exists. They are useful when diagnosing "why did my combined log
disappear?" on a non-Windows box.

Rotation robustness on Windows
------------------------------
On Windows, ``os.replace`` / ``os.remove`` can raise ``PermissionError``
when the target file is held open by another process (editor, tail,
antivirus). Since the combined file is now enabled on Windows, this
module wraps the ``doRollover()`` call inside ``LineCountRotatingHandler
.emit()`` in its own ``try/except OSError`` so that:

  * the record is **never lost** — if rotation fails we reopen the
    existing stream (if ``doRollover`` closed it) and continue
    appending;
  * a warning is printed **once per handler instance** so the operator
    sees the situation without the log stream being flooded;
  * rotation is retried on the next emit, and succeeds as soon as the
    file lock is released.

The practical consequence: on a Windows box where the combined log is
being tailed by an editor, the file may grow past ``COMBINED_MAX_LINES``
until the lock is released. That is a deliberate trade-off — losing
records is worse than a temporarily un-rotated file.
"""

from __future__ import annotations

import logging
import os
import platform
import sys

# --------------------------------------------------------------------------- #
# Paths + project name                                                        #
# --------------------------------------------------------------------------- #
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_THIS_DIR)

LOGS_DIR = os.path.join(_PROJECT_ROOT, "logs")
TEXT_LOGS_DIR = os.path.join(_PROJECT_ROOT, "logs_text")

# Base name for the single combined project-wide log files.
# Override with PROJECT_LOG_NAME=<name> in .env if you want e.g.
# "fastapi_project.log" or "trading_engine.log".
PROJECT_LOG_NAME = (
    os.getenv("PROJECT_LOG_NAME", "").strip() or "local_project"
)

# Rotation thresholds for the combined project log. Kept larger than
# the per-module thresholds because the combined file receives the
# traffic of every module (typically 20-50x the per-module volume).
COMBINED_MAX_LINES = int(os.getenv("COMBINED_MAX_LINES", "50000"))
COMBINED_BACKUP_COUNT = int(os.getenv("COMBINED_BACKUP_COUNT", "10"))

# Rotation thresholds for the per-module logs (unchanged defaults).
PER_MODULE_MAX_LINES = int(os.getenv("PER_MODULE_MAX_LINES", "1000"))
PER_MODULE_BACKUP_COUNT = int(os.getenv("PER_MODULE_BACKUP_COUNT", "3"))

# --------------------------------------------------------------------------- #
# Platform gate for the combined project-wide log pair.                       #
#                                                                             #
# 2026-10-30 change: the combined log is now ENABLED ON WINDOWS ONLY and      #
# DISABLED on every other OS. This is the opposite of the previous            #
# behaviour, which disabled the combined file on Windows because             #
# ``os.replace`` inside ``doRollover()`` can raise ``PermissionError``        #
# when the file is held open by another process.                              #
#                                                                             #
# We now handle that failure path in ``LineCountRotatingHandler.emit()``     #
# (see that class) so the record is never lost and the app never crashes.    #
# Enabling the combined file on Windows gives operators one place to grep    #
# across every module — which is exactly what production needs.              #
# --------------------------------------------------------------------------- #
ENABLE_COMBINED_LOG = os.name == "nt"

# --------------------------------------------------------------------------- #
# Startup diagnostics — OS + combined-log status.                             #
#                                                                             #
# Printed once at module import time (before any logger exists, so we use     #
# plain print()). This makes it obvious in the startup output which OS the    #
# host is running and whether the shared combined log is expected to appear   #
# at ``logs/<PROJECT_LOG_NAME>.log`` / ``logs_text/<PROJECT_LOG_NAME>.txt``.  #
# --------------------------------------------------------------------------- #
_COMBINED_LOG_STATE = (
    "ENABLED"
    if ENABLE_COMBINED_LOG
    else "DISABLED (os.name != 'nt' — non-Windows; combined file gated to "
         "Windows only, per the 2026-10-30 logging policy)"
)

print(
    f"[core.logger] OS detected: "
    f"name={os.name!r} "
    f"sys.platform={sys.platform!r} "
    f"system={platform.system()!r} "
    f"release={platform.release()!r} "
    f"machine={platform.machine()!r} "
    f"python={platform.python_version()} "
    f"-> combined project log is {_COMBINED_LOG_STATE}",
    flush=True,
)


# --------------------------------------------------------------------------- #
# Handlers                                                                    #
# --------------------------------------------------------------------------- #
class LineCountRotatingHandler(logging.FileHandler):
    """
    A file handler that rotates the log file after a maximum number of lines.
    Keeps a maximum specified count of old log files.

    Rotation robustness
    -------------------
    On Windows ``os.replace`` / ``os.remove`` can raise ``PermissionError``
    when the target file is held open by another process. Since the
    combined project-wide file is now enabled on Windows (2026-10-30),
    ``emit()`` wraps the ``doRollover()`` call in its own try/except so
    that:

      * the record is never lost — if rotation fails mid-way, the
        stream is reopened and the record is appended to the current
        file;
      * a warning is printed **once per handler instance**;
      * rotation is retried on the next emit.

    The practical consequence is that the file may grow past
    ``maxLines`` while the lock is held, then rotate as soon as the
    lock is released. This is deliberate — losing records is worse
    than a temporarily un-rotated file.
    """

    def __init__(self, filename, maxLines=1000, backupCount=3, encoding="utf-8"):
        super().__init__(filename, mode="a", encoding=encoding)
        self.maxLines = maxLines
        self.backupCount = backupCount
        self.line_count = self._count_existing_lines()
        # One-shot warning flag for the "rollover failed" case.
        self._rollover_warned = False

    def _count_existing_lines(self) -> int:
        """
        Counts existing lines in the log file on startup
        if it already exists.
        """
        if os.path.exists(self.baseFilename):
            try:
                with open(
                    self.baseFilename,
                    "r",
                    encoding=self.encoding,
                    errors="ignore",
                ) as file:
                    return sum(1 for _ in file)
            except (OSError, UnicodeError):
                return 0

        return 0

    def emit(self, record):
        """
        Emits a record and rotates if writing the record would exceed
        the configured line threshold.

        A rotation failure (most commonly ``PermissionError`` on
        Windows when the file is held open by another process) is
        caught here so that the record is still written to the
        current file and rotation is retried on the next emit.
        """
        try:
            formatted_message = self.format(record)
            record_line_count = formatted_message.count("\n") + 1

            if self.shouldRollover(record_line_count):
                try:
                    self.doRollover()
                except OSError as exc:
                    # Rotation failed — most likely Windows holding a
                    # lock on the file. Reopen the stream if
                    # doRollover() closed it, then continue appending
                    # so the record is not lost. Rate-limit the
                    # warning to once per handler instance.
                    if self.stream is None:
                        try:
                            self.stream = self._open()
                        except Exception:
                            # Could not reopen at all — fall through to
                            # handleError so the operator sees it.
                            self.handleError(record)
                            return

                    if not self._rollover_warned:
                        self._rollover_warned = True
                        try:
                            print(
                                f"[core.logger] WARNING: rollover failed "
                                f"for {self.baseFilename} "
                                f"({type(exc).__name__}); continuing to "
                                f"append without rotation. This is "
                                f"expected on Windows when the file is "
                                f"held open by another process. Rotation "
                                f"will be retried on the next emit.",
                                file=sys.stderr,
                                flush=True,
                            )
                        except Exception:
                            # print() into sys.stderr can itself fail on
                            # exotic hosts; never let the log path crash.
                            pass
                    # Fall through to append to the current stream.

            super().emit(record)
            self.line_count += record_line_count

        except Exception:
            self.handleError(record)

    def shouldRollover(self, new_line_count=1) -> bool:
        """
        Determines whether the new log record would exceed maxLines.
        """
        return (
            self.line_count > 0
            and self.line_count + new_line_count > self.maxLines
        )

    def doRollover(self):
        """
        Rotates log files:

        file.log.2 -> file.log.3
        file.log.1 -> file.log.2
        file.log   -> file.log.1

        May raise ``OSError`` (``PermissionError`` on Windows) when a
        target file is held open by another process. The caller —
        ``emit()`` — handles that exception gracefully.
        """
        if self.stream:
            self.stream.close()
            self.stream = None

        if self.backupCount > 0:
            for i in range(self.backupCount - 1, 0, -1):
                source_file = f"{self.baseFilename}.{i}"
                destination_file = f"{self.baseFilename}.{i + 1}"

                if os.path.exists(source_file):
                    if os.path.exists(destination_file):
                        os.remove(destination_file)

                    os.replace(source_file, destination_file)

            first_backup = f"{self.baseFilename}.1"

            if os.path.exists(first_backup):
                os.remove(first_backup)

            if os.path.exists(self.baseFilename):
                os.replace(self.baseFilename, first_backup)

        elif os.path.exists(self.baseFilename):
            os.remove(self.baseFilename)

        self.stream = self._open()
        self.line_count = 0


class PlainTextHandler(logging.FileHandler):
    """
    A simple append-only plain-text log handler.

    Writes each log record to a .txt file (mirror of the .log file)
    using a slightly simpler, human-friendly format. No rotation is
    performed by default, so the file keeps a complete history in a
    single easy-to-open text file.
    """

    def __init__(self, filename, encoding="utf-8"):
        super().__init__(filename, mode="a", encoding=encoding)


# --------------------------------------------------------------------------- #
# Formatter                                                                   #
# --------------------------------------------------------------------------- #
def _build_formatter() -> logging.Formatter:
    """
    Shared formatter used by every handler.

    The ``[%(name)s]`` field carries the module logger name
    (``live_sr_calc_service``, ``isolated_instrument_routes``, …) and
    is what makes the combined file scannable when records from
    different modules interleave. ``[%(filename)s:%(lineno)d]`` and
    ``[%(funcName)s]`` pinpoint the exact source line of every record.
    """
    return logging.Formatter(
        "%(asctime)s [%(levelname)s] [%(name)s] "
        "[%(filename)s:%(lineno)d] [%(funcName)s]: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


# --------------------------------------------------------------------------- #
# Combined (project-wide) handler singletons                                  #
# --------------------------------------------------------------------------- #
# These are created lazily, once per process, the first time
# get_logger() is called — and only on Windows (see ENABLE_COMBINED_LOG
# above). Every subsequent get_logger() call attaches the *same two
# handler instances* to that module's logger, so there is exactly one
# writer on each combined file for the whole process — rotation is
# therefore race-free.
_COMBINED_LOG_HANDLER: LineCountRotatingHandler | None = None
_COMBINED_TEXT_HANDLER: PlainTextHandler | None = None


def _ensure_combined_handlers() -> tuple[
    LineCountRotatingHandler, PlainTextHandler
]:
    """Create (once) and return the shared combined handler pair.

    Called from ``get_logger()`` after the log directories are known
    to exist, and only when ``ENABLE_COMBINED_LOG`` is True (i.e. on
    Windows platforms). Safe to call concurrently — Python's module
    import lock guarantees the ``if None:`` check is only executed
    once.
    """
    global _COMBINED_LOG_HANDLER, _COMBINED_TEXT_HANDLER

    if _COMBINED_LOG_HANDLER is not None and _COMBINED_TEXT_HANDLER is not None:
        return _COMBINED_LOG_HANDLER, _COMBINED_TEXT_HANDLER

    formatter = _build_formatter()

    combined_log_path = os.path.join(
        LOGS_DIR, f"{PROJECT_LOG_NAME}.log"
    )
    combined_log_handler = LineCountRotatingHandler(
        combined_log_path,
        maxLines=COMBINED_MAX_LINES,
        backupCount=COMBINED_BACKUP_COUNT,
        encoding="utf-8",
    )
    combined_log_handler.setLevel(logging.DEBUG)
    combined_log_handler.setFormatter(formatter)

    combined_text_path = os.path.join(
        TEXT_LOGS_DIR, f"{PROJECT_LOG_NAME}.txt"
    )
    combined_text_handler = PlainTextHandler(
        combined_text_path,
        encoding="utf-8",
    )
    combined_text_handler.setLevel(logging.DEBUG)
    combined_text_handler.setFormatter(formatter)

    _COMBINED_LOG_HANDLER = combined_log_handler
    _COMBINED_TEXT_HANDLER = combined_text_handler

    # One-time confirmation print — fires the first time any module calls
    # get_logger() on a platform that supports the combined pair. Useful
    # in the startup console to prove the shared file is actually wired.
    print(
        f"[core.logger] Combined project log ACTIVATED: "
        f"{combined_log_path} + {combined_text_path}",
        flush=True,
    )

    return combined_log_handler, combined_text_handler


# --------------------------------------------------------------------------- #
# Public API                                                                  #
# --------------------------------------------------------------------------- #
def get_logger(filename: str, log_level=logging.DEBUG) -> logging.Logger:
    """
    Creates and returns a logger that logs all standard logging levels
    to:

      - the console
      - a rotating .log file under logs/<module>.log
      - an append-only .txt file under logs_text/<module>.txt

    and, **on Windows platforms only**, additionally to the shared
    combined project log:

      - logs/<PROJECT_LOG_NAME>.log
      - logs_text/<PROJECT_LOG_NAME>.txt

    Supported levels:
    - DEBUG
    - INFO
    - WARNING
    - ERROR
    - CRITICAL

    The formatter automatically includes:
    - Source filename
    - Source line number
    - Function or method name

    :param filename:
        File or module name, normally passed as __file__.

    :param log_level:
        Logging level. Defaults to logging.DEBUG.
    """

    # Extract the pure filename without its path or extension.
    base_name = os.path.splitext(os.path.basename(filename))[0]

    # Ensure that the logs directories exist.
    os.makedirs(LOGS_DIR, exist_ok=True)
    os.makedirs(TEXT_LOGS_DIR, exist_ok=True)

    logger = logging.getLogger(base_name)
    logger.setLevel(log_level)

    # Prevent messages from reaching root logger handlers.
    logger.propagate = False

    # Avoid adding duplicate handlers.
    if logger.handlers:
        return logger

    formatter = _build_formatter()

    # ------------------------------------------------------------------ #
    # 1. Console handler                                                  #
    # ------------------------------------------------------------------ #
    console_handler = logging.StreamHandler()
    console_handler.setLevel(log_level)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # ------------------------------------------------------------------ #
    # 2. Per-module rotating .log file handler                            #
    # ------------------------------------------------------------------ #
    log_file_path = os.path.join(LOGS_DIR, f"{base_name}.log")

    file_handler = LineCountRotatingHandler(
        log_file_path,
        maxLines=PER_MODULE_MAX_LINES,
        backupCount=PER_MODULE_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setLevel(log_level)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    # ------------------------------------------------------------------ #
    # 3. Per-module plain-text .txt file handler (mirror)                 #
    # ------------------------------------------------------------------ #
    text_file_path = os.path.join(TEXT_LOGS_DIR, f"{base_name}.txt")

    text_handler = PlainTextHandler(
        text_file_path,
        encoding="utf-8",
    )
    text_handler.setLevel(log_level)
    text_handler.setFormatter(formatter)
    logger.addHandler(text_handler)

    # ------------------------------------------------------------------ #
    # 4. Combined project-wide handler pair (shared singletons)           #
    #                                                                     #
    # Enabled on Windows only (2026-10-30 logging policy). Windows is     #
    # the production host and operators want a single file to grep        #
    # across every module. Rotation failures (``PermissionError`` when    #
    # the file is held open) are handled inside ``emit()`` so records    #
    # are never lost.                                                     #
    #                                                                     #
    # These two handler instances are created once for the entire         #
    # process and are shared by every module logger. This is what         #
    # guarantees a race-free combined file — see the module docstring.    #
    #                                                                     #
    # We set the handler level to DEBUG regardless of log_level, so       #
    # that a module which calls get_logger(..., log_level=INFO) still     #
    # contributes to the combined file at full DEBUG fidelity. The        #
    # module's own logger level (set above) is what gates whether a       #
    # record reaches *any* of its handlers — including the combined       #
    # ones — so no extra filtering is needed here.                        #
    # ------------------------------------------------------------------ #
    if ENABLE_COMBINED_LOG:
        combined_log_handler, combined_text_handler = _ensure_combined_handlers()
        logger.addHandler(combined_log_handler)
        logger.addHandler(combined_text_handler)

    return logger