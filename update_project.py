import os
import subprocess
import sys


# ============================================================
# Configuration
# ============================================================

PROJECT_DIR = os.path.dirname(
    os.path.abspath(__file__)
)

START_SCRIPT = "start.sh"
STOP_SCRIPT = "stop.sh"


# ============================================================
# Run Command
# ============================================================

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
        print()
        print(
            "ERROR: Command failed "
            f"with exit code {result.returncode}"
        )

        sys.exit(result.returncode)


# ============================================================
# Main
# ============================================================

def main() -> None:

    print("==========================================")
    print("     Project Update Service")
    print("==========================================")

    print()
    print("Project directory:")
    print(PROJECT_DIR)

    # ========================================================
    # 1. Stop running application
    # ========================================================

    stop_script = os.path.join(
        PROJECT_DIR,
        STOP_SCRIPT,
    )

    if os.path.exists(stop_script):

        print()
        print("Stopping application...")

        run_command(
            ["bash", STOP_SCRIPT],
            check=False,
        )

    else:

        print()
        print(
            f"WARNING: {STOP_SCRIPT} not found."
        )

    # ========================================================
    # 2. Fetch latest Git information
    # ========================================================

    print()
    print("Fetching latest Git information...")

    run_command(
        ["git", "fetch", "--all"],
    )

    # ========================================================
    # 3. Reset local tracked changes
    # ========================================================

    print()
    print("Resetting all local tracked changes...")

    run_command(
        ["git", "reset", "--hard", "HEAD"],
    )

    # ========================================================
    # 4. Remove untracked files/directories
    # ========================================================

    print()
    print("Removing untracked files and directories...")

    run_command(
        ["git", "clean", "-fd"],
    )

    # ========================================================
    # 5. Pull latest code
    # ========================================================

    print()
    print("Pulling latest code...")

    run_command(
        ["git", "pull"],
    )

    # ========================================================
    # 6. Set script permissions
    # ========================================================

    print()
    print("Setting script permissions...")

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

        print(
            f"WARNING: {START_SCRIPT} not found."
        )

    if os.path.exists(stop_script):

        run_command(
            ["chmod", "+x", STOP_SCRIPT],
        )

    else:

        print(
            f"WARNING: {STOP_SCRIPT} not found."
        )

    # ========================================================
    # 7. Show final Git status
    # ========================================================

    print()
    print("Final Git status:")

    run_command(
        ["git", "status"],
    )

    # ========================================================
    # 8. Start application
    # ========================================================

    if os.path.exists(start_script):

        print()
        print("Starting application...")

        run_command(
            ["bash", START_SCRIPT],
        )

    else:

        print()
        print(
            f"WARNING: {START_SCRIPT} not found."
        )

    # ========================================================
    # Completed
    # ========================================================

    print()
    print("==========================================")
    print("     Project update completed")
    print("==========================================")


# ============================================================
# Entry Point
# ============================================================

if __name__ == "__main__":
    main()