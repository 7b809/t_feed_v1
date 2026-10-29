# app/services/levels_state_store.py
#
# Persists per-index pivot levels to:
#   storage/mega/index/<folder>/levels.json
#
# Where <folder> comes from INDEX_METADATA[key]["folder"] (e.g. "nifty",
# "banknifty", "midcpnifty", "sensex"). Falls back to underlying.lower()
# for entries that predate the folder field.
#
# Atomic writes via .json.tmp + os.replace so a crash mid-write cannot
# corrupt the on-disk file.

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from app.config import Settings

logger = logging.getLogger(__name__)


class LevelsStateStore:
    def __init__(self, config: Settings) -> None:
        self.config = config
        self.base_dir = config.destination_dir

    # ------------------------------------------------------------------ #
    def levels_file(self, instrument_key: str, meta: dict[str, Any]) -> Path:
        folder = (
            meta.get("folder")
            or (meta.get("underlying") or "")
            or "index"
        ).strip().lower() or "index"
        return self.base_dir / "index" / folder / "levels.json"

    # ------------------------------------------------------------------ #
    def load(
        self, instrument_key: str, meta: dict[str, Any]
    ) -> dict[str, Any] | None:
        path = self.levels_file(instrument_key, meta)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Could not read levels file %s: %s", path, exc)
            return None

    # ------------------------------------------------------------------ #
    def save(
        self,
        instrument_key: str,
        meta: dict[str, Any],
        record: dict[str, Any],
    ) -> Path:
        path = self.levels_file(instrument_key, meta)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(record, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(path)
        return path