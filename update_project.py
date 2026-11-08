# update_project.py
"""Project update flow: backup logs, stop, git pull, restart.

Also sends Telegram notifications during the update IF
``UPDATE_DEBUG_MODE=true`` (default false — otherwise every update
spams the operator channel). Notifications go through the project's
existing ``core.telegram_notifier`` module, so they obey the same
TELEGRAM_ENABLED / TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID gating as the
running application.

Usage
-----
    python3 update_project.py

    # or, more reliably, with the project venv:
    myenv/bin/python update_project.py

    # to enable Telegram notifications for this run only:
    UPDATE_DEBUG_MODE=true python3 update_project.py
"""

import os
import subprocess
import sys
import shutil
from datetime import datetime
from zoneinfo import ZoneInfo


# --------------------------------------------------------------------------- #
# Import core.telegram_notifier — with a graceful re-exec into myenv if the  #
# active Python is missing the project's dependencies (dotenv / requests).   #
# --------------------------------------------------------------------------- #
try:
    from core.telegram_notifier import (
        is_enabled as telegram_is_enabled,
        send_telegram_message,
    )
except ImportError as _import_err:
    _project_dir = os.path.dirname(os.path.abspath(__file__))
    _venv_python = os.path.join(_project_dir, "myenv", "bin", "python")
    _already_reexeced = os.environ.get("_UPDATE_REEXEC_DONE") == "1"

    # If we're running with system Python and a venv exists, re-exec into it
    # so subsequent imports resolve. Guarded against infinite re-exec.
    if (
        not _already_reexeced
        and os.path.isfile(_venv_python)
        and sys.executable != _venv_python
    ):
        os.environ["_UPDATE_REEXEC_DONE"] = "1"
        os.execv(_venv_python, [_venv_python, *sys.argv])

    print(
        f"ERROR: {_import_err}\n"
        "\n"
        "This usually means the active Python interpreter is missing the\n"
        "project's dependencies (python-dotenv, requests, pymongo, ...).\n"
        "\n"
        f"  current Python:  {sys.executable}\n"
        f"  project venv:    {_venv_python}\n"
        "\n"
        "Try running with the project venv:\n"
        f"  {_venv_python} update_project.py\n"
        "\n"
        "Or activate the venv first:\n"
        "  source myenv/bin/activate && python update_project.py",
        file=sys.stderr,
    )
    sys.exit(1)


PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))

START_SCRIPT = "start.sh"
STOP_SCRIPT = "stop.sh"

LOGS_DIR = os.path.join(PROJECT_DIR, "logs")
META_DATA_DIR = os.path.join(PROJECT_DIR, "meta_data")

TIMEZONE = "Asia/Kolkata"

# Local gate for update-time notifications. Default False so routine
# updates do not spam the operator channel. Set UPDATE_DEBUG_MODE=true
# in the shell (or in .env) when you want to see the full audit trail
# of a specific update.
DEBUG_MODE = (
    os.getenv("UPDATE_DEBUG_MODE", "false").strip().lower() == "true"
)


# --------------------------------------------------------------------------- #
# Telegram helper                                                             #
# --------------------------------------------------------------------------- #
_LEVEL_EMOJI = {
    "INFO": "ℹ️",
    "WARNING": "⚠️",
    "ERROR": "❌",
    "SUCCESS": "✅",
    "STARTUP": "🚀",
}


def send_telegram(
    title: str,
    message: str,
    level: str = "INFO",
) -> None:
    """Send a Telegram notification through ``core.telegram_notifier``.

    Silently no-ops when:

      * ``DEBUG_MODE`` is False (default — routine updates stay quiet)
      * Telegram is not enabled or not configured (``is_enabled()`` is
        False, so no HTTP call is attempted)

    A failure to send never interrupts the update flow. ``send_telegram_message``
    is contractually non-raising; the ``try/except`` below only guards the
    string-formatting path against unexpected ``title``/``message`` types.
    """
    if not DEBUG_MODE:
        return

    if not telegram_is_enabled():
        return

    emoji = _LEVEL_EMOJI.get(level.upper(), "ℹ️")
    text = f"{emoji} {title}\n{message}"

    try:
        send_telegram_message(text)
    except Exception as error:  # pragma: no cover - defensive
        print(f"WARNING: Telegram notification failed: {error}")


# --------------------------------------------------------------------------- #
# Shell helper                                                                #
# --------------------------------------------------------------------------- #
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


# --------------------------------------------------------------------------- #
# Log backup                                                                  #
# --------------------------------------------------------------------------- #
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

        current_datetime = datetime.now(ZoneInfo(TIMEZONE)).strftime(
            "%Y%m%d_%H%M%S"
        )

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


# --------------------------------------------------------------------------- #
# Main                                                                        #
# --------------------------------------------------------------------------- #
def main() -> None:
    print("==========================================")
    print("  Market Data Foundation")
    print("  Project Update")
    print("==========================================")

    print("\nProject directory:")
    print(PROJECT_DIR)

    telegram_state = (
        "ENABLED"
        if DEBUG_MODE and telegram_is_enabled()
        else "DISABLED"
    )
    print(f"\nTelegram (update flow): {telegram_state}")
    if not DEBUG_MODE:
        print(
            "  (set UPDATE_DEBUG_MODE=true to enable update notifications)"
        )

    send_telegram(
        title="Project Update Started",
        message=(
            "Project update process started.\n"
            f"Project: {os.path.basename(PROJECT_DIR)}"
        ),
        level="STARTUP",
    )

    try:
        # 1. Backup logs
        backup_logs()

        # 2. Stop application
        stop_script = os.path.join(PROJECT_DIR, STOP_SCRIPT)

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

        # 5. Remove untracked files and directories
        # Exclude meta_data so that log backups are preserved. .env and
        # data/ are gitignored, so `git clean -fd` (which honours
        # .gitignore) will not touch them.
        print("\nRemoving untracked files and directories except meta_data...")

        run_command(
            ["git", "clean", "-fd", "-e", "meta_data/"],
            step_name="Git Clean",
        )

        # 6. Pull latest code
        print("\nPulling latest code...")

        run_command(
            ["git", "pull"],
            step_name="Git Pull",
        )

        # 7. Set script permissions
        print("\nSetting script permissions...")

        send_telegram(
            title="Script Permissions Started",
            message="Updating start and stop script permissions.",
            level="INFO",
        )

        start_script = os.path.join(PROJECT_DIR, START_SCRIPT)

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

        # 8. Show final Git status
        print("\nFinal Git status:")

        run_command(
            ["git", "status"],
            step_name="Final Git Status",
        )

        # 9. Start application
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
            message = (
                f"{START_SCRIPT} not found. Application was not started."
            )

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