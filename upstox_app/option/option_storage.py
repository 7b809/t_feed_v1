"""
upstox_app/option_storage.py

Local JSON storage for option snapshots.

Single tree:
    data/runtime/options/{NIFTY,SENSEX,...}.json
    data/runtime/options/_meta.json

No readonly tree. Every successful load overwrites the runtime snapshot.

Cross-platform notes:
    - POSIX: folder mode enforced via chmod.
    - Windows: chmod affects only the read-only attribute on files;
      we log a warning once and proceed best-effort.
"""
import json
import os
import stat
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

from core.logger import get_logger
from upstox_app.common.config import upstox_config

logger = get_logger(__name__)

_lock = threading.RLock()
_warned_windows = False


# ── helpers ──────────────────────────────────────────────────────
def _is_windows() -> bool:
    return os.name == "nt"


def _safe_chmod(path: Path, mode: int) -> bool:
    try:
        os.chmod(path, mode)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("chmod failed | path=%s | mode=%o | err=%s", path, mode, exc)
        return False


# ── storage ──────────────────────────────────────────────────────
class OptionStorage:
    """Owns the single runtime tree for option snapshots."""

    def __init__(self) -> None:
        self.runtime_root: Path = Path(upstox_config.OPTIONS_RUNTIME_DIR)
        self.runtime_options: Path = self.runtime_root / "options"

    # ── lifecycle ────────────────────────────────────────────────
    def ensure_dirs(self) -> None:
        """Create the runtime tree and apply configured permissions."""
        global _warned_windows
        with _lock:
            for path in (self.runtime_root, self.runtime_options):
                path.mkdir(parents=True, exist_ok=True)

            _safe_chmod(self.runtime_root,    upstox_config.OPTIONS_RUNTIME_MODE)
            _safe_chmod(self.runtime_options, upstox_config.OPTIONS_RUNTIME_MODE)

            if _is_windows() and not _warned_windows:
                logger.info(
                    "Storage on Windows: directory permissions are best-effort "
                    "(files marked writable; ACLs not enforced by Python chmod)"
                )
                _warned_windows = True

            logger.info("Storage dir ready | runtime=%s", self.runtime_root.resolve())

    # ── path helpers ─────────────────────────────────────────────
    def runtime_path(self, index_name: str) -> Path:
        return self.runtime_options / f"{index_name.upper()}.json"

    def meta_path(self) -> Path:
        return self.runtime_options / "_meta.json"

    # ── write ────────────────────────────────────────────────────
    def _unlock(self, path: Path) -> None:
        """Make a file writable before overwrite (Windows readonly bit)."""
        if path.exists():
            try:
                os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
            except Exception:  # noqa: BLE001
                pass

    def write_runtime(self, index_name: str, payload: Dict[str, Any]) -> Optional[Path]:
        """Write / overwrite the runtime snapshot for an index."""
        path = self.runtime_path(index_name)
        with _lock:
            try:
                self._unlock(path)
                with path.open("w", encoding="utf-8") as fh:
                    json.dump(payload, fh, indent=2, default=str)
                _safe_chmod(path, 0o644)
                logger.info("Runtime snapshot written | index=%s | path=%s", index_name, path)
                return path
            except Exception as exc:  # noqa: BLE001
                logger.exception("Failed to write runtime snapshot | index=%s | err=%s", index_name, exc)
                return None

    def write_meta(self, meta: Dict[str, Any]) -> Optional[Path]:
        path = self.meta_path()
        with _lock:
            try:
                self._unlock(path)
                with path.open("w", encoding="utf-8") as fh:
                    json.dump(meta, fh, indent=2, default=str)
                _safe_chmod(path, 0o644)
                return path
            except Exception as exc:  # noqa: BLE001
                logger.exception("Failed to write meta | err=%s", exc)
                return None

    # ── read ─────────────────────────────────────────────────────
    def _load_json(self, path: Path) -> Optional[Dict[str, Any]]:
        if not path.exists():
            return None
        try:
            with path.open("r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to read snapshot | path=%s | err=%s", path, exc)
            return None

    def read_runtime(self, index_name: str) -> Optional[Dict[str, Any]]:
        return self._load_json(self.runtime_path(index_name))

    def read_meta(self) -> Optional[Dict[str, Any]]:
        return self._load_json(self.meta_path())

    def runtime_age_seconds(self, index_name: str) -> Optional[float]:
        path = self.runtime_path(index_name)
        if not path.exists():
            return None
        try:
            return time.time() - path.stat().st_mtime
        except Exception:  # noqa: BLE001
            return None

    def is_runtime_fresh(self, index_name: str) -> bool:
        age = self.runtime_age_seconds(index_name)
        if age is None:
            return False
        return age <= upstox_config.OPTIONS_RUNTIME_MAX_AGE_SEC

    # ── listing ──────────────────────────────────────────────────
    def list_runtime_indexes(self) -> list[str]:
        if not self.runtime_options.exists():
            return []
        return sorted(
            p.stem for p in self.runtime_options.glob("*.json") if p.stem != "_meta"
        )


option_storage = OptionStorage()