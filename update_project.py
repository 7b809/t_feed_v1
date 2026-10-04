import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_NAME = "UpstoxAppV2"

START_SCRIPT = "start.sh"
STOP_SCRIPT = "stop.sh"

LOGS_DIR = os.path.join(PROJECT_DIR, "logs")
META_DATA_DIR = os.path.join(PROJECT_DIR, "meta_data")


# ---------------------------------------------------------------------------
# Path policy
# ---------------------------------------------------------------------------
# PRESERVE_PATHS — must survive `git clean` and any explicit removal.
#   git clean -fd respects .gitignore by default, so gitignored paths
#   would normally be safe; we also pass each one with `-e` to be
#   defensive against a path being accidentally tracked or a stray
#   .gitignore change wiping state.
#
# REMOVE_PATHS — explicitly deleted after `git reset --hard` and before
#   `git clean -fd`. Use this for untracked generated folders that git
#   clean would normally leave in place because they are ignored.
#
# Conflict rule: if a path appears in both lists, PRESERVE wins and
#   no removal action is performed for it. A warning is printed.
#
# Path format: simple folder names or relative paths, with or without
#   a trailing slash. `__pycache__`-style names are matched recursively
#   at every depth. Do NOT list tracked source folders here — git will
#   restore them on `git pull`.
# ---------------------------------------------------------------------------
PRESERVE_PATHS = [
    "meta_data",        # log backups created by this script
    "data",             # runtime state: options, candles, crossovers, isolation
    "logs",             # protect the live log file while the app runs
    ".env",             # secrets — must never be deleted
    ".env.local",       # optional local overrides
    ".venv",            # local virtualenv (if present)
    "venv",             # alternative virtualenv directory name
]

REMOVE_PATHS = [
    "temp",             # scratch space
    "output",           # generated build/runtime outputs
    "__pycache__",      # Python bytecode cache (recursive)
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "data",
]

TIMEZONE = "Asia/Kolkata"

DEBUG_MODE = False


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------
def _normalize(path: str) -> str:
    """Strip whitespace and trailing slashes for comparison."""
    return str(path).strip().rstrip("/")


def _resolve_removal_targets():
    """
    Return (effective, conflicts).

    effective — sorted list of REMOVE_PATHS not present in PRESERVE_PATHS
    conflicts — sorted list of REMOVE_PATHS that ARE in PRESERVE_PATHS
                (these are never acted upon)
    """
    preserve = {_normalize(p) for p in PRESERVE_PATHS}
    remove = {_normalize(p) for p in REMOVE_PATHS}

    effective = sorted(remove - preserve)
    conflicts = sorted(remove & preserve)

    return effective, conflicts


def _is_safe_remove_target(abs_path: Path) -> bool:
    """
    Refuse to remove:
      * the project root itself
      * any path outside the project root
      * any ancestor of a PRESERVE_PATHS entry
    """
    try:
        project_root = Path(PROJECT_DIR).resolve()
        target = abs_path.resolve()

        if target == project_root:
            return False

        try:
            target.relative_to(project_root)
        except ValueError:
            return False

        for preserved in PRESERVE_PATHS:
            preserve_abs = (project_root / _normalize(preserved)).resolve()
            if target == preserve_abs:
                return False
            try:
                preserve_abs.relative_to(target)
                return False
            except ValueError:
                pass

        return True
    except Exception:
        return False


def _expand_removal_patterns(relative_path: str) -> list[Path]:
    """
    Return every directory under PROJECT_DIR whose name matches
    `relative_path`. Supports nested matches for names like
    `__pycache__` at any depth.
    """
    project_root = Path(PROJECT_DIR)
    target_name = _normalize(relative_path)
    if not target_name:
        return []

    matches: list[Path] = []
    seen: set[str] = set()

    for candidate in project_root.rglob(target_name):
        try:
            if not candidate.is_dir():
                continue
            key = str(candidate.resolve())
            if key in seen:
                continue
            seen.add(key)
            matches.append(candidate)
        except Exception:
            continue

    return matches


# ---------------------------------------------------------------------------
# Telegram (best-effort, disabled by default)
# ---------------------------------------------------------------------------
def send_telegram(
    title: str,
    message: str,
    level: str = "INFO",
) -> None:
    """
    Send a Telegram notification without interrupting the update process
    if Telegram is unavailable or fails.

    Disabled by default (DEBUG_MODE = False). When enabled, the import
    is performed lazily so this script keeps working even when the
    telegram_app package is not importable in the current environment.
    """
    if not DEBUG_MODE:
        return

    try:
        from telegram_app.manager import telegram_manager  # type: ignore
    except Exception as error:
        print(f"WARNING: Telegram module unavailable: {error}")
        return

    try:
        telegram_manager.send_message(
            title=title,
            message=message,
            level=level,
            notification_context="project_update",
        )
    except Exception as error:
        print(f"WARNING: Telegram notification failed: {error}")


# ---------------------------------------------------------------------------
# Generic command runner
# ---------------------------------------------------------------------------
def run_command(
    command: list[str],
    step_name: str,
    check: bool = True,
) -> None:
    print()
    print(f"> {' '.join(command)}")

    try:
        result = subprocess.run(
            command,
            cwd=PROJECT_DIR,
        )

        if result.returncode != 0:
            error_message = (
                f"{step_name} failed.\n"
                f"Exit code: {result.returncode}"
            )

            print(f"\nERROR: {error_message}")

            send_telegram(
                title=f"{step_name} Failed",
                message=error_message,
                level="ERROR",
            )

            if check:
                sys.exit(result.returncode)

            return

        send_telegram(
            title=f"{step_name} Completed",
            message=f"{step_name} completed successfully.",
            level="INFO",
        )

    except Exception as error:
        error_message = (
            f"{step_name} encountered an exception.\n"
            f"Error: {type(error).__name__}: {error}"
        )

        print(f"\nERROR: {error_message}")

        send_telegram(
            title=f"{step_name} Exception",
            message=error_message,
            level="ERROR",
        )

        if check:
            sys.exit(1)


# ---------------------------------------------------------------------------
# Logs backup
# ---------------------------------------------------------------------------
def backup_logs() -> None:
    print("\n==========================================")
    print("  Creating Logs Backup")
    print("==========================================")

    send_telegram(
        title="Logs Backup Started",
        message="Starting logs backup.",
        level="INFO",
    )

    if not os.path.exists(LOGS_DIR):
        message = (
            f"Logs directory not found.\n"
            f"Path: {LOGS_DIR}\n"
            f"Skipping logs backup."
        )

        print(f"\nWARNING: {message}")

        send_telegram(
            title="Logs Backup Skipped",
            message=message,
            level="WARNING",
        )

        return

    try:
        os.makedirs(META_DATA_DIR, exist_ok=True)

        current_datetime = datetime.now(ZoneInfo(TIMEZONE)).strftime("%Y%m%d_%H%M%S")

        archive_base_name = os.path.join(
            META_DATA_DIR,
            f"logs_{current_datetime}",
        )

        archive_path = shutil.make_archive(
            base_name=archive_base_name,
            format="zip",
            root_dir=PROJECT_DIR,
            base_dir="logs",
        )

        print("\nLogs backup created successfully:")
        print(archive_path)

        shutil.rmtree(LOGS_DIR)

        send_telegram(
            title="Logs Backup Completed",
            message=(
                "Logs backup created successfully.\n"
                f"Backup: {os.path.basename(archive_path)}\n"
                "Original logs directory removed."
            ),
            level="SUCCESS",
        )

    except Exception as error:
        error_message = (
            "Failed to create logs backup.\n"
            f"Error: {type(error).__name__}: {error}"
        )

        print(f"\nERROR: {error_message}")

        send_telegram(
            title="Logs Backup Failed",
            message=error_message,
            level="ERROR",
        )

        sys.exit(1)


# ---------------------------------------------------------------------------
# Folder removal
# ---------------------------------------------------------------------------
def remove_folders() -> None:
    """
    Explicitly remove every folder in REMOVE_PATHS that is not in
    PRESERVE_PATHS.

    Preserve wins on conflict: paths present in both lists are printed
    as conflicts and no removal action is performed for them.

    Refuses to remove:
      * the project root
      * any path outside the project root
      * any ancestor of a preserved path
    """
    print("\n==========================================")
    print("  Removing Folders")
    print("==========================================")

    send_telegram(
        title="Folder Removal Started",
        message="Starting explicit folder removal.",
        level="INFO",
    )

    effective, conflicts = _resolve_removal_targets()

    print("\nPreserve paths (never removed):")
    if PRESERVE_PATHS:
        for path in PRESERVE_PATHS:
            print(f"  - {path}")
    else:
        print("  (none)")

    print("\nRemove paths (candidates):")
    if REMOVE_PATHS:
        for path in REMOVE_PATHS:
            print(f"  - {path}")
    else:
        print("  (none)")

    if conflicts:
        print("\nConflicts (preserve wins, no action performed):")
        for path in conflicts:
            print(f"  - {path}")
    else:
        print("\nConflicts (preserve wins, no action performed):")
        print("  (none)")

    print("\nEffective removal:")
    if effective:
        for path in effective:
            print(f"  - {path}")
    else:
        print("  (none)")
        send_telegram(
            title="Folder Removal Skipped",
            message="No effective removal paths after preserve conflicts.",
            level="INFO",
        )
        return

    print()

    removed_count = 0
    skipped_count = 0
    error_count = 0

    for relative_path in effective:
        matches = _expand_removal_patterns(relative_path)

        if not matches:
            print(f"Skipped (not present): {relative_path}")
            skipped_count += 1
            continue

        for match in matches:
            try:
                rel = match.relative_to(Path(PROJECT_DIR))
            except Exception:
                rel = match

            if not _is_safe_remove_target(match):
                print(f"REFUSED (unsafe target): {rel}")
                skipped_count += 1
                continue

            try:
                shutil.rmtree(match)
                print(f"Removed: {rel}")
                removed_count += 1
            except Exception as error:
                print(f"FAILED to remove {rel}: {type(error).__name__}: {error}")
                error_count += 1

    print()
    print(
        f"Summary: removed={removed_count} "
        f"skipped={skipped_count} errors={error_count}"
    )

    send_telegram(
        title="Folder Removal Completed",
        message=(
            "Explicit folder removal finished.\n"
            f"Removed: {removed_count}\n"
            f"Skipped: {skipped_count}\n"
            f"Errors:  {error_count}"
        ),
        level="SUCCESS" if error_count == 0 else "WARNING",
    )


# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------
def main() -> None:
    print("==========================================")
    print(f"  {PROJECT_NAME}")
    print("  Project Update")
    print("==========================================")

    print("\nProject directory:")
    print(PROJECT_DIR)

    send_telegram(
        title="Project Update Started",
        message=(
            "Project update process started.\n"
            f"Project: {PROJECT_NAME}"
        ),
        level="STARTUP",
    )

    try:
        # 1. Backup logs
        backup_logs()

        # 2. Stop application
        stop_script = os.path.join(
            PROJECT_DIR,
            STOP_SCRIPT,
        )

        if os.path.exists(stop_script):
            print("\nStopping application...")

            send_telegram(
                title="Application Stop Started",
                message="Stopping the running application.",
                level="INFO",
            )

            run_command(
                ["bash", STOP_SCRIPT],
                step_name="Application Stop",
                check=False,
            )

        else:
            message = f"{STOP_SCRIPT} not found. Skipping application stop."

            print(f"\nWARNING: {message}")

            send_telegram(
                title="Application Stop Skipped",
                message=message,
                level="WARNING",
            )

        # 3. Fetch latest Git information
        print("\nFetching latest Git information...")

        run_command(
            ["git", "fetch", "--all"],
            step_name="Git Fetch",
        )

        # 4. Reset tracked changes
        print("\nResetting all local changes...")

        run_command(
            ["git", "reset", "--hard", "HEAD"],
            step_name="Git Reset",
        )

        # 5. Remove explicitly configured folders
        #    Runs AFTER git reset (so tracked files are already restored)
        #    and BEFORE git clean (so ignored generated folders that git
        #    clean would normally skip are still removed here).
        remove_folders()

        # 6. Remove remaining untracked files and directories.
        #    Preserve paths are excluded so runtime state, secrets, and
        #    cached market data survive the update.
        print("\n==========================================")
        print("  Running Git Clean")
        print("==========================================")

        print("\nPreserving during git clean:")
        for path in PRESERVE_PATHS:
            print(f"  - {path}")

        clean_command = ["git", "clean", "-fd"]
        for path in PRESERVE_PATHS:
            clean_command.extend(["-e", path])

        run_command(
            clean_command,
            step_name="Git Clean",
        )

        # 7. Pull latest code
        print("\nPulling latest code...")

        run_command(
            ["git", "pull"],
            step_name="Git Pull",
        )

        # 8. Set script permissions
        print("\nSetting script permissions...")

        send_telegram(
            title="Script Permissions Started",
            message="Updating start and stop script permissions.",
            level="INFO",
        )

        start_script = os.path.join(
            PROJECT_DIR,
            START_SCRIPT,
        )

        stop_script = os.path.join(
            PROJECT_DIR,
            STOP_SCRIPT,
        )

        if os.path.exists(start_script):
            run_command(
                ["chmod", "+x", START_SCRIPT],
                step_name="Start Script Permission Update",
            )

        else:
            message = f"{START_SCRIPT} not found."

            print(f"WARNING: {message}")

            send_telegram(
                title="Start Script Missing",
                message=message,
                level="WARNING",
            )

        if os.path.exists(stop_script):
            run_command(
                ["chmod", "+x", STOP_SCRIPT],
                step_name="Stop Script Permission Update",
            )

        else:
            message = f"{STOP_SCRIPT} not found."

            print(f"WARNING: {message}")

            send_telegram(
                title="Stop Script Missing",
                message=message,
                level="WARNING",
            )

        # 9. Show final Git status
        print("\nFinal Git status:")

        run_command(
            ["git", "status"],
            step_name="Final Git Status",
        )

        # 10. Start application
        if os.path.exists(start_script):
            print("\nStarting application...")

            send_telegram(
                title="Application Start Started",
                message="Starting the updated application.",
                level="STARTUP",
            )

            run_command(
                ["bash", START_SCRIPT],
                step_name="Application Start",
            )

        else:
            message = f"{START_SCRIPT} not found. Application was not started."

            print(f"\nWARNING: {message}")

            send_telegram(
                title="Application Start Skipped",
                message=message,
                level="WARNING",
            )

        print()
        print("==========================================")
        print("  Project update completed")
        print("==========================================")

        send_telegram(
            title="Project Update Completed",
            message=(
                "Project update completed successfully.\n"
                "Application update and startup process finished."
            ),
            level="SUCCESS",
        )

    except KeyboardInterrupt:
        message = "Project update interrupted by user."

        print(f"\nWARNING: {message}")

        send_telegram(
            title="Project Update Interrupted",
            message=message,
            level="WARNING",
        )

        sys.exit(130)

    except Exception as error:
        error_message = (
            "Unexpected exception during project update.\n"
            f"Error: {type(error).__name__}: {error}"
        )

        print(f"\nERROR: {error_message}")

        send_telegram(
            title="Project Update Failed",
            message=error_message,
            level="ERROR",
        )

        sys.exit(1)


if __name__ == "__main__":
    main()