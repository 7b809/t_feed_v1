#!/usr/bin/env python3
"""Safely update the sandbox_v1 service: back up logs, stop, sync, restart.

Run from the project root with:
    python3 update_project.py
    myenv/bin/python update_project.py

Optional update notifications:
    UPDATE_DEBUG_MODE=true python3 update_project.py

Telegram notifications are optional. The script first uses the project's
core.telegram_notifier when available; otherwise it can use the Telegram
environment variables directly through Python's standard library.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


PROJECT_DIR = Path(__file__).resolve().parent
BRANCH = "sandbox_v1"
REMOTE = "origin"
START_SCRIPT = "start.sh"
STOP_SCRIPT = "stop.sh"
LOGS_DIR = PROJECT_DIR / "logs"
META_DATA_DIR = PROJECT_DIR / "meta_data"
TIMEZONE = "Asia/Kolkata"

# Never silently discard tracked local edits. Set this to true only when
# intentionally allowing the updater to replace tracked working-tree changes.
ALLOW_DIRTY_RESET = os.getenv("UPDATE_ALLOW_DIRTY_RESET", "false").strip().lower() == "true"

_LEVEL_EMOJI = {
    "INFO": "ℹ️",
    "WARNING": "⚠️",
    "ERROR": "❌",
    "SUCCESS": "✅",
    "STARTUP": "🚀",
}


def load_optional_env() -> None:
    """Load only updater/Telegram settings from .env without extra packages."""
    env_file = PROJECT_DIR / ".env"
    if not env_file.is_file():
        return

    try:
        for raw_line in env_file.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if key not in {
                "UPDATE_DEBUG_MODE",
                "TELEGRAM_ENABLED",
                "TELEGRAM_BOT_TOKEN",
                "TELEGRAM_CHAT_ID",
            }:
                continue
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)
    except OSError as error:
        print(f"WARNING: Could not read optional .env settings: {error}")


load_optional_env()
DEBUG_MODE = os.getenv("UPDATE_DEBUG_MODE", "false").strip().lower() == "true"


def telegram_is_enabled() -> bool:
    if os.getenv("TELEGRAM_ENABLED", "false").strip().lower() != "true":
        return False
    return bool(os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"))


def send_telegram(title: str, message: str, level: str = "INFO") -> None:
    """Best-effort notification; notification failures never stop an update."""
    if not telegram_is_enabled():
        return
    # Keep per-step INFO chatter behind UPDATE_DEBUG_MODE, but always send
    # update start, completion, warning and failure events.
    if level.upper() == "INFO" and not DEBUG_MODE:
        return

    text = f"{_LEVEL_EMOJI.get(level.upper(), 'ℹ️')} {title}\n{message}"
    try:
        sys.path.insert(0, str(PROJECT_DIR))
        from core.telegram_notifier import send_telegram_message
        send_telegram_message(text)
        return
    except Exception as error:
        print(f"WARNING: Shared Telegram notifier unavailable: {error}")

    # Keep the updater usable with system Python if app dependencies are
    # installed only inside myenv.
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return
    try:
        payload = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=payload,
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=8) as response:
            if response.status >= 300:
                raise RuntimeError(f"Telegram HTTP status {response.status}")
    except Exception as error:
        print(f"WARNING: Telegram notification failed: {error}")


def run_command(
    command: list[str],
    step_name: str,
    *,
    check: bool = True,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run a command in the project directory and report useful failures."""
    print(f"\n[UPDATE] {step_name}")
    print(f"$ {' '.join(command)}")

    try:
        result = subprocess.run(
            command,
            cwd=PROJECT_DIR,
            text=True,
            check=False,
            capture_output=capture_output,
        )
    except OSError as error:
        message = f"{step_name} could not run: {type(error).__name__}: {error}"
        print(f"ERROR: {message}")
        send_telegram(f"{step_name} Failed", message, "ERROR")
        if check:
            raise RuntimeError(message) from error
        return subprocess.CompletedProcess(command, 1, "", str(error))

    if capture_output and result.stdout:
        print(result.stdout.rstrip())
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        message = f"{step_name} failed (exit code {result.returncode})."
        if detail:
            message += f"\n{detail[-2500:]}"
        print(f"ERROR: {message}")
        send_telegram(f"{step_name} Failed", message, "ERROR")
        if check:
            raise RuntimeError(message)
    else:
        print(f"[OK] {step_name}")
        send_telegram(f"{step_name} Completed", f"{step_name} completed.", "INFO")

    return result


def verify_project_branch() -> None:
    result = run_command(
        ["git", "branch", "--show-current"],
        "Verify Git branch",
        capture_output=True,
    )
    current_branch = (result.stdout or "").strip()
    if current_branch != BRANCH:
        raise RuntimeError(
            f"Updater must run on '{BRANCH}', but current branch is "
            f"'{current_branch or 'detached HEAD'}'. No update was performed."
        )


def verify_no_tracked_changes() -> None:
    result = run_command(
        ["git", "status", "--porcelain"],
        "Check working tree",
        capture_output=True,
    )
    lines = (result.stdout or "").splitlines()
    # Ignore untracked entries: the updater's cleanup step handles those.
    tracked_changes = [
        line for line in lines
        if line and not line.startswith("??")
    ]
    if tracked_changes and not ALLOW_DIRTY_RESET:
        preview = "\n".join(tracked_changes[:20])
        raise RuntimeError(
            "Tracked local changes were found. To protect them, the update "
            "was stopped before reset/pull. Commit or stash these changes, "
            "or explicitly set UPDATE_ALLOW_DIRTY_RESET=true to discard them.\n"
            f"{preview}"
        )


def backup_logs() -> Path | None:
    print("\n==========================================")
    print("  Sandbox Logs Backup")
    print("==========================================")
    send_telegram("Logs Backup Started", "Backing up sandbox logs.", "INFO")

    if not LOGS_DIR.is_dir():
        print(f"WARNING: Logs directory not found: {LOGS_DIR}. Skipping backup.")
        send_telegram("Logs Backup Skipped", "No logs directory was found.", "WARNING")
        return None

    META_DATA_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(ZoneInfo(TIMEZONE)).strftime("%Y%m%d_%H%M%S")
    archive_base = META_DATA_DIR / f"logs_{timestamp}"

    try:
        archive = Path(
            shutil.make_archive(
                base_name=str(archive_base),
                format="zip",
                root_dir=str(PROJECT_DIR),
                base_dir="logs",
            )
        )
        if not archive.is_file() or archive.stat().st_size == 0:
            raise RuntimeError("Archive was not created or is empty.")

        # Remove source logs only after the archive has been verified.
        shutil.rmtree(LOGS_DIR)
        print(f"[OK] Logs backup created: {archive}")
        send_telegram(
            "Logs Backup Completed",
            f"Backup created: {archive.name}\nOriginal logs directory cleared.",
            "SUCCESS",
        )
        return archive
    except Exception as error:
        message = f"Could not back up logs: {type(error).__name__}: {error}"
        print(f"ERROR: {message}")
        send_telegram("Logs Backup Failed", message, "ERROR")
        raise


def main() -> int:
    print("==========================================")
    print("  Sandbox Project Update")
    print("==========================================")
    print(f"Project: {PROJECT_DIR}")
    print(f"Target:  {REMOTE}/{BRANCH}")
    print("Service port: configured by start.sh / .env (default 8004)")
    print(f"Telegram update notifications: {'ENABLED' if telegram_is_enabled() else 'DISABLED'}")

    send_telegram(
        "Sandbox Update Started",
        f"Updating {PROJECT_DIR.name} from {REMOTE}/{BRANCH}.",
        "STARTUP",
    )

    try:
        # Verify the checkout before making any local changes.
        verify_project_branch()
        verify_no_tracked_changes()

        # 1. Back up logs before stopping the service.
        backup_logs()

        # 2. Stop the currently running application.
        stop_path = PROJECT_DIR / STOP_SCRIPT
        if stop_path.is_file():
            run_command(["bash", STOP_SCRIPT], "Stop sandbox application")
        else:
            print(f"WARNING: {STOP_SCRIPT} not found; stop step skipped.")

        # 3. Fetch only the intended remote branch.
        run_command(
            ["git", "fetch", REMOTE, BRANCH],
            "Fetch latest sandbox_v1",
        )

        # 4. Synchronize exactly to origin/sandbox_v1.
        # This is equivalent to the original reset + pull flow, but avoids
        # accidentally pulling a different upstream branch.
        if ALLOW_DIRTY_RESET:
            print("WARNING: UPDATE_ALLOW_DIRTY_RESET=true; tracked edits may be discarded.")
        run_command(
            ["git", "reset", "--hard", f"{REMOTE}/{BRANCH}"],
            "Reset to origin/sandbox_v1",
        )

        # 5. Remove ordinary untracked files only. Explicit exclusions document
        # runtime data that must survive even if .gitignore changes later.
        run_command(
            [
                "git", "clean", "-fd",
                "-e", ".env",
                "-e", "data/",
                "-e", "data/orders/",
                "-e", "myenv/",
                "-e", "meta_data/",
                "-e", "logs/",
            ],
            "Clean untracked project files",
        )

        # 6. Ensure shell scripts remain executable.
        for script in (START_SCRIPT, STOP_SCRIPT):
            if (PROJECT_DIR / script).is_file():
                run_command(["chmod", "+x", script], f"Set {script} executable")
            else:
                print(f"WARNING: {script} not found.")

        # 7. Show the resulting checkout.
        run_command(["git", "status", "--short", "--branch"], "Final Git status")

        # 8. Restart the service using the existing project startup script.
        if not (PROJECT_DIR / START_SCRIPT).is_file():
            raise RuntimeError(f"{START_SCRIPT} is missing; application cannot be restarted.")

        run_command(["bash", START_SCRIPT], "Start updated sandbox application")

        print("\n==========================================")
        print("  Sandbox update completed successfully")
        print("==========================================")
        print("Runtime data preserved: .env, data/orders/, myenv/, meta_data/")
        print("Application port remains controlled by start.sh / .env (default 8004).")
        send_telegram(
            "Sandbox Update Completed",
            "Code synchronized and application startup script completed.",
            "SUCCESS",
        )
        return 0

    except KeyboardInterrupt:
        print("\nWARNING: Update interrupted by user.")
        send_telegram("Sandbox Update Interrupted", "Update was interrupted.", "WARNING")
        return 130
    except Exception as error:
        message = f"{type(error).__name__}: {error}"
        print(f"\nUPDATE FAILED: {message}")
        send_telegram("Sandbox Update Failed", message, "ERROR")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
