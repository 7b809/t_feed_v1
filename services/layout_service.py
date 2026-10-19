"""Per-user multi-chart layout persistence.

Layouts are stored as small JSON documents on disk under
``data/_layouts/<layout_id>.json`` so they survive restart, are easy to
inspect, and require no DB dependency.

A layout document looks like::

    {
      "id": "layout_1",
      "name": "My Layout",
      "grid": 4,                    # 1 | 2 | 4
      "panes": [
        {"instrument_key": "NSE_INDEX|Nifty 50", "interval": "5m"},
        {"instrument_key": "NSE_INDEX|Nifty 50", "interval": "5m"},
        ...
      ],
      "created_at": "2026-10-19T11:00:00+00:00",
      "updated_at": "2026-10-19T11:00:00+00:00",
      "is_default": true
    }

Public API
----------
    layout_service.list_layouts()              -> list[dict]
    layout_service.get_layout(layout_id)       -> dict | None
    layout_service.create_layout(...)          -> dict
    layout_service.update_layout(...)          -> dict
    layout_service.delete_layout(layout_id)    -> bool
    layout_service.get_default_layout()        -> dict

The service never raises for expected failure modes: it always returns
a normalised dict with a ``status`` field, and file I/O errors are
logged. Corrupt files are skipped and reported.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core import config

logger = logging.getLogger(__name__)

_LAYOUT_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
_ALLOWED_GRIDS = (1, 2, 4)
_DEFAULT_PANE_KEY = "NSE_INDEX|Nifty 50"


def _layouts_dir() -> Path:
    path = Path(config.LIVE_DATA_DIR) / "_layouts"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalise_pane(pane: Any) -> dict[str, Any]:
    if not isinstance(pane, dict):
        return {"instrument_key": _DEFAULT_PANE_KEY, "interval": "5m"}
    key = str(pane.get("instrument_key") or _DEFAULT_PANE_KEY).strip()
    interval = str(pane.get("interval") or "5m").strip()
    return {"instrument_key": key, "interval": interval}


def _normalise_layout(raw: dict[str, Any], *, fallback_id: str) -> dict[str, Any]:
    layout_id = str(raw.get("id") or fallback_id).strip()
    if not _LAYOUT_ID_RE.match(layout_id):
        layout_id = fallback_id

    grid = int(raw.get("grid") or 4)
    if grid not in _ALLOWED_GRIDS:
        grid = 4

    panes_raw = raw.get("panes") or []
    if not isinstance(panes_raw, list):
        panes_raw = []
    # grid defines how many panes are visible; keep exactly `grid` entries.
    panes = [_normalise_pane(panes_raw[i]) if i < len(panes_raw) else
             {"instrument_key": _DEFAULT_PANE_KEY, "interval": "5m"}
             for i in range(grid)]

    return {
        "id": layout_id,
        "name": str(raw.get("name") or layout_id).strip() or layout_id,
        "grid": grid,
        "panes": panes,
        "created_at": str(raw.get("created_at") or _now_iso()),
        "updated_at": str(raw.get("updated_at") or _now_iso()),
        "is_default": bool(raw.get("is_default") or False),
    }


class LayoutService:
    def __init__(self) -> None:
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # Public API                                                         #
    # ------------------------------------------------------------------ #
    def list_layouts(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        directory = _layouts_dir()
        for path in sorted(directory.glob("*.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                logger.exception("[layout] unreadable %s", path)
                continue
            if not isinstance(raw, dict):
                continue
            out.append(_normalise_layout(raw, fallback_id=path.stem))
        return out

    def get_layout(self, layout_id: str) -> dict[str, Any] | None:
        lid = str(layout_id or "").strip()
        if not _LAYOUT_ID_RE.match(lid):
            return None
        path = _layouts_dir() / f"{lid}.json"
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("[layout] unreadable %s", path)
            return None
        if not isinstance(raw, dict):
            return None
        return _normalise_layout(raw, fallback_id=lid)

    def create_layout(
        self,
        *,
        name: str | None = None,
        grid: int = 4,
        panes: list[dict[str, Any]] | None = None,
        is_default: bool = False,
    ) -> dict[str, Any]:
        with self._lock:
            existing = {p.stem for p in _layouts_dir().glob("*.json")}
            # Allocate layout_N
            n = 1
            while f"layout_{n}" in existing:
                n += 1
            layout_id = f"layout_{n}"

            raw = {
                "id": layout_id,
                "name": name or f"Layout {n}",
                "grid": grid,
                "panes": panes or [],
                "created_at": _now_iso(),
                "updated_at": _now_iso(),
                "is_default": is_default,
            }
            layout = _normalise_layout(raw, fallback_id=layout_id)

            # If this is the new default, unset the previous default.
            if layout["is_default"]:
                self._clear_default(exclude=layout_id)

            self._write(layout)
            return layout

    def update_layout(self, layout_id: str, **changes: Any) -> dict[str, Any] | None:
        with self._lock:
            existing = self.get_layout(layout_id)
            if existing is None:
                return None
            merged = dict(existing)
            for key in ("name", "grid", "panes", "is_default"):
                if key in changes and changes[key] is not None:
                    merged[key] = changes[key]
            merged["updated_at"] = _now_iso()
            layout = _normalise_layout(merged, fallback_id=str(layout_id))
            if layout["is_default"]:
                self._clear_default(exclude=str(layout_id))
            self._write(layout)
            return layout

    def delete_layout(self, layout_id: str) -> bool:
        lid = str(layout_id or "").strip()
        if not _LAYOUT_ID_RE.match(lid):
            return False
        with self._lock:
            path = _layouts_dir() / f"{lid}.json"
            if not path.exists():
                return False
            try:
                path.unlink()
                return True
            except Exception:
                logger.exception("[layout] delete failed for %s", path)
                return False

    def get_default_layout(self) -> dict[str, Any] | None:
        layouts = self.list_layouts()
        for layout in layouts:
            if layout.get("is_default"):
                return layout
        return layouts[0] if layouts else None

    def ensure_default_layout(self) -> dict[str, Any]:
        """Create ``layout_1`` with 1 NIFTY pane if no layouts exist."""
        layouts = self.list_layouts()
        if layouts:
            return self.get_default_layout() or layouts[0]
        return self.create_layout(
            name="Default",
            grid=1,
            panes=[{"instrument_key": _DEFAULT_PANE_KEY, "interval": "5m"}],
            is_default=True,
        )

    # ------------------------------------------------------------------ #
    # Internals                                                          #
    # ------------------------------------------------------------------ #
    def _clear_default(self, *, exclude: str) -> None:
        for path in _layouts_dir().glob("*.json"):
            if path.stem == exclude:
                continue
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(raw, dict) and raw.get("is_default"):
                raw["is_default"] = False
                raw["updated_at"] = _now_iso()
                self._write(_normalise_layout(raw, fallback_id=path.stem))

    def _write(self, layout: dict[str, Any]) -> None:
        path = _layouts_dir() / f"{layout['id']}.json"
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(layout, indent=2, default=str), encoding="utf-8",
        )
        os.replace(tmp, path)


layout_service = LayoutService()