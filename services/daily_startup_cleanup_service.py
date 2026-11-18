"""Safe once-per-IST-day startup cleanup for explicitly configured runtime folders.

Only direct child directories of the project root are eligible. The project
root, source tree, virtual environments, .git and environment files are never
cleanup targets. Active log handlers are truncated in place before old files
are removed so their open file descriptors remain valid.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
from datetime import datetime, time as dt_time
from pathlib import Path
from zoneinfo import ZoneInfo

from core.logger import get_logger

logger = get_logger(__file__)
IST = ZoneInfo("Asia/Kolkata")
PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATE_FILE = PROJECT_ROOT / ".daily_cleanup_state.json"

DEFAULT_TARGETS = ("data", "logs", "logs_text")
FORBIDDEN_TARGETS = {
    ".git", ".github", ".venv", "venv", "myenv", "env",
    "app", "api", "core", "jobs", "repositories", "services",
    "tests", "docs", "templates", "static",
}


def _configured_targets() -> list[Path]:
    raw = os.getenv("DAILY_CLEANUP_DIRS", ",".join(DEFAULT_TARGETS))
    targets = []
    for item in raw.split(","):
        name = item.strip()
        if (
            not name
            or name in (".", "..")
            or name.startswith(".")
            or "/" in name
            or "\\" in name
            or name.lower() in FORBIDDEN_TARGETS
        ):
            logger.error("Rejected unsafe DAILY_CLEANUP_DIRS entry: %r", name)
            continue
        target = PROJECT_ROOT / name
        try:
            if target.resolve().parent != PROJECT_ROOT.resolve():
                logger.error("Rejected cleanup path outside project root: %s", target)
                continue
        except OSError:
            continue
        targets.append(target)
    return targets


def _truncate_open_log_handlers(target: Path) -> set[Path]:
    """Truncate active FileHandlers without invalidating their open streams."""
    preserved: set[Path] = set()
    target_resolved = target.resolve()
    for value in logging.Logger.manager.loggerDict.values():
        if not isinstance(value, logging.Logger):
            continue
        for handler in list(value.handlers):
            filename = getattr(handler, "baseFilename", None)
            if not filename:
                continue
            path = Path(filename)
            try:
                if path.resolve().parent != target_resolved:
                    continue
            except OSError:
                continue
            try:
                handler.acquire()
                try:
                    if handler.stream:
                        handler.flush()
                        handler.stream.seek(0)
                        handler.stream.truncate(0)
                        handler.flush()
                    if hasattr(handler, "line_count"):
                        handler.line_count = 0
                    preserved.add(path.resolve())
                finally:
                    handler.release()
            except Exception:
                logger.exception("Could not truncate active log handler: %s", path)
    return preserved


def _remove_children(target: Path, preserve: set[Path] | None = None) -> tuple[int, int]:
    preserve = preserve or set()
    removed_files = 0
    removed_dirs = 0
    if not target.exists() or not target.is_dir() or target.is_symlink():
        return removed_files, removed_dirs

    for child in list(target.iterdir()):
        try:
            resolved = child.resolve()
            if child.is_symlink():
                child.unlink()
                removed_files += 1
            elif child.is_dir():
                files, dirs = _remove_children(child, preserve)
                if not any(p == resolved or resolved in p.parents for p in preserve):
                    child.rmdir()
                    removed_dirs += 1
                removed_files += files
                removed_dirs += dirs
            elif resolved not in preserve:
                child.unlink()
                removed_files += 1
        except FileNotFoundError:
            continue
        except Exception:
            logger.exception("Startup cleanup could not remove %s", child)
    return removed_files, removed_dirs


def run_daily_startup_cleanup(now: datetime | None = None) -> dict:
    """Clear configured runtime directories once per day, only 01:00–09:00 IST."""
    if os.getenv("DAILY_CLEANUP_ENABLED", "true").strip().lower() != "true":
        return {"enabled": False, "state": "disabled"}

    now = now or datetime.now(IST)
    if now.tzinfo is None:
        now = now.replace(tzinfo=IST)
    else:
        now = now.astimezone(IST)

    start_hour = int(os.getenv("DAILY_CLEANUP_START_HOUR", "1"))
    end_hour = int(os.getenv("DAILY_CLEANUP_END_HOUR", "9"))
    if not (dt_time(start_hour, 0) <= now.time().replace(tzinfo=None) < dt_time(end_hour, 0)):
        return {
            "enabled": True,
            "state": "outside_cleanup_window",
            "time_ist": now.isoformat(),
        }

    date_key = now.date().isoformat()
    try:
        previous = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if previous.get("last_cleanup_date_ist") == date_key:
            return {
                "enabled": True,
                "state": "already_cleaned_today",
                "trade_date_ist": date_key,
                "targets": previous.get("targets", []),
            }
    except (OSError, json.JSONDecodeError, AttributeError):
        pass

    results = []
    for target in _configured_targets():
        if target.name in ("logs", "logs_text"):
            preserved = _truncate_open_log_handlers(target)
        else:
            preserved = set()
        files, dirs = _remove_children(target, preserved)
        results.append({
            "directory": target.name,
            "removed_files": files,
            "removed_directories": dirs,
            "preserved_active_log_files": len(preserved),
        })
        logger.info(
            "Daily startup cleanup completed for %s: removed_files=%d removed_dirs=%d",
            target.name, files, dirs,
        )

    state = {
        "last_cleanup_date_ist": date_key,
        "cleaned_at_ist": now.isoformat(),
        "targets": [row["directory"] for row in results],
        "results": results,
    }
    temp = STATE_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(temp, STATE_FILE)

    logger.info(
        "Daily startup cleanup finished: date=%s targets=%s",
        date_key, state["targets"],
    )
    return {"enabled": True, "state": "completed", **state}
