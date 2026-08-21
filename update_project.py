#!/usr/bin/env python3

import subprocess
import sys


# ============================================================
# Configuration
# ============================================================

BRANCH = "ws-feed-v2-dev"


# ============================================================
# Helpers
# ============================================================

def run_command(command):
    """
    Run a shell command and stop the script if it fails.
    """

    print()
    print(f">>> {' '.join(command)}")

    try:
        result = subprocess.run(
            command,
            check=True,
            text=True,
        )

        return result.returncode

    except subprocess.CalledProcessError as exc:
        print()
        print(
            f"ERROR: Command failed: {' '.join(command)}"
        )
        print(
            f"Exit code: {exc.returncode}"
        )

        sys.exit(exc.returncode)


# ============================================================
# Main Update Process
# ============================================================

def main():

    print("=" * 50)
    print("       T Feed Engine - Git Update")
    print("=" * 50)

    # --------------------------------------------------------
    # 1. Discard tracked local modifications
    # --------------------------------------------------------

    run_command([
        "git",
        "restore",
        ".",
    ])

    # --------------------------------------------------------
    # 2. Remove untracked files/directories
    # --------------------------------------------------------

    run_command([
        "git",
        "clean",
        "-fd",
    ])

    # --------------------------------------------------------
    # 3. Fetch latest remote information
    # --------------------------------------------------------

    run_command([
        "git",
        "fetch",
        "origin",
    ])

    # --------------------------------------------------------
    # 4. Make local branch exactly match remote branch
    #
    # This is intentional because ws-feed-v2-dev may have
    # been force-updated on GitHub.
    # --------------------------------------------------------

    run_command([
        "git",
        "reset",
        "--hard",
        f"origin/{BRANCH}",
    ])

    # --------------------------------------------------------
    # 5. Clean again after reset
    # --------------------------------------------------------

    run_command([
        "git",
        "clean",
        "-fd",
    ])

    # --------------------------------------------------------
    # 6. Show final status
    # --------------------------------------------------------

    print()
    print(">>> git status")

    subprocess.run(
        [
            "git",
            "status",
        ],
        check=False,
    )

    print()
    print("=" * 50)
    print("       Git Update Completed Successfully")
    print("=" * 50)
    print()


# ============================================================
# Entry Point
# ============================================================

if __name__ == "__main__":
    main()