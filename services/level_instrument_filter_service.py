# services/level_instrument_filter_service.py
"""Level-instrument filter — G1 v3.2 multi-index variant.

Per-index OR level processing:
    * NIFTY      -> filtered CE/PE strike list (execution universe)
    * SENSEX     -> filtered CE/PE strike list (info only)
    * BANKNIFTY  -> filtered CE/PE strike list (info only)
    * MIDCPNIFTY -> filtered CE/PE strike list (info only)

The isolation engine routes ALL touches to NIFTY strikes, so only
NIFTY's level_instruments.json is used for execution. The other three
are still generated because first_candle.json must exist for them
(each index needs its own R3/S3 for touch detection).

Feed subscription (2026-10-30)
------------------------------
After writing ``level_instruments.json`` the service registers the
CE+PE strikes for each index with **both**:

    * ``live_sr_levels_service`` — so the SR engine tracks them;
    * ``feed_subscription_manager.set_level_mandatory()`` — so the
      live feed keeps streaming their ticks.

The second registration is the important one. Before 2026-10-30 the
strikes in ``level_instruments.json`` were only registered with the
SR engine (Tier-2 evictable ATM band, or the isolated / promoted key
if one of them happened to be the level strike). In practice the ATM
band slides away from a level strike within minutes of the open, at
which point the SR engine stopped receiving ticks for exactly the
contracts the isolation engine was watching. The strategy armed on a
stale candle series and the option-chain LTP panel froze.

``set_level_mandatory()`` closes that hole: it registers the strikes
as Tier-1 (never evicted) so they keep ticking for the whole session
regardless of where the ATM band has moved.

The behaviour is gated on ``LIVE_FEED_LEVEL_INSTRUMENTS_MANDATORY``
(default True). Set it to false in ``.env`` to restore the pre-
2026-10-30 behaviour (SR-tracked but evictable). The flag is read via
``getattr(..., True)`` so the feature works even if ``core/config.py``
has not been updated with the constant yet.

Design notes
------------
* The mandatory registration is deliberately in ``_process_index``
  rather than ``_register_with_sr``: they share a lifecycle
  (both run after ``level_instruments.json`` is written) but they
  fail independently — an SR-registration failure must not stop the
  mandatory set from being updated, and vice versa.
* ``set_level_mandatory()`` uses **replace** semantics. Every
  ``run()`` recomputes the strikes from today's OR levels, which do
  not change mid-session, so a re-run inside the same day is a no-op
  (the set is identical). The replace semantics matter for the edge
  case where the filter is re-run after a data reload with a smaller
  strike set — the stale keys are unregistered automatically.
* An empty CE+PE list (e.g. ``no_first_candle``) results in
  ``set_level_mandatory(underlying, [])``, which removes the level-
  mandatory set for that index. This is the correct fallback: if we
  cannot resolve strikes, we should not hold stale keys as mandatory.
* Keys added here do NOT collide with dummy-order mandatory keys.
  ``feed_subscription_manager`` keeps them in a separate dict
  (``_level_mandatory``) so a ``set_mandatory()`` replacement from a
  feed restart never wipes them, and a level-filter re-run never
  wipes a dummy-order pin.
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
INDEX_REGISTRY_KEYS: dict[str, str] = {
    "NIFTY":       "NSE_INDEX|Nifty 50",
    "BANKNIFTY":   "NSE_INDEX|Nifty Bank",
    "MIDCPNIFTY":  "NSE_INDEX|NIFTY MID SELECT",
    "SENSEX":      "BSE_INDEX|SENSEX",
}

_DEFAULT_STEPS: dict[str, int] = {
    "NIFTY":       getattr(config, "LEVEL_FILTER_STRIKE_STEP_NIFTY", 50),
    "BANKNIFTY":   getattr(config, "LEVEL_FILTER_STRIKE_STEP_BANKNIFTY", 100),
    "MIDCPNIFTY":  getattr(config, "LEVEL_FILTER_STRIKE_STEP_MIDCPNIFTY", 25),
    "SENSEX":      getattr(config, "LEVEL_FILTER_STRIKE_STEP_SENSEX", 100),
}

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

_RESISTANCE_LEVELS = ("R1", "R2", "R3", "R3_Threshold")
_SUPPORT_LEVELS = ("S1", "S2", "S3", "S3_Threshold")

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
    past: list[tuple[date, dict[str, Any]]] = []
    for e in entries:
        d = _parse_expiry_date(e.get("expiry"))
        if d is not None:
            past.append((d, e))
    if past:
        past.sort(key=lambda t: t[0], reverse=True)
        return past[0][1]
    return undated[0] if undated else entries[0]

def _feed_level_mandatory_enabled() -> bool:
    """Read ``LIVE_FEED_LEVEL_INSTRUMENTS_MANDATORY`` defensively.

    Defaults to True so the behaviour is on out of the box even if
    ``core/config.py`` has not been updated with the constant yet.
    """
    return bool(getattr(config, "LIVE_FEED_LEVEL_INSTRUMENTS_MANDATORY", True))

# --------------------------------------------------------------------------- #
# Service                                                                     #
# --------------------------------------------------------------------------- #
class LevelInstrumentFilterService:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_status: dict[str, Any] = {"status": "never"}

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

            # Execution index (NIFTY) must succeed for the service to
            # be considered OK. Other indexes are informational.
            exec_underlying = "NIFTY"
            execution_ok = (
                results.get(exec_underlying, {}).get("status") == "ok"
            )
            any_ok = any(
                r.get("status") == "ok" for r in results.values()
            )
            summary = {
                "status": "ok" if execution_ok else (
                    "no_data" if not any_ok else "partial"
                ),
                "reason": reason,
                "date": today,
                "execution_index": exec_underlying,
                "indexes": results,
                "ran_at": _now_iso(),
            }
            return self._record(summary)

    def last_status(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._last_status)

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

        all_strikes: dict[int, list[str]] = {}
        for strike, level_names in ce_candidates.items():
            bucket = all_strikes.setdefault(strike, [])
            for name in level_names:
                if name not in bucket:
                    bucket.append(name)
        for strike, level_names in pe_candidates.items():
            bucket = all_strikes.setdefault(strike, [])
            for name in level_names:
                if name not in bucket:
                    bucket.append(name)

        if config.LEVEL_FILTER_INCLUDE_ATM_PLUS_MINUS > 0:
            atm_strike = self._atm_strike(first_candle, or_levels, step)
            n = int(config.LEVEL_FILTER_INCLUDE_ATM_PLUS_MINUS)
            for offset in range(-n, n + 1):
                strike = atm_strike + offset * step
                if strike <= 0:
                    continue
                tag = f"ATM{offset:+d}"
                bucket = all_strikes.setdefault(strike, [])
                if tag not in bucket:
                    bucket.append(tag)

        lo_strike, hi_strike = self._range_bounds(or_levels, step, range_mode)

        ce_instruments = self._resolve_instruments(
            underlying, all_strikes, "CE", step, "up",
            lo_strike, hi_strike,
        )
        pe_instruments = self._resolve_instruments(
            underlying, all_strikes, "PE", step, "down",
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
            "execution_routed_to": str(
                config.STRATEGY_ROUTED_EXECUTION_INDEX or "NSE_INDEX|Nifty 50"
            ),
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

        if config.LIVE_SR_ENABLED:
            self._register_with_sr(index_key, ce_instruments, pe_instruments)

        # NEW (2026-10-30): register the CE+PE strikes as feed-mandatory
        # so their ticks keep flowing for the whole session, regardless
        # of where the ATM band slides. Runs unconditionally of
        # ``LIVE_SR_ENABLED`` — the mandatory-tier registration is
        # useful even when the SR engine is off, because it guarantees
        # the dashboard LTP panel keeps updating for the level strikes.
        feed_mandatory_summary = self._register_with_feed_mandatory(
            underlying, ce_instruments, pe_instruments,
        )

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
            "feed_mandatory": feed_mandatory_summary,
        }

    @staticmethod
    def _build_candidates(
        or_levels: dict[str, Any],
        level_names: tuple[str, ...],
        step: int,
        direction: str,
    ) -> dict[int, list[str]]:
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
            level_names = candidates[strike]
            is_core = any(
                name in _RESISTANCE_LEVELS or name in _SUPPORT_LEVELS
                for name in level_names
            )
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
                "type": strike_type,
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
    # NEW (2026-10-30): feed-mandatory registration                      #
    # ------------------------------------------------------------------ #
    def _register_with_feed_mandatory(
        self,
        underlying: str,
        ce_instruments: list[dict[str, Any]],
        pe_instruments: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Mark the level strikes for ``underlying`` as feed-mandatory.

        Called from ``_process_index`` after ``level_instruments.json``
        is written. Delegates to
        ``feed_subscription_manager.set_level_mandatory()``, which:

          * stores the CE+PE strikes for this index in the manager's
            level-mandatory dict (separate from the base mandatory
            set, so a feed restart never wipes them);
          * calls ``apply()`` (when the set changed) so the streamer
            subscribes to the new keys immediately;
          * never raises.

        If ``LIVE_FEED_LEVEL_INSTRUMENTS_MANDATORY`` is false, returns
        ``{"status": "disabled"}`` without touching the manager.

        If the CE+PE lists are empty (e.g. ``no_first_candle`` path
        never reaches here, but a successful run with zero resolved
        strikes would), the call becomes
        ``set_level_mandatory(underlying, [])`` which **removes** the
        per-index set. That is the correct fallback: we should not
        keep stale keys mandatory once we cannot resolve today's
        strikes.

        Never raises. Returns a summary dict for the caller to embed
        in its per-index payload.
        """
        keys: list[str] = []
        for inst in list(ce_instruments) + list(pe_instruments):
            if not isinstance(inst, dict):
                continue
            key = str(inst.get("instrument_key") or "").strip()
            if key and key not in keys:
                keys.append(key)

        summary: dict[str, Any] = {
            "status": "ok",
            "underlying": underlying,
            "keys_total": len(keys),
            "added": [],
            "removed": [],
            "size": 0,
            "error": None,
        }

        if not _feed_level_mandatory_enabled():
            summary["status"] = "disabled"
            return summary

        try:
            from services.feed_subscription_manager import (
                feed_subscription_manager,
            )
        except Exception as exc:
            logger.exception(
                "[level-filter] feed_subscription_manager import failed "
                "for %s", underlying,
            )
            summary["status"] = "error"
            summary["error"] = f"{type(exc).__name__}: {exc}"
            return summary

        try:
            result = feed_subscription_manager.set_level_mandatory(
                underlying, keys,
            )
        except Exception as exc:
            logger.exception(
                "[level-filter] set_level_mandatory failed for %s",
                underlying,
            )
            summary["status"] = "error"
            summary["error"] = f"{type(exc).__name__}: {exc}"
            return summary

        if isinstance(result, dict):
            summary["added"] = list(result.get("added") or [])
            summary["removed"] = list(result.get("removed") or [])
            summary["size"] = int(result.get("size") or 0)
            if not keys:
                summary["status"] = "cleared"
        else:
            # Defensive: a mismatch between this service and the
            # manager's return contract should not break the filter.
            summary["size"] = len(keys)

        logger.info(
            "[level-filter] feed-mandatory %s — keys=%d added=%d "
            "removed=%d size=%d",
            underlying,
            summary["keys_total"],
            len(summary["added"]),
            len(summary["removed"]),
            summary["size"],
        )
        return summary

    def _record(self, status: dict[str, Any]) -> dict[str, Any]:
        status.setdefault("ran_at", _now_iso())
        self._last_status = status
        return status

level_instrument_filter_service = LevelInstrumentFilterService()