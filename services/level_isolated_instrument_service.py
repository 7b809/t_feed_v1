# services/level_isolated_instrument_service.py
"""Level-isolation engine — pick one filtered CE/PE instrument when
NIFTY or SENSEX touches R3 / S3.

Design
------
* Trigger: NIFTY or SENSEX index LTP crosses its own R3 (>=) or S3 (<=),
  read from ``data/index/<idx>/first_candle.json -> or_levels``.
* Candidate instruments come from ``data/index/<idx>/level_instruments.json``
  (produced by ``level_instrument_filter_service``). First preference:
  instruments whose ``levels`` array contains ``R3`` / ``R3_Threshold``
  (CE) or ``S3`` / ``S3_Threshold`` (PE). Fallback: all instruments of
  that side.
* Priority (one-shot per side, per day):
      NIFTY-R3 -> NIFTY-S3 -> SENSEX-R3 -> SENSEX-S3
* NIFTY / SENSEX pick: nearest strike to the index LTP that triggered
  the touch.
* SENSEX-only touch: read the NIFTY LTP *at that moment* (cached from the
  last NIFTY tick). If it is fresh (``LEVEL_ISOLATION_NIFTY_TICK_STALE_SECONDS``),
  pick the **nearest NIFTY filtered instrument** (CE or PE) to that LTP.
  If stale, fall back to SENSEX's own nearest candidate.
* Persisted result: ``data/<LEVEL_ISOLATION_FILENAME>`` — one JSON doc
  per day, containing ``isolated``, ``events[]``, ``touch_state``.

Instrument snapshot
-------------------
On isolation, ``_collect_instrument_snapshot()`` enriches the
``isolated`` record with the picked instrument's OWN data at the moment
of the touch:

  * ``instrument_ltp``              — the CE/PE's LTP
  * ``instrument_touch_ohlc``       — the CE/PE's O/H/L/C at the touch
                                      minute (only populated when the
                                      exact touch bar is found)
  * ``instrument_first_candle``     — the CE/PE's own 09:15 first candle
                                      OHLC
  * ``instrument_or_levels``        — the CE/PE's own OR levels
  * ``instrument_quote_source``     — which source was used
                                      ("live_feed_cache" /
                                       "intraday_bar" /
                                       "intraday_latest_bar" /
                                       "first_candle_close" /
                                       "or_midpoint" /
                                       "unavailable")

Records created before this feature existed will have NULL snapshot
fields. ``run_backfill()`` auto-detects stale records and re-collects
the snapshot **once** without touching the isolation decision — the
recheck job fires this every 5 minutes during market hours, so the
record self-heals shortly after the code is deployed.

Startup backfill
----------------
Between ``LEVEL_ISOLATION_BACKFILL_WINDOW_START_*`` and ``_END_*`` on a
trading day, ``run_backfill()`` replays today's 1-min intraday candles
minute-by-minute and reconstructs the "would-have-isolated" instrument:
  * touch detection uses bar **high** for R3, bar **low** for S3;
  * LTP-based decisions (nearest strike) use bar **close**.
On the backfill path, the instrument snapshot also does one intraday
REST call per isolated instrument (once per day, at startup, on a
worker thread — never blocks the live feed).

UI-facing detail endpoint
-------------------------
``get_detail()`` returns a JSON-safe snapshot for the ``/isolated`` HTML
page and the ``GET /admin/level-isolation/detail`` JSON endpoint.

Safety
------
* Never raises for expected failure modes; always returns a summary.
* Non-trading hours / non-trading days / missing OR files → no-op.
* Atomic writes (``.tmp`` + ``replace``) so a crash cannot corrupt the
  persisted state file.
* Telegram fires from a **background daemon thread** so the live feed
  loop is never blocked by an HTTP call.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, time as dtime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from core import config
from core.logger import get_logger

logger = get_logger(__file__)

_MARKET_TZ = ZoneInfo("Asia/Kolkata")

# Registry key for the two indexes we care about.
CORE_INDEX_KEYS: dict[str, str] = {
    "NSE_INDEX|Nifty 50": "NIFTY",
    "BSE_INDEX|SENSEX":   "SENSEX",
}

_R3_TRIGGER_LEVELS = ("R3", "R3_Threshold")
_S3_TRIGGER_LEVELS = ("S3", "S3_Threshold")

_MARKET_OPEN = dtime(9, 15)
_MARKET_CLOSE = dtime(15, 30)
_OR_READY = dtime(9, 16, 10)
_VALID_WEEKDAYS = {0, 1, 2, 3, 4}


def _now_market() -> datetime:
    return datetime.now(_MARKET_TZ)


def _today_iso() -> str:
    return _now_market().date().isoformat()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class LevelIsolatedInstrumentService:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._state: dict[str, Any] = self._empty_state()
        self._last_nifty_ltp: float | None = None
        self._last_nifty_tick_at: datetime | None = None
        self._last_sensex_ltp: float | None = None
        self._last_sensex_tick_at: datetime | None = None
        self._last_check_monotonic: float = 0.0
        self._load_or_create_state()

    # ------------------------------------------------------------------ #
    # State file I/O                                                     #
    # ------------------------------------------------------------------ #
    def _state_path(self) -> Path:
        return Path(config.LIVE_DATA_DIR) / config.LEVEL_ISOLATION_FILENAME

    def _empty_state(self) -> dict[str, Any]:
        return {
            "day": _today_iso(),
            "isolated": None,
            "events": [],
            "touch_state": {
                "NIFTY_R3": False, "NIFTY_S3": False,
                "SENSEX_R3": False, "SENSEX_S3": False,
            },
            "last_update": None,
        }

    def _load_or_create_state(self) -> None:
        path = self._state_path()
        if not path.exists():
            return
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("[level-isolation] unreadable state file")
            return
        if not isinstance(loaded, dict):
            return
        if loaded.get("day") != self._state["day"]:
            return
        self._state = loaded

    def _save_state(self) -> None:
        path = self._state_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._state["last_update"] = _now_iso()
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(self._state, indent=2, default=str),
                encoding="utf-8",
            )
            tmp.replace(path)
        except Exception:
            logger.exception("[level-isolation] failed saving state")

    def _ensure_today(self) -> None:
        today = _today_iso()
        if self._state.get("day") != today:
            self._state = self._empty_state()
            self._last_nifty_ltp = None
            self._last_nifty_tick_at = None
            self._last_sensex_ltp = None
            self._last_sensex_tick_at = None
            self._last_check_monotonic = 0.0
            self._load_or_create_state()

    # ------------------------------------------------------------------ #
    # Market gates                                                       #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _in_market_hours(now: datetime) -> bool:
        return (
            now.weekday() in _VALID_WEEKDAYS
            and _MARKET_OPEN <= now.time() <= _MARKET_CLOSE
        )

    def _is_trading_day(self, now: datetime) -> bool:
        if now.weekday() not in _VALID_WEEKDAYS:
            return False
        try:
            from services.market_holiday_service import market_holiday_service
            payload = market_holiday_service.get_today()
            return str(payload.get("status") or "").lower() != "holiday"
        except Exception:
            # Provider error -> assume trading day (never silently skip).
            return True

    def _in_backfill_window(self, now: datetime) -> bool:
        start = dtime(
            config.LEVEL_ISOLATION_BACKFILL_WINDOW_START_HOUR,
            config.LEVEL_ISOLATION_BACKFILL_WINDOW_START_MINUTE,
        )
        end = dtime(
            config.LEVEL_ISOLATION_BACKFILL_WINDOW_END_HOUR,
            config.LEVEL_ISOLATION_BACKFILL_WINDOW_END_MINUTE,
        )
        return start <= now.time() <= end

    # ------------------------------------------------------------------ #
    # Live tick entry point                                              #
    # ------------------------------------------------------------------ #
    def on_tick(self, instrument_key: str, ltp: Any) -> None:
        """Called from ``live_feed_service._handle_feed`` for NIFTY/SENSEX."""
        if not config.LEVEL_ISOLATION_ENABLED:
            return

        canonical = CORE_INDEX_KEYS.get(instrument_key)
        if not canonical:
            return

        try:
            ltp_f = float(ltp)
        except (TypeError, ValueError):
            return

        now = _now_market()
        if not self._in_market_hours(now):
            return

        with self._lock:
            self._ensure_today()

            if canonical == "NIFTY":
                self._last_nifty_ltp = ltp_f
                self._last_nifty_tick_at = now
            else:
                self._last_sensex_ltp = ltp_f
                self._last_sensex_tick_at = now

            now_mono = time.monotonic()
            if (
                now_mono - self._last_check_monotonic
                < config.LEVEL_ISOLATION_CHECK_INTERVAL_SECONDS
            ):
                return
            self._last_check_monotonic = now_mono

            self._check_touches(now)

    # ------------------------------------------------------------------ #
    # Input file readers                                                 #
    # ------------------------------------------------------------------ #
    def _load_or_levels(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for index_name, idx_lower in (("NIFTY", "nifty"), ("SENSEX", "sensex")):
            path = Path(config.LIVE_DATA_DIR) / "index" / idx_lower / "first_candle.json"
            if not path.exists():
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                logger.exception("[level-isolation] unreadable %s", path)
                continue
            if str(payload.get("date") or "") != _today_iso():
                continue
            levels = payload.get("or_levels") or {}
            if levels:
                out[index_name] = levels
        return out

    def _load_filtered_instruments(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for index_name, idx_lower in (("NIFTY", "nifty"), ("SENSEX", "sensex")):
            path = (
                Path(config.LIVE_DATA_DIR)
                / "index" / idx_lower / "level_instruments.json"
            )
            if not path.exists():
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                logger.exception("[level-isolation] unreadable %s", path)
                continue
            if str(payload.get("date") or "") != _today_iso():
                continue
            out[index_name] = payload
        return out

    # ------------------------------------------------------------------ #
    # Live touch checks                                                  #
    # ------------------------------------------------------------------ #
    def _check_touches(self, now: datetime) -> None:
        if self._state.get("isolated") is not None:
            return

        or_levels = self._load_or_levels()
        filtered = self._load_filtered_instruments()
        if not or_levels or not filtered:
            return

        checks = [
            ("NIFTY",  "R3", self._last_nifty_ltp),
            ("NIFTY",  "S3", self._last_nifty_ltp),
            ("SENSEX", "R3", self._last_sensex_ltp),
            ("SENSEX", "S3", self._last_sensex_ltp),
        ]

        for index_name, side, ltp in checks:
            if ltp is None:
                continue
            state_key = f"{index_name}_{side}"
            if self._state["touch_state"].get(state_key):
                continue
            level_value = or_levels.get(index_name, {}).get(side)
            if level_value is None:
                continue
            try:
                level_f = float(level_value)
            except (TypeError, ValueError):
                continue

            touched = (
                (side == "R3" and ltp >= level_f)
                or (side == "S3" and ltp <= level_f)
            )
            if not touched:
                continue

            self._state["touch_state"][state_key] = True
            self._process_touch(
                index_name, side, ltp, level_f,
                or_levels, filtered, now,
            )
            if self._state.get("isolated") is not None:
                break

    def _process_touch(
        self,
        index_name: str,
        side: str,
        ltp: float,
        level_value: float,
        or_levels: dict[str, dict[str, Any]],
        filtered: dict[str, dict[str, Any]],
        now: datetime,
    ) -> None:
        candidates = self._find_candidates(index_name, side, filtered)
        event: dict[str, Any] = {
            "index": index_name,
            "side": side,
            "trigger_level": side,
            "trigger_level_value": level_value,
            "trigger_ltp": ltp,
            "touched_at": now.isoformat(),
            "candidates_count": len(candidates),
            "candidates": self._enrich_candidates(candidates, ltp),
        }

        if self._state.get("isolated") is not None:
            event["status"] = "already_isolated"
            event["existing_isolated_instrument"] = (
                self._state["isolated"].get("instrument_key")
            )
            self._state["events"].append(event)
            self._save_state()
            return

        if not candidates:
            event["status"] = "no_candidates"
            self._state["events"].append(event)
            self._save_state()
            logger.warning(
                "[level-isolation] %s %s touched LTP=%s level=%s — no candidates",
                index_name, side, ltp, level_value,
            )
            return

        if index_name == "SENSEX":
            nifty_ltp = self._last_nifty_ltp
            nifty_tick_at = self._last_nifty_tick_at
            if nifty_ltp is not None and nifty_tick_at is not None:
                stale = (now - nifty_tick_at).total_seconds()
                max_stale = config.LEVEL_ISOLATION_NIFTY_TICK_STALE_SECONDS
                if stale <= max_stale:
                    nifty_candidates = self._all_nifty_filtered(filtered)
                    if nifty_candidates:
                        pick = min(
                            nifty_candidates,
                            key=lambda c: abs(self._strike(c) - nifty_ltp),
                        )
                        event["nifty_reference_ltp"] = nifty_ltp
                        event["nifty_reference_at"] = nifty_tick_at.isoformat()
                        event["nifty_reference_candidates_count"] = len(nifty_candidates)
                        event["nifty_reference_candidates"] = self._enrich_candidates(
                            nifty_candidates,
                            nifty_ltp,
                            distance_field="distance_to_nifty_ltp",
                        )
                        self._isolate(
                            pick,
                            f"sensex_{side.lower()}_with_nifty_reference",
                            event,
                        )
                        return
                else:
                    event["nifty_stale_seconds"] = round(stale, 1)
                    logger.warning(
                        "[level-isolation] NIFTY tick stale (%.0fs) at SENSEX "
                        "%s touch — using SENSEX fallback",
                        stale, side,
                    )

            pick = min(candidates, key=lambda c: abs(self._strike(c) - ltp))
            self._isolate(pick, f"sensex_{side.lower()}_fallback", event)
            return

        pick = min(candidates, key=lambda c: abs(self._strike(c) - ltp))
        self._isolate(pick, f"{index_name.lower()}_{side.lower()}_touch", event)

    # ------------------------------------------------------------------ #
    # Candidate helpers                                                  #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _strike(candidate: dict[str, Any]) -> float:
        try:
            return float(candidate.get("strike") or 0)
        except (TypeError, ValueError):
            return 0.0

    def _enrich_candidates(
        self,
        candidates: list[dict[str, Any]],
        ref_ltp: float,
        distance_field: str = "distance_to_trigger_ltp",
    ) -> list[dict[str, Any]]:
        return [
            {
                "instrument_key": c.get("instrument_key"),
                "trading_symbol": c.get("trading_symbol"),
                "strike": c.get("strike"),
                "type": c.get("type"),
                "levels": c.get("levels"),
                distance_field: round(abs(self._strike(c) - ref_ltp), 2),
            }
            for c in candidates[:20]
        ]

    def _find_candidates(
        self,
        index_name: str,
        side: str,
        filtered: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        payload = filtered.get(index_name, {})
        if side == "R3":
            instruments = payload.get("ce_instruments") or []
            trigger_levels = _R3_TRIGGER_LEVELS
        else:
            instruments = payload.get("pe_instruments") or []
            trigger_levels = _S3_TRIGGER_LEVELS

        primary = [
            inst for inst in instruments
            if any(
                lvl in trigger_levels
                for lvl in (inst.get("levels") or [])
            )
        ]
        if primary:
            return primary
        return list(instruments)

    def _all_nifty_filtered(
        self,
        filtered: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        payload = filtered.get("NIFTY", {})
        return (
            list(payload.get("ce_instruments") or [])
            + list(payload.get("pe_instruments") or [])
        )

    # ------------------------------------------------------------------ #
    # Underlying resolution                                              #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _resolve_underlying_for_candidate(
        candidate: dict[str, Any],
    ) -> str:
        """Return the option's underlying ticker (NIFTY / SENSEX / …).

        Resolution order:
          1. registry lookup (most accurate)
          2. trading_symbol first token (e.g. "NIFTY 22400 PE 27 OCT 26")
          3. empty string

        This deliberately ignores ``event.index`` because a SENSEX
        trigger may have picked a NIFTY instrument (the
        "with_nifty_reference" path), and the folder layout on disk is
        keyed by the *instrument's* underlying, not the trigger's.
        """
        key = str(candidate.get("instrument_key") or "").strip()
        if key:
            try:
                from services.instrument_registry import instrument_registry
                entry = instrument_registry.get(key)
                if entry and entry.get("underlying"):
                    return str(entry["underlying"]).strip().upper()
            except Exception:
                logger.exception(
                    "[level-isolation] registry underlying lookup failed for %s",
                    key,
                )
        symbol = str(candidate.get("trading_symbol") or "").strip()
        if symbol:
            first = symbol.split()[0]
            if first:
                return first.upper()
        return ""

    # ------------------------------------------------------------------ #
    # OR-level computation (matches first_candle_service)                #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _compute_or_levels(fc: dict[str, Any]) -> dict[str, float] | None:
        """Compute OR levels from a first-candle dict, or None."""
        try:
            H = float(fc["high"])
            L = float(fc["low"])
        except (TypeError, ValueError, KeyError):
            return None
        A = (H + L) / 2.0
        D_H = abs(H - A)
        D_L = abs(A - L)
        R1 = A + D_H / 2.0
        S1 = A - D_L / 2.0
        R2 = H + D_H
        S2 = L - D_L
        R3 = R2 + D_H
        S3 = S2 - D_L
        return {
            "H": H, "L": L, "A": A,
            "D_H": D_H, "D_L": D_L,
            "R1": R1, "R2": R2, "R3": R3,
            "S1": S1, "S2": S2, "S3": S3,
            "R3_Threshold": (R2 + R3) / 2.0,
            "S3_Threshold": (S2 + S3) / 2.0,
        }

    # ------------------------------------------------------------------ #
    # Instrument snapshot (own data at touch)                            #
    # ------------------------------------------------------------------ #
    def _collect_instrument_snapshot(
        self,
        candidate: dict[str, Any],
        underlying: str,
        touch_dt: datetime | None,
        allow_rest: bool,
    ) -> dict[str, Any]:
        """Collect the isolated instrument's OWN data at the touch.

        Resolution order for ``instrument_ltp``:
          1. live feed cache   (instant; populated on the live path)
          2. exact touch bar   (backfill path only; ~200–500 ms)
          3. latest intraday   (backfill path only; when touch bar missing)
          4. first_candle.close (weak fallback if 1–3 unavailable)
          5. OR midpoint        (last resort; always present when OR exists)

        Alongside LTP, we also try to read the instrument's own
        ``first_candle.json`` from its folder. If the file is missing
        (e.g. it was one of the 26 skipped during startup backfill),
        we derive both the first-candle OHLC and the OR levels from the
        intraday 09:15 bar — no extra network call.
        """
        snapshot: dict[str, Any] = {
            "instrument_ltp": None,
            "instrument_touch_ohlc": None,
            "instrument_first_candle": None,
            "instrument_or_levels": None,
            "instrument_quote_source": "unavailable",
        }

        key = str(candidate.get("instrument_key") or "").strip()
        if not key:
            return snapshot

        # ---- 1. Live feed cache (fast; used on live path) ---------- #
        try:
            from services.live_feed_service import live_feed_service
            ltp = live_feed_service.get_latest_ltp(key)
            if ltp is not None:
                snapshot["instrument_ltp"] = float(ltp)
                snapshot["instrument_quote_source"] = "live_feed_cache"
        except Exception:
            logger.exception(
                "[level-isolation] live LTP cache read failed for %s", key,
            )

        # ---- 2. Read the instrument's own first_candle.json -------- #
        resolved_underlying = self._resolve_underlying_for_candidate(candidate)
        folder_underlying = (resolved_underlying or underlying or "").strip().lower()
        strike = candidate.get("strike")
        type_upper = str(candidate.get("type") or "").upper()

        if folder_underlying and strike is not None and type_upper:
            try:
                strike_int = int(float(strike))
            except (TypeError, ValueError):
                strike_int = None
            if strike_int is not None:
                folder = (
                    Path(config.LIVE_DATA_DIR)
                    / folder_underlying
                    / f"{strike_int}_{type_upper}"
                )
                fc_path = folder / "first_candle.json"
                if fc_path.exists():
                    try:
                        payload = json.loads(fc_path.read_text(encoding="utf-8"))
                        # Only use the file if it's for today.
                        if str(payload.get("date") or "") == _today_iso():
                            snapshot["instrument_first_candle"] = payload.get("first_candle")
                            snapshot["instrument_or_levels"] = payload.get("or_levels")
                            # Weak fallback for LTP if cache was empty.
                            if snapshot["instrument_ltp"] is None:
                                fc_close = (payload.get("first_candle") or {}).get("close")
                                if fc_close is not None:
                                    try:
                                        snapshot["instrument_ltp"] = float(fc_close)
                                        snapshot["instrument_quote_source"] = "first_candle_close"
                                    except (TypeError, ValueError):
                                        pass
                    except Exception:
                        logger.exception(
                            "[level-isolation] unreadable first_candle.json for %s",
                            key,
                        )
                else:
                    logger.info(
                        "[level-isolation] first_candle.json missing for %s at %s",
                        key, fc_path,
                    )

        # ---- 3. Intraday REST (backfill path only) ----------------- #
        if allow_rest:
            try:
                candles = self._fetch_intraday(key)
            except Exception:
                candles = []

            if candles:
                # 3a. Match exact touch bar (best signal).
                matched: dict[str, Any] | None = None
                if touch_dt is not None:
                    try:
                        target_minute = touch_dt.strftime("%Y-%m-%dT%H:%M")
                    except Exception:
                        target_minute = None
                    if target_minute:
                        for c in candles:
                            if str(c.get("time", "")).startswith(target_minute):
                                matched = c
                                break

                if matched is not None:
                    snapshot["instrument_touch_ohlc"] = {
                        "open": matched.get("open"),
                        "high": matched.get("high"),
                        "low": matched.get("low"),
                        "close": matched.get("close"),
                        "volume": matched.get("volume"),
                        "time_iso": matched.get("time"),
                    }
                    try:
                        snapshot["instrument_ltp"] = float(matched["close"])
                        snapshot["instrument_quote_source"] = "intraday_bar"
                    except (TypeError, ValueError, KeyError):
                        pass
                else:
                    # 3b. Fallback: latest bar for LTP only. We do NOT
                    # populate instrument_touch_ohlc from the wrong bar
                    # (that would silently lie about "the touch price").
                    last = candles[-1]
                    try:
                        snapshot["instrument_ltp"] = float(last["close"])
                        snapshot["instrument_quote_source"] = "intraday_latest_bar"
                    except (TypeError, ValueError, KeyError):
                        pass
                    if touch_dt is not None:
                        logger.warning(
                            "[level-isolation] exact touch bar not found "
                            "for %s at %s — using latest intraday bar for "
                            "LTP only (touch_ohlc left empty)",
                            key, touch_dt,
                        )

                # 3c. Derive first_candle from the 09:15 intraday bar
                # when the on-disk file was missing. This rescues
                # instruments that were skipped during startup backfill.
                if snapshot["instrument_first_candle"] is None:
                    for c in candles:
                        t = str(c.get("time", ""))
                        if "T09:15" in t:
                            fc = {
                                "time_iso": c.get("time"),
                                "open": c.get("open"),
                                "high": c.get("high"),
                                "low": c.get("low"),
                                "close": c.get("close"),
                            }
                            snapshot["instrument_first_candle"] = fc
                            snapshot["instrument_or_levels"] = self._compute_or_levels(fc)
                            logger.info(
                                "[level-isolation] derived first_candle + "
                                "OR levels for %s from intraday 09:15 bar",
                                key,
                            )
                            break

        # ---- 4. Last-resort: OR midpoint (H+L)/2 ------------------- #
        if snapshot["instrument_ltp"] is None:
            orl = snapshot["instrument_or_levels"] or {}
            try:
                a = float(orl.get("A"))
                snapshot["instrument_ltp"] = a
                snapshot["instrument_quote_source"] = "or_midpoint"
            except (TypeError, ValueError):
                pass

        return snapshot

    # ------------------------------------------------------------------ #
    # Isolation                                                          #
    # ------------------------------------------------------------------ #
    def _isolate(
        self,
        candidate: dict[str, Any],
        reason: str,
        event: dict[str, Any],
    ) -> None:
        # Determine the touch moment (used for the instrument snapshot's
        # touch-bar match).
        touch_dt: datetime | None = None
        touched_at_str = event.get("touched_at")
        if touched_at_str:
            try:
                touch_dt = datetime.fromisoformat(str(touched_at_str))
            except Exception:
                touch_dt = None
        if touch_dt is None:
            touch_dt = _now_market()

        # Whether we're on the backfill path (only then do we do an
        # intraday REST call for the picked instrument's OHLC).
        is_backfill = str(event.get("source") or "") == "backfill"

        underlying = str(event.get("index") or "").strip().upper()
        snapshot = self._collect_instrument_snapshot(
            candidate,
            underlying=underlying,
            touch_dt=touch_dt,
            allow_rest=is_backfill,
        )

        isolated = {
            "instrument_key": candidate.get("instrument_key"),
            "trading_symbol": candidate.get("trading_symbol"),
            "strike": candidate.get("strike"),
            "type": candidate.get("type"),
            "expiry": candidate.get("expiry"),
            "levels": candidate.get("levels"),
            "reason": reason,
            "isolated_at": _now_market().isoformat(),
            "trigger_index": event.get("index"),
            "trigger_side": event.get("side"),
            "trigger_level": event.get("trigger_level"),
            "trigger_level_value": event.get("trigger_level_value"),
            "trigger_ltp": event.get("trigger_ltp"),
            "touched_at": event.get("touched_at"),
            "nifty_reference_ltp": event.get("nifty_reference_ltp"),
            "nifty_reference_at": event.get("nifty_reference_at"),
            "nifty_reference_candidates_count": event.get(
                "nifty_reference_candidates_count"
            ),
            # ---- NEW: the isolated instrument's OWN data at touch -- #
            "instrument_ltp": snapshot.get("instrument_ltp"),
            "instrument_touch_ohlc": snapshot.get("instrument_touch_ohlc"),
            "instrument_first_candle": snapshot.get("instrument_first_candle"),
            "instrument_or_levels": snapshot.get("instrument_or_levels"),
            "instrument_quote_source": snapshot.get("instrument_quote_source"),
        }
        self._state["isolated"] = isolated
        event["status"] = "isolated"
        event["isolated_instrument"] = candidate.get("instrument_key")
        event["instrument_ltp"] = snapshot.get("instrument_ltp")
        event["instrument_quote_source"] = snapshot.get("instrument_quote_source")
        self._state["events"].append(event)
        self._save_state()

        logger.info(
            "[level-isolation] ISOLATED key=%s reason=%s trigger=%s %s "
            "idx_ltp=%s inst_ltp=%s src=%s has_fc=%s has_or=%s",
            candidate.get("instrument_key"), reason,
            event.get("index"), event.get("side"),
            event.get("trigger_ltp"),
            snapshot.get("instrument_ltp"),
            snapshot.get("instrument_quote_source"),
            snapshot.get("instrument_first_candle") is not None,
            snapshot.get("instrument_or_levels") is not None,
        )

        if config.LEVEL_ISOLATION_TELEGRAM_ENABLED:
            threading.Thread(
                target=self._send_telegram,
                args=(isolated,),
                daemon=True,
                name="level-isolation-telegram",
            ).start()

    def _send_telegram(self, isolated: dict[str, Any]) -> None:
        try:
            from services.telegram_service import telegram_service
            _DIV = "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
            _THIN = "────────────────────────────────"
            inst_ltp = isolated.get("instrument_ltp")
            inst_ltp_line = (
                f"💵 Inst LTP @touch:{inst_ltp}\n"
                if inst_ltp is not None else ""
            )
            msg = (
                f"{_DIV}\n"
                f"🎯 LEVEL ISOLATION · INSTRUMENT LOCKED\n"
                f"{_DIV}\n"
                f"📌 Instrument:    {isolated.get('trading_symbol') or isolated.get('instrument_key')}\n"
                f"🔑 Key:           {isolated.get('instrument_key')}\n"
                f"📊 Strike/Type:   {isolated.get('strike')} {isolated.get('type')}\n"
                f"📅 Expiry:        {isolated.get('expiry')}\n"
                f"🏷️ Levels:        {', '.join(isolated.get('levels') or [])}\n"
                f"🎯 Trigger:       {isolated.get('reason')}\n"
                f"📈 Trigger LTP:   {isolated.get('trigger_ltp')}\n"
                f"📊 Trigger Level: {isolated.get('trigger_level')} = "
                f"{isolated.get('trigger_level_value')}\n"
                f"{inst_ltp_line}"
                f"🕒 Isolated At:   {isolated.get('isolated_at')}\n"
                f"{_THIN}\n"
                f"💾 Saved to data/{config.LEVEL_ISOLATION_FILENAME}"
            )
            telegram_service.send_message(
                "Level Isolation · Instrument Locked",
                msg,
                level="SUCCESS",
            )
        except Exception:
            logger.exception("[level-isolation] telegram send failed")

    # ------------------------------------------------------------------ #
    # Startup backfill                                                   #
    # ------------------------------------------------------------------ #
    def _snapshot_needs_backfill(self, iso: dict[str, Any]) -> bool:
        """True if the record has an isolated entry but no snapshot."""
        if not isinstance(iso, dict):
            return True
        return (
            iso.get("instrument_ltp") is None
            and iso.get("instrument_touch_ohlc") is None
            and iso.get("instrument_first_candle") is None
            and iso.get("instrument_or_levels") is None
        )

    @staticmethod
    def _parse_touched_at(touched_at: Any) -> datetime | None:
        if not touched_at:
            return None
        try:
            return datetime.fromisoformat(str(touched_at))
        except Exception:
            return None

    def _backfill_snapshot_for_existing(
        self,
        iso: dict[str, Any],
    ) -> dict[str, Any]:
        """Re-collect the snapshot for an already-isolated record.

        Does NOT re-run the isolation decision and does NOT touch
        ``touch_state``. Only the five snapshot fields are updated.
        """
        candidate = {
            "instrument_key": iso.get("instrument_key"),
            "trading_symbol": iso.get("trading_symbol"),
            "strike": iso.get("strike"),
            "type": iso.get("type"),
        }
        underlying = (
            self._resolve_underlying_for_candidate(candidate)
            or str(iso.get("trigger_index") or "").strip().upper()
        )
        touch_dt = self._parse_touched_at(iso.get("touched_at"))
        snapshot = self._collect_instrument_snapshot(
            candidate,
            underlying=underlying,
            touch_dt=touch_dt,
            allow_rest=True,
        )
        iso.update(snapshot)
        self._save_state()
        logger.info(
            "[level-isolation] snapshot backfilled — key=%s ltp=%s "
            "src=%s has_fc=%s has_or=%s has_touch=%s",
            iso.get("instrument_key"),
            snapshot.get("instrument_ltp"),
            snapshot.get("instrument_quote_source"),
            snapshot.get("instrument_first_candle") is not None,
            snapshot.get("instrument_or_levels") is not None,
            snapshot.get("instrument_touch_ohlc") is not None,
        )
        return snapshot

    def refresh_snapshot(self) -> dict[str, Any]:
        """Public entry point — re-collect the snapshot on demand."""
        with self._lock:
            self._ensure_today()
            iso = self._state.get("isolated")
            if not iso:
                return {"status": "no_isolated_record"}
            try:
                self._backfill_snapshot_for_existing(iso)
                return {
                    "status": "ok",
                    "instrument_ltp": iso.get("instrument_ltp"),
                    "instrument_quote_source": iso.get("instrument_quote_source"),
                    "has_first_candle": iso.get("instrument_first_candle") is not None,
                    "has_or_levels": iso.get("instrument_or_levels") is not None,
                    "has_touch_ohlc": iso.get("instrument_touch_ohlc") is not None,
                }
            except Exception as exc:
                logger.exception("[level-isolation] refresh_snapshot failed")
                return {
                    "status": "error",
                    "error": f"{type(exc).__name__}: {exc}",
                }

    def run_backfill(self, reason: str = "startup") -> dict[str, Any]:
        if not config.LEVEL_ISOLATION_ENABLED:
            return {"status": "disabled"}

        now = _now_market()
        if not self._is_trading_day(now):
            return {"status": "skipped", "reason": "non_trading_day"}
        if not self._in_backfill_window(now):
            return {"status": "skipped", "reason": "outside_window"}

        with self._lock:
            self._ensure_today()

            if self._state.get("isolated") is not None:
                iso = self._state["isolated"]
                # Self-heal records created before the snapshot feature
                # existed. Safe: does not re-run isolation, does not
                # touch touch_state, only fills in the five snapshot
                # fields. The 5-minute recheck job drives this.
                if self._snapshot_needs_backfill(iso):
                    logger.info(
                        "[level-isolation] isolated record exists but "
                        "snapshot fields are empty — backfilling snapshot "
                        "(no re-decision)"
                    )
                    try:
                        self._backfill_snapshot_for_existing(iso)
                    except Exception:
                        logger.exception(
                            "[level-isolation] snapshot backfill for "
                            "existing record failed"
                        )
                return {
                    "status": "already_isolated",
                    "isolated": self._state["isolated"],
                }

            or_levels = self._load_or_levels()
            filtered = self._load_filtered_instruments()
            if not or_levels or not filtered:
                return {"status": "skipped", "reason": "missing_inputs"}

            nifty = self._fetch_intraday("NSE_INDEX|Nifty 50")
            sensex = self._fetch_intraday("BSE_INDEX|SENSEX")
            if not nifty and not sensex:
                return {"status": "skipped", "reason": "no_intraday_data"}

            merged = self._merge_by_minute(nifty, sensex)
            if not merged:
                return {"status": "skipped", "reason": "no_merged_data"}

            logger.info(
                "[level-isolation] backfill replay — reason=%s candles=%d",
                reason, len(merged),
            )

            isolated = self._replay(merged, or_levels, filtered)
            self._save_state()

            return {
                "status": "ok" if isolated else "no_touch",
                "reason": reason,
                "candles_replayed": len(merged),
                "isolated": isolated,
            }

    def _fetch_intraday(self, instrument_key: str) -> list[dict[str, Any]]:
        try:
            import upstox_client
            cfg = upstox_client.Configuration()
            api = upstox_client.HistoryV3Api(upstox_client.ApiClient(cfg))
            response = api.get_intra_day_candle_data(
                instrument_key, "minutes", "1",
            )
            body = response
            if hasattr(response, "to_dict"):
                body = response.to_dict()
            elif hasattr(response, "data"):
                body = response.data

            if not isinstance(body, dict):
                return []
            candles = (body.get("data") or {}).get("candles") or []
            if not isinstance(candles, list):
                return []

            out: list[dict[str, Any]] = []
            for raw in candles:
                if not isinstance(raw, list) or len(raw) < 5:
                    continue
                try:
                    out.append({
                        "time": str(raw[0]),
                        "open": float(raw[1]),
                        "high": float(raw[2]),
                        "low": float(raw[3]),
                        "close": float(raw[4]),
                        "volume": float(raw[5]) if len(raw) > 5 else 0.0,
                    })
                except (ValueError, TypeError):
                    continue
            out.sort(key=lambda c: c["time"])
            return out
        except Exception:
            logger.exception(
                "[level-isolation] intraday fetch failed for %s",
                instrument_key,
            )
            return []

    def _merge_by_minute(
        self,
        nifty: list[dict[str, Any]],
        sensex: list[dict[str, Any]],
    ) -> list[tuple[str, dict[str, Any] | None, dict[str, Any] | None]]:
        n_by = {c["time"][:16]: c for c in nifty if c.get("time")}
        s_by = {c["time"][:16]: c for c in sensex if c.get("time")}
        minutes = sorted(set(n_by.keys()) | set(s_by.keys()))
        return [(m, n_by.get(m), s_by.get(m)) for m in minutes]

    def _replay(
        self,
        merged: list[tuple[str, dict[str, Any] | None, dict[str, Any] | None]],
        or_levels: dict[str, dict[str, Any]],
        filtered: dict[str, dict[str, Any]],
    ) -> dict[str, Any] | None:
        for minute, n_bar, s_bar in merged:
            if n_bar is not None:
                self._last_nifty_ltp = n_bar["close"]
                self._last_nifty_tick_at = datetime.fromisoformat(minute).replace(
                    tzinfo=_MARKET_TZ,
                )
            if s_bar is not None:
                self._last_sensex_ltp = s_bar["close"]
                self._last_sensex_tick_at = datetime.fromisoformat(minute).replace(
                    tzinfo=_MARKET_TZ,
                )

            checks = [
                ("NIFTY",  "R3", n_bar),
                ("NIFTY",  "S3", n_bar),
                ("SENSEX", "R3", s_bar),
                ("SENSEX", "S3", s_bar),
            ]

            for index_name, side, bar in checks:
                if bar is None:
                    continue
                state_key = f"{index_name}_{side}"
                if self._state["touch_state"].get(state_key):
                    continue
                level_value = or_levels.get(index_name, {}).get(side)
                if level_value is None:
                    continue
                try:
                    level_f = float(level_value)
                except (TypeError, ValueError):
                    continue

                if side == "R3":
                    touched = float(bar["high"]) >= level_f
                else:
                    touched = float(bar["low"]) <= level_f
                if not touched:
                    continue

                self._state["touch_state"][state_key] = True

                decision_ltp = float(bar["close"])
                event: dict[str, Any] = {
                    "index": index_name,
                    "side": side,
                    "trigger_level": side,
                    "trigger_level_value": level_f,
                    "trigger_ltp": decision_ltp,
                    "touched_at": minute + ":00",
                    "source": "backfill",
                    "candidates_count": 0,
                    "candidates": [],
                }

                candidates = self._find_candidates(index_name, side, filtered)
                event["candidates_count"] = len(candidates)
                event["candidates"] = self._enrich_candidates(
                    candidates, decision_ltp,
                )
                if not candidates:
                    event["status"] = "no_candidates"
                    self._state["events"].append(event)
                    continue

                if index_name == "SENSEX":
                    nifty_ref = self._last_nifty_ltp
                    if nifty_ref is not None:
                        nifty_candidates = self._all_nifty_filtered(filtered)
                        if nifty_candidates:
                            pick = min(
                                nifty_candidates,
                                key=lambda c: abs(self._strike(c) - nifty_ref),
                            )
                            event["nifty_reference_ltp"] = nifty_ref
                            event["nifty_reference_candidates_count"] = len(
                                nifty_candidates
                            )
                            event["nifty_reference_candidates"] = (
                                self._enrich_candidates(
                                    nifty_candidates,
                                    nifty_ref,
                                    distance_field="distance_to_nifty_ltp",
                                )
                            )
                            self._isolate(
                                pick,
                                f"sensex_{side.lower()}_with_nifty_reference",
                                event,
                            )
                            return self._state["isolated"]

                    pick = min(
                        candidates,
                        key=lambda c: abs(self._strike(c) - decision_ltp),
                    )
                    self._isolate(pick, f"sensex_{side.lower()}_fallback", event)
                    return self._state["isolated"]

                pick = min(
                    candidates,
                    key=lambda c: abs(self._strike(c) - decision_ltp),
                )
                self._isolate(pick, f"{index_name.lower()}_{side.lower()}_touch", event)
                return self._state["isolated"]

        return None

    # ------------------------------------------------------------------ #
    # Status / detail / reset                                            #
    # ------------------------------------------------------------------ #
    def last_status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "enabled": config.LEVEL_ISOLATION_ENABLED,
                "day": self._state.get("day"),
                "isolated": self._state.get("isolated"),
                "touch_state": self._state.get("touch_state"),
                "events_count": len(self._state.get("events") or []),
                "events": self._state.get("events") or [],
                "last_update": self._state.get("last_update"),
                "nifty_ltp": self._last_nifty_ltp,
                "sensex_ltp": self._last_sensex_ltp,
                "nifty_tick_at": (
                    self._last_nifty_tick_at.isoformat()
                    if self._last_nifty_tick_at else None
                ),
                "sensex_tick_at": (
                    self._last_sensex_tick_at.isoformat()
                    if self._last_sensex_tick_at else None
                ),
            }

    def _holiday_info(self, now: datetime) -> dict[str, Any]:
        """Return a best-effort holiday classification for ``now``."""
        if now.weekday() not in _VALID_WEEKDAYS:
            return {
                "is_weekend": True,
                "is_holiday": False,
                "description": None,
                "error": None,
            }
        try:
            from services.market_holiday_service import market_holiday_service
            payload = market_holiday_service.get_today()
            status = str(payload.get("status") or "").lower()
            is_holiday = status == "holiday"
            holidays = payload.get("holidays") or []
            desc = None
            if holidays and isinstance(holidays[0], dict):
                desc = holidays[0].get("description")
            return {
                "is_weekend": False,
                "is_holiday": is_holiday,
                "description": desc,
                "error": payload.get("error") if status == "error" else None,
            }
        except Exception as exc:
            return {
                "is_weekend": False,
                "is_holiday": False,
                "description": None,
                "error": f"{type(exc).__name__}: {exc}",
            }

    def _market_context(self, now: datetime) -> dict[str, Any]:
        """Classify the current market moment for the UI.

        The returned ``state`` is one of:
          * ``weekend``         — Saturday / Sunday
          * ``holiday``         — trading holiday per Upstox
          * ``pre_open``        — weekday before 09:15 IST
          * ``or_warming_up``   — 09:15 – 09:16:10 IST
          * ``in_session``      — 09:16:10 – 15:30 IST
          * ``post_close``      — after 15:30 IST
        """
        info = self._holiday_info(now)
        is_weekend = info["is_weekend"]
        is_holiday = info["is_holiday"]
        is_trading = not is_weekend and not is_holiday

        or_available_at = now.replace(
            hour=_OR_READY.hour,
            minute=_OR_READY.minute,
            second=_OR_READY.second,
            microsecond=0,
        )
        or_ready = is_trading and now >= or_available_at
        in_session = (
            is_trading
            and _MARKET_OPEN <= now.time() <= _MARKET_CLOSE
        )

        if is_weekend:
            state = "weekend"
            message = (
                f"Weekend ({now.strftime('%A')}) — market closed. "
                f"Isolation resumes on the next weekday."
            )
        elif is_holiday:
            state = "holiday"
            desc = info["description"] or "trading holiday"
            message = f"Market holiday ({desc}). No isolation today."
        elif now.time() < _MARKET_OPEN:
            state = "pre_open"
            message = (
                "Market opens at 09:15 IST. OR levels will be computed at "
                "09:16:10 IST; isolation will begin watching for R3 / S3 "
                "touches right after."
            )
        elif not or_ready:
            state = "or_warming_up"
            message = (
                "First candle closed at 09:16 — OR levels are being computed. "
                "Isolation will begin watching for R3 / S3 touches in a moment."
            )
        elif not in_session:
            state = "post_close"
            message = (
                "Market closed at 15:30 IST. No new isolation until the "
                "next session."
            )
        else:
            state = "in_session"
            message = "Watching NIFTY / SENSEX for R3 / S3 touches."

        return {
            "is_trading_day": is_trading,
            "is_weekend": is_weekend,
            "is_holiday": is_holiday,
            "holiday_description": info["description"],
            "holiday_error": info.get("error"),
            "in_session": in_session,
            "or_ready": or_ready,
            "now_ist": now.isoformat(),
            "or_available_at": or_available_at.isoformat(),
            "session_open": _MARKET_OPEN.isoformat(),
            "session_close": _MARKET_CLOSE.isoformat(),
            "state": state,
            "message": message,
        }

    def get_detail(self) -> dict[str, Any]:
        """Return a UI-facing snapshot of the level-isolation state."""
        with self._lock:
            now = _now_market()
            market = self._market_context(now)
            or_levels = self._load_or_levels()
            isolated = self._state.get("isolated")
            events = list(self._state.get("events") or [])

            trigger_event: dict[str, Any] | None = None
            if isolated:
                for ev in reversed(events):
                    if ev.get("status") == "isolated":
                        trigger_event = ev
                        break

            return {
                "enabled": config.LEVEL_ISOLATION_ENABLED,
                "day": self._state.get("day") or _today_iso(),
                "market": market,
                "isolated": isolated,
                "trigger_event": trigger_event,
                "candidates": (
                    list(trigger_event.get("candidates") or [])
                    if trigger_event else []
                ),
                "events": events,
                "touch_state": self._state.get("touch_state") or {},
                "or_levels": or_levels,
                "last_update": self._state.get("last_update"),
                "generated_at": _now_iso(),
            }

    def reset(self) -> dict[str, Any]:
        with self._lock:
            self._state = self._empty_state()
            self._last_nifty_ltp = None
            self._last_nifty_tick_at = None
            self._last_sensex_ltp = None
            self._last_sensex_tick_at = None
            self._last_check_monotonic = 0.0
            self._save_state()
            logger.info("[level-isolation] state reset")
            return self.get_detail()


level_isolated_instrument_service = LevelIsolatedInstrumentService()