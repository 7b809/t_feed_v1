"""Execution timing and optional elapsed progress display."""
from __future__ import annotations
import subprocess
import sys
import time

def format_duration(seconds: float) -> str:
    if seconds < 1.0:
        return f"{seconds * 1000:.0f} ms"
    if seconds < 60.0:
        return f"{seconds:.2f} s"
    minutes = int(seconds // 60)
    return f"{minutes}m {seconds % 60:.2f}s"

def clear_current_line(stream=sys.stderr) -> None:
    if stream.isatty():
        stream.write("\r\x1b[2K")
        stream.flush()

def _print_elapsed(seconds: int, stream=sys.stderr) -> None:
    clear_current_line(stream)
    label = "second" if seconds == 1 else "seconds"
    message = f"[PROGRESS] {seconds} {label} completed"
    if stream.isatty():
        stream.write(message)
        stream.flush()
    else:
        print(message, file=stream, flush=True)

def wait_with_progress(process: subprocess.Popen, start_perf: float,
                       interval_sec: int = 5, enabled: bool = False) -> int:
    if not enabled:
        return process.wait()
    if interval_sec < 1:
        raise ValueError("Progress interval must be at least one second.")
    next_milestone = interval_sec
    try:
        while True:
            return_code = process.poll()
            if return_code is not None:
                clear_current_line(sys.stderr)
                return return_code
            elapsed = time.perf_counter() - start_perf
            if elapsed >= next_milestone:
                _print_elapsed(next_milestone, sys.stderr)
                next_milestone += interval_sec
            time.sleep(min(0.25, max(0.0, next_milestone - (time.perf_counter() - start_perf))))
    except KeyboardInterrupt:
        clear_current_line(sys.stderr)
        process.terminate()
        try:
            return process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            return process.wait()
