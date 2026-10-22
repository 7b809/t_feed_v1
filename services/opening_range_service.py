# File: services/opening_range_service.py
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

REST fallback
-------------
The live WebSocket feed can finalize the 09:15 candle late (the
first populated Nifty 1m OHLC sometimes only appears at 09:18).
When the in-memory EMA engine does not yet have the 09:15 candle,
this service now fetches today's intraday 1-minute candles from
Upstox for that instrument and uses the 09:15 candle from the REST
response. This removes the OR → Stage-1 race that previously left
core indexes with `or_unavailable:not_calculated`.

Persistence
-----------
Per-instrument JSON file ``opening_range.json`` written next to
``ema_crosses.json`` in the same folder. For an instrument whose
``ema_path`` resolves to ``<LIVE_DATA_DIR>/index/<safe_key>/...``
(e.g. an ad-hoc index registered by ``instrument_registry`` or by
``routes/admin_routes._ensure_core_and_strategy_indexes_registered``),
the file lands at ``<LIVE_DATA_DIR>/index/<safe_key>/opening_range.json``.
For an instrument with no ``ema_path`` at all, the fallback is
``<LIVE_DATA_DIR>/_adhoc/<safe_key>/opening_range.json``.

Target-folder copy (Stage-1 strike folders)
-------------------------------------------
``calculate_one()`` accepts an optional ``target_folder`` argument.
When provided, a copy of the resulting ``opening_range.json`` is
written into that folder **as well as** the canonical location.

This is what lets Stage 1 write each strike's own OR into its
Stage-1 strike folder::

    data/strategy/stage_1_initial/<TICKER>/<INDEX>/<strike>_<TYPE>/
        opening_range.json     <- written by this service via target_folder

That is the exact path ``strategy_live_tracker_service`` and
``strategy_2_isolation_service._read_contract_isolation_level()``
read to learn each contract's R2 / S3 level. Before this parameter
existed, the OR was only ever written to the canonical
``data/index/<safe_key>/opening_range.json`` path, so every strike
the tracker examined had ``isolation_level = None`` and was skipped
— which is the root of the 2026-10-22 ``no_activations`` /
``placed=0`` failure even though Stage 1 + Stage 2 had produced 68
contracts.

Self-healing lookup
-------------------
``get_opening_range`` will ad-hoc register an unknown key on demand,
so a legitimate index key (e.g. ``NSE_INDEX|Nifty 50``) is never
reported as ``not_registered`` when it arrives before the
registration step in the unified refresh has run.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from core import config
from core.logger import get_logger
from services.instrument_registry import instrument_registry
from services.live_ema_engine import live_ema_engine
from services.token_service import token_service

logger = get_logger(__file__)

# --------------------------------------------------------------------------- #
# Upstox REST fallback (used only when the live 09:15 candle is missing)      #
# --------------------------------------------------------------------------- #
_UPSTOX_BASE = "https://api.upstox.com"
_HTTP_TIMEOUT = 15

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

def _write_json_atomic(path: Path, payload: Any) -> None:
    """Write ``payload`` to ``path`` atomically (tmp file + replace).

    Creates parent directories as needed. Raises on failure — the
    caller is expected to catch and report.

    This helper is the single writer for every ``opening_range.json``
    this module produces, so the canonical write and the
    ``target_folder`` copy always use identical atomic-write semantics.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, indent=2, default=str),
        encoding="utf-8",
    )
    tmp.replace(path)

# --------------------------------------------------------------------------- #
# REST fallback                                                                #
# --------------------------------------------------------------------------- #

def _fetch_first_candle_via_rest(
    instrument_key: str,
    target_minute: str,
) -> dict[str, Any] | None:
    """Fetch today's intraday 1m candles and return the target-minute one.

    Used only when the in-memory EMA engine does not yet hold the
    09:15 candle for this instrument. Returns a normalised candle dict
    (``minute``, ``timestamp``, ``open``, ``high``, ``low``, ``close``,
    ``volume``) or None if the REST call fails or the 09:15 candle is
    not present.
    """
    key = str(instrument_key or "").strip()
    if not key:
        return None

    try:
        token = token_service.get_access_token()
    except Exception:
        token = None
    if not token:
        logger.info(
            "[or] REST fallback skipped for %s (no Upstox token)", key
        )
        return None

    url = (
        f"{_UPSTOX_BASE}/v3/historical-candle/intraday/"
        f"{key}/minutes/1"
    )
    try:
        resp = requests.get(
            url,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {token}",
            },
            timeout=_HTTP_TIMEOUT,
        )
    except requests.RequestException:
        logger.warning("[or] REST fallback network error for %s", key)
        return None

    if resp.status_code >= 400:
        logger.info(
            "[or] REST fallback HTTP %s for %s: %s",
            resp.status_code, key, resp.text[:160],
        )
        return None

    try:
        body = resp.json()
    except ValueError:
        return None

    raw = (
        (body.get("data") or {}).get("candles")
        if isinstance(body.get("data"), dict)
        else body.get("candles")
    ) or []
    if not isinstance(raw, list):
        return None

    for row in raw:
        if not isinstance(row, (list, tuple)) or len(row) < 5:
            continue
        try:
            minute = str(row[0])[:16]
        except Exception:
            continue
        if minute != target_minute:
            continue
        try:
            return {
                "minute": minute,
                "timestamp": str(row[0]),
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5]) if len(row) >= 6 else 0.0,
            }
        except (TypeError, ValueError):
            continue

    return None

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

    def calculate_one(
        self,
        instrument_key: str,
        target_folder: Path | str | None = None,
    ) -> dict[str, Any]:
        """Compute OR levels for a single instrument.

        Parameters
        ----------
        instrument_key :
            The registry key, e.g. ``"NSE_FO|43919"``. If the key is
            not yet in the registry it is ad-hoc registered first.
        target_folder :
            Optional folder into which a **copy** of the resulting
            ``opening_range.json`` is also written. This is how Stage 1
            puts each strike's own OR into its Stage-1 strike folder
            (``data/strategy/stage_1_initial/.../<strike>_<TYPE>/``),
            which is where the S1/S2 R2/S3 trackers look for it.

            The canonical copy is still written to ``_path_for(entry)``
            (``data/index/<safe_key>/opening_range.json`` for ad-hoc
            instruments) regardless of this argument.

            Passing ``None`` (the default) preserves the pre-fix
            behaviour exactly: only the canonical copy is written.

        Returns
        -------
        dict
            The same dict ``_calculate_one()`` produces, with
            ``status="ok"`` on success. On ``skipped`` (no 09:15
            candle) or ``error`` no file is written to either
            location, and ``target_folder`` is echoed back in the
            result for the caller to log.
        """
        entry = instrument_registry.get(instrument_key)
        if entry is None:
            entry = instrument_registry.register_adhoc(instrument_key)

        # Normalise the optional target folder once, so the internal
        # writer never has to re-handle str vs Path.
        target = (
            Path(target_folder)
            if target_folder is not None
            else None
        )

        return self._calculate_one(
            entry,
            _first_candle_minute(),
            target_folder=target,
        )

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
        target_folder: Path | None = None,
    ) -> dict[str, Any]:
        """Compute OR levels for one instrument and persist them.

        Writes the canonical ``opening_range.json`` to
        ``_path_for(entry)``. When ``target_folder`` is supplied and
        the calculation succeeds, an identical copy is also written
        to ``<target_folder>/opening_range.json``.
        """
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

        candle_source = "live_engine"

        # ---- REST fallback when the live 09:15 candle is missing ------ #
        if first_candle is None:
            rest_candle = _fetch_first_candle_via_rest(key, target_minute)
            if rest_candle is not None:
                first_candle = rest_candle
                candle_source = "rest_intraday_fallback"
                logger.info(
                    "[or] %s: 09:15 candle recovered via REST fallback",
                    key,
                )

        if first_candle is None:
            return {
                "status": "skipped",
                "reason": "first_candle_not_available",
                "instrument_key": key,
                "target_minute": target_minute,
                "candle_source": candle_source,
                "target_folder": (
                    str(target_folder) if target_folder is not None else None
                ),
            }

        h = first_candle.get("high")
        l = first_candle.get("low")
        if h is None or l is None:
            return {
                "status": "skipped",
                "reason": "incomplete_candle",
                "instrument_key": key,
                "target_minute": target_minute,
                "candle_source": candle_source,
                "target_folder": (
                    str(target_folder) if target_folder is not None else None
                ),
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
            "candle_source": candle_source,
            "levels": levels,
            "calculated_at": datetime.now(timezone.utc).isoformat(),
        }

        # ---- 1. Canonical write (unchanged behaviour) ----------------- #
        canonical_path = _path_for(entry)
        try:
            _write_json_atomic(canonical_path, result)
        except Exception:
            logger.exception("[or] failed writing %s", canonical_path)
            return {
                "status": "error",
                "reason": "write_failed",
                "instrument_key": key,
                "path": str(canonical_path),
            }

        # ---- 2. Optional target-folder copy --------------------------- #
        # This is how Stage 1 puts the strike's OR into its Stage-1
        # strike folder, which is where the S1/S2 trackers read it.
        # Only write if the calculation actually produced levels — the
        # copy is skipped on the early returns above.
        target_written: str | None = None
        if target_folder is not None:
            try:
                target_path = (
                    target_folder / config.OPENING_RANGE_FILENAME
                )
                _write_json_atomic(target_path, result)
                target_written = str(target_path)
                logger.info(
                    "[or] %s: strike OR copied to %s",
                    key, target_path,
                )
            except Exception:
                # A failed copy must not fail the whole call — the
                # canonical write already succeeded, and the caller
                # (Stage 1) will fall back to its own copy logic if
                # this returns None for target_written.
                logger.exception(
                    "[or] %s: failed copying OR to %s",
                    key, target_folder,
                )

        result["status"] = "ok"
        result["canonical_path"] = str(canonical_path)
        result["target_path"] = target_written
        return result


opening_range_service = OpeningRangeService()