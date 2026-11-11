# app/services/sr_state_store.py
"""Filesystem-backed SR state persistence — two files per config folder.

Layout under ``SR_STATE_DIR``:

    {sr_state_dir}/
    └── {leg_role}__{strike}_{option_type}_{instrument_key_safe}/
        └── {config_safe}/
            ├── today_snapshot.json       # snapshot event (frozen opening state)
            ├── today_live_sr.json        # sr_update events (current live state)
            └── _heartbeat_history.jsonl  # opt-in (SR_STORE_HEARTBEATS=true)

Example:

    data/sr_state/
    ├── isolated__25400_ce_NSE_FO_45406/
    │   ├── 15_3/
    │   │   ├── today_snapshot.json
    │   │   ├── today_live_sr.json
    │   │   └── _heartbeat_history.jsonl
    │   ├── 15_15/...
    │   └── 15_30/...
    └── opposite__25400_pe_NSE_FO_45407/
        ├── 15_3/...
        ├── 15_15/...
        └── 15_30/...

File semantics (mirrors the reference ``TodaySrSink`` client):

    * ``snapshot`` event
        - write ``today_snapshot.json`` (frozen opening state for this
          connection). ``today_live_sr.json`` is NOT touched — it
          keeps the previous live state until a fresh ``sr_update``
          arrives. This lets you compare "freshly opened" vs "last
          known live" after a reconnect.
        - ``last_event = "snapshot"``, ``update_count = 1``.

    * ``sr_update`` event
        - write ``today_live_sr.json`` with fresh ``current_price``,
          ``last_ts``, ``levels``, ``resistances``, ``supports``,
          ``sr_level_count``.
        - ``trading_symbol``, ``left_bars``, ``right_bars``,
          ``cycle_number``, ``cycle_time``, ``scope_note`` are
          **carried forward** from ``today_snapshot.json`` (or the
          existing ``today_live_sr.json`` if no snapshot yet) — the
          server does not resend them on sr_update frames. Mirrors
          the reference client's ``_last_meta`` but persisted to
          disk so it survives restarts.
        - ``last_event = "sr_update"``, ``update_count = previous + 1``.
        - ``today_snapshot.json`` is NOT touched (frozen reference).

    * ``heartbeat`` event
        - refresh ``last_heartbeat`` on both existing files.
        - ``update_count`` and ``updated_at`` are NOT touched — they
          always reflect the last *level-carrying* frame.
        - if no file exists yet (edge case: heartbeat before any
          snapshot), a minimal ``today_live_sr.json`` is created with
          ``last_event = "heartbeat"`` and ``update_count = 0``.

Both files carry the same reference-aligned schema:

    {
      "_id":            "NSE_FO|45406__15/3",
      "instrument_key": "NSE_FO|45406",
      "leg_role":       "isolated" | "opposite",
      "isolation_id":   "2026-11-11__NSE_FO_45406",
      "config":         "15/3" | "15/15" | "15/30",
      "strike_option":  "25400_ce" | "25400_pe",

      # Snapshot-frame fields (carried forward on sr_update)
      "trading_symbol": "NIFTY 25400 CE 17 NOV 26",
      "left_bars":      15,
      "right_bars":     3,
      "cycle_number":   3,
      "cycle_time":     "2026-11-11T13:19:10.000895+05:30",
      "scope_note":     "For further reference, only today's SR levels are used",

      # Frame-common fields (refreshed on every snapshot / sr_update)
      "data_scope":     "today_only",
      "today_ist_date": "2026-11-11",
      "current_price":  197.35,
      "last_ts":        "2026-11-11T13:18:00+05:30",
      "level_counts":   { "resistances": 5, "supports": 4, "total": 9 },
      "sr_level_count": 21,
      "levels":         [...full chronological pivot list...],
      "resistances":    [...5 nearest above current_price...],
      "supports":       [...5 nearest below current_price...],

      # Bookkeeping
      "last_event":     "snapshot" | "sr_update" | "heartbeat",
      "updated_at":     "2026-11-11T13:20:07.587491+05:30",
      "update_count":   1,
      "last_heartbeat": { cycle, cycle_time, nearest_resistance,
                          nearest_support, current_price, sr_level_count,
                          status, received_at }
    }

Reads (``get_latest`` / ``get_all_configs``) return the file with the
most recent ``updated_at`` — in normal operation that is
``today_live_sr.json``; immediately after a reconnect (fresh snapshot,
no sr_update yet) it is ``today_snapshot.json``.

All writes are atomic (temp file + ``os.replace``) so a crash mid-write
never leaves a corrupt file. The public API is unchanged.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from ..config import Settings

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")

# Any character outside [A-Za-z0-9_.-] is replaced with "_".
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")

# Two state files per config folder.
SNAPSHOT_FILENAME = "today_snapshot.json"
LIVE_FILENAME = "today_live_sr.json"
HEARTBEAT_HISTORY_FILENAME = "_heartbeat_history.jsonl"

# Fields that sr_update frames from the server do NOT carry (or carry
# less reliably) — they are carried forward from the existing snapshot
# file when a sr_update writes the live file. Mirrors the reference
# client's ``_last_meta``.
_CARRIED_FIELDS = (
    "trading_symbol",
    "left_bars",
    "right_bars",
    "cycle_number",
    "cycle_time",
    "scope_note",
    "last_heartbeat",
)


def _now_ist_iso() -> str:
    return datetime.now(IST).isoformat()


def _safe_name(value: Any) -> str:
    """Sanitize a string for use as a filesystem name."""
    return _SAFE_NAME_RE.sub("_", str(value))


def _format_strike(strike: Any) -> str:
    """``25400.0`` → ``'25400'``; ``25400.5`` → ``'25400_5'``."""
    try:
        f = float(strike)
        if f == int(f):
            return str(int(f))
        return str(f).replace(".", "_")
    except (TypeError, ValueError):
        return _safe_name(strike)


def _extract_strike_option(payload: Dict[str, Any]) -> str:
    """Return ``'25400_ce'`` / ``'25400_pe'``; ``''`` if not determinable."""
    meta = payload.get("meta")
    if isinstance(meta, dict):
        strike = meta.get("strike_price")
        opt_type = meta.get("strike_type")
        if strike is not None and opt_type:
            return f"{_format_strike(strike)}_{str(opt_type).lower()}"

    ts = payload.get("trading_symbol") or ""
    if isinstance(ts, str) and ts.strip():
        parts = ts.split()
        for i, token in enumerate(parts):
            if token in ("CE", "PE") and i > 0:
                try:
                    strike = float(parts[i - 1])
                    return f"{_format_strike(strike)}_{token.lower()}"
                except ValueError:
                    continue

    return ""


def _extract_trading_symbol(payload: Dict[str, Any]) -> Optional[str]:
    """Resolve ``trading_symbol`` from top-level or ``meta.trading_symbol``."""
    ts = payload.get("trading_symbol")
    if isinstance(ts, str) and ts.strip():
        return ts
    meta = payload.get("meta")
    if isinstance(meta, dict):
        ts = meta.get("trading_symbol")
        if isinstance(ts, str) and ts.strip():
            return ts
    return None


class SrStateStore:
    """Filesystem-backed SR state store — two files per config folder."""

    def __init__(self, config: Settings) -> None:
        self._config = config
        self._base_dir = Path(config.sr_state_dir).resolve()
        self._base_dir.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------- id
    @staticmethod
    def make_id(instrument_key: str, config: str) -> str:
        """Logical ``_id`` stored inside each JSON document."""
        return f"{instrument_key}__{config}"

    # ------------------------------------------------------------ paths
    def _instrument_dir(
        self, leg_role: str, instrument_key: str, strike_option: str
    ) -> Path:
        """Folder: ``{leg}__{strike_option}_{key}``."""
        leg = _safe_name(leg_role)
        key = _safe_name(instrument_key)
        if strike_option:
            folder = f"{leg}__{strike_option}_{key}"
        else:
            folder = f"{leg}__{key}"
        return self._base_dir / folder

    def _config_dir(
        self,
        leg_role: str,
        instrument_key: str,
        strike_option: str,
        config: str,
    ) -> Path:
        """``{instrument_dir}/{config_safe}/`` — holds both state files."""
        return self._instrument_dir(
            leg_role, instrument_key, strike_option
        ) / _safe_name(config)

    def _snapshot_path(
        self,
        leg_role: str,
        instrument_key: str,
        strike_option: str,
        config: str,
    ) -> Path:
        return self._config_dir(
            leg_role, instrument_key, strike_option, config
        ) / SNAPSHOT_FILENAME

    def _live_path(
        self,
        leg_role: str,
        instrument_key: str,
        strike_option: str,
        config: str,
    ) -> Path:
        return self._config_dir(
            leg_role, instrument_key, strike_option, config
        ) / LIVE_FILENAME

    # ------------------------------------------------------------ I/O
    def _write_json_atomic(self, path: Path, doc: Dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(doc, fh, indent=2, default=str, ensure_ascii=False)
            os.replace(tmp_path, path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
            raise

    def _read_json(self, path: Path) -> Optional[Dict[str, Any]]:
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("failed to read sr_state file %s", path)
            return None

    def _carry_forward(
        self,
        doc: Dict[str, Any],
        snapshot_doc: Optional[Dict[str, Any]],
        live_doc: Optional[Dict[str, Any]],
        fields: tuple,
    ) -> None:
        """Fill any missing field in ``doc``.

        Priority: ``snapshot_doc`` first (authoritative for snapshot-only
        metadata), then ``live_doc`` (fallback when no snapshot exists).
        Mirrors the reference ``_last_meta`` behaviour, but reads from
        disk so carried values survive process restarts.
        """
        for field in fields:
            if doc.get(field) is not None:
                continue
            if snapshot_doc and snapshot_doc.get(field) is not None:
                doc[field] = snapshot_doc[field]
            elif live_doc and live_doc.get(field) is not None:
                doc[field] = live_doc[field]

    # ---------------------------------------------------------- snapshot / sr_update
    def upsert_sr_state(
        self,
        payload: Dict[str, Any],
        instrument_key: str,
        leg_role: str,
        isolation_id: str,
        frame_source: str,
    ) -> None:
        cfg = payload.get("config")
        if not cfg:
            logger.warning("sr_state frame without config; skipping")
            return

        strike_option = _extract_strike_option(payload)
        if not strike_option:
            logger.warning(
                "sr_state frame without determinable strike_option; "
                "skipping (%s config=%s event=%s)",
                instrument_key,
                cfg,
                frame_source,
            )
            return

        # -------- build reference-aligned doc ---------------------------
        resistances = list(payload.get("resistances") or [])
        supports = list(payload.get("supports") or [])
        levels = list(payload.get("levels") or [])

        sr_level_count = payload.get("sr_level_count")
        if sr_level_count is None:
            sr_level_count = len(levels)

        now = _now_ist_iso()
        doc: Dict[str, Any] = {
            "_id": self.make_id(instrument_key, cfg),
            "instrument_key": instrument_key,
            "leg_role": leg_role,
            "isolation_id": isolation_id,
            "config": cfg,
            "strike_option": strike_option,
            "trading_symbol": _extract_trading_symbol(payload),
            "left_bars": payload.get("left_bars"),
            "right_bars": payload.get("right_bars"),
            "data_scope": payload.get("data_scope"),
            "today_ist_date": payload.get("today_ist_date"),
            "scope_note": payload.get("scope_note"),
            "current_price": payload.get("current_price"),
            "last_ts": payload.get("last_ts"),
            "cycle_number": payload.get("cycle_number"),
            "cycle_time": payload.get("cycle_time"),
            "level_counts": {
                "resistances": len(resistances),
                "supports": len(supports),
                "total": len(resistances) + len(supports),
            },
            "sr_level_count": sr_level_count,
            "levels": levels,
            "resistances": resistances,
            "supports": supports,
            "last_event": frame_source,
            "updated_at": now,
        }

        snapshot_path = self._snapshot_path(
            leg_role, instrument_key, strike_option, cfg
        )
        live_path = self._live_path(
            leg_role, instrument_key, strike_option, cfg
        )
        snapshot_doc = self._read_json(snapshot_path)
        live_doc = self._read_json(live_path)

        if frame_source == "snapshot":
            # Snapshot → frozen opening state; live file NOT touched.
            doc["update_count"] = 1
            # preserve last_heartbeat from any existing file
            self._carry_forward(
                doc, snapshot_doc, live_doc, ("last_heartbeat",)
            )
            target = snapshot_path
        else:
            # sr_update → live file only; increment update_count.
            prev = 0
            if live_doc:
                try:
                    prev = int(live_doc.get("update_count") or 0)
                except (TypeError, ValueError):
                    prev = 0
            elif snapshot_doc:
                try:
                    prev = int(snapshot_doc.get("update_count") or 0)
                except (TypeError, ValueError):
                    prev = 0
            doc["update_count"] = prev + 1
            # carry forward snapshot-only metadata
            self._carry_forward(
                doc, snapshot_doc, live_doc, _CARRIED_FIELDS
            )
            target = live_path

        try:
            self._write_json_atomic(target, doc)
            logger.debug(
                "sr_state write file=%s event=%s levels=%s count=%s",
                target,
                frame_source,
                doc["sr_level_count"],
                doc["update_count"],
            )
        except Exception:
            logger.exception(
                "failed to write sr_state file %s "
                "(key=%s cfg=%s event=%s)",
                target,
                instrument_key,
                cfg,
                frame_source,
            )

    # ---------------------------------------------------------- heartbeat
    def update_heartbeat(
        self,
        payload: Dict[str, Any],
        instrument_key: str,
        leg_role: str,
    ) -> None:
        cfg = payload.get("config")
        if not cfg:
            return

        strike_option = _extract_strike_option(payload)
        if not strike_option:
            logger.warning(
                "heartbeat without determinable strike_option; "
                "skipping (%s config=%s)",
                instrument_key,
                cfg,
            )
            return

        snapshot_path = self._snapshot_path(
            leg_role, instrument_key, strike_option, cfg
        )
        live_path = self._live_path(
            leg_role, instrument_key, strike_option, cfg
        )

        now = _now_ist_iso()
        heartbeat = {
            "cycle": payload.get("cycle"),
            "cycle_time": payload.get("cycle_time"),
            "nearest_resistance": payload.get("nearest_resistance"),
            "nearest_support": payload.get("nearest_support"),
            "current_price": payload.get("current_price"),
            "sr_level_count": payload.get("sr_level_count"),
            "status": payload.get("status"),
            "received_at": now,
        }

        updated_any = False
        for path in (snapshot_path, live_path):
            existing = self._read_json(path)
            if existing is None:
                continue
            existing["last_heartbeat"] = heartbeat
            # NOTE: ``updated_at`` and ``last_event`` intentionally NOT
            # touched — they reflect the last level-carrying frame.
            try:
                self._write_json_atomic(path, existing)
                updated_any = True
            except Exception:
                logger.exception(
                    "failed to update heartbeat in %s", path
                )

        if not updated_any:
            # No level-carrying frame yet → seed a minimal live file.
            minimal: Dict[str, Any] = {
                "_id": self.make_id(instrument_key, cfg),
                "instrument_key": instrument_key,
                "leg_role": leg_role,
                "config": cfg,
                "strike_option": strike_option,
                "trading_symbol": _extract_trading_symbol(payload),
                "data_scope": payload.get("data_scope"),
                "last_event": "heartbeat",
                "updated_at": now,
                "update_count": 0,
                "last_heartbeat": heartbeat,
            }
            try:
                self._write_json_atomic(live_path, minimal)
            except Exception:
                logger.exception(
                    "failed to seed heartbeat-only state file for %s/%s",
                    instrument_key,
                    cfg,
                )

        if self._config.sr_store_heartbeats:
            self._append_heartbeat_history(
                live_path.parent,
                instrument_key,
                leg_role,
                strike_option,
                cfg,
                heartbeat,
            )

    def _append_heartbeat_history(
        self,
        config_dir: Path,
        instrument_key: str,
        leg_role: str,
        strike_option: str,
        config: str,
        heartbeat: Dict[str, Any],
    ) -> None:
        try:
            config_dir.mkdir(parents=True, exist_ok=True)
            path = config_dir / HEARTBEAT_HISTORY_FILENAME
            line = json.dumps(
                {
                    "instrument_key": instrument_key,
                    "leg_role": leg_role,
                    "config": config,
                    "strike_option": strike_option,
                    **heartbeat,
                },
                default=str,
                ensure_ascii=False,
            )
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception:
            logger.exception(
                "heartbeat history append failed for %s/%s",
                instrument_key,
                config,
            )

    # ---------------------------------------------------------- reads
    def get_latest(
        self,
        instrument_key: str,
        config: str,
        leg_role: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Read the current state for a single config.

        Both ``today_live_sr.json`` and ``today_snapshot.json`` are
        considered; the one with the most recent ``updated_at`` wins.
        In normal operation that is the live file; immediately after a
        reconnect (fresh snapshot, no sr_update yet) it is the snapshot
        file. Across multiple legs, the newest document of all wins.
        """
        key_safe = _safe_name(instrument_key)
        cfg_safe = _safe_name(config)
        if leg_role:
            leg_pattern = f"{_safe_name(leg_role)}__*_{key_safe}"
        else:
            leg_pattern = f"*__*_{key_safe}"

        best: Optional[Dict[str, Any]] = None
        for leg_dir in self._base_dir.glob(leg_pattern):
            if not leg_dir.is_dir():
                continue
            config_dir = leg_dir / cfg_safe
            if not config_dir.exists():
                continue
            for fname in (LIVE_FILENAME, SNAPSHOT_FILENAME):
                doc = self._read_json(config_dir / fname)
                if not doc or doc.get("instrument_key") != instrument_key:
                    continue
                if best is None or (
                    doc.get("updated_at") or ""
                ) > (best.get("updated_at") or ""):
                    best = doc
        return best

    def get_all_configs(
        self,
        instrument_key: str,
        leg_role: Optional[str] = None,
    ) -> Dict[str, Dict[str, Any]]:
        """Return ``{config: doc}`` for every stored config.

        Per config folder, the document with the most recent
        ``updated_at`` (live or snapshot) wins. Across multiple legs,
        the more recently updated doc per config wins.
        """
        key_safe = _safe_name(instrument_key)
        if leg_role:
            leg_pattern = f"{_safe_name(leg_role)}__*_{key_safe}"
        else:
            leg_pattern = f"*__*_{key_safe}"

        out: Dict[str, Dict[str, Any]] = {}
        for leg_dir in self._base_dir.glob(leg_pattern):
            if not leg_dir.is_dir():
                continue
            for config_dir in leg_dir.iterdir():
                if not config_dir.is_dir():
                    continue
                for fname in (LIVE_FILENAME, SNAPSHOT_FILENAME):
                    doc = self._read_json(config_dir / fname)
                    if not doc or doc.get("instrument_key") != instrument_key:
                        continue
                    cfg = doc.get("config")
                    if not cfg:
                        continue
                    existing = out.get(cfg)
                    if existing is None or (
                        doc.get("updated_at") or ""
                    ) > (existing.get("updated_at") or ""):
                        out[cfg] = doc
        return out

    # ---------------------------------------------------------- purge
    def delete_for_instrument_keys(self, keys: List[str]) -> int:
        """Delete all SR state folders for the given instrument keys.

        Removes the whole ``{leg}__{strike}_{option}_{key}`` folder
        (including both state files, config subfolders, and heartbeat
        history) for each leg that references the key. Called by
        ``overwrite=true`` so stale levels don't leak into fresh WS
        snapshots. Returns the number of files removed.
        """
        if not keys:
            return 0

        deleted = 0
        for key in keys:
            safe = _safe_name(key)
            pattern = f"*_{safe}"
            try:
                for folder in list(self._base_dir.glob(pattern)):
                    if not folder.is_dir():
                        continue
                    try:
                        file_count = sum(
                            1 for p in folder.rglob("*") if p.is_file()
                        )
                        shutil.rmtree(folder)
                        deleted += file_count
                        logger.debug(
                            "sr_state folder removed path=%s files=%d",
                            folder,
                            file_count,
                        )
                    except Exception:
                        logger.exception(
                            "failed to delete sr_state folder %s", folder
                        )
            except Exception:
                logger.exception(
                    "failed to enumerate sr_state folders for %s", key
                )

        logger.info("sr_state purge keys=%s files_deleted=%d", keys, deleted)
        return deleted