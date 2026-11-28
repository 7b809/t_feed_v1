"""Launch AGY with the existing Upstox token and safe execution logging."""
from __future__ import annotations
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional
from .config import app_dir, execution_log_path, resolve_env_file
from .timing import clear_current_line, format_duration, wait_with_progress

def resolve_agy_path() -> Optional[str]:
    candidate = shutil.which("agy")
    if candidate:
        return candidate
    fallback = Path.home() / ".local" / "bin" / "agy"
    return str(fallback) if fallback.is_file() else None

def _write_execution_log(record: dict) -> None:
    """Append metadata only; never record prompts, tokens, or environment values."""
    path = execution_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(path.parent, 0o700)
        except OSError:
            pass
        fd = os.open(str(path), os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, sort_keys=True) + "\n")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError as exc:
        print(f"[WARN] Could not write execution log: {exc}", file=sys.stderr)

def launch_agy_with_token(prompt: Optional[str] = None, model: Optional[str] = None,
        skip_permissions: bool = False, output_format: Optional[str] = None,
        env_path: str = ".env", show_progress: bool = False, progress_interval: int = 5,
        command_name: str = "prompt", skill_name: Optional[str] = None,
        access_level: str = "unknown", allow_write: bool = False) -> int:
    start_dt = datetime.now().astimezone()
    start_perf = time.perf_counter()
    started_iso = start_dt.isoformat(timespec="milliseconds")
    tz_label = start_dt.tzname() or ""
    print(f"[TIMING] Start:    {start_dt.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]} {tz_label}".rstrip())
    return_code = 1
    error_type = None
    try:
        try:
            from dotenv import load_dotenv
        except ImportError as exc:
            raise RuntimeError("python-dotenv is required in the ALGOAI virtual environment.") from exc
        env_file = resolve_env_file(env_path)
        load_dotenv(dotenv_path=env_file, override=False)
        from .token_manager import load_access_token
        token = load_access_token(env_file)
        child_env = os.environ.copy()
        child_env["UPSTOX_ACCESS_TOKEN"] = token
        gemini_key = os.getenv("GEMINI_API_KEY", "").strip()
        if gemini_key:
            child_env["GEMINI_API_KEY"] = gemini_key
        agy_path = resolve_agy_path()
        if not agy_path or not os.path.isfile(agy_path):
            raise FileNotFoundError("Could not find 'agy'. Install it with: curl -fsSL https://antigravity.google/cli/install.sh | bash")
        command = [agy_path]
        if model:
            command.extend(["--model", model])
        if skip_permissions:
            command.append("--dangerously-skip-permissions")
        if output_format:
            command.extend(["--output-format", output_format])
        if prompt:
            command.extend(["-p", prompt])
        if skip_permissions:
            print("[SAFETY] AGY permission prompts are bypassed for this invocation.")
            print("[SAFETY] This does not grant trading/write authorization.")
        process = subprocess.Popen(command, env=child_env, cwd=str(app_dir()))
        return_code = wait_with_progress(process, start_perf, progress_interval, show_progress)
        return return_code
    except Exception as exc:
        error_type = type(exc).__name__
        raise
    finally:
        if show_progress:
            clear_current_line(sys.stderr)
        end_dt = datetime.now().astimezone()
        elapsed = time.perf_counter() - start_perf
        end_tz = end_dt.tzname() or ""
        print(f"[TIMING] End:      {end_dt.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]} {end_tz}".rstrip())
        print(f"[TIMING] Duration: {format_duration(elapsed)} (exit code: {return_code})")
        _write_execution_log({
            "command": command_name, "skill": skill_name, "started_at": started_iso,
            "ended_at": end_dt.isoformat(timespec="milliseconds"),
            "duration_seconds": round(elapsed, 3), "exit_code": return_code,
            "output_format": output_format or "default",
            "permission_bypass": bool(skip_permissions),
            "write_authorized": bool(allow_write), "access_level": access_level,
            "error_type": error_type,
        })
