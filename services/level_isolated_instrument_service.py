# services/level_isolated_instrument_service.py
"""Level-isolation engine — G1 v4.0 cross-index with NIFTY routing.

Key changes vs G1 v3.2:
    * Priority order flipped to MIDCPNIFTY > BANKNIFTY > NIFTY > SENSEX
      (cross-index lead-lag: mid-cap and high-beta lead the broader
      market, so their R3/S3 touches carry the highest arbitration
      priority).
    * The `INDEX_METADATA` dict preserves this order so first-touch
      iteration and the returned `monitored_indices` list both reflect
      the v4.0 arbitration sequence.
    * All other behaviour (NIFTY-only strike routing, 15/3 fast pivot
      scan, auto-reverse on bias-side rejection, once-per-day lock)
      is unchanged from v3.2.

G1 v4.1 additions (2026-10-29)
------------------------------
    * `_process_winning_touch()` now collects **both** the bias side AND
      the counter side (PE for an R3 touch, CE for an S3 touch) as
      candidates when `STRATEGY_ENTRY_ALLOW_CROSSOVER=true`. Each
      candidate is tagged `side="bias"` or `side="counter"` so the UI
      can show the full 4-candidate set rather than the bias-side-only 2.
      The **pick** still comes from bias-side candidates only — the
      arbitration behaviour is unchanged.

    * `_isolate()` now registers the counter-side option with
      `live_sr_levels_service` **and** seeds it with today's intraday
      candles (`seed_candles`) before returning. This closes the
      long-standing "entry price = live LTP instead of 09:23 candle
      close" bug that appeared in simulate mode: previously
      `strategy_entry_signal_service._candle_close_at()` could not find
      the counter-side candle state (because the PE was never registered
      with the SR engine) and fell back to `live_ltp`. Same fix also
      removes the SR tracker's "Before: 0 / After: 29" bug — the PE's
      pivots are now present in the SR engine when `on_isolation()`
      runs for the promotion, so `_snapshot_current_levels()` returns
      the full pre-isolation set instead of `[]`.

    * `run_backfill()` now verifies each index's `first_candle.json`
      H/L against the actual 09:15 bar returned by the intraday API
      (`_verify_or_against_intraday`). Any mismatch forces a fresh
      `first_candle_service.run(backfill=True)` before the replay so a
      stale OR cannot produce a phantom touch. This is the fix for the
      scenario where a `first_candle.json` from a previous day survives
      a restart with today's date but yesterday's H/L.

    * A feed subscription is also requested for the counter-side
      instrument (best-effort) so that, on a live day, the counter-side
      SR state continues to accumulate ticks and does not go stale
      before the promotion runs.

G1 v4.2 additions (2026-10-29)
------------------------------
    * `trigger_is_self_nifty` and `execution_path` are now persisted on
      every isolation record (and on the winning-touch event). This lets
      downstream services (dummy-order, promotion, UI) branch on
      "was the trigger NIFTY itself, or an external index?" without
      re-deriving the answer from `trigger_index`. `execution_path`
      is one of:
        - "A_self_trigger"     — the trigger index was NIFTY
        - "B_external_trigger" — the trigger index was MIDCPNIFTY /
                                 BANKNIFTY / SENSEX (routed to NIFTY)

    * New `get_execution_path()` helper returns the current isolation's
      execution path, or ``""`` when no isolation exists yet — so
      callers can distinguish "not isolated yet" from either of the two
      paths.

    * `get_detail()` now exposes both fields at the top level for the
      UI / dummy-order service.

G1 v4.1 flag-wiring fix (2026-10-29)
------------------------------------
Three v4.1 kill switches were defined in ``core/config.py`` but never
read by this service, so setting them to ``false`` in ``.env`` had no
effect. All three are now wired:

    * ``STRATEGY_ENTRY_REGISTER_COUNTER_SIDE`` (§5.A)
        ``_isolate()`` now gates the counter-side candidate lookup,
        SR registration, and inclusion in ``event["candidates"]`` on
        this flag (in addition to ``STRATEGY_ENTRY_ALLOW_CROSSOVER``).
        When either is false, only the bias side is registered /
        surfaced — reproducing pre-v4.1 behaviour for bisecting the
        counter-side entry-price bug.

    * ``LEVEL_ISOLATION_REPLAY_USE_SERIES_OR`` (§5.D — replay)
        ``run_backfill()`` now recomputes OR levels from the **same
        intraday series** the replay iterates (via
        ``first_candle_service.compute_for_series``) when this flag is
        true. When false, the on-disk ``first_candle.json`` OR levels
        are used unchanged (pre-v4.1 behaviour). The on-disk file is
        still verified/healed by the separate
        ``_verify_or_against_intraday`` step so the two mechanisms
        remain complementary: the file gets corrected for future
        runs, while the current replay uses series-derived OR.

    * ``LEVEL_ISOLATION_OR_SELF_HEAL_VERIFY_HL`` (§5.D — self-heal)
        ``_maybe_self_heal()`` now additionally compares the cached
        ``first_candle.json`` H/L against today's real 09:15 intraday
        bar (via ``live_sr_calc_service.get_first_intraday_candle``)
        when this flag is true. A mismatch triggers a **targeted**
        recompute via ``first_candle_service.recompute_for_underlying``
        (cheap — 1-2 instruments) rather than the full ~100-instrument
        sweep that ``first_candle_service.run()`` performs. Structural
        issues (missing file, stale date, unreadable) still trigger
        the full sweep because those cannot be resolved per-underlying.
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

# --------------------------------------------------------------------------- #
# Monitored index metadata (G1 v4.0 — MIDCPNIFTY leads)
# --------------------------------------------------------------------------- #
INDEX_METADATA: dict[str, dict[str, Any]] = {
    "NSE_INDEX|NIFTY MID SELECT": {
        "canonical": "MIDCPNIFTY",
        "folder": "midcpnifty",
        "priority": int(config.STRATEGY_INDEX_PRIORITY.get("MIDCPNIFTY", 1)),
    },
    "NSE_INDEX|Nifty Bank": {
        "canonical": "BANKNIFTY",
        "folder": "banknifty",
        "priority": int(config.STRATEGY_INDEX_PRIORITY.get("BANKNIFTY", 2)),
    },
    "NSE_INDEX|Nifty 50": {
        "canonical": "NIFTY",
        "folder": "nifty",
        "priority": int(config.STRATEGY_INDEX_PRIORITY.get("NIFTY", 3)),
    },
    "BSE_INDEX|SENSEX": {
        "canonical": "SENSEX",
        "folder": "sensex",
        "priority": int(config.STRATEGY_INDEX_PRIORITY.get("SENSEX", 4)),
    },
}

# Lookup maps
KEY_TO_CANONICAL: dict[str, str] = {
    k: v["canonical"] for k, v in INDEX_METADATA.items()
}
CANONICAL_TO_KEY: dict[str, str] = {
    v["canonical"]: k for k, v in INDEX_METADATA.items()
}

# Backward-compat alias used by live_feed_service
CORE_INDEX_KEYS: dict[str, str] = KEY_TO_CANONICAL

_R3_TRIGGER_LEVELS = ("R3", "R3_Threshold")
_S3_TRIGGER_LEVELS = ("S3", "S3_Threshold")

_MARKET_OPEN = dtime(9, 15)
_MARKET_CLOSE = dtime(15, 30)
_OR_READY = dtime(9, 17, 10)
_VALID_WEEKDAYS = {0, 1, 2, 3, 4}

# Tolerance for comparing stored first-candle H/L against the intraday
# 09:15 bar (defensive self-heal check). Upstox reports the same values
# for both paths so any deviation beyond a small rounding slack means
# the OR file is stale.
_OR_MISMATCH_TOLERANCE = 0.5


# --------------------------------------------------------------------------- #
# Small helpers                                                               #
# --------------------------------------------------------------------------- #
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
        self._last_ltp: dict[str, float] = {}
        self._last_tick_at: dict[str, datetime] = {}
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
                "MIDCPNIFTY_R3": False, "MIDCPNIFTY_S3": False,
                "BANKNIFTY_R3": False, "BANKNIFTY_S3": False,
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
        for k, v in self._state["touch_state"].items():
            self._state["touch_state"][k] = bool(v)

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
            self._last_ltp.clear()
            self._last_tick_at.clear()
            self._last_check_monotonic = 0.0
            self._load_or_create_state()

    # ------------------------------------------------------------------ #
    # Isolated-SR tracker attachment                                     #
    # ------------------------------------------------------------------ #
    def _ensure_tracker_attached(self) -> None:
        iso = self._state.get("isolated")
        if not isinstance(iso, dict):
            return

        tracker_target: dict[str, Any] = iso
        try:
            from services.isolated_instrument_promotion_service import (
                isolated_instrument_promotion_service,
            )
            active = isolated_instrument_promotion_service.read_active()
            if (
                isinstance(active, dict)
                and isinstance(active.get("record"), dict)
            ):
                promoted_record = active["record"]
                promoted_key = str(
                    promoted_record.get("instrument_key") or ""
                ).strip()
                if promoted_key:
                    tracker_target = promoted_record
        except Exception:
            logger.exception(
                "[level-isolation] promotion lookup failed during "
                "tracker attach; falling back to official record"
            )

        key = str(tracker_target.get("instrument_key") or "").strip()
        if not key:
            return
        try:
            from services.isolated_sr_tracker_service import (
                isolated_sr_tracker_service,
            )
            if not isolated_sr_tracker_service.is_tracking(key):
                isolated_sr_tracker_service.on_isolation(tracker_target)
        except Exception:
            logger.exception(
                "[level-isolation] SR tracker attach failed"
            )

    # ------------------------------------------------------------------ #
    # Self-heal (filesystem-level: missing / stale-date / H/L mismatch)  #
    # ------------------------------------------------------------------ #
    def _maybe_self_heal(self, reason: str = "startup") -> None:
        """Detect stale / missing OR files and fix them.

        Structural issues (missing file, unreadable JSON, stale date)
        always trigger the full ``first_candle_service.run()`` sweep
        because they cannot be resolved on a per-underlying basis.

        H/L mismatches against the real 09:15 intraday bar are
        additionally checked when ``LEVEL_ISOLATION_OR_SELF_HEAL_VERIFY_HL``
        is true (default). For those, the targeted
        ``first_candle_service.recompute_for_underlying()`` is used
        (cheap — 1-2 instruments) unless structural issues are also
        present, in which case the full sweep handles both cases.

        Never raises. Every failure path logs and continues — a failure
        to self-heal must not break the caller (``run_backfill``), which
        will still proceed with the on-disk OR files.
        """
        today = _today_iso()
        needs_heal: list[str] = []

        # ---- 1. Structural checks (always on) ------------------------ #
        for underlying in config.LEVEL_FILTER_INDEXES:
            u = str(underlying or "").strip().upper()
            if not u:
                continue
            idx_lower = u.lower()
            for filename in ("first_candle.json", "level_instruments.json"):
                path = (
                    Path(config.LIVE_DATA_DIR)
                    / "index" / idx_lower / filename
                )
                if not path.exists():
                    needs_heal.append(f"{u}/{filename} (missing)")
                    continue
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                except Exception:
                    needs_heal.append(f"{u}/{filename} (unreadable)")
                    continue
                if str(payload.get("date") or "") != today:
                    needs_heal.append(
                        f"{u}/{filename} (stale: "
                        f"{payload.get('date')!r})"
                    )

        # ---- 2. H/L verification (gated by flag) --------------------- #
        # §5.D (2/2): when this flag is off, only structural issues
        # trigger self-heal. When on, H/L mismatches against the real
        # 09:15 intraday bar are also detected and self-healed.
        hl_mismatch_indices: list[str] = []
        if bool(getattr(config, "LEVEL_ISOLATION_OR_SELF_HEAL_VERIFY_HL", True)):
            try:
                hl_mismatch_indices = (
                    self._verify_or_hl_against_intraday_for_selfheal()
                )
            except Exception:
                logger.exception(
                    "[level-isolation] self-heal H/L verification failed"
                )
                hl_mismatch_indices = []
            for canonical in hl_mismatch_indices:
                needs_heal.append(
                    f"{canonical}/first_candle.json "
                    f"(H/L mismatch vs intraday)"
                )

        if not needs_heal:
            return

        logger.warning(
            "[level-isolation] self-heal triggered (reason=%s): %s",
            reason, needs_heal,
        )

        # ---- 3. Choose targeted vs full-sweep recompute -------------- #
        has_structural_issues = any(
            "(missing)" in item
            or "(unreadable)" in item
            or "(stale:" in item
            for item in needs_heal
        )

        try:
            from services.first_candle_service import first_candle_service

            if hl_mismatch_indices and not has_structural_issues:
                # Cheap path: only H/L mismatches — recompute the
                # affected underlyings only.
                for canonical in hl_mismatch_indices:
                    try:
                        summary = first_candle_service.recompute_for_underlying(
                            canonical.lower(),
                            reason=f"level_isolation_or_hl:{reason}",
                            backfill=True,
                        )
                        logger.info(
                            "[level-isolation] targeted OR recompute for "
                            "%s — total=%s computed=%s errors=%s",
                            canonical,
                            summary.get("total"),
                            summary.get("computed"),
                            summary.get("errors"),
                        )
                    except Exception:
                        logger.exception(
                            "[level-isolation] recompute_for_underlying "
                            "failed for %s", canonical,
                        )
            else:
                # Full sweep: structural issues present (or the H/L
                # path produced no targeted candidates but structural
                # issues exist).
                summary = first_candle_service.run(
                    reason=f"level_isolation_self_heal:{reason}",
                    backfill=True,
                )
                logger.info(
                    "[level-isolation] self-heal complete — total=%s "
                    "computed=%s skipped=%s errors=%s elapsed=%ss",
                    summary.get("total"),
                    summary.get("computed"),
                    summary.get("instrument_skipped"),
                    summary.get("errors"),
                    summary.get("elapsed_seconds"),
                )
        except Exception:
            logger.exception(
                "[level-isolation] self-heal first_candle failed"
            )

    def _verify_or_hl_against_intraday_for_selfheal(
        self,
    ) -> list[str]:
        """G1 v4.1 §5.D — verify each index's cached
        ``first_candle.json`` H/L against today's real 09:15 bar.

        Uses ``live_sr_calc_service.get_first_intraday_candle(key)``
        which is a fresh per-index API fetch. That is why this check is
        gated by ``LEVEL_ISOLATION_OR_SELF_HEAL_VERIFY_HL`` and why it
        is only called from ``_maybe_self_heal`` (startup / 5-minute
        recheck) — never from the per-tick path.

        Returns the list of canonical index names whose stored H/L does
        not match the intraday bar within ``_OR_MISMATCH_TOLERANCE``.
        An empty list means either every OR file matches today's real
        first bar, or no verification could be performed (missing file,
        API error, no 09:15 bar in the intraday response) — the caller
        treats those cases as "nothing to self-heal from this check"
        because the structural checks already handle the missing-file
        case and the intraday data may simply not be ready yet.

        Never raises. All failure modes are silently skipped.
        """
        mismatches: list[str] = []

        try:
            from services.live_sr_calc_service import live_sr_calc_service
        except Exception:
            logger.exception(
                "[level-isolation] self-heal H/L verify: "
                "live_sr_calc_service import failed"
            )
            return mismatches

        for key, meta in INDEX_METADATA.items():
            canonical = str(meta.get("canonical") or "").strip()
            if not canonical:
                continue

            fc_path = (
                Path(config.LIVE_DATA_DIR) / "index"
                / canonical.lower() / "first_candle.json"
            )
            if not fc_path.exists():
                # Structural check handles missing-file case
                continue

            try:
                payload = json.loads(fc_path.read_text(encoding="utf-8"))
            except Exception:
                # Structural check handles unreadable case
                continue

            stored_fc = payload.get("first_candle") or {}
            try:
                stored_high = float(stored_fc.get("high"))
                stored_low = float(stored_fc.get("low"))
            except (TypeError, ValueError):
                continue

            try:
                first_bar = live_sr_calc_service.get_first_intraday_candle(key)
            except Exception:
                logger.exception(
                    "[level-isolation] get_first_intraday_candle failed "
                    "for %s", key,
                )
                continue
            if not isinstance(first_bar, dict):
                continue

            try:
                intraday_high = float(first_bar.get("high"))
                intraday_low = float(first_bar.get("low"))
            except (TypeError, ValueError):
                continue

            if (
                abs(stored_high - intraday_high) > _OR_MISMATCH_TOLERANCE
                or abs(stored_low - intraday_low) > _OR_MISMATCH_TOLERANCE
            ):
                logger.warning(
                    "[level-isolation] self-heal H/L mismatch for %s: "
                    "stored H/L=%.2f/%.2f vs intraday H/L=%.2f/%.2f "
                    "(tol=%.2f)",
                    canonical,
                    stored_high, stored_low,
                    intraday_high, intraday_low,
                    _OR_MISMATCH_TOLERANCE,
                )
                mismatches.append(canonical)

        return mismatches

    # ------------------------------------------------------------------ #
    # NEW: OR-vs-intraday content verification (post-intraday-fetch)     #
    # ------------------------------------------------------------------ #
    def _verify_or_against_intraday(
        self,
        intraday: dict[str, list[dict[str, Any]]],
    ) -> list[str]:
        """Verify each index's ``first_candle.json`` H/L matches the real
        09:15 bar from the intraday API.

        Returns the list of canonical index names (e.g. ``["MIDCPNIFTY"]``)
        whose OR needs to be recomputed. An empty list means every OR
        file on disk is consistent with today's intraday data.

        This check catches the case that ``_maybe_self_heal`` cannot: a
        ``first_candle.json`` file that has today's ``date`` field (so
        the date check passes) but whose H/L came from a different day
        — e.g. a file written by an earlier service run that was never
        overwritten after the backup was re-extracted.

        This is the "late detection" counterpart to
        ``_verify_or_hl_against_intraday_for_selfheal``. It runs
        unconditionally (not gated by any flag) because it consumes the
        intraday candle dict the replay has already fetched — no extra
        API calls. It catches any residual staleness after
        ``_maybe_self_heal`` ran, in case the self-heal couldn't fix
        a file (e.g. because the intraday bar was not yet available at
        self-heal time but has arrived by replay time).

        Never raises. All failure modes (missing file, unparseable JSON,
        no intraday candles for that index) are treated as "cannot
        verify" — i.e. that index is **not** flagged as stale. The
        caller decides what to do with the result.
        """
        stale: list[str] = []

        for key, meta in INDEX_METADATA.items():
            canonical = str(meta.get("canonical") or "").strip()
            if not canonical:
                continue

            candles = intraday.get(key) or []
            if not candles:
                continue

            # Find the 09:15 bar from the intraday series
            first_bar: dict[str, Any] | None = None
            for candle in candles:
                t = str(candle.get("time") or "")
                if "T09:15" in t:
                    first_bar = candle
                    break
            if first_bar is None:
                continue

            try:
                intraday_high = float(first_bar.get("high"))
                intraday_low = float(first_bar.get("low"))
            except (TypeError, ValueError):
                continue
            if not (intraday_high > 0 and intraday_low > 0):
                continue

            # Read the stored first_candle.json
            folder = (
                Path(config.LIVE_DATA_DIR)
                / "index" / canonical.lower()
            )
            fc_path = folder / "first_candle.json"
            if not fc_path.exists():
                # Missing is handled by _maybe_self_heal — skip here
                continue
            try:
                payload = json.loads(fc_path.read_text(encoding="utf-8"))
            except Exception:
                stale.append(canonical)
                continue

            stored_fc = payload.get("first_candle") or {}
            try:
                stored_high = float(stored_fc.get("high"))
                stored_low = float(stored_fc.get("low"))
            except (TypeError, ValueError):
                stale.append(canonical)
                continue

            if (
                abs(stored_high - intraday_high) > _OR_MISMATCH_TOLERANCE
                or abs(stored_low - intraday_low) > _OR_MISMATCH_TOLERANCE
            ):
                logger.warning(
                    "[level-isolation] OR mismatch for %s: "
                    "stored H/L=%.2f/%.2f vs intraday H/L=%.2f/%.2f "
                    "(tol=%.2f) — flagging stale",
                    canonical,
                    stored_high, stored_low,
                    intraday_high, intraday_low,
                    _OR_MISMATCH_TOLERANCE,
                )
                stale.append(canonical)

        return stale

    # ------------------------------------------------------------------ #
    # NEW: series-derived OR computation (G1 v4.1 §5.D replay fix)       #
    # ------------------------------------------------------------------ #
    def _recompute_or_from_intraday(
        self,
        intraday: dict[str, list[dict[str, Any]]],
    ) -> dict[str, dict[str, Any]]:
        """G1 v4.1 §5.D — recompute OR levels from the same intraday
        series the replay iterates.

        For each monitored index, calls
        ``first_candle_service.compute_for_series(canonical.lower(),
        candles)`` which locates the 09:15 bar in ``candles`` and
        computes the OR levels from that bar. Returns a dict keyed by
        canonical name (e.g. ``"MIDCPNIFTY"``) whose values match the
        shape of ``_load_or_levels()`` — so the caller can swap them
        transparently.

        Indices whose candles are missing, whose ``compute_for_series``
        call raises, or whose series has no 09:15 bar are simply
        omitted from the result. During replay, an absent canonical
        means no touches are detected for that index — which is the
        correct fallback when the series cannot support an OR (better
        to miss a touch than to fabricate one from a stale OR file).

        Never raises.
        """
        out: dict[str, dict[str, Any]] = {}

        try:
            from services.first_candle_service import first_candle_service
        except Exception:
            logger.exception(
                "[level-isolation] series-OR: first_candle_service "
                "import failed"
            )
            return out

        for key, meta in INDEX_METADATA.items():
            canonical = str(meta.get("canonical") or "").strip()
            if not canonical:
                continue

            candles = intraday.get(key) or []
            if not candles:
                continue

            try:
                result = first_candle_service.compute_for_series(
                    canonical.lower(), candles,
                )
            except Exception:
                logger.exception(
                    "[level-isolation] series-OR: compute_for_series "
                    "failed for %s", canonical,
                )
                continue

            if not isinstance(result, dict):
                continue

            or_levels = result.get("or_levels")
            if isinstance(or_levels, dict) and or_levels:
                out[canonical] = or_levels

        return out

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
        if not config.LEVEL_ISOLATION_ENABLED:
            return

        canonical = KEY_TO_CANONICAL.get(instrument_key)
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

            self._last_ltp[instrument_key] = ltp_f
            self._last_tick_at[instrument_key] = now

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
        for underlying in config.LEVEL_FILTER_INDEXES:
            u = str(underlying or "").strip().upper()
            if not u:
                continue
            idx_lower = u.lower()
            path = (
                Path(config.LIVE_DATA_DIR)
                / "index" / idx_lower / "first_candle.json"
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
            levels = payload.get("or_levels") or {}
            if levels:
                out[u] = levels
        return out

    def _load_filtered_instruments(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for underlying in config.LEVEL_FILTER_INDEXES:
            u = str(underlying or "").strip().upper()
            if not u:
                continue
            idx_lower = u.lower()
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
            out[u] = payload
        return out

    # ------------------------------------------------------------------ #
    # Live touch check (G1 v4.0 — MIDCPNIFTY-first arbitration)          #
    # ------------------------------------------------------------------ #
    def _check_touches(self, now: datetime) -> None:
        if self._state.get("isolated") is not None:
            return

        or_levels = self._load_or_levels()
        filtered = self._load_filtered_instruments()
        if not or_levels:
            return
        nifty_filtered = filtered.get("NIFTY")
        if not nifty_filtered:
            return

        touches: list[dict[str, Any]] = []
        for canonical, levels in or_levels.items():
            key = CANONICAL_TO_KEY.get(canonical)
            if not key:
                continue
            ltp = self._last_ltp.get(key)
            if ltp is None:
                continue
            for side, level_names in (
                ("R3", _R3_TRIGGER_LEVELS),
                ("S3", _S3_TRIGGER_LEVELS),
            ):
                level_value = levels.get(side)
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
                state_key = f"{canonical}_{side}"
                if self._state["touch_state"].get(state_key):
                    continue
                touches.append({
                    "canonical": canonical,
                    "instrument_key": key,
                    "side": side,
                    "level_value": level_f,
                    "trigger_ltp": ltp,
                    "priority": INDEX_METADATA[key]["priority"],
                    "touched_at": now.isoformat(),
                })

        if not touches:
            return

        # v4.0 arbitration: MIDCPNIFTY > BANKNIFTY > NIFTY > SENSEX
        touches.sort(key=lambda t: (t["priority"], t["touched_at"]))

        simultaneous = len(touches) > 1
        winner = touches[0]

        for t in touches:
            self._state["touch_state"][
                f"{t['canonical']}_{t['side']}"
            ] = True

        if simultaneous:
            logger.info(
                "[level-isolation] multi-touch poll: %d indices touched "
                "simultaneously — winner=%s %s (priority=%d)",
                len(touches), winner["canonical"], winner["side"],
                winner["priority"],
            )
        else:
            logger.info(
                "[level-isolation] multi-touch poll: %s touched %s "
                "(%.2f vs level %.2f) priority=%d",
                winner["canonical"], winner["side"],
                winner["trigger_ltp"], winner["level_value"],
                winner["priority"],
            )

        self._process_winning_touch(
            winner, or_levels, nifty_filtered, now,
            source="live",
            simultaneous_touches=[
                {
                    "canonical": t["canonical"],
                    "side": t["side"],
                    "priority": t["priority"],
                }
                for t in touches[1:]
            ],
        )

    # ------------------------------------------------------------------ #
    # MODIFIED: winning-touch handler — bias + counter candidates        #
    # ------------------------------------------------------------------ #
    def _process_winning_touch(
        self,
        winner: dict[str, Any],
        or_levels: dict[str, dict[str, Any]],
        nifty_filtered: dict[str, Any],
        now: datetime,
        source: str,
        simultaneous_touches: list[dict[str, Any]] | None = None,
    ) -> None:
        trigger_canonical = winner["canonical"]
        trigger_side = winner["side"]

        # v4.0 — bias from R3 = CE, from S3 = PE
        bias = config.STRATEGY_RELATIONAL_R3_BIAS if trigger_side == "R3" \
            else config.STRATEGY_RELATIONAL_S3_BIAS
        if bias not in {"CE", "PE"}:
            bias = "CE" if trigger_side == "R3" else "PE"

        nifty_key = CANONICAL_TO_KEY.get("NIFTY")
        nifty_ltp = self._last_ltp.get(nifty_key) if nifty_key else None
        nifty_ltp_source = "live_feed_cache"

        if nifty_ltp is None:
            if trigger_canonical == "NIFTY":
                nifty_ltp = winner["trigger_ltp"]
                nifty_ltp_source = "trigger_ltp_fallback"
            else:
                logger.warning(
                    "[level-isolation] NIFTY LTP missing at %s %s touch — "
                    "cannot route to NIFTY strike; skipping isolation",
                    trigger_canonical, trigger_side,
                )
                return

        # ---- Bias-side candidates (used for the pick) ---------------- #
        bias_candidates = self._find_nifty_candidates(
            trigger_side, nifty_filtered,
        )
        if not bias_candidates:
            logger.warning(
                "[level-isolation] no NIFTY %s candidates available for "
                "trigger %s %s — isolation skipped",
                bias, trigger_canonical, trigger_side,
            )
            return

        pick = min(
            bias_candidates,
            key=lambda c: abs(self._strike(c) - float(nifty_ltp)),
        )

        # ---- Counter-side candidates (display / auto-reverse context) - #
        # G1 v4.1: when crossover is enabled, the entry engine evaluates
        # BOTH sides. Surface the counter side in the candidate list so
        # the UI shows the full 4-candidate set instead of bias-side-only.
        # The `pick` above is still chosen from bias_candidates only —
        # arbitration behaviour is unchanged.
        #
        # G1 v4.1 flag-wiring fix (§5.A): the counter-side collection is
        # additionally gated by STRATEGY_ENTRY_REGISTER_COUNTER_SIDE. When
        # either flag is false, only bias-side candidates are surfaced —
        # reproducing the pre-v4.1 behaviour where the counter side is
        # neither registered with the SR engine nor shown on the UI.
        counter_candidates: list[dict[str, Any]] = []
        register_counter = (
            bool(getattr(config, "STRATEGY_ENTRY_ALLOW_CROSSOVER", True))
            and bool(getattr(config, "STRATEGY_ENTRY_REGISTER_COUNTER_SIDE", True))
        )
        if register_counter:
            counter_side = "S3" if trigger_side == "R3" else "R3"
            counter_candidates = self._find_nifty_candidates(
                counter_side, nifty_filtered,
            )

        tagged_candidates: list[dict[str, Any]] = []
        for c in bias_candidates:
            tagged_candidates.append({**c, "side": "bias"})
        for c in counter_candidates:
            tagged_candidates.append({**c, "side": "counter"})

        # G1 v4.2: persist the execution path on the event so downstream
        # services (dummy-order, promotion, UI) can branch on it without
        # re-deriving from trigger_canonical.
        trigger_is_self_nifty = (trigger_canonical == "NIFTY")
        execution_path = (
            "A_self_trigger" if trigger_is_self_nifty
            else "B_external_trigger"
        )

        event: dict[str, Any] = {
            "index": trigger_canonical,
            "side": trigger_side,
            "trigger_level": trigger_side,
            "trigger_level_value": winner["level_value"],
            "trigger_ltp": winner["trigger_ltp"],
            "touched_at": winner["touched_at"],
            "source": source,
            "trigger_priority": winner["priority"],
            "trigger_is_self_nifty": trigger_is_self_nifty,
            "execution_path": execution_path,
            "simultaneous_touches": simultaneous_touches or [],
            "routed_nifty_ltp": float(nifty_ltp),
            "routed_nifty_ltp_source": nifty_ltp_source,
            "candidates_count": len(tagged_candidates),
            "bias_candidates_count": len(bias_candidates),
            "counter_candidates_count": len(counter_candidates),
            "candidates": self._enrich_candidates(
                tagged_candidates, float(nifty_ltp),
            ),
        }

        reason = f"{trigger_canonical.lower()}_{trigger_side.lower()}_touch"
        self._isolate(pick, reason, event, trigger_canonical)

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
        # G1 v4.1: carry the "side" tag through so the UI can show
        # a bias/counter column without re-deriving it. Existing
        # candidates without a side default to "bias".
        return [
            {
                "instrument_key": c.get("instrument_key"),
                "trading_symbol": c.get("trading_symbol"),
                "strike": c.get("strike"),
                "type": c.get("type"),
                "side": c.get("side", "bias"),
                "levels": c.get("levels"),
                distance_field: round(abs(self._strike(c) - ref_ltp), 2),
            }
            for c in candidates[:20]
        ]

    def _find_nifty_candidates(
        self,
        side: str,
        nifty_filtered: dict[str, Any],
    ) -> list[dict[str, Any]]:
        if side == "R3":
            instruments = nifty_filtered.get("ce_instruments") or []
            trigger_levels = _R3_TRIGGER_LEVELS
        else:
            instruments = nifty_filtered.get("pe_instruments") or []
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

    # ------------------------------------------------------------------ #
    # NEW: counter-side resolution + SR registration                     #
    # ------------------------------------------------------------------ #
    def _find_counter_candidate(
        self,
        bias_candidate: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Resolve the counter-side option (PE↔CE) at the same strike.

        Returns a candidate dict shaped like the ones from
        ``_find_nifty_candidates`` — with ``instrument_key``,
        ``trading_symbol``, ``strike``, ``type``, ``expiry`` and
        ``levels`` fields — or ``None`` when the registry cannot resolve
        the counter side.

        Never raises.
        """
        bias_type = str(bias_candidate.get("type") or "").upper()
        if bias_type not in {"CE", "PE"}:
            return None

        strike = bias_candidate.get("strike")
        if strike is None:
            return None

        underlying = self._resolve_underlying_for_candidate(bias_candidate)
        if not underlying:
            underlying = "NIFTY"

        counter_type = "PE" if bias_type == "CE" else "CE"

        try:
            from services.instrument_registry import instrument_registry
            entry = instrument_registry.resolve(underlying, strike, counter_type)
            if entry is None:
                return None
            return {
                "instrument_key": entry.get("instrument_key"),
                "trading_symbol": entry.get("trading_symbol"),
                "strike": entry.get("strike_price"),
                "type": counter_type,
                "expiry": entry.get("expiry"),
                "levels": [],
            }
        except Exception:
            logger.exception(
                "[level-isolation] counter-side resolve failed for "
                "%s %s %s",
                underlying, strike, counter_type,
            )
            return None

    def _register_isolated_pair_with_sr(
        self,
        bias_candidate: dict[str, Any],
        counter_candidate: dict[str, Any] | None,
    ) -> None:
        """Register the bias-side **and** counter-side instruments with
        the SR engine, seeding both with today's intraday candles.

        Why this exists
        ---------------
        ``strategy_entry_signal_service._entry_price_for()`` looks up the
        confirming candle's close via ``_candle_close_at()`` which reads
        from ``live_sr_levels_service._states[key]["candles"]``. If the
        counter-side option was never registered with the SR engine,
        that lookup returns ``None`` and the entry price silently falls
        back to ``_live_ltp()`` — which during a **simulate-mode replay**
        means the *current* wall-clock price (~₹61) instead of the
        *touch-time* candle close (~₹186). That produced both the wrong
        entry price on ``/isolated`` and the wrong stop / target / R
        multiple on the dummy order.

        Seeding the counter side here, immediately after isolation (and
        therefore before any entry-signal computation), closes that hole.

        Also registers the same instrument with the live feed (best
        effort) so that on a live day the counter-side SR state keeps
        growing and does not go stale before the promotion runs.

        Never raises. Every failure path logs and continues — a failure
        to register the counter side must not break the isolation itself.
        """
        try:
            from services.live_sr_levels_service import live_sr_levels_service
            from services.instrument_registry import instrument_registry
        except Exception:
            logger.exception(
                "[level-isolation] SR/registry import failed during "
                "counter-side registration"
            )
            return

        candidates: list[dict[str, Any]] = [bias_candidate]
        if counter_candidate:
            candidates.append(counter_candidate)

        for candidate in candidates:
            key = str(candidate.get("instrument_key") or "").strip()
            if not key:
                continue

            try:
                entry = instrument_registry.get(key)
                if entry is None:
                    entry = instrument_registry.register_adhoc(key)
                if entry is None:
                    continue

                already_tracking = live_sr_levels_service.is_tracking(key)

                # Register (idempotent — no-op if already present today)
                live_sr_levels_service.register_instrument(entry)

                # Seed today's candles when this is the first time we are
                # tracking this instrument. Critical for the backfill /
                # simulate path where the SR engine was populated by the
                # feed-start backfill but the counter side was not yet a
                # registered instrument.
                if not already_tracking:
                    self._seed_candles_for_instrument(key, entry)

                # Best-effort feed subscription so live ticks continue
                # updating the counter-side SR state after isolation.
                try:
                    from services.live_feed_service import live_feed_service
                    if not live_feed_service.is_subscribed(key):
                        live_feed_service.subscribe_instrument(key)
                except Exception:
                    logger.exception(
                        "[level-isolation] feed subscribe failed for %s", key,
                    )
            except Exception:
                logger.exception(
                    "[level-isolation] SR registration failed for %s", key,
                )

    def _seed_candles_for_instrument(
        self,
        key: str,
        entry: dict[str, Any],
    ) -> None:
        """Fetch today's intraday candles for ``key`` and seed the SR
        engine with them (plus the historical tail when available).

        Idempotent — callers should only invoke this when the instrument
        is not yet in the SR engine. Never raises.
        """
        try:
            from services.live_sr_calc_service import live_sr_calc_service
            from services.live_sr_levels_service import live_sr_levels_service
        except Exception:
            logger.exception(
                "[level-isolation] seed_candles import failed for %s", key,
            )
            return

        try:
            intraday = live_sr_calc_service.fetch_intraday_candles_for_entry(key)
        except Exception:
            logger.exception(
                "[level-isolation] intraday fetch failed for %s", key,
            )
            return
        if not intraday:
            logger.info(
                "[level-isolation] no intraday candles for %s — "
                "seed skipped", key,
            )
            return

        historical: list[dict[str, Any]] = []
        try:
            historical = live_sr_calc_service.load_historical_candles_for_entry(
                entry,
                int(config.LIVE_SR_CALC_DEFAULT_LOOKBACK_DAYS),
            )
        except Exception:
            historical = []

        merged = (
            live_sr_calc_service.merge_candles(historical, intraday)
            if historical else intraday
        )
        if not merged:
            return

        try:
            summary = live_sr_levels_service.seed_candles(key, entry, merged)
            logger.info(
                "[level-isolation] seeded SR candles for %s — "
                "candles=%s pivots=%s",
                key,
                summary.get("candles_seeded"),
                summary.get("pivots_detected"),
            )
        except Exception:
            logger.exception(
                "[level-isolation] seed_candles raised for %s", key,
            )

    # ------------------------------------------------------------------ #
    # Underlying resolution                                              #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _resolve_underlying_for_candidate(
        candidate: dict[str, Any],
    ) -> str:
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

    @staticmethod
    def _compute_or_levels(fc: dict[str, Any]) -> dict[str, float] | None:
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
    # Instrument snapshot                                                #
    # ------------------------------------------------------------------ #
    def _collect_instrument_snapshot(
        self,
        candidate: dict[str, Any],
        underlying: str,
        touch_dt: datetime | None,
        allow_rest: bool,
    ) -> dict[str, Any]:
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
                        if str(payload.get("date") or "") == _today_iso():
                            snapshot["instrument_first_candle"] = payload.get("first_candle")
                            snapshot["instrument_or_levels"] = payload.get("or_levels")
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

        if allow_rest:
            try:
                candles = self._fetch_intraday(key)
            except Exception:
                candles = []

            if candles:
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
                    last = candles[-1]
                    try:
                        snapshot["instrument_ltp"] = float(last["close"])
                        snapshot["instrument_quote_source"] = "intraday_latest_bar"
                    except (TypeError, ValueError, KeyError):
                        pass

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
                            break

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
    # MODIFIED: isolation — register both sides with SR (flag-gated)     #
    # ------------------------------------------------------------------ #
    def _isolate(
        self,
        candidate: dict[str, Any],
        reason: str,
        event: dict[str, Any],
        trigger_index_canonical: str,
    ) -> None:
        touch_dt: datetime | None = None
        touched_at_str = event.get("touched_at")
        if touched_at_str:
            try:
                touch_dt = datetime.fromisoformat(str(touched_at_str))
            except Exception:
                touch_dt = None
        if touch_dt is None:
            touch_dt = _now_market()

        is_backfill = str(event.get("source") or "") == "backfill"

        routed_key = str(config.STRATEGY_ROUTED_EXECUTION_INDEX or "NSE_INDEX|Nifty 50")
        routed_canonical = KEY_TO_CANONICAL.get(routed_key, "NIFTY")

        snapshot = self._collect_instrument_snapshot(
            candidate,
            underlying=routed_canonical,
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
            "source": "backfill" if is_backfill else "live",
            "isolated_at": _now_market().isoformat(),
            "trigger_index": trigger_index_canonical,
            "trigger_instrument_key": event.get("instrument_index_key")
                or CANONICAL_TO_KEY.get(trigger_index_canonical),
            "trigger_side": event.get("side"),
            "trigger_level": event.get("trigger_level"),
            "trigger_level_value": event.get("trigger_level_value"),
            "trigger_ltp": event.get("trigger_ltp"),
            "trigger_priority": event.get("trigger_priority"),
            "simultaneous_touches": event.get("simultaneous_touches") or [],
            "routed_index": routed_canonical,
            "routed_instrument_key": routed_key,
            "routed_nifty_ltp": event.get("routed_nifty_ltp"),
            "routed_nifty_ltp_source": event.get("routed_nifty_ltp_source"),
            "touched_at": event.get("touched_at"),
            "instrument_ltp": snapshot.get("instrument_ltp"),
            "instrument_touch_ohlc": snapshot.get("instrument_touch_ohlc"),
            "instrument_first_candle": snapshot.get("instrument_first_candle"),
            "instrument_or_levels": snapshot.get("instrument_or_levels"),
            "instrument_quote_source": snapshot.get("instrument_quote_source"),
        }

        # G1 v4.2: propagate trigger_is_self_nifty / execution_path onto
        # the isolated record so downstream services (dummy-order,
        # promotion, UI) can branch without re-deriving it from
        # `trigger_index`. Legacy records (pre-v4.2) will be handled
        # defensively by `get_execution_path()`.
        isolated["trigger_is_self_nifty"] = bool(
            event.get(
                "trigger_is_self_nifty",
                trigger_index_canonical == "NIFTY",
            )
        )
        isolated["execution_path"] = event.get(
            "execution_path",
            "A_self_trigger"
            if isolated["trigger_is_self_nifty"]
            else "B_external_trigger",
        )

        self._state["isolated"] = isolated
        event["status"] = "isolated"
        event["isolated_instrument"] = candidate.get("instrument_key")
        event["instrument_ltp"] = snapshot.get("instrument_ltp")
        event["instrument_quote_source"] = snapshot.get("instrument_quote_source")
        self._state["events"].append(event)
        self._save_state()

        logger.info(
            "[level-isolation] ISOLATED reason=%s trigger=%s %s "
            "priority=%d routed_%s_ltp=%s strike=%s bias=%s "
            "source=%s execution_path=%s",
            reason, trigger_index_canonical, event.get("side"),
            event.get("trigger_priority") or 0,
            routed_canonical.lower(),
            event.get("routed_nifty_ltp"),
            candidate.get("strike"),
            candidate.get("type"),
            isolated["source"],
            isolated.get("execution_path"),
        )

        # ---- G1 v4.1 §5.A: register BOTH sides with the SR engine ----- #
        # This runs BEFORE the SR tracker hook and BEFORE any later
        # entry-signal computation. It makes the counter-side option's
        # candle series available in `live_sr_levels_service._states`,
        # which is what `strategy_entry_signal_service._candle_close_at()`
        # reads from. Without this, the counter-side entry price silently
        # falls back to `live_ltp` on a simulate-mode replay.
        #
        # G1 v4.1 flag-wiring fix (§5.A): the counter-side registration
        # is gated on STRATEGY_ENTRY_REGISTER_COUNTER_SIDE **and**
        # STRATEGY_ENTRY_ALLOW_CROSSOVER. When either is false, only the
        # bias side is registered — reproducing the pre-v4.1 behaviour
        # where the counter side was never registered with the SR engine.
        counter_candidate: dict[str, Any] | None = None
        try:
            register_counter = (
                bool(getattr(config, "STRATEGY_ENTRY_ALLOW_CROSSOVER", True))
                and bool(getattr(
                    config, "STRATEGY_ENTRY_REGISTER_COUNTER_SIDE", True,
                ))
            )
            if register_counter:
                counter_candidate = self._find_counter_candidate(candidate)
            self._register_isolated_pair_with_sr(candidate, counter_candidate)
        except Exception:
            logger.exception(
                "[level-isolation] counter-side SR registration failed"
            )

        try:
            from services.isolated_sr_tracker_service import (
                isolated_sr_tracker_service,
            )
            isolated_sr_tracker_service.on_isolation(isolated)
        except Exception:
            logger.exception(
                "[level-isolation] SR tracker on_isolation failed"
            )

        if config.LEVEL_ISOLATION_TELEGRAM_ENABLED:
            threading.Thread(
                target=self._send_telegram,
                args=(isolated,),
                daemon=True,
                name="level-isolation-telegram",
            ).start()

    # ------------------------------------------------------------------ #
    # NEW (G1 v4.2): execution-path accessor                             #
    # ------------------------------------------------------------------ #
    def get_execution_path(self) -> str:
        """Return the current isolation's execution path, or ''.

        '' when no isolation exists yet (so callers can distinguish
        'not isolated yet' from either of the two paths).
        """
        with self._lock:
            iso = self._state.get("isolated") or {}
            path = str(iso.get("execution_path") or "").strip()
            if path:
                return path
            # Legacy records (pre-v4.1) have no path — derive defensively.
            if iso:
                return (
                    "A_self_trigger"
                    if str(iso.get("trigger_index") or "").upper() == "NIFTY"
                    else "B_external_trigger"
                )
            return ""

    def _send_telegram(self, isolated: dict[str, Any]) -> None:
        try:
            from services.telegram_service import telegram_service
            _DIV = "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
            _THIN = "────────────────────────────────"

            source_tag = "SIMULATE" if isolated.get("source") == "backfill" else "LIVE"
            trigger_idx = isolated.get("trigger_index") or "—"
            trigger_side = isolated.get("trigger_side") or "—"
            trigger_ltp = isolated.get("trigger_ltp")
            trigger_level = isolated.get("trigger_level")
            trigger_val = isolated.get("trigger_level_value")
            routed_ltp = isolated.get("routed_nifty_ltp")
            routed_idx = isolated.get("routed_index") or "NIFTY"

            sim_tag = ""
            if isolated.get("source") == "backfill":
                sim_tag = (
                    f"🔄 Mode:          {source_tag} (post-close replay)\n"
                )

            priority = isolated.get("trigger_priority") or "—"

            msg = (
                f"{_DIV}\n"
                f"🎯 LEVEL ISOLATION · NIFTY STRIKE LOCKED\n"
                f"{_DIV}\n"
                f"{sim_tag}"
                f"⚡ Trigger:       {trigger_idx} {trigger_side} "
                f"(priority={priority})\n"
                f"📈 Trigger LTP:   {trigger_ltp}\n"
                f"📊 Level:         {trigger_level} = {trigger_val}\n"
                f"🎯 Routed to:     {routed_idx} (spot LTP {routed_ltp})\n"
                f"{_THIN}\n"
                f"📌 Instrument:    {isolated.get('trading_symbol') or isolated.get('instrument_key')}\n"
                f"🔑 Key:           {isolated.get('instrument_key')}\n"
                f"📊 Strike/Type:   {isolated.get('strike')} {isolated.get('type')}\n"
                f"📅 Expiry:        {isolated.get('expiry')}\n"
                f"🕒 Isolated At:   {isolated.get('isolated_at')}\n"
                f"{_THIN}\n"
                f"💾 data/{config.LEVEL_ISOLATION_FILENAME}"
            )
            telegram_service.send_message(
                "Level Isolation · NIFTY Strike Locked",
                msg,
                level="SUCCESS",
            )
        except Exception:
            logger.exception("[level-isolation] telegram send failed")

    # ------------------------------------------------------------------ #
    # Startup backfill (G1 v4.0 — 4 index replay, v4.1 OR verify)        #
    # ------------------------------------------------------------------ #
    def _snapshot_needs_backfill(self, iso: dict[str, Any]) -> bool:
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
        candidate = {
            "instrument_key": iso.get("instrument_key"),
            "trading_symbol": iso.get("trading_symbol"),
            "strike": iso.get("strike"),
            "type": iso.get("type"),
        }
        underlying = (
            self._resolve_underlying_for_candidate(candidate)
            or str(iso.get("routed_index") or "NIFTY").strip().upper()
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
        return snapshot

    def refresh_snapshot(self) -> dict[str, Any]:
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

        self._maybe_self_heal(reason=reason)

        with self._lock:
            self._ensure_today()

            if self._state.get("isolated") is not None:
                iso = self._state["isolated"]
                self._ensure_tracker_attached()

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

            filtered = self._load_filtered_instruments()
            nifty_filtered = filtered.get("NIFTY")
            if not nifty_filtered:
                return {"status": "skipped", "reason": "missing_nifty_filtered"}

            intraday: dict[str, list[dict[str, Any]]] = {}
            for key in INDEX_METADATA.keys():
                intraday[key] = self._fetch_intraday(key)

            if not any(intraday.values()):
                return {"status": "skipped", "reason": "no_intraday_data"}

            # Load OR from disk first (used both as a fallback and as the
            # baseline the verification step below compares against).
            or_levels = self._load_or_levels()

            # -------------------------------------------------------- #
            # G1 v4.1: verify each index's stored OR against the real  #
            # 09:15 bar from the intraday series. Any mismatch means    #
            # first_candle.json is stale — recompute it before replay   #
            # so a phantom touch cannot fire on a wrong R3 / S3.        #
            # -------------------------------------------------------- #
            # NOTE: this check is the "late detection" counterpart to
            # the H/L verification inside `_maybe_self_heal` (which is
            # gated by LEVEL_ISOLATION_OR_SELF_HEAL_VERIFY_HL). It runs
            # unconditionally because it consumes the intraday candles
            # already fetched for the replay — no extra API calls.
            try:
                stale_indices = self._verify_or_against_intraday(intraday)
            except Exception:
                stale_indices = []
                logger.exception(
                    "[level-isolation] OR-vs-intraday verification failed"
                )

            if stale_indices:
                logger.warning(
                    "[level-isolation] stale OR detected for %s — "
                    "recomputing first_candle before replay",
                    stale_indices,
                )
                try:
                    from services.first_candle_service import (
                        first_candle_service,
                    )
                    first_candle_service.run(
                        reason=f"level_isolation_or_stale:{reason}",
                        backfill=True,
                    )
                    or_levels = self._load_or_levels()
                except Exception:
                    logger.exception(
                        "[level-isolation] OR recompute failed — "
                        "replaying with existing or_levels"
                    )

            # -------------------------------------------------------- #
            # G1 v4.1 §5.D (replay fix): when the flag is on, replace  #
            # the on-disk OR with OR recomputed from the SAME intraday #
            # series the replay iterates. This is the primary fix for  #
            # the "phantom MIDCPNIFTY R3 touch" class of bug — the OR  #
            # used for the trigger check is guaranteed consistent with #
            # the candles. When the flag is off, the (possibly-healed)  #
            # on-disk OR is used unchanged (pre-v4.1 behaviour).       #
            # -------------------------------------------------------- #
            if bool(getattr(config, "LEVEL_ISOLATION_REPLAY_USE_SERIES_OR", True)):
                series_or = self._recompute_or_from_intraday(intraday)
                if series_or:
                    or_levels = series_or
                    logger.info(
                        "[level-isolation] replay OR recomputed from "
                        "series — indices=%s",
                        sorted(series_or.keys()),
                    )
                elif not or_levels:
                    return {
                        "status": "skipped",
                        "reason": "missing_or_levels",
                    }
                else:
                    logger.info(
                        "[level-isolation] series-OR produced no entries — "
                        "falling back to on-disk OR levels (indices=%s)",
                        sorted(or_levels.keys()),
                    )
            elif not or_levels:
                return {"status": "skipped", "reason": "missing_or_levels"}

            merged = self._merge_all_by_minute(intraday)
            if not merged:
                return {"status": "skipped", "reason": "no_merged_data"}

            logger.info(
                "[level-isolation] backfill replay — reason=%s candles=%d "
                "indices=%d source=backfill",
                reason, len(merged), len(intraday),
            )

            isolated = self._replay(
                merged, intraday, or_levels, nifty_filtered,
            )
            self._save_state()

            return {
                "status": "ok" if isolated else "no_touch",
                "reason": reason,
                "candles_replayed": len(merged),
                "isolated": isolated,
                "source": "backfill",
                "stale_or_indices": stale_indices,
            }

    def _fetch_intraday(self, instrument_key: str) -> list[dict[str, Any]]:
        try:
            import upstox_client
            cfg = upstox_client.Configuration()
            try:
                from services.token_service import token_service
                token = token_service.get_access_token()
                if token:
                    cfg.access_token = token
            except Exception:
                pass
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

    def _merge_all_by_minute(
        self,
        intraday: dict[str, list[dict[str, Any]]],
    ) -> list[str]:
        minutes: set[str] = set()
        for candles in intraday.values():
            for c in candles:
                if c.get("time"):
                    minutes.add(str(c["time"])[:16])
        return sorted(minutes)

    def _replay(
        self,
        merged: list[str],
        intraday: dict[str, list[dict[str, Any]]],
        or_levels: dict[str, dict[str, Any]],
        nifty_filtered: dict[str, Any],
    ) -> dict[str, Any] | None:
        by_minute: dict[str, dict[str, dict[str, Any]]] = {}
        for key, candles in intraday.items():
            by_minute[key] = {
                str(c["time"])[:16]: c for c in candles if c.get("time")
            }

        nifty_key = CANONICAL_TO_KEY.get("NIFTY")

        for minute in merged:
            nifty_candle = (
                by_minute.get(nifty_key, {}).get(minute)
                if nifty_key else None
            )
            if nifty_candle is not None:
                self._last_ltp[nifty_key] = float(nifty_candle["close"])
                self._last_tick_at[nifty_key] = datetime.fromisoformat(
                    minute,
                ).replace(tzinfo=_MARKET_TZ)

            touches: list[dict[str, Any]] = []
            for key, meta in INDEX_METADATA.items():
                candle = by_minute.get(key, {}).get(minute)
                if candle is None:
                    continue
                canonical = meta["canonical"]
                levels = or_levels.get(canonical, {})
                for side, level_names in (
                    ("R3", _R3_TRIGGER_LEVELS),
                    ("S3", _S3_TRIGGER_LEVELS),
                ):
                    level_value = levels.get(side)
                    if level_value is None:
                        continue
                    try:
                        level_f = float(level_value)
                    except (TypeError, ValueError):
                        continue
                    if side == "R3":
                        touched = float(candle["high"]) >= level_f
                    else:
                        touched = float(candle["low"]) <= level_f
                    if not touched:
                        continue
                    state_key = f"{canonical}_{side}"
                    if self._state["touch_state"].get(state_key):
                        continue
                    touches.append({
                        "canonical": canonical,
                        "instrument_key": key,
                        "side": side,
                        "level_value": level_f,
                        "trigger_ltp": float(candle["close"]),
                        "priority": meta["priority"],
                        "touched_at": minute + ":00",
                    })

            if not touches:
                continue

            touches.sort(key=lambda t: (t["priority"], t["touched_at"]))
            winner = touches[0]

            for t in touches:
                self._state["touch_state"][
                    f"{t['canonical']}_{t['side']}"
                ] = True

            simultaneous = len(touches) > 1

            logger.info(
                "[level-isolation] backfill: %s touched %s at %s "
                "(%s, priority=%d)%s",
                winner["canonical"], winner["side"], minute,
                winner["level_value"], winner["priority"],
                " [simultaneous]" if simultaneous else "",
            )

            if nifty_key and by_minute.get(nifty_key, {}).get(minute):
                self._last_ltp[nifty_key] = float(
                    by_minute[nifty_key][minute]["close"]
                )

            self._process_winning_touch(
                winner, or_levels, nifty_filtered,
                datetime.fromisoformat(minute).replace(tzinfo=_MARKET_TZ),
                source="backfill",
                simultaneous_touches=[
                    {
                        "canonical": t["canonical"],
                        "side": t["side"],
                        "priority": t["priority"],
                    }
                    for t in touches[1:]
                ],
            )
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
                "monitored_indices": [
                    {
                        "canonical": v["canonical"],
                        "instrument_key": k,
                        "priority": v["priority"],
                    }
                    for k, v in INDEX_METADATA.items()
                ],
                "routed_execution_index": str(
                    config.STRATEGY_ROUTED_EXECUTION_INDEX
                    or "NSE_INDEX|Nifty 50"
                ),
                "nifty_ltp": self._last_ltp.get(
                    CANONICAL_TO_KEY.get("NIFTY", "")
                ),
                "live_ltps": {
                    KEY_TO_CANONICAL[k]: v
                    for k, v in self._last_ltp.items()
                },
            }

    def _holiday_info(self, now: datetime) -> dict[str, Any]:
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
                "Market closed at 15:30 IST. The isolation engine can "
                "reconstruct the day's touch in SIMULATE MODE by clicking "
                "Run Backfill, or on next startup before 23:59."
            )
        else:
            state = "in_session"
            message = (
                "Watching 4 indices (MIDCPNIFTY / BANKNIFTY / NIFTY / "
                "SENSEX) for R3 / S3 touches."
            )

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
            "simulate_mode": (not in_session) and self._in_backfill_window(now),
        }

    def _read_live_sr_for(
        self,
        source: dict[str, Any],
        reference_dt: datetime | None,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "available": False,
            "path": None,
            "candle_count": 0,
            "total_levels": 0,
            "before_count": 0,
            "after_count": 0,
            "touched_at": source.get("touched_at") if isinstance(source, dict) else None,
            "reference_at": reference_dt.isoformat() if reference_dt else None,
            "levels": [],
            "error": None,
        }

        if not isinstance(source, dict):
            result["error"] = "no_source"
            return result

        key = str(source.get("instrument_key") or "").strip()
        if not key:
            result["error"] = "missing_instrument_key"
            return result

        underlying = ""
        if source.get("underlying"):
            underlying = str(source["underlying"]).strip().lower()
        if not underlying:
            try:
                from services.instrument_registry import instrument_registry
                entry = instrument_registry.get(key)
                if entry and entry.get("underlying"):
                    underlying = str(entry["underlying"]).strip().lower()
            except Exception:
                pass
        if not underlying:
            symbol = str(source.get("trading_symbol") or "").strip()
            if symbol:
                underlying = symbol.split()[0].lower()
        if not underlying and source.get("trigger_index"):
            underlying = str(source["trigger_index"]).strip().lower()

        strike = source.get("strike")
        if strike is None:
            strike = source.get("strike_price")
        strike_type = str(
            source.get("type") or source.get("strike_type") or ""
        ).upper()

        if not underlying or strike is None or not strike_type:
            result["error"] = "cannot_resolve_path"
            return result

        try:
            strike_int = int(float(strike))
        except (TypeError, ValueError):
            result["error"] = "invalid_strike"
            return result

        folder = (
            Path(config.LIVE_DATA_DIR)
            / underlying
            / f"{strike_int}_{strike_type}"
        )
        path = folder / config.LIVE_SR_FILENAME
        result["path"] = str(path)

        if not path.exists():
            result["error"] = "file_missing"
            return result

        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            result["error"] = f"unreadable: {type(exc).__name__}"
            return result

        if not isinstance(payload, dict):
            result["error"] = "not_a_dict"
            return result

        result["available"] = True
        try:
            result["candle_count"] = int(payload.get("candle_count") or 0)
        except (TypeError, ValueError):
            result["candle_count"] = 0

        raw_levels = payload.get("levels") or []
        if not isinstance(raw_levels, list):
            raw_levels = []

        cleaned: list[dict[str, Any]] = []
        before_count = 0
        after_count = 0

        for lvl in raw_levels:
            if not isinstance(lvl, dict):
                continue
            copy = dict(lvl)
            copy.pop("broken", None)
            copy.pop("broken_at", None)
            copy.pop("broken_price", None)
            copy.pop("broken_bar_index", None)

            after = False
            confirmed_str = str(copy.get("confirmed_time") or "")
            if reference_dt is not None and confirmed_str:
                try:
                    confirmed_dt = datetime.fromisoformat(
                        confirmed_str.replace("Z", "+00:00")
                    )
                    if confirmed_dt.tzinfo is None:
                        confirmed_dt = confirmed_dt.replace(tzinfo=_MARKET_TZ)
                    after = confirmed_dt >= reference_dt
                except Exception:
                    after = False

            copy["_after_isolation"] = bool(after)
            if after:
                after_count += 1
            else:
                before_count += 1

            cleaned.append(copy)

        cleaned.sort(key=lambda l: str(l.get("confirmed_time") or ""))

        result["levels"] = cleaned
        result["total_levels"] = len(cleaned)
        result["before_count"] = before_count
        result["after_count"] = after_count

        return result

    def _load_live_sr_levels_for_isolated(
        self,
        iso: dict[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(iso, dict):
            return {
                "available": False,
                "path": None,
                "candle_count": 0,
                "total_levels": 0,
                "before_count": 0,
                "after_count": 0,
                "touched_at": None,
                "reference_at": None,
                "levels": [],
                "error": "no_isolated_record",
            }

        touched_at_raw = iso.get("touched_at")
        isolated_at_raw = iso.get("isolated_at")
        ref_str = touched_at_raw or isolated_at_raw

        ref_dt: datetime | None = None
        if ref_str:
            try:
                ref_dt = datetime.fromisoformat(str(ref_str))
                if ref_dt.tzinfo is None:
                    ref_dt = ref_dt.replace(tzinfo=_MARKET_TZ)
            except Exception:
                ref_dt = None

        return self._read_live_sr_for(iso, ref_dt)

    def get_instrument_sr_comparison(
        self,
        instrument_key: str,
    ) -> dict[str, Any]:
        key = str(instrument_key or "").strip()
        if not key:
            return {
                "status": "invalid_input",
                "error": "instrument_key is required",
                "generated_at": _now_iso(),
            }

        with self._lock:
            iso = self._state.get("isolated")

        if not isinstance(iso, dict):
            return {
                "status": "no_official_isolation",
                "error": (
                    "No instrument has been officially isolated yet today. "
                    "Wait for an R3 / S3 touch on any monitored index, "
                    "then try again."
                ),
                "instrument_key": key,
                "live_sr_levels": None,
                "generated_at": _now_iso(),
            }

        official_key = str(iso.get("instrument_key") or "").strip()
        is_official = (key == official_key)

        source: dict[str, Any] = {"instrument_key": key}
        try:
            from services.instrument_registry import instrument_registry
            entry = instrument_registry.get(key)
            if entry is None:
                entry = instrument_registry.register_adhoc(key)
            if entry:
                source.update({
                    "trading_symbol": entry.get("trading_symbol"),
                    "underlying": entry.get("underlying"),
                    "strike_price": entry.get("strike_price"),
                    "strike_type": entry.get("strike_type"),
                    "expiry": entry.get("expiry"),
                })
        except Exception:
            logger.exception(
                "[level-isolation] registry lookup failed for %s", key,
            )

        touched_at = iso.get("touched_at") or iso.get("isolated_at")
        ref_dt: datetime | None = None
        if touched_at:
            try:
                ref_dt = datetime.fromisoformat(str(touched_at))
                if ref_dt.tzinfo is None:
                    ref_dt = ref_dt.replace(tzinfo=_MARKET_TZ)
            except Exception:
                ref_dt = None

        live = self._read_live_sr_for(source, ref_dt)

        return {
            "status": "ok",
            "instrument_key": key,
            "trading_symbol": source.get("trading_symbol"),
            "underlying": source.get("underlying"),
            "strike": source.get("strike_price"),
            "type": source.get("strike_type"),
            "expiry": source.get("expiry"),
            "is_official_isolated": is_official,
            "official_instrument_key": official_key,
            "official_trading_symbol": iso.get("trading_symbol"),
            "official_touched_at": touched_at,
            "reference_at": live.get("reference_at"),
            "live_sr_levels": live,
            "generated_at": _now_iso(),
        }

    def get_detail(self) -> dict[str, Any]:
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

            self._ensure_tracker_attached()

            detail = {
                "enabled": config.LEVEL_ISOLATION_ENABLED,
                "day": self._state.get("day") or _today_iso(),
                "market": market,
                "isolated": isolated,
                # G1 v4.2: expose execution_path / trigger_is_self_nifty at
                # the top level so the UI and dummy-order service can branch
                # without digging into `isolated` themselves.
                "execution_path": (
                    (isolated or {}).get("execution_path")
                    if isinstance(isolated, dict) else None
                ),
                "trigger_is_self_nifty": (
                    (isolated or {}).get("trigger_is_self_nifty")
                    if isinstance(isolated, dict) else None
                ),
                "trigger_event": trigger_event,
                "candidates": (
                    list(trigger_event.get("candidates") or [])
                    if trigger_event else []
                ),
                "events": events,
                "touch_state": self._state.get("touch_state") or {},
                "or_levels": or_levels,
                "last_update": self._state.get("last_update"),
                "monitored_indices": [
                    {
                        "canonical": v["canonical"],
                        "instrument_key": k,
                        "priority": v["priority"],
                    }
                    for k, v in INDEX_METADATA.items()
                ],
                "routed_execution_index": str(
                    config.STRATEGY_ROUTED_EXECUTION_INDEX
                    or "NSE_INDEX|Nifty 50"
                ),
                "generated_at": _now_iso(),
            }

        try:
            if isinstance(isolated, dict):
                detail["live_sr_levels"] = self._load_live_sr_levels_for_isolated(
                    isolated
                )
            else:
                detail["live_sr_levels"] = None
        except Exception:
            logger.exception("[level-isolation] live_sr_levels load failed")
            detail["live_sr_levels"] = None

        try:
            from services.isolated_sr_tracker_service import (
                isolated_sr_tracker_service,
            )
            detail["sr_tracking"] = isolated_sr_tracker_service.get_detail()
        except Exception:
            logger.exception("[level-isolation] sr_tracking merge failed")
            detail["sr_tracking"] = None

        return detail

    def reset(self) -> dict[str, Any]:
        with self._lock:
            self._state = self._empty_state()
            self._last_ltp.clear()
            self._last_tick_at.clear()
            self._last_check_monotonic = 0.0
            self._save_state()
            try:
                from services.isolated_sr_tracker_service import (
                    isolated_sr_tracker_service,
                )
                isolated_sr_tracker_service.reset()
            except Exception:
                logger.exception(
                    "[level-isolation] SR tracker reset failed"
                )
            logger.info("[level-isolation] state reset")
            return self.get_detail()


level_isolated_instrument_service = LevelIsolatedInstrumentService()