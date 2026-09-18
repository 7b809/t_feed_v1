
import os
import subprocess
import sys
import shutil
from datetime import datetime
from zoneinfo import ZoneInfo


PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))

START_SCRIPT = "start.sh"
STOP_SCRIPT = "stop.sh"

LOGS_DIR = os.path.join(PROJECT_DIR, "logs")
META_DATA_DIR = os.path.join(PROJECT_DIR, "meta_data")

TIMEZONE = "Asia/Kolkata"


def run_command(
    command: list[str],
    check: bool = True,
) -> None:
    print()
    print(f"> {' '.join(command)}")

    result = subprocess.run(
        command,
        cwd=PROJECT_DIR,
    )

    if check and result.returncode != 0:
        print(
            f"\nERROR: Command failed with exit code "
            f"{result.returncode}"
        )
        sys.exit(result.returncode)


def backup_logs() -> None:
    print("\n==========================================")
    print("  Creating Logs Backup")
    print("==========================================")

    if not os.path.exists(LOGS_DIR):
        print(f"\nWARNING: Logs directory not found: {LOGS_DIR}")
        print("Skipping logs backup.")
        return

    os.makedirs(META_DATA_DIR, exist_ok=True)

    current_datetime = datetime.now(
        ZoneInfo(TIMEZONE)
    ).strftime("%Y%m%d_%H%M%S")

    archive_base_name = os.path.join(
        META_DATA_DIR,
        f"logs_{current_datetime}",
    )

    try:
        archive_path = shutil.make_archive(
            base_name=archive_base_name,
            format="zip",
            root_dir=PROJECT_DIR,
            base_dir="logs",
        )

        print("\nLogs backup created successfully:")
        print(archive_path)

    except Exception as error:
        print("\nERROR: Failed to create logs backup.")
        print(error)
        sys.exit(1)


def main() -> None:
    print("==========================================")
    print("  Upstox Order Request Receiver")
    print("  Project Update")
    print("==========================================")

    print("\nProject directory:")
    print(PROJECT_DIR)

    # ---------------------------------------------------------
    # 1. Backup logs before doing anything
    # ---------------------------------------------------------
    backup_logs()

    # ---------------------------------------------------------
    # 2. Stop running application
    # ---------------------------------------------------------
    stop_script = os.path.join(
        PROJECT_DIR,
        STOP_SCRIPT,
    )

    if os.path.exists(stop_script):
        print("\nStopping application...")

        run_command(
            ["bash", STOP_SCRIPT],
            check=False,
        )

    else:
        print(f"\nWARNING: {STOP_SCRIPT} not found.")

    # ---------------------------------------------------------
    # 3. Fetch latest remote information
    # ---------------------------------------------------------
    print("\nFetching latest Git information...")

    run_command(
        ["git", "fetch", "--all"],
    )

    # ---------------------------------------------------------
    # 4. Reset all tracked changes
    # ---------------------------------------------------------
    print("\nResetting all local changes...")

    run_command(
        ["git", "reset", "--hard", "HEAD"],
    )

    # ---------------------------------------------------------
    # 5. Remove untracked files/directories
    # ---------------------------------------------------------
    print("\nRemoving untracked files and directories...")

    run_command(
        ["git", "clean", "-fd"],
    )

    # ---------------------------------------------------------
    # 6. Pull latest code
    # ---------------------------------------------------------
    print("\nPulling latest code...")

    run_command(
        ["git", "pull"],
    )

    # ---------------------------------------------------------
    # 7. Make start.sh and stop.sh executable
    # ---------------------------------------------------------
    print("\nSetting script permissions...")

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
        )

    else:
        print(f"WARNING: {START_SCRIPT} not found.")

    if os.path.exists(stop_script):
        run_command(
            ["chmod", "+x", STOP_SCRIPT],
        )

    else:
        print(f"WARNING: {STOP_SCRIPT} not found.")

    # ---------------------------------------------------------
    # 8. Show final Git status
    # ---------------------------------------------------------
    print("\nFinal Git status:")

    run_command(
        ["git", "status"],
    )

    # ---------------------------------------------------------
    # 9. Start application
    # ---------------------------------------------------------
    if os.path.exists(start_script):
        print("\nStarting application...")

        run_command(
            ["bash", START_SCRIPT],
        )

    else:
        print(f"\nWARNING: {START_SCRIPT} not found.")

    print()
    print("==========================================")
    print("  Project update completed")
    print("==========================================")


if __name__ == "__main__":
    main()