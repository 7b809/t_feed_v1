import os
import subprocess
import sys

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))

START_SCRIPT = "start.sh"
STOP_SCRIPT = "stop.sh"


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
        print(f"\nERROR: Command failed with exit code " f"{result.returncode}")
        sys.exit(result.returncode)


def main() -> None:
    print("==========================================")
    print("  Upstox Order Request Receiver")
    print("  Project Update")
    print("==========================================")

    print(f"\nProject directory:")
    print(PROJECT_DIR)

    # ---------------------------------------------------------
    # 1. Stop running application
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
    # 2. Fetch latest remote information
    # ---------------------------------------------------------
    print("\nFetching latest Git information...")

    run_command(
        ["git", "fetch", "--all"],
    )

    # ---------------------------------------------------------
    # 3. Reset all tracked changes
    # ---------------------------------------------------------
    print("\nResetting all local changes...")

    run_command(
        ["git", "reset", "--hard", "HEAD"],
    )

    # ---------------------------------------------------------
    # 4. Remove untracked files/directories
    # ---------------------------------------------------------
    print("\nRemoving untracked files and directories...")

    run_command(
        ["git", "clean", "-fd"],
    )

    # ---------------------------------------------------------
    # 5. Pull latest code
    # ---------------------------------------------------------
    print("\nPulling latest code...")

    run_command(
        ["git", "pull"],
    )

    # ---------------------------------------------------------
    # 6. Make start.sh and stop.sh executable
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
    # 7. Show final Git status
    # ---------------------------------------------------------
    print("\nFinal Git status:")

    run_command(
        ["git", "status"],
    )

    # ---------------------------------------------------------
    # 8. Start application
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
