# File: services/level_instrument_filter_service.py
"""Level-instrument filter — turns an index's OR levels into a list of
CE / PE option instruments that should be tracked by the live SR engine.

Algorithm (per index, per side)
--------------------------------
1. Read ``data/index/<underlying_lower>/first_candle.json`` (written by
   ``first_candle_service``).
2. Skip when the file is missing, unparseable, or dated before today.
3. Compute strike candidates:
       CE  (resistance):  strike = ceil(raw_level / step) * step
       PE  (support)   :  strike = floor(raw_level / step) * step
   for R1, R2, R3, R3_Threshold  and  S1, S2, S3, S3_Threshold.
4. De-duplicate strikes; attach every matched level tag to the same
   instrument (e.g. R2 + R3_Threshold -> one CE at 22800).
5. Range filter (``LEVEL_FILTER_RANGE_MODE``):
       "wide"   -> S3 <= strike <= R3
       "strict" -> S3_Threshold <= strike <= R3_Threshold
   Core level strikes are always kept even when a stricter range would
   exclude them — this matches the worked example in the reference doc
   where R3 (22850) and S3 (22400) appear in the output.
6. Resolve each strike to a **listed** instrument via the registry.
   Unlisted strikes are moved outward (up for CE, down for PE) until a
   listed strike is found (configurable max steps).
7. Persist ``data/index/<underlying_lower>/level_instruments.json``.
8. Register every resolved instrument (plus the index itself, unless
   disabled) with ``live_sr_levels_service``.

Output file shape (``level_instruments.json``)
----------------------------------------------
{
  "underlying": "NIFTY",
  "index_instrument_key": "NSE_INDEX|Nifty 50",
  "strike_step": 50,
  "date": "2026-10-27",
  "first_candle": {"time_iso": ..., "open": ..., "high": ..., "low": ..., "close": ...},
  "or_levels": {"H": ..., "L": ..., "A": ..., "D_H": ..., "D_L": ...,
                "R1": ..., "R2": ..., "R3": ..., "S1": ..., "S2": ..., "S3": ...,
                "R3_Threshold": ..., "S3_Threshold": ...},
  "range_filter": {"mode": "wide", "lo": ..., "hi": ...},
  "ce_instruments": [
    {"strike": 22700, "instrument_key": "NSE_FO|...", "trading_symbol": "...",
     "expiry": "...", "levels": ["R1"], "reasons": ["resistance pivot"]},
    ...
  ],
  "pe_instruments": [...],
  "index_sr_tracked": true,
  "generated_at": "..."
}

Safety
------
* Idempotent — running the service twice on the same day produces the
  same JSON (apart from ``generated_at``).
* Never raises — always returns a summary dict.
* No credentials are read or written by this module.
"""

from __future__ import annotations

import json
import math
import threading
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from core import config
from core.logger import get_logger
from services.instrument_registry import instrument_registry

logger = get_logger(__file__)

# --------------------------------------------------------------------------- #
# Index metadata                                                              #
# --------------------------------------------------------------------------- #
# Canonical ticker -> registry instrument_key for the two built-in indexes.
INDEX_REGISTRY_KEYS: dict[str, str] = {
    "NIFTY":  "NSE_INDEX|Nifty 50",
    "SENSEX": "BSE_INDEX|SENSEX",
}

# Fallback strike steps when LEVEL_FILTER_STRIKE_STEPS_JSON is absent/invalid.
_DEFAULT_STEPS: dict[str, int] = {
    "NIFTY":  config.LEVEL_FILTER_STRIKE_STEP_NIFTY,
    "SENSEX": config.LEVEL_FILTER_STRIKE_STEP_SENSEX,
    "BANKNIFTY": 100,
    "FINNIFTY": 50,
    "MIDCPNIFTY": 100,
}

# Human-readable reason per level (goes into the JSON for UI/debug).
LEVEL_REASONS: dict[str, str] = {
    "R1": "resistance pivot",
    "R2": "resistance extension",
    "R3": "outer resistance",
    "R3_Threshold": "resistance threshold",
    "S1": "support pivot",
    "S2": "support extension",
    "S3": "outer support",
    "S3_Threshold": "support threshold",
}

# Level name -> side (used when merging duplicate strikes).
_RESISTANCE_LEVELS = ("R1", "R2", "R3", "R3_Threshold")
_SUPPORT_LEVELS = ("S1", "S2", "S3", "S3_Threshold")

# Maximum outward steps when hunting for a listed strike.
_MAX_LISTING_STEPS = 20

# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def _today_iso() -> str:
    return datetime.now().astimezone().date().isoformat()

def _parse_strike_step_overrides() -> dict[str, int]:
    raw = (config.LEVEL_FILTER_STRIKE_STEPS_JSON or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning(
            "[level-filter] LEVEL_FILTER_STRIKE_STEPS_JSON is not valid "
            "JSON; falling back to scalar defaults."
        )
        return {}
    if not isinstance(parsed, dict):
        return {}
    out: dict[str, int] = {}
    for k, v in parsed.items():
        try:
            step = int(v)
        except (TypeError, ValueError):
            continue
        if step > 0:
            out[str(k).strip().upper()] = step
    return out

def _resolve_strike_step(underlying: str) -> int:
    overrides = _parse_strike_step_overrides()
    if underlying in overrides:
        return overrides[underlying]
    return _DEFAULT_STEPS.get(underlying, 50)

def _round_up(price: float, step: int) -> int:
    return int(math.ceil(float(price) / float(step)) * step)

def _round_down(price: float, step: int) -> int:
    return int(math.floor(float(price) / float(step)) * step)

def _parse_expiry_date(value: Any) -> date | None:
    """Return a ``date`` for an expiry that is ISO-string or epoch-millis."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        n = float(value)
        if n > 1e12:
            n /= 1000.0
        try:
            return datetime.fromtimestamp(n, tz=timezone.utc).date()
        except (OSError, OverflowError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    cleaned = text.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(cleaned).date()
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(cleaned.split("T", 1)[0]).date()
    except ValueError:
        return None

def _pick_nearest_expiry(entries: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Pick the entry with the nearest expiry >= today; fall back to first."""
    if not entries:
        return None
    today = date.today()
    dated: list[tuple[date, dict[str, Any]]] = []
    undated: list[dict[str, Any]] = []
    for e in entries:
        d = _parse_expiry_date(e.get("expiry"))
        if d is None:
            undated.append(e)
        elif d >= today:
            dated.append((d, e))
    if dated:
        dated.sort(key=lambda t: t[0])
        return dated[0][1]
    # No future expiry — prefer the most recent past one.
    past: list[tuple[date, dict[str, Any]]] = []
    for e in entries:
        d = _parse_expiry_date(e.get("expiry"))
        if d is not None:
            past.append((d, e))
    if past:
        past.sort(key=lambda t: t[0], reverse=True)
        return past[0][1]
    return undated[0] if undated else entries[0]

# --------------------------------------------------------------------------- #
# Service                                                                     #
# --------------------------------------------------------------------------- #
class LevelInstrumentFilterService:
    """Convert OR levels -> filtered CE/PE instruments -> SR registrations."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_status: dict[str, Any] = {"status": "never"}

    # ------------------------------------------------------------------ #
    # Public API                                                         #
    # ------------------------------------------------------------------ #
    def run(self, reason: str = "manual") -> dict[str, Any]:
        if not config.LEVEL_FILTER_ENABLED:
            return self._record({
                "status": "disabled",
                "reason": reason,
                "ran_at": _now_iso(),
            })

        today = _today_iso()
        results: dict[str, dict[str, Any]] = {}

        with self._lock:
            for underlying in config.LEVEL_FILTER_INDEXES:
                u = str(underlying or "").strip().upper()
                if not u:
                    continue
                try:
                    results[u] = self._process_index(u, today)
                except Exception as exc:
                    logger.exception(
                        "[level-filter] %s failed", u
                    )
                    results[u] = {
                        "status": "error",
                        "underlying": u,
                        "error": f"{type(exc).__name__}: {exc}",
                    }

            any_ok = any(
                r.get("status") == "ok" for r in results.values()
            )
            summary = {
                "status": "ok" if any_ok else "no_data",
                "reason": reason,
                "date": today,
                "indexes": results,
                "ran_at": _now_iso(),
            }
            return self._record(summary)

    def last_status(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._last_status)

    # ------------------------------------------------------------------ #
    # Per-index pipeline                                                 #
    # ------------------------------------------------------------------ #
    def _process_index(self, underlying: str, today: str) -> dict[str, Any]:
        index_key = INDEX_REGISTRY_KEYS.get(underlying)
        if not index_key:
            return {
                "status": "unknown_index",
                "underlying": underlying,
                "reason": "no mapping for this underlying in INDEX_REGISTRY_KEYS",
            }

        folder = Path(config.LIVE_DATA_DIR) / "index" / underlying.lower()
        fc_path = folder / "first_candle.json"
        if not fc_path.exists():
            return {
                "status": "no_first_candle",
                "underlying": underlying,
                "path": str(fc_path),
            }

        try:
            fc_payload = json.loads(fc_path.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("[level-filter] cannot parse %s", fc_path)
            return {
                "status": "unparseable_first_candle",
                "underlying": underlying,
                "path": str(fc_path),
            }

        if str(fc_payload.get("date") or "") != today:
            return {
                "status": "stale_first_candle",
                "underlying": underlying,
                "first_candle_date": fc_payload.get("date"),
                "today": today,
            }

        or_levels = fc_payload.get("or_levels") or {}
        first_candle = fc_payload.get("first_candle") or {}
        if not or_levels:
            return {
                "status": "no_or_levels",
                "underlying": underlying,
            }

        step = _resolve_strike_step(underlying)
        range_mode = str(config.LEVEL_FILTER_RANGE_MODE or "wide").lower()

        ce_candidates = self._build_candidates(or_levels, _RESISTANCE_LEVELS, step, "up")
        pe_candidates = self._build_candidates(or_levels, _SUPPORT_LEVELS, step, "down")

        # Optional ATM +/- N extras (filtered by the S3..R3 range below).
        if config.LEVEL_FILTER_INCLUDE_ATM_PLUS_MINUS > 0:
            atm_strike = self._atm_strike(first_candle, or_levels, step)
            n = int(config.LEVEL_FILTER_INCLUDE_ATM_PLUS_MINUS)
            for offset in range(-n, n + 1):
                strike = atm_strike + offset * step
                if strike <= 0:
                    continue
                ce_candidates.setdefault(strike, []).append(f"ATM{offset:+d}")
                pe_candidates.setdefault(strike, []).append(f"ATM{offset:+d}")

        lo_strike, hi_strike = self._range_bounds(or_levels, step, range_mode)

        ce_instruments = self._resolve_instruments(
            underlying, ce_candidates, "CE", step, "up",
            lo_strike, hi_strike,
        )
        pe_instruments = self._resolve_instruments(
            underlying, pe_candidates, "PE", step, "down",
            lo_strike, hi_strike,
        )

        payload = {
            "underlying": underlying,
            "index_instrument_key": index_key,
            "strike_step": step,
            "date": today,
            "first_candle": first_candle,
            "or_levels": or_levels,
            "range_filter": {
                "mode": range_mode,
                "lo": lo_strike,
                "hi": hi_strike,
            },
            "ce_instruments": ce_instruments,
            "pe_instruments": pe_instruments,
            "index_sr_tracked": bool(config.LEVEL_FILTER_INCLUDE_INDEX_SR),
            "generated_at": _now_iso(),
        }

        out_path = folder / "level_instruments.json"
        folder.mkdir(parents=True, exist_ok=True)
        tmp = out_path.with_suffix(out_path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(payload, indent=2, default=str),
            encoding="utf-8",
        )
        tmp.replace(out_path)
        logger.info(
            "[level-filter] %s -> %s (CE=%d, PE=%d, step=%d, range=%s)",
            underlying, out_path, len(ce_instruments), len(pe_instruments),
            step, range_mode,
        )

        # Register with the live SR engine.
        if config.LIVE_SR_ENABLED:
            self._register_with_sr(index_key, ce_instruments, pe_instruments)

        return {
            "status": "ok",
            "underlying": underlying,
            "strike_step": step,
            "range_mode": range_mode,
            "ce_count": len(ce_instruments),
            "pe_count": len(pe_instruments),
            "ce_strikes": [i["strike"] for i in ce_instruments],
            "pe_strikes": [i["strike"] for i in pe_instruments],
            "out_path": str(out_path),
        }

    # ------------------------------------------------------------------ #
    # Candidate construction                                             #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _build_candidates(
        or_levels: dict[str, Any],
        level_names: tuple[str, ...],
        step: int,
        direction: str,
    ) -> dict[int, list[str]]:
        """Map raw levels to rounded strikes, merging duplicate strikes."""
        out: dict[int, list[str]] = {}
        for name in level_names:
            raw = or_levels.get(name)
            if raw is None:
                continue
            try:
                raw_f = float(raw)
            except (TypeError, ValueError):
                continue
            if direction == "up":
                strike = _round_up(raw_f, step)
            else:
                strike = _round_down(raw_f, step)
            out.setdefault(strike, [])
            if name not in out[strike]:
                out[strike].append(name)
        return out

    @staticmethod
    def _atm_strike(
        first_candle: dict[str, Any],
        or_levels: dict[str, Any],
        step: int,
    ) -> int:
        ref = first_candle.get("close") if isinstance(first_candle, dict) else None
        if ref is None and isinstance(or_levels, dict):
            ref = or_levels.get("A")
        try:
            return int(round(float(ref) / float(step)) * step)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _range_bounds(
        or_levels: dict[str, Any],
        step: int,
        mode: str,
    ) -> tuple[float | None, float | None]:
        if mode == "strict":
            lo_raw = or_levels.get("S3_Threshold")
            hi_raw = or_levels.get("R3_Threshold")
        else:
            lo_raw = or_levels.get("S3")
            hi_raw = or_levels.get("R3")
        try:
            lo = float(lo_raw) if lo_raw is not None else None
        except (TypeError, ValueError):
            lo = None
        try:
            hi = float(hi_raw) if hi_raw is not None else None
        except (TypeError, ValueError):
            hi = None
        return lo, hi

    # ------------------------------------------------------------------ #
    # Resolve strikes to listed instruments                              #
    # ------------------------------------------------------------------ #
    def _resolve_instruments(
        self,
        underlying: str,
        candidates: dict[int, list[str]],
        strike_type: str,
        step: int,
        direction: str,
        lo: float | None,
        hi: float | None,
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for strike in sorted(candidates.keys()):
            # Range filter — applied only to extras (ATM +/- N). Core level
            # strikes are always kept, matching the reference example where
            # R3 (22850) and S3 (22400) appear even though they lie just
            # outside the raw S3..R3 band.
            level_names = candidates[strike]
            is_core = any(name in _RESISTANCE_LEVELS or name in _SUPPORT_LEVELS
                          for name in level_names)
            if not is_core and lo is not None and hi is not None:
                if not (lo <= float(strike) <= hi):
                    continue

            entry = self._find_listed_instrument(
                underlying, strike, strike_type, step, direction,
            )
            if entry is None:
                logger.warning(
                    "[level-filter] no listed %s strike near %s for %s",
                    strike_type, strike, underlying,
                )
                continue

            resolved_strike = self._entry_strike(entry)
            if resolved_strike is None:
                continue

            existing = next(
                (x for x in out if x["instrument_key"] == entry["instrument_key"]),
                None,
            )
            if existing is not None:
                for name in level_names:
                    if name not in existing["levels"]:
                        existing["levels"].append(name)
                        reason = LEVEL_REASONS.get(name)
                        if reason and reason not in existing["reasons"]:
                            existing["reasons"].append(reason)
                continue

            out.append({
                "strike": int(resolved_strike),
                "instrument_key": entry["instrument_key"],
                "trading_symbol": entry.get("trading_symbol"),
                "expiry": entry.get("expiry"),
                "levels": list(level_names),
                "reasons": [
                    LEVEL_REASONS[name]
                    for name in level_names
                    if name in LEVEL_REASONS
                ],
            })
        return out

    @staticmethod
    def _entry_strike(entry: dict[str, Any]) -> int | None:
        try:
            return int(float(entry.get("strike_price")))
        except (TypeError, ValueError):
            return None

    def _find_listed_instrument(
        self,
        underlying: str,
        target_strike: int,
        strike_type: str,
        step: int,
        direction: str,
    ) -> dict[str, Any] | None:
        all_entries = instrument_registry.query(
            underlying=underlying, strike_type=strike_type,
        )
        if not all_entries:
            return None

        by_strike: dict[int, list[dict[str, Any]]] = {}
        for e in all_entries:
            s = self._entry_strike(e)
            if s is None:
                continue
            by_strike.setdefault(s, []).append(e)

        strike = int(target_strike)
        for _ in range(_MAX_LISTING_STEPS):
            if strike in by_strike:
                return _pick_nearest_expiry(by_strike[strike])
            if direction == "up":
                strike += step
            else:
                strike -= step
            if strike <= 0:
                break
        return None

    # ------------------------------------------------------------------ #
    # Register with live SR engine                                       #
    # ------------------------------------------------------------------ #
    def _register_with_sr(
        self,
        index_key: str,
        ce_instruments: list[dict[str, Any]],
        pe_instruments: list[dict[str, Any]],
    ) -> None:
        try:
            from services.live_sr_levels_service import live_sr_levels_service
        except Exception:
            logger.exception(
                "[level-filter] live_sr_levels_service import failed"
            )
            return

        if config.LEVEL_FILTER_INCLUDE_INDEX_SR:
            idx_entry = instrument_registry.get(index_key)
            if idx_entry is not None:
                try:
                    live_sr_levels_service.register_instrument(idx_entry)
                except Exception:
                    logger.exception(
                        "[level-filter] SR register index %s failed", index_key,
                    )

        for inst in list(ce_instruments) + list(pe_instruments):
            key = inst.get("instrument_key")
            if not key:
                continue
            entry = instrument_registry.get(key)
            if entry is None:
                continue
            try:
                live_sr_levels_service.register_instrument(entry)
            except Exception:
                logger.exception(
                    "[level-filter] SR register %s failed", key,
                )

    # ------------------------------------------------------------------ #
    # Internals                                                          #
    # ------------------------------------------------------------------ #
    def _record(self, status: dict[str, Any]) -> dict[str, Any]:
        status.setdefault("ran_at", _now_iso())
        self._last_status = status
        return status

level_instrument_filter_service = LevelInstrumentFilterService()