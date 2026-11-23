import argparse
from datetime import datetime
import os
import shutil
import subprocess
import sys
import time
from typing import List, Optional

from dotenv import load_dotenv
from fetch_token import load_upstox_access_token


def format_duration(seconds: float) -> str:
    if seconds < 1.0:
        return f"{seconds * 1000:.0f} ms"
    if seconds < 60.0:
        return f"{seconds:.2f} s"
    minutes = int(seconds // 60)
    rem = seconds % 60
    return f"{minutes}m {rem:.2f}s"


def _clear_current_line(stream=sys.stderr) -> None:
    """Clear only the current/previous single status line."""
    if stream.isatty():
        stream.write("\r\x1b[2K")
        stream.flush()


def _print_elapsed_status(seconds_elapsed: int, stream=sys.stderr) -> None:
    """Overwrite only the previous seconds status line with the new count."""
    _clear_current_line(stream)
    label = "second" if seconds_elapsed == 1 else "seconds"
    msg = f"[PROGRESS] {seconds_elapsed} {label} completed"
    if stream.isatty():
        stream.write(msg)
        stream.flush()
    else:
        print(msg, file=stream, flush=True)


def _wait_with_progress(
    proc: subprocess.Popen,
    start_perf: float,
    interval_sec: int = 5,
    enabled: bool = False,
) -> int:
    """Wait for process completion while optionally updating elapsed seconds in place."""
    if not enabled:
        return proc.wait()

    next_milestone = interval_sec
    try:
        while True:
            rc = proc.poll()
            if rc is not None:
                _clear_current_line(sys.stderr)
                return rc

            elapsed = time.perf_counter() - start_perf
            if elapsed >= next_milestone:
                _print_elapsed_status(next_milestone, sys.stderr)
                next_milestone += interval_sec

            # Short sleep keeps milestone timing responsive and Ctrl+C snappy
            sleep_for = min(0.25, max(0.0, next_milestone - (time.perf_counter() - start_perf)))
            time.sleep(sleep_for)
    except KeyboardInterrupt:
        _clear_current_line(sys.stderr)
        proc.terminate()
        try:
            return proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            return proc.wait()


def launch_agy_with_token(
    prompt: Optional[str] = None,
    model: Optional[str] = None,
    skip_permissions: bool = False,
    output_format: Optional[str] = None,
    env_path: str = ".env",
    show_progress: bool = False,
    progress_interval: int = 5,
) -> int:
    start_dt = datetime.now().astimezone()
    start_perf = time.perf_counter()
    tz_label = start_dt.tzname() or ""
    print(f"[TIMING] Start:    {start_dt.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]} {tz_label}".rstrip())

    return_code = 1
    try:
        load_dotenv(dotenv_path=env_path, override=False)
        token = load_upstox_access_token(env_path=env_path)

        env = os.environ.copy()
        env["UPSTOX_ACCESS_TOKEN"] = token

        gemini_api_key = os.getenv("GEMINI_API_KEY", "").strip()
        if gemini_api_key:
            env["GEMINI_API_KEY"] = gemini_api_key

        agy_cmd = shutil.which("agy") or os.path.expanduser("~/.local/bin/agy")
        if not os.path.isfile(agy_cmd):
            raise FileNotFoundError(
                "Could not find 'agy'. Run: curl -fsSL https://antigravity.google/cli/install.sh | bash"
            )

        cmd: List[str] = [agy_cmd]
        if model:
            cmd.extend(["--model", model])
        if skip_permissions:
            cmd.append("--dangerously-skip-permissions")
        if output_format:
            cmd.extend(["--output-format", output_format])
        if prompt:
            cmd.extend(["-p", prompt])

        proc = subprocess.Popen(cmd, env=env)
        return_code = _wait_with_progress(
            proc=proc,
            start_perf=start_perf,
            interval_sec=progress_interval,
            enabled=show_progress,
        )
        return return_code
    finally:
        if show_progress:
            _clear_current_line(sys.stderr)
        end_dt = datetime.now().astimezone()
        elapsed_sec = time.perf_counter() - start_perf
        end_tz = end_dt.tzname() or ""
        print(f"[TIMING] End:      {end_dt.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]} {end_tz}".rstrip())
        print(f"[TIMING] Duration: {format_duration(elapsed_sec)} (exit code: {return_code})")


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Load Upstox access token from MongoDB and launch Antigravity CLI (agy)."
    )
    parser.add_argument(
        "positional_prompt",
        nargs="?",
        default=None,
        help="Optional positional prompt for non-interactive (-p) execution.",
    )
    parser.add_argument(
        "-p",
        "--prompt",
        dest="flag_prompt",
        default=None,
        help="Optional prompt flag for non-interactive (-p) execution.",
    )
    parser.add_argument(
        "-m",
        "--model",
        default=None,
        help="Optional model override (run 'agy models' to list available models).",
    )
    parser.add_argument(
        "--skip-permissions",
        action="store_true",
        help="Pass --dangerously-skip-permissions to agy to skip tool approval prompts.",
    )
    parser.add_argument(
        "--output-format",
        choices=["text", "json", "stream-json"],
        default=None,
        help="Optional agy output format for headless runs.",
    )
    parser.add_argument(
        "--env-file",
        default=".env",
        help="Path to .env file (default: .env).",
    )
    parser.add_argument(
        "-y",
        "--elapsed-log",
        action="store_true",
        default=False,
        help="Show elapsed seconds every interval (disabled by default).",
    )
    parser.add_argument(
        "--progress-interval",
        type=int,
        default=5,
        help="Interval in seconds for -y elapsed updates (default: 5).",
    )
    args = parser.parse_args(argv)
    args.prompt = args.flag_prompt if args.flag_prompt is not None else args.positional_prompt
    return args


if __name__ == "__main__":
    try:
        args = parse_args()
        sys.exit(
            launch_agy_with_token(
                prompt=args.prompt,
                model=args.model,
                skip_permissions=args.skip_permissions,
                output_format=args.output_format,
                env_path=args.env_file,
                show_progress=args.elapsed_log,
                progress_interval=args.progress_interval,
            )
        )
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        sys.exit(1)
