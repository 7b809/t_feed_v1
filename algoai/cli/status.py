"""Non-invasive launcher and environment status."""
from __future__ import annotations
import os
import subprocess
from algoai import __version__
from algoai.core.agy_launcher import resolve_agy_path
from algoai.core.config import app_dir, resolve_env_file, skill_roots
from algoai.cli.skills import get_skills

def print_status(env_path: str = ".env") -> int:
    root = app_dir()
    env_file = resolve_env_file(env_path)
    venv_python = root / "venv" / "bin" / "python3"
    agy_path = resolve_agy_path()
    agy_version = "unavailable"
    if agy_path:
        try:
            result = subprocess.run([agy_path, "--version"], capture_output=True,
                text=True, timeout=5, check=False)
            output = (result.stdout or result.stderr).strip()
            if result.returncode == 0 and output:
                agy_version = output.splitlines()[0]
            elif output:
                agy_version = "error: " + output.splitlines()[0][:160]
        except Exception as exc:
            agy_version = f"error ({type(exc).__name__})"
    skills = get_skills()
    print("ALGOAI STATUS")
    print("─" * 72)
    print(f"Launcher version:  {__version__}")
    print(f"Project directory: {root} ({'OK' if root.is_dir() else 'MISSING'})")
    print(f"Python venv:       {venv_python} ({'OK' if venv_python.is_file() else 'FALLBACK'})")
    print(f"AGY CLI path:      {agy_path or 'MISSING'}")
    print(f"AGY CLI version:   {agy_version}")
    print(f"Environment file:  {env_file} ({'FOUND' if env_file.is_file() else 'NOT FOUND'})")
    print(f"GEMINI_API_KEY:    {'SET' if os.getenv('GEMINI_API_KEY', '').strip() else 'NOT SET'}")
    print(f"Discovered skills: {len(skills)}")
    print("Skill search roots:")
    for path in skill_roots():
        print(f"  - {path} ({'FOUND' if path.is_dir() else 'not present'})")
    print("Execution log:     ~/.local/state/algoai/executions.jsonl (or ALGOAI_LOG_FILE)")
    return 0
