# services/live_sr_levels_service.py
"""Live pivot-based support / resistance engine.

Receives ticks from ``services/live_feed_service._handle_feed`` for
every instrument the live feed is subscribed to, builds 1-min candles
from the tick stream, and detects strict pivot highs / lows using the
rule from ``SR_LEVELS_SPEC.md``:

    resistance pivot at i  iff  C[i].high > C[j].high
                                for j in [i-left, i-1]
                                and   j in [i+1, i+right]
    support    pivot at i  iff  C[i].low  < C[j].low
                                for j in [i-left, i-1]
                                and   j in [i+1, i+right]

A pivot at index ``i`` is confirmed at bar ``i + right_bars``.
Confirmed pivots are recorded with both ``pivot_time`` and
``confirmed_time`` and written to ``live_sr_levels.json`` next to the
instrument's ``candles.json`` / ``first_candle.json``.

Mid-session backfill
--------------------
``seed_candles(key, entry, candles)`` accepts a list of pre-existing
1-min candles (typically today's intraday series from Upstox). It
clears the instrument's in-memory state, feeds the full series in
chronological order, runs ``_scan_all_pivots`` over the whole buffer,
and persists once at the end. This is how the dashboard has correct
SR levels immediately after a mid-session startup, without waiting for
live ticks to accumulate 31+ candles. Seeding is idempotent.

Scope
-----
When ``LIVE_SR_TRACK_ALL_INSTRUMENTS`` is True (default), every
instrument the live feed subscribes to is tracked. When False, only
instruments explicitly registered by ``level_instrument_filter_service``
are tracked.

Candle source
-------------
* Index feeds carry ``candle_1m`` (interval ``I1``) — used directly.
* Option feeds only carry ``ltpc`` — OHLC is built from LTP ticks
  bucketed to the minute.

Persistence
-----------
The file is written when:
  * the instrument is registered (empty file, or hydrated)
  * a new pivot is confirmed
  * an existing level is broken
  * every ``LIVE_SR_PERSIST_EVERY_N_CANDLES`` finalized candles
  * at the end of a ``seed_candles`` call

The ``_suppress_persist`` flag lets the seed path defer disk writes
until the whole series is processed.

Logging policy (VERBOSE_LOG)
----------------------------
The per-instrument log lines are **gated on ``config.VERBOSE_LOG``**:

  * ``[live-sr] tracking <key> (<symbol>)``  — one line per
    ``register_instrument`` call. Fires 167× on feed start when
    ``LIVE_SR_TRACK_ALL_INSTRUMENTS=true``. INFO noise unless
    debugging → gated on ``VERBOSE_LOG``.
  * ``[live-sr] hydrated <key> from disk: N level(s)`` — one line per
    instrument on mid-day restart. Gated on ``VERBOSE_LOG``.
  * ``[live-sr] seeded <key>: candles=N pivots=M`` — one line per
    instrument on backfill. Gated on ``VERBOSE_LOG``.

The **summary** lines are always logged (single line, high value):

  * ``[live-sr] register_all complete — N instrument(s) tracked``
  * ``[live-sr] in-memory state cleared``
  * every exception / persistence-failure line is always logged.

To see per-instrument detail, set ``VERBOSE_LOG=true`` in ``.env``
and restart. There is no code change required.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from core import config
from core.logger import get_logger

logger = get_logger(__file__)

_MARKET_TZ = ZoneInfo("Asia/Kolkata")


# --------------------------------------------------------------------------- #
# Verbose-log helper                                                          #
# --------------------------------------------------------------------------- #
def _verbose() -> bool:
    """Return the current ``VERBOSE_LOG`` flag.

    Read fresh every call so a runtime toggle (in tests) is honoured.
    Defaults to False when the attribute is missing (older builds).
    """
    return bool(getattr(config, "VERBOSE_LOG", False))


# --------------------------------------------------------------------------- #
# Path helpers                                                                #
# --------------------------------------------------------------------------- #
def _safe_folder(name: Any) -> str:
    text = str(name or "unknown").strip()
    for ch in ("|", "/", "\\", ":", "*", "?", '"', "<", ">", " "):
        text = text.replace(ch, "_")
    return text.strip("._") or "unknown"


def _folder_for_entry(entry: dict[str, Any]) -> Path:
    underlying = str(entry.get("underlying") or "").strip().lower()
    strike = entry.get("strike_price")
    strike_type = str(entry.get("strike_type") or "").upper()
    is_index = bool(entry.get("is_index")) or strike is None

    if is_index:
        idx_name = underlying or _safe_folder(entry.get("instrument_key"))
        return Path(config.LIVE_DATA_DIR) / "index" / idx_name

    if underlying and strike is not None and strike_type:
        try:
            strike_int = int(float(strike))
        except (TypeError, ValueError):
            strike_int = str(strike)
        return (
            Path(config.LIVE_DATA_DIR)
            / underlying
            / f"{strike_int}_{strike_type}"
        )

    return (
        Path(config.LIVE_DATA_DIR)
        / "index"
        / _safe_folder(entry.get("instrument_key"))
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today_iso() -> str:
    return datetime.now(_MARKET_TZ).date().isoformat()


def _to_ms(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        n = float(value)
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(
                str(value).replace("Z", "+00:00")
            )
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp() * 1000)
        except Exception:
            return None
    if n > 1e12:
        return int(n)
    if n > 1e10:
        return int(n)
    return int(n * 1000.0)


def _ms_to_iso(ms: int | None) -> str | None:
    if ms is None:
        return None
    try:
        dt = datetime.fromtimestamp(ms / 1000.0, tz=_MARKET_TZ)
        return dt.isoformat()
    except (OSError, OverflowError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Service                                                                     #
# --------------------------------------------------------------------------- #
class LiveSRLevelsService:
    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._registered: set[str] = set()
        self._states: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------ #
    # Registration                                                       #
    # ------------------------------------------------------------------ #
    def register_instrument(self, entry: dict[str, Any]) -> bool:
        if not config.LIVE_SR_ENABLED:
            return False
        key = str(entry.get("instrument_key") or "").strip()
        if not key:
            return False

        today = _today_iso()
        state: dict[str, Any] | None = None
        needs_write = False

        with self._guard:
            existing = self._states.get(key)
            if existing is not None and existing.get("date") == today:
                self._registered.add(key)
                return True
            state = (
                self._try_hydrate_from_disk(entry, today)
                or self._init_state(entry, today)
            )
            self._states[key] = state
            self._registered.add(key)
            needs_write = True

        if needs_write and state is not None:
            try:
                self._persist(key, state)
            except Exception:
                logger.exception(
                    "[live-sr] initial persist failed for %s", key,
                )
            # Per-instrument log line — gated on VERBOSE_LOG because
            # `register_all` calls this once per instrument (167× on
            # feed start when LIVE_SR_TRACK_ALL_INSTRUMENTS=true).
            # The `register_all complete` summary line below is the
            # operator-facing signal; use that instead.
            if _verbose():
                logger.info(
                    "[live-sr] tracking %s (%s)",
                    key, entry.get("trading_symbol"),
                )
        return True

    def register_all(self, entries: Iterable[dict[str, Any]]) -> int:
        if not config.LIVE_SR_ENABLED:
            return 0
        count = 0
        for entry in entries:
            try:
                if self.register_instrument(entry):
                    count += 1
            except Exception:
                logger.exception(
                    "[live-sr] register_all failed for %s",
                    entry.get("instrument_key") if isinstance(entry, dict) else entry,
                )
        # Summary line — always logged (one line, high signal).
        logger.info(
            "[live-sr] register_all complete — %d instrument(s) tracked",
            len(self._registered),
        )
        return count

    def is_tracking(self, instrument_key: str) -> bool:
        key = str(instrument_key or "").strip()
        if not key:
            return False
        with self._guard:
            return key in self._registered

    def registered_keys(self) -> list[str]:
        with self._guard:
            return sorted(self._registered)

    # ------------------------------------------------------------------ #
    # Seed from pre-existing candles (mid-session backfill)              #
    # ------------------------------------------------------------------ #
    def seed_candles(
        self,
        instrument_key: str,
        entry: dict[str, Any],
        candles: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Bulk-seed the SR engine from a list of 1-min candles.

        Clears the instrument's candles/levels, feeds the series in
        chronological order, runs ``_scan_all_pivots`` over the whole
        buffer, then persists ONCE at the end. Ticks arriving during
        the seed are ignored on the instrument side (live tick path is
        suppressed by ``_suppress_persist``; the state dict is
        overwritten by the seed).

        Idempotent: calling it twice produces the same levels.
        """
        result: dict[str, Any] = {
            "instrument_key": instrument_key,
            "candles_seeded": 0,
            "pivots_detected": 0,
            "total_levels": 0,
            "error": None,
        }
        if not config.LIVE_SR_ENABLED:
            result["error"] = "sr_disabled"
            return result

        key = str(instrument_key or "").strip()
        if not key or not isinstance(candles, list) or not candles:
            result["error"] = "no_candles"
            return result

        self.register_instrument(entry)
        with self._guard:
            state = self._states.get(key)
        if state is None:
            result["error"] = "no_state"
            return result

        internal: dict[int, dict[str, Any]] = {}
        for raw in candles:
            if not isinstance(raw, dict):
                continue
            ts_ms = _to_ms(raw.get("time") or raw.get("ts_ms"))
            if ts_ms is None:
                continue
            try:
                candle = {
                    "time": _ms_to_iso(ts_ms),
                    "ts_ms": ts_ms,
                    "bucket": ts_ms // 60000,
                    "open": float(raw["open"]),
                    "high": float(raw["high"]),
                    "low": float(raw["low"]),
                    "close": float(raw["close"]),
                    "volume": float(raw.get("volume") or 0.0),
                }
            except (KeyError, TypeError, ValueError):
                continue
            internal[candle["bucket"]] = candle

        if not internal:
            result["error"] = "no_valid_candles"
            return result

        ordered = [internal[b] for b in sorted(internal.keys())]

        original_levels = list(state.get("levels") or [])
        original_candles = list(state.get("candles") or [])
        original_last_checked = state.get("last_checked_index", -1)

        state["_suppress_persist"] = True
        try:
            state["candles"] = ordered
            state["levels"] = []
            state["forming"] = None
            state["last_checked_index"] = -1

            self._scan_all_pivots(state)
        except Exception as exc:
            logger.exception(
                "[live-sr] seed scan failed for %s; rolling back", key,
            )
            state["levels"] = original_levels
            state["candles"] = original_candles
            state["last_checked_index"] = original_last_checked
            result["error"] = f"{type(exc).__name__}: {exc}"
            return result
        finally:
            state["_suppress_persist"] = False
            state["candles_since_persist"] = 0

        try:
            self._persist(key, state)
        except Exception:
            logger.exception("[live-sr] seed persist failed for %s", key)

        result["candles_seeded"] = len(ordered)
        result["pivots_detected"] = len(state["levels"])
        result["total_levels"] = len(state["levels"])
        # Per-instrument log line — gated on VERBOSE_LOG because
        # `sr_backfill_service.run()` seeds every instrument in the
        # registry (167× mid-session). The backfill service already
        # emits a COMPLETE summary line; this per-instrument line is
        # only useful when debugging a specific instrument.
        if _verbose():
            logger.info(
                "[live-sr] seeded %s: candles=%d pivots=%d",
                key, len(ordered), len(state["levels"]),
            )
        return result

    def _scan_all_pivots(self, state: dict[str, Any]) -> bool:
        """Scan every possible pivot index in the current candle buffer.

        Used by ``seed_candles`` — the normal tick path only checks the
        latest pivotable index. Here we walk every candidate so the
        full day's pivots are (re)detected after a seed.
        """
        candles = state["candles"]
        left = int(state["left_bars"])
        right = int(state["right_bars"])
        n = len(candles)
        if n < left + right + 1:
            state["last_checked_index"] = n - right - 1
            return False

        changed = False
        for candidate_index in range(left, n - right):
            c = candles[candidate_index]
            left_ok_res = all(
                candles[j]["high"] < c["high"]
                for j in range(candidate_index - left, candidate_index)
            )
            right_ok_res = all(
                candles[j]["high"] < c["high"]
                for j in range(candidate_index + 1, candidate_index + right + 1)
            )
            left_ok_sup = all(
                candles[j]["low"] > c["low"]
                for j in range(candidate_index - left, candidate_index)
            )
            right_ok_sup = all(
                candles[j]["low"] > c["low"]
                for j in range(candidate_index + 1, candidate_index + right + 1)
            )
            if left_ok_res and right_ok_res:
                if self._append_level(
                    state, "resistance", c, candidate_index, left, right,
                ):
                    changed = True
            if left_ok_sup and right_ok_sup:
                if self._append_level(
                    state, "support", c, candidate_index, left, right,
                ):
                    changed = True

        state["last_checked_index"] = n - right - 1
        return changed

    # ------------------------------------------------------------------ #
    # Tick ingestion                                                     #
    # ------------------------------------------------------------------ #
    def on_tick(
        self,
        instrument_key: str,
        entry: dict[str, Any],
        tick: dict[str, Any],
    ) -> None:
        if not config.LIVE_SR_ENABLED:
            return
        key = str(instrument_key or "").strip()
        if not key or not isinstance(tick, dict):
            return

        with self._guard:
            state = self._states.get(key)
            tracked = key in self._registered

        if not tracked:
            if not config.LIVE_SR_TRACK_ALL_INSTRUMENTS:
                return
            try:
                self.register_instrument(entry)
            except Exception:
                logger.exception(
                    "[live-sr] auto-register failed for %s", key,
                )
                return
            with self._guard:
                state = self._states.get(key)

        if state is None:
            return
        if state.get("_suppress_persist"):
            # A seed is in progress on this instrument — ignore ticks
            # until the seed clears the flag.
            return

        try:
            candle_1m = tick.get("candle_1m")
            if isinstance(candle_1m, dict) and candle_1m.get("open") is not None:
                self._ingest_feed_candle(key, state, candle_1m, tick)
            else:
                self._ingest_ltp_tick(key, state, tick)
        except Exception:
            logger.exception("[live-sr] on_tick failed for %s", key)

    def _ingest_feed_candle(
        self,
        key: str,
        state: dict[str, Any],
        candle_1m: dict[str, Any],
        tick: dict[str, Any],
    ) -> None:
        ts_ms = _to_ms(candle_1m.get("ts"))
        if ts_ms is None:
            ts_ms = _to_ms(tick.get("ltt")) or int(
                datetime.now(timezone.utc).timestamp() * 1000
            )
        candle = {
            "bucket": ts_ms // 60000,
            "ts_ms": ts_ms,
            "open": float(candle_1m["open"]),
            "high": float(candle_1m["high"]),
            "low": float(candle_1m["low"]),
            "close": float(candle_1m["close"]),
            "volume": float(tick.get("volume") or 0.0),
        }
        self._advance_candle(key, state, candle)

    def _ingest_ltp_tick(
        self,
        key: str,
        state: dict[str, Any],
        tick: dict[str, Any],
    ) -> None:
        ltp = tick.get("ltp")
        if ltp is None:
            return
        try:
            ltp_f = float(ltp)
        except (TypeError, ValueError):
            return
        volume = float(tick.get("volume") or 0.0)
        ts_ms = _to_ms(tick.get("ltt")) or int(
            datetime.now(timezone.utc).timestamp() * 1000
        )
        bucket = ts_ms // 60000

        forming = state.get("forming")
        if forming is None:
            state["forming"] = {
                "bucket": bucket, "ts_ms": ts_ms,
                "open": ltp_f, "high": ltp_f, "low": ltp_f, "close": ltp_f,
                "volume": volume,
            }
            return

        if bucket > forming["bucket"]:
            self._finalize_forming(key, state)
            state["forming"] = {
                "bucket": bucket, "ts_ms": ts_ms,
                "open": ltp_f, "high": ltp_f, "low": ltp_f, "close": ltp_f,
                "volume": volume,
            }
        elif bucket == forming["bucket"]:
            forming["high"] = max(forming["high"], ltp_f)
            forming["low"] = min(forming["low"], ltp_f)
            forming["close"] = ltp_f
            forming["ts_ms"] = ts_ms
            if volume > forming["volume"]:
                forming["volume"] = volume

    def _advance_candle(
        self,
        key: str,
        state: dict[str, Any],
        candle: dict[str, Any],
    ) -> None:
        forming = state.get("forming")
        if forming is None:
            state["forming"] = candle
            return
        if candle["bucket"] > forming["bucket"]:
            self._finalize_forming(key, state)
            state["forming"] = candle
        elif candle["bucket"] == forming["bucket"]:
            state["forming"] = candle

    # ------------------------------------------------------------------ #
    # Finalization + pivot detection                                     #
    # ------------------------------------------------------------------ #
    def _finalize_forming(self, key: str, state: dict[str, Any]) -> None:
        forming = state.get("forming")
        if forming is None:
            return
        candle = {
            "time": _ms_to_iso(forming["ts_ms"]),
            "ts_ms": forming["ts_ms"],
            "bucket": forming["bucket"],
            "open": forming["open"],
            "high": forming["high"],
            "low": forming["low"],
            "close": forming["close"],
            "volume": forming["volume"],
        }
        candles = state["candles"]
        if candles and candles[-1]["bucket"] == candle["bucket"]:
            candles[-1] = candle
        else:
            candles.append(candle)

        max_candles = int(config.LIVE_SR_MAX_CANDLES)
        if len(candles) > max_candles:
            del candles[: len(candles) - max_candles]
        state["forming"] = None

        changed = False
        try:
            if self._update_breaks(state, candle):
                changed = True
            if self._detect_pivot(state):
                changed = True
        finally:
            state["candles_since_persist"] = (
                int(state.get("candles_since_persist") or 0) + 1
            )
            if not state.get("_suppress_persist"):
                if changed or self._should_persist_periodic(state):
                    try:
                        self._persist(key, state)
                    finally:
                        state["candles_since_persist"] = 0

    def _should_persist_periodic(self, state: dict[str, Any]) -> bool:
        n = int(config.LIVE_SR_PERSIST_EVERY_N_CANDLES)
        if n <= 0:
            return False
        return int(state.get("candles_since_persist") or 0) >= n

    def _detect_pivot(self, state: dict[str, Any]) -> bool:
        candles = state["candles"]
        left = int(state["left_bars"])
        right = int(state["right_bars"])
        if len(candles) < left + right + 1:
            return False
        candidate_index = len(candles) - right - 1
        if candidate_index < left:
            return False
        if candidate_index <= state["last_checked_index"]:
            return False
        state["last_checked_index"] = candidate_index

        c = candles[candidate_index]
        left_ok_res = all(
            candles[j]["high"] < c["high"]
            for j in range(candidate_index - left, candidate_index)
        )
        right_ok_res = all(
            candles[j]["high"] < c["high"]
            for j in range(candidate_index + 1, candidate_index + right + 1)
        )
        left_ok_sup = all(
            candles[j]["low"] > c["low"]
            for j in range(candidate_index - left, candidate_index)
        )
        right_ok_sup = all(
            candles[j]["low"] > c["low"]
            for j in range(candidate_index + 1, candidate_index + right + 1)
        )

        changed = False
        if left_ok_res and right_ok_res:
            if self._append_level(
                state, "resistance", c, candidate_index, left, right,
            ):
                changed = True
        if left_ok_sup and right_ok_sup:
            if self._append_level(
                state, "support", c, candidate_index, left, right,
            ):
                changed = True
        return changed

    def _append_level(
        self,
        state: dict[str, Any],
        level_type: str,
        candle: dict[str, Any],
        pivot_index: int,
        left: int,
        right: int,
    ) -> bool:
        confirmed_index = pivot_index + right
        if confirmed_index >= len(state["candles"]):
            return False
        confirmed_candle = state["candles"][confirmed_index]

        price = float(
            candle["high"] if level_type == "resistance" else candle["low"]
        )
        for existing in state["levels"]:
            if (
                existing["level"] == level_type
                and abs(existing["price"] - price) < 1e-6
                and existing["pivot_bar_index"] == pivot_index
            ):
                return False

        state["levels"].append({
            "level": level_type,
            "price": price,
            "pivot_bar_index": pivot_index,
            "pivot_time": candle["time"],
            "confirmed_bar_index": confirmed_index,
            "confirmed_time": confirmed_candle["time"],
            "left_bars": left,
            "right_bars": right,
            "broken": False,
            "broken_at": None,
            "broken_price": None,
            "broken_bar_index": None,
        })
        return True

    def _update_breaks(
        self,
        state: dict[str, Any],
        candle: dict[str, Any],
    ) -> bool:
        close = float(candle["close"])
        bar_index = len(state["candles"]) - 1
        changed = False
        for level in state["levels"]:
            if level["broken"]:
                continue
            if level["level"] == "resistance" and close > level["price"]:
                level["broken"] = True
                level["broken_at"] = candle["time"]
                level["broken_price"] = close
                level["broken_bar_index"] = bar_index
                changed = True
            elif level["level"] == "support" and close < level["price"]:
                level["broken"] = True
                level["broken_at"] = candle["time"]
                level["broken_price"] = close
                level["broken_bar_index"] = bar_index
                changed = True
        return changed

    # ------------------------------------------------------------------ #
    # Persistence                                                        #
    # ------------------------------------------------------------------ #
    def _persist(self, key: str, state: dict[str, Any]) -> None:
        entry = state.get("entry") or {}
        try:
            folder = _folder_for_entry(entry)
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / config.LIVE_SR_FILENAME
        except Exception:
            logger.exception("[live-sr] path resolution failed for %s", key)
            return

        payload = {
            "instrument_key": key,
            "underlying": entry.get("underlying"),
            "strike_price": entry.get("strike_price"),
            "strike_type": entry.get("strike_type"),
            "trading_symbol": entry.get("trading_symbol"),
            "date": state.get("date"),
            "left_bars": state.get("left_bars"),
            "right_bars": state.get("right_bars"),
            "candle_count": len(state.get("candles") or []),
            "levels": list(state.get("levels") or []),
            "last_candle": (state["candles"][-1] if state.get("candles") else None),
            "updated_at": _now_iso(),
        }
        try:
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(payload, indent=2, default=str),
                encoding="utf-8",
            )
            tmp.replace(path)
        except Exception:
            logger.exception("[live-sr] persist failed for %s", key)

    # ------------------------------------------------------------------ #
    # State hydration / init / read                                      #
    # ------------------------------------------------------------------ #
    def _try_hydrate_from_disk(
        self,
        entry: dict[str, Any],
        today: str,
    ) -> dict[str, Any] | None:
        try:
            folder = _folder_for_entry(entry)
            path = folder / config.LIVE_SR_FILENAME
        except Exception:
            return None
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("[live-sr] unreadable hydrate file: %s", path)
            return None
        if str(payload.get("date") or "") != today:
            return None

        state = self._init_state(entry, today)
        state["levels"] = list(payload.get("levels") or [])
        # Per-instrument log line — gated on VERBOSE_LOG. Fires once
        # per instrument on a mid-day restart. The summary line from
        # `register_all complete` covers the operator-facing signal.
        if _verbose():
            logger.info(
                "[live-sr] hydrated %s from disk: %d level(s)",
                entry.get("instrument_key"),
                len(state["levels"]),
            )
        return state

    def _init_state(self, entry: dict[str, Any], date_str: str) -> dict[str, Any]:
        return {
            "entry": dict(entry),
            "date": date_str,
            "candles": [],
            "forming": None,
            "levels": [],
            "last_checked_index": -1,
            "candles_since_persist": 0,
            "left_bars": int(config.LIVE_SR_LEFT_BARS),
            "right_bars": int(config.LIVE_SR_RIGHT_BARS),
            "_suppress_persist": False,
        }

    def get_levels(self, instrument_key: str) -> dict[str, Any] | None:
        key = str(instrument_key or "").strip()
        with self._guard:
            state = self._states.get(key)
        if state is None:
            return None
        entry = state.get("entry") or {}
        folder = _folder_for_entry(entry)
        path = folder / config.LIVE_SR_FILENAME
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("[live-sr] read failed for %s", key)
            return None

    def reset_state(self) -> None:
        with self._guard:
            self._states.clear()
            self._registered.clear()
        # Always logged — rare event (daily refresh), single line.
        logger.info("[live-sr] in-memory state cleared")

    def status(self) -> dict[str, Any]:
        with self._guard:
            tracking = len(self._states)
            candle_total = sum(
                len(s.get("candles") or []) for s in self._states.values()
            )
            level_total = sum(
                len(s.get("levels") or []) for s in self._states.values()
            )
            per_instrument = {
                k: {
                    "date": s.get("date"),
                    "candles": len(s.get("candles") or []),
                    "levels": len(s.get("levels") or []),
                }
                for k, s in list(self._states.items())[:100]
            }
        return {
            "enabled": bool(config.LIVE_SR_ENABLED),
            "track_all": bool(config.LIVE_SR_TRACK_ALL_INSTRUMENTS),
            "left_bars": int(config.LIVE_SR_LEFT_BARS),
            "right_bars": int(config.LIVE_SR_RIGHT_BARS),
            "max_candles": int(config.LIVE_SR_MAX_CANDLES),
            "persist_every_n_candles": int(config.LIVE_SR_PERSIST_EVERY_N_CANDLES),
            "registered": len(self._registered),
            "tracking": tracking,
            "candles_total": candle_total,
            "levels_total": level_total,
            "instruments_sample": per_instrument,
            "instruments_sample_size": len(per_instrument),
            "instruments_total": tracking,
            "generated_at": _now_iso(),
        }


live_sr_levels_service = LiveSRLevelsService()