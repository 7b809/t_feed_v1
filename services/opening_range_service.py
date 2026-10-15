"""Opening Range (OR) levels service.

Calculates OR support/resistance levels from the first 1-minute
candle of the trading day (09:15 – 09:16 IST) for every instrument
in the registry, and persists them per-day under each instrument's
folder.

Formulas
--------
    fc  = first candle of day (09:15 – 09:16 IST)
    H   = fc.high
    L   = fc.low
    A   = (H + L) / 2
    D_H = |H - A|
    D_L = |A - L|

    R1 = A + D_H / 2
    S1 = A - D_L / 2

    R2 = H + D_H
    S2 = L - D_L

    R3 = R2 + D_H
    S3 = S2 - D_L

    R3_Threshold = (R2 + R3) / 2
    S3_Threshold = (S2 + S3) / 2

Timing
------
The 09:15 candle finalizes at 09:16 IST. The scheduler triggers
calculation at 09:16:10 IST (configurable). Project startup and
manual hard refresh also call this service; if the 09:15 candle
doesn't yet exist for today, that instrument is skipped with a
clear status (no error).

Persistence
-----------
Per-instrument JSON file ``opening_range.json`` written next to
``ema_crosses.json`` in the same folder. For ad-hoc instruments
(indices without a data folder), the file is written under
``data/_adhoc/<safe_key>/opening_range.json``.

Self-healing lookup
-------------------
``get_opening_range`` will ad-hoc register an unknown key on demand,
so a legitimate index key (e.g. ``NSE_INDEX|Nifty 50``) is never
reported as ``not_registered`` when it arrives before the
registration step in the unified refresh has run.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core import config
from services.instrument_registry import instrument_registry
from services.live_ema_engine import live_ema_engine

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Level math                                                                   #
# --------------------------------------------------------------------------- #

def compute_levels(high: float, low: float) -> dict[str, float]:
    """Return the full OR level set for a first candle's H/L."""
    h = float(high)
    l = float(low)
    a = (h + l) / 2
    d_h = abs(h - a)
    d_l = abs(a - l)

    r1 = a + d_h / 2
    s1 = a - d_l / 2

    r2 = h + d_h
    s2 = l - d_l

    r3 = r2 + d_h
    s3 = s2 - d_l

    r3_threshold = (r2 + r3) / 2
    s3_threshold = (s2 + s3) / 2

    return {
        "H": h,
        "L": l,
        "A": a,
        "D_H": d_h,
        "D_L": d_l,
        "R1": r1,
        "S1": s1,
        "R2": r2,
        "S2": s2,
        "R3": r3,
        "S3": s3,
        "R3_Threshold": r3_threshold,
        "S3_Threshold": s3_threshold,
    }

# --------------------------------------------------------------------------- #
# Path helpers                                                                 #
# --------------------------------------------------------------------------- #

def _safe_folder_name(key: str) -> str:
    return (
        str(key or "unknown")
        .replace("|", "_")
        .replace("/", "_")
        .replace("\\", "_")
        .strip()
        or "unknown"
    )

def _path_for(entry: dict[str, Any]) -> Path:
    """Return the ``opening_range.json`` path for one instrument.

    Preferred location: same folder as the instrument's
    ``ema_crosses.json``. Fallback for ad-hoc instruments whose
    ``ema_path`` is empty: ``data/_adhoc/<safe_key>/opening_range.json``.
    """
    ema_path = str(entry.get("ema_path") or "").strip()
    if ema_path:
        return Path(ema_path).with_name(config.OPENING_RANGE_FILENAME)

    key = str(entry.get("instrument_key") or "unknown")
    fallback_dir = (
        Path(config.LIVE_DATA_DIR) / "_adhoc" / _safe_folder_name(key)
    )
    return fallback_dir / config.OPENING_RANGE_FILENAME

def _today_ist_prefix() -> str:
    """Return today's date prefix in the market timezone as
    ``YYYY-MM-DD``."""
    return datetime.now().astimezone().date().isoformat()

def _first_candle_minute() -> str:
    """Full ISO minute key for today's first candle, e.g.
    ``2026-10-15T09:15``."""
    return f"{_today_ist_prefix()}T{config.OPENING_RANGE_FIRST_MINUTE}"

def _trigger_time_reached() -> bool:
    """Return True once the configured trigger time has passed today."""
    now = datetime.now().astimezone()
    trigger = now.replace(
        hour=config.OPENING_RANGE_HOUR,
        minute=config.OPENING_RANGE_MINUTE,
        second=config.OPENING_RANGE_SECOND,
        microsecond=0,
    )
    return now >= trigger

# --------------------------------------------------------------------------- #
# Service                                                                      #
# --------------------------------------------------------------------------- #

class OpeningRangeService:
    """Compute + persist OR levels for every registry instrument."""

    # ------------------------------------------------------------------ #
    # Public API                                                         #
    # ------------------------------------------------------------------ #
    def calculate_all(self) -> dict[str, Any]:
        """Compute OR levels for every instrument in the registry.

        Returns a summary dict. Individual instrument failures are
        logged and reported in the summary but never abort the run.
        """
        entries = instrument_registry.all()
        if not entries:
            logger.info("[or] no instruments in registry — skipping")
            return {
                "status": "no_instruments",
                "day": _today_ist_prefix(),
                "total": 0,
                "calculated": 0,
                "skipped": 0,
                "errors": 0,
            }

        today = _today_ist_prefix()
        target_minute = _first_candle_minute()

        calculated = 0
        skipped = 0
        errors = 0

        logger.info(
            "[or] START — instruments=%d target_minute=%s trigger_reached=%s",
            len(entries),
            target_minute,
            _trigger_time_reached(),
        )

        for entry in entries:
            key = str(entry.get("instrument_key") or "")
            try:
                result = self._calculate_one(entry, target_minute)
            except Exception:
                logger.exception("[or] calc failed for %s", key)
                errors += 1
                continue

            status = result.get("status")
            if status == "ok":
                calculated += 1
            elif status == "error":
                errors += 1
            else:
                skipped += 1

        summary = {
            "status": "ok",
            "day": today,
            "target_minute": target_minute,
            "total": len(entries),
            "calculated": calculated,
            "skipped": skipped,
            "errors": errors,
            "ran_at": datetime.now(timezone.utc).isoformat(),
        }
        logger.info(
            "[or] COMPLETE — calculated=%d skipped=%d errors=%d total=%d",
            calculated, skipped, errors, len(entries),
        )
        return summary

    def calculate_one(self, instrument_key: str) -> dict[str, Any]:
        """Compute OR levels for a single instrument (public helper)."""
        entry = instrument_registry.get(instrument_key)
        if entry is None:
            entry = instrument_registry.register_adhoc(instrument_key)
        return self._calculate_one(entry, _first_candle_minute())

    def get_opening_range(self, instrument_key: str) -> dict[str, Any]:
        """Read the on-disk OR payload for one instrument.

        Self-healing: if the key is a legitimate index key that was
        not yet registered (e.g. very early in startup), we register
        it ad-hoc so this lookup never fails with ``not_registered``
        for valid inputs.
        """
        key = str(instrument_key or "").strip()
        entry = instrument_registry.get(key)

        if entry is None and key:
            try:
                entry = instrument_registry.register_adhoc(key)
                logger.info("[or] ad-hoc registered on lookup: %s", key)
            except Exception:
                logger.exception(
                    "[or] failed ad-hoc registering on lookup: %s", key
                )
                entry = None

        if entry is None:
            return {
                "instrument_key": key,
                "available": False,
                "reason": "not_registered",
            }

        path = _path_for(entry)
        if not path.exists():
            return {
                "instrument_key": key,
                "available": False,
                "reason": "not_calculated",
                "path": str(path),
            }

        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("[or] unreadable %s", path)
            return {
                "instrument_key": key,
                "available": False,
                "reason": "unreadable",
                "path": str(path),
            }

        # Only return today's data; yesterday's file is reported as stale.
        today = _today_ist_prefix()
        payload["available"] = True
        payload["is_today"] = str(payload.get("day")) == today
        return payload

    def list_all(self, only_today: bool = True) -> dict[str, Any]:
        """Return OR payloads for every registered instrument."""
        today = _today_ist_prefix()
        items: list[dict[str, Any]] = []
        for entry in instrument_registry.all():
            key = entry.get("instrument_key")
            if not key:
                continue
            payload = self.get_opening_range(key)
            if only_today and not payload.get("is_today"):
                continue
            items.append(payload)
        return {
            "count": len(items),
            "day": today,
            "items": items,
        }

    # ------------------------------------------------------------------ #
    # Internals                                                          #
    # ------------------------------------------------------------------ #
    def _calculate_one(
        self,
        entry: dict[str, Any],
        target_minute: str,
    ) -> dict[str, Any]:
        key = str(entry.get("instrument_key") or "")
        if not key:
            return {"status": "skipped", "reason": "no_key"}

        # Grab the full seeded + live candle buffer for this instrument.
        payload = live_ema_engine.get_candles(key, limit=0)
        candles = payload.get("candles") or []

        first_candle = next(
            (c for c in candles if str(c.get("minute")) == target_minute),
            None,
        )

        if first_candle is None:
            return {
                "status": "skipped",
                "reason": "first_candle_not_available",
                "instrument_key": key,
                "target_minute": target_minute,
            }

        h = first_candle.get("high")
        l = first_candle.get("low")
        if h is None or l is None:
            return {
                "status": "skipped",
                "reason": "incomplete_candle",
                "instrument_key": key,
                "target_minute": target_minute,
            }

        try:
            levels = compute_levels(float(h), float(l))
        except Exception:
            logger.exception("[or] level math failed for %s", key)
            return {
                "status": "error",
                "reason": "level_math_failed",
                "instrument_key": key,
            }

        result = {
            "instrument_key": key,
            "underlying": entry.get("underlying"),
            "strike_price": entry.get("strike_price"),
            "strike_type": entry.get("strike_type"),
            "expiry": entry.get("expiry"),
            "trading_symbol": entry.get("trading_symbol"),
            "day": target_minute.split("T", 1)[0],
            "first_candle": {
                "minute": first_candle.get("minute"),
                "timestamp": first_candle.get("timestamp"),
                "open": first_candle.get("open"),
                "high": first_candle.get("high"),
                "low": first_candle.get("low"),
                "close": first_candle.get("close"),
                "volume": first_candle.get("volume"),
            },
            "levels": levels,
            "calculated_at": datetime.now(timezone.utc).isoformat(),
        }

        path = _path_for(entry)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(result, indent=2, default=str),
                encoding="utf-8",
            )
            tmp.replace(path)
        except Exception:
            logger.exception("[or] failed writing %s", path)
            return {
                "status": "error",
                "reason": "write_failed",
                "instrument_key": key,
                "path": str(path),
            }

        result["status"] = "ok"
        return result


opening_range_service = OpeningRangeService()