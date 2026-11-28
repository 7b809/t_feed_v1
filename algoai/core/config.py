"""Application and skill-directory configuration."""
from __future__ import annotations
import os
from pathlib import Path
from typing import List

DEFAULT_APP_DIR = Path(__file__).resolve().parents[2]

def app_dir() -> Path:
    configured = os.getenv("ALGOAI_APP_DIR", "").strip()
    candidate = Path(configured).expanduser() if configured else DEFAULT_APP_DIR
    return candidate.resolve()

def resolve_env_file(value: str = ".env") -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = app_dir() / path
    return path.resolve()

def skill_roots() -> List[Path]:
    """Return skill roots in precedence order, independent of current directory."""
    candidates: List[Path] = []
    explicit = os.getenv("ALGOAI_SKILLS_DIR", "").strip()
    if explicit:
        for raw in explicit.split(os.pathsep):
            if raw.strip():
                path = Path(raw.strip()).expanduser()
                if not path.is_absolute():
                    path = app_dir() / path
                candidates.append(path.resolve())
    candidates.extend([
        (Path.home() / ".config" / "algoai" / "skills").resolve(),
        (app_dir() / ".agents" / "skills").resolve(),
    ])
    workspace_root = (Path.cwd() / ".agents" / "skills").resolve()
    if workspace_root not in candidates:
        candidates.append(workspace_root)
    unique: List[Path] = []
    seen = set()
    for path in candidates:
        if str(path) not in seen:
            seen.add(str(path))
            unique.append(path)
    return unique

def execution_log_path() -> Path:
    override = os.getenv("ALGOAI_LOG_FILE", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    state_home = Path(os.getenv("XDG_STATE_HOME", Path.home() / ".local" / "state")).expanduser()
    return (state_home / "algoai" / "executions.jsonl").resolve()
