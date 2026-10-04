from pathlib import Path
import subprocess
import shutil
import os


# Default file to open when "y" is entered
DEFAULT_FILE = Path("files.txt")


def clear_screen():
    """Clear the terminal screen."""
    os.system("cls" if os.name == "nt" else "clear")


def open_in_vscode(path: Path):
    """Open a file in VS Code."""
    code_command = shutil.which("code")

    if not code_command:
        # Common VS Code installation locations on Windows
        possible_paths = [
            Path.home() / "AppData/Local/Programs/Microsoft VS Code/bin/code.cmd",
            Path("C:/Program Files/Microsoft VS Code/bin/code.cmd"),
            Path("C:/Program Files (x86)/Microsoft VS Code/bin/code.cmd"),
        ]

        for candidate in possible_paths:
            if candidate.exists():
                code_command = str(candidate)
                break

    if not code_command:
        print("Could not find VS Code.")
        return

    try:
        subprocess.Popen(
            [code_command, "--reuse-window", str(path)],
            shell=True
        )
        print(f"Opened in VS Code: {path}")

    except Exception as e:
        print(f"Could not open in VS Code: {e}")


def create_file():
    clear_screen()

    print("=== Empty File Creator ===")
    print("Enter a file path to create it.")
    print("Enter 'y' to open files.txt.")
    print("Enter 'n' to stop.")
    print("Press Ctrl+C anytime to exit.\n")

    while True:
        try:
            filepath = input("Enter file path / y / n: ").strip()

            # -----------------------------------------
            # Open default files.txt
            # -----------------------------------------
            if filepath.lower() == "y":
                if not DEFAULT_FILE.exists():
                    DEFAULT_FILE.touch()
                    print(f"\nCreated: {DEFAULT_FILE}")

                open_in_vscode(DEFAULT_FILE)

                clear_screen()
                continue

            # -----------------------------------------
            # Stop
            # -----------------------------------------
            if filepath.lower() == "n":
                print("Stopped.")
                break

            # -----------------------------------------
            # Empty input
            # -----------------------------------------
            if not filepath:
                print("Please enter a valid file path.")
                input("\nPress Enter to continue...")
                clear_screen()
                continue

            # -----------------------------------------
            # Create requested file
            # -----------------------------------------
            path = Path(filepath)

            # Create parent directories
            path.parent.mkdir(parents=True, exist_ok=True)

            # Create empty file
            path.touch(exist_ok=True)

            print(f"\nCreated: {path}")

            # Open created file in VS Code
            open_in_vscode(path)

            # Clear screen before next iteration
            clear_screen()

        except KeyboardInterrupt:
            print("\n\nStopped by Ctrl+C.")
            break

        except Exception as e:
            print(f"\nError: {e}")
            input("\nPress Enter to continue...")
            clear_screen()


if __name__ == "__main__":
    create_file()
